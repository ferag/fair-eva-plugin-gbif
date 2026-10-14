"""ICA contributions in percentage points, following the TFM with ISO intervals."""

from concurrent.futures import ThreadPoolExecutor
import datetime as dt
from decimal import Decimal
import math
from pathlib import Path
import re

from dwca.read import DwCAReader
import geopandas as gpd
import pandas as pd
import pycountry
from shapely.geometry import Point

from .catalogue import Catalogue, normalize_name

TAXONOMIC = [
    "genus",
    "specificEpithet",
    "higherClassification",
    "kingdom",
    "class",
    "order",
    "family",
    "identifiedBy",
]
GEOGRAPHIC = [
    "decimalLatitude",
    "decimalLongitude",
    "countryCode",
    "coordinateUncertaintyInMeters",
]
TEMPORAL = ["eventDate", "verbatimEventDate", "year", "month", "day"]
SHP_PATH = Path(__file__).parent / "static/ne_110m_admin_0_countries.shp"
DECIMAL = re.compile(r"^-?\d+(\.\d+)?$")
ISO_DATE = re.compile(r"^([0-9]{4})(?:-([0-9]{2})(?:-([0-9]{2}))?)?$")
ISO_BASIC_DATE = re.compile(r"^([0-9]{4})([0-9]{2})([0-9]{2})$")
ISO_ORDINAL = re.compile(r"^([0-9]{4})-?([0-9]{3})$")
ISO_WEEK = re.compile(r"^([0-9]{4})(?:-W([0-9]{2})(?:-([1-7]))?|W([0-9]{2})([1-7])?)$")
ISO_TIME = re.compile(
    r"^(?P<hour>[0-9]{2})(?:(?::(?P<minute>[0-9]{2})(?::(?P<second>[0-9]{2}))?)"
    r"|(?P<basic_minute>[0-9]{2})(?P<basic_second>[0-9]{2})?)?"
    r"(?P<fraction>[.,][0-9]+)?(?P<zone>Z|[+-][0-9]{2}(?::?[0-9]{2})?)?$"
)


def present(value):
    return not pd.isna(value) and (not isinstance(value, str) or bool(value.strip()))


def prepare(df, columns):
    return (
        df.reindex(columns=columns)
        .apply(
            lambda column: column.map(
                lambda value: value.strip() if isinstance(value, str) else value
            )
        )
        .replace(r"^\s*$", None, regex=True)
    )


def ICA(filepath, catalogue_dir=None, catalogue_check_interval_hours=24):
    """Return weighted ICA scores and the exact local Catalogue of Life release."""
    columns = TAXONOMIC + GEOGRAPHIC + TEMPORAL
    with DwCAReader(filepath) as archive:
        # Keep one physical column to preserve the row count even if all ICA
        # fields are absent. Defaults declared in meta.xml are added by pd_read.
        first = archive.get_descriptor_for(archive.core_file_location).short_headers[0]
        df = archive.pd_read(
            archive.core_file_location,
            usecols=lambda col: col in columns or col == first,
            low_memory=False,
            keep_default_na=False,
        )
    if df.empty:
        raise ValueError("The Darwin Core Archive contains no records")
    df = prepare(df, columns)
    # Consume all futures so dependency and validation failures reach the caller.
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [
            executor.submit(
                taxonomic_percentajes,
                df[TAXONOMIC],
                catalogue_dir=catalogue_dir,
                catalogue_check_interval_hours=catalogue_check_interval_hours,
            ),
            executor.submit(geographic_percentajes, df[GEOGRAPHIC]),
            executor.submit(temporal_percentajes, df[TEMPORAL]),
        ]
        result = {}
        for future in futures:
            result.update(future.result())
    result["ICA"] = sum(result[key] for key in ("Taxonomic", "Geographic", "Temporal"))
    return result


def taxonomic_percentajes(
    df, catalogue=None, catalogue_dir=None, catalogue_check_interval_hours=24
):
    df = prepare(df, TAXONOMIC)
    if df.empty:
        raise ValueError("Cannot evaluate an empty dataset")
    own = catalogue is None
    if own:
        catalogue = Catalogue(catalogue_dir, catalogue_check_interval_hours).acquire()
    try:
        counts = df["genus"].dropna().map(normalize_name).value_counts()
        genus = sum(n for name, n in counts.items() if catalogue.contains(name)) / len(df)
        has = {column: df[column].map(present) for column in TAXONOMIC}
        higher = has["higherClassification"]
        result = {
            "Genus": 20 * genus,
            "Species": 10 * has["specificEpithet"].mean(),
            "Kingdom": 3 * (higher | has["kingdom"]).mean(),
            "Class/Order": 3 * (higher | has["class"] | has["order"]).mean(),
            "Family": 3 * (higher | has["family"]).mean(),
            "Identifiers": 6 * has["identifiedBy"].mean(),
        }
        result["Taxonomic"] = sum(result.values())
        result["Hierarchy"] = sum(result[key] for key in ("Kingdom", "Class/Order", "Family"))
        result["Catalogue"] = dict(catalogue.metadata)
        return result
    finally:
        if own:
            catalogue.close()


def geographic_percentajes(df, borders=None):
    df = prepare(df, GEOGRAPHIC)
    if df.empty:
        raise ValueError("Cannot evaluate an empty dataset")
    # Failure to read the reference geometry must not become a data-quality score.
    if borders is None:
        borders = gpd.read_file(SHP_PATH)
    if borders.empty or not {"ADM0_A3", "geometry"}.issubset(borders.columns):
        raise ValueError("Country boundaries are unavailable or invalid")
    if borders.crs is None:
        raise ValueError("Country boundaries have no coordinate reference system")
    borders = borders.to_crs("EPSG:4326")
    geometries = {}
    cache = {}

    def incorrect(lat, lon, country):
        if not present(lat) or not present(lon):
            return False
        if not DECIMAL.fullmatch(str(lat)) or not DECIMAL.fullmatch(str(lon)):
            return True
        lat, lon = float(lat), float(lon)
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return True
        if not present(country):
            return False
        iso = pycountry.countries.get(alpha_2=str(country))
        if iso is None:
            return True
        if iso.alpha_3 not in geometries:
            subset = borders.loc[borders["ADM0_A3"] == iso.alpha_3, "geometry"]
            if subset.empty or subset.isna().all():
                raise ValueError("Country boundary unavailable for " + str(country))
            geometries[iso.alpha_3] = (
                subset.union_all() if hasattr(subset, "union_all") else subset.unary_union
            )
        # Retain the existing strict polygon containment rule.
        return not geometries[iso.alpha_3].contains(Point(lon, lat))

    complete = df["decimalLatitude"].map(present) & df["decimalLongitude"].map(present)
    countries = df["countryCode"].map(
        lambda code: present(code) and pycountry.countries.get(alpha_2=str(code)) is not None
    )
    uncertainty = pd.to_numeric(df["coordinateUncertaintyInMeters"], errors="coerce")
    uncertainty_valid = uncertainty.map(lambda value: math.isfinite(value) and value > 0)
    invalid = 0
    for lat, lon, country in df[GEOGRAPHIC[:3]].itertuples(index=False, name=None):
        key = tuple(value if present(value) else None for value in (lat, lon, country))
        if key not in cache:
            cache[key] = incorrect(*key)
        invalid += cache[key]
    result = {
        "Coordinates": 20 * complete.mean(),
        "Countries": 10 * countries.mean(),
        "CoordinatesUncertainty": 5 * uncertainty_valid.mean(),
        "IncorrectCoordinates": -20 * invalid / len(df),
    }
    result["Geographic"] = sum(result.values())
    return result


def _iso_time(text):
    """Return time since midnight and offset, including ISO decimal fractions."""
    match = ISO_TIME.fullmatch(text)
    if not match:
        raise ValueError("Invalid ISO time")
    hour = int(match["hour"])
    minute_text = match["minute"] or match["basic_minute"]
    second_text = match["second"] or match["basic_second"]
    minute, second = int(minute_text or 0), int(second_text or 0)
    fraction = Decimal("0" + (match["fraction"] or "").replace(",", "."))
    if hour > 24 or minute > 59 or second > 59:
        raise ValueError("Invalid clock time")
    if hour == 24 and (minute or second or fraction):
        raise ValueError("Only midnight can use hour 24")
    unit = 1 if second_text is not None else 60 if minute_text is not None else 3600
    elapsed = dt.timedelta(
        hours=hour,
        minutes=minute,
        seconds=second,
        microseconds=int(fraction * unit * 1000000),
    )
    zone = match["zone"]
    offset = dt.timezone.utc
    if zone and zone != "Z":
        digits = zone[1:].replace(":", "")
        hours, minutes = int(digits[:2]), int(digits[2:] or 0)
        if hours > 23 or minutes > 59:
            raise ValueError("Invalid UTC offset")
        delta = dt.timedelta(hours=hours, minutes=minutes)
        offset = dt.timezone(delta if zone[0] == "+" else -delta)
    return elapsed, offset


def _date_parts(text, today):
    """Return component credits, earliest instant, validity and day precision.

    Ordinal dates and weeks with a weekday identify a calendar day. A week
    without a weekday earns year credit only; its Monday is used for ordering.
    """
    date_text, separator, time_text = text.partition("T")
    match = ISO_DATE.fullmatch(date_text) or ISO_BASIC_DATE.fullmatch(date_text)
    ordinal = ISO_ORDINAL.fullmatch(date_text)
    week = ISO_WEEK.fullmatch(date_text)
    credits = (False, False, False)
    full = False
    try:
        if match:
            year, month, day = (int(value) if value else None for value in match.groups())
            credits = (
                0 < year <= today.year,
                month is not None and 1 <= month <= 12,
                day is not None and 1 <= day <= 31,
            )
            full = day is not None
            date = dt.date(year, month if month is not None else 1, day if day is not None else 1)
        elif ordinal:
            year, day = map(int, ordinal.groups())
            credits = (0 < year <= today.year, False, False)
            date = dt.date(year, 1, 1) + dt.timedelta(days=day - 1)
            if day < 1 or date.year != year:
                raise ValueError("Invalid ordinal day")
            credits = (credits[0], True, True)
            full = True
        elif week:
            year = int(week[1])
            number, weekday = int(week[2] or week[4]), week[3] or week[5]
            credits = (0 < year <= today.year, False, False)
            jan4 = dt.date(year, 1, 4)
            date = jan4 + dt.timedelta(
                days=(number - 1) * 7 + int(weekday or 1) - jan4.isoweekday()
            )
            if date.isocalendar()[:2] != (year, number):
                raise ValueError("Invalid ISO week")
            full = weekday is not None
            credits = (credits[0], full, full)
        else:
            return credits, None, False, full
        timestamp = dt.datetime.combine(date, dt.time(), tzinfo=dt.timezone.utc)
        if separator:
            if not full:
                raise ValueError("A time requires a complete date")
            elapsed, offset = _iso_time(time_text)
            timestamp = timestamp.replace(tzinfo=offset) + elapsed
        return credits, timestamp, timestamp.date() <= today, full
    except (ValueError, OverflowError):
        return credits, None, False, full


def temporal_percentajes(df, today=None):
    df = prepare(df, TEMPORAL)
    if df.empty:
        raise ValueError("Cannot evaluate an empty dataset")
    today = today or dt.date.today()
    years = months = days = incorrect = 0
    for event, verbatim in df[["eventDate", "verbatimEventDate"]].itertuples(
        index=False, name=None
    ):
        value = event if present(event) else verbatim
        if not present(value):
            continue
        parts = str(value).strip().split("/")
        parsed = [_date_parts(part.strip(), today) for part in parts]
        credits = tuple(all(item[0][index] for item in parsed) for index in range(3))
        valid = len(parsed) in (1, 2) and all(item[2] for item in parsed)
        if len(parsed) == 2:
            valid = valid and parsed[0][1] <= parsed[1][1]
        years += credits[0]
        months += credits[1]
        days += credits[2]
        incorrect += not valid
    result = {
        "Years": 11 * years / len(df),
        "Months": 7 * months / len(df),
        "Days": 2 * days / len(df),
        "IncorrectDates": -15 * incorrect / len(df),
    }
    result["Temporal"] = sum(result.values())
    return result
