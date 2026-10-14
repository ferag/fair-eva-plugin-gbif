# fair-eva-plugin-gbif

GBIF plugin for [FAIR EVA](https://github.com/IFCA-Advanced-Computing/FAIR_eva), adapted to the `cookiecutter-fair-eva-plugin` package structure.

## What It Does

This plugin:

- loads GBIF dataset metadata from the GBIF API,
- exposes the plugin as `fair_eva.plugin.gbif`,
- runs GBIF-specific accessibility checks,
- downloads Darwin Core Archives when available,
- computes the `data_01` ICA data-quality summary.

## Repository Layout

Main files:

- [fair_eva/plugin/gbif/plugin.py](fair_eva/plugin/gbif/plugin.py)
- [fair_eva/plugin/gbif/gbif_data.py](fair_eva/plugin/gbif/gbif_data.py)
- [fair_eva/plugin/gbif/config.ini](fair_eva/plugin/gbif/config.ini)

Static resources used by some FAIR checks are bundled under `fair_eva/plugin/gbif/static/`.

## Installation

Create a virtual environment and install the plugin as a regular wheel:

```bash
python3 -m venv .venv
.venv/bin/pip install .
```

Current FAIR EVA API builds discover plugins by scanning the installed
`fair_eva/plugin` directory in `site-packages`. An editable install can make
the package importable while still keeping it out of that directory listing, so
`repo=gbif` may not be discovered at runtime.

## Usage

The plugin is discovered by FAIR EVA through the `fair_eva.plugin` namespace.

Example API request:

```bash
curl -X POST http://127.0.0.1:9090/v1.0/rda/rda_all \
  -H 'Content-Type: application/json' \
  -d '{"repo":"gbif","id":"10.15470/f0diow"}'
```

## Configuration

`config.ini` is committed with empty GBIF credentials:

```ini
api_mail =
api_user =
api_pass =
```

If a private GBIF download account is needed, set those values locally before running the plugin, but do not commit them.

## Current Status

The plugin loads correctly and `data_01` is executed successfully. Some generic FAIR-EVA indicators still depend on additional metadata-path alignment work between the GBIF XML parser and the FAIR EVA base evaluator.

## License

See [LICENSE](LICENSE).

## Local Catalogue of Life and ICA

Taxonomic matching uses **Catalogue of Life Base monthly releases**, from the
[official downloads](https://www.catalogueoflife.org/data/download) and its
[monthly archive](https://download.checklistbank.org/col/monthly/). Only dated
`YYYY-MM-DD_dwca.zip` files are selected; Extended releases are excluded.
The plugin imports genus-rank names, including synonyms, into SQLite. Matching is
exact after Unicode NFKC normalization, whitespace normalization and case folding.
It does not validate species names, accepted status or hierarchy consistency.
Results can differ from the former global ChecklistBank name-matching API.

Optional settings in the existing `[gbif]` section:

```ini
catalogue_dir = /var/cache/fair-eva/col
catalogue_check_interval_hours = 24
```

The service user must be able to write to the directory. Without configuration,
the directory is `$XDG_CACHE_HOME/fair-eva-gbif/col` or
`~/.cache/fair-eva-gbif/col`. Keep it on persistent local storage. The updater uses
POSIX file locks (Linux/macOS); all workers should share the same directory.
No credentials are needed for the public catalogue downloads. Existing GBIF
credentials and the dataset-download process are independent of this catalogue.

The first evaluation downloads and builds the index synchronously. Later
evaluations open the existing edition immediately and trigger a background check
when at least 24 hours have elapsed since the last attempt. No evaluation means
no update check. Workers must remain running for background downloads to finish;
a stopped worker leaves the previous catalogue usable. A failed check is retried
on the next eligible evaluation, not on every record or request.

Downloads are streamed to temporary storage, checked for ZIP integrity and
imported in batches of at most 5,000 names. The index is verified before atomic
activation. `current.sqlite` and `previous.sqlite` retain the two latest usable
editions; normal imports remove their temporary ZIP and working files. An
interrupted process can leave `import-*` directories, which administrators may
remove when no update is running. Each evaluation holds one database connection
for its complete taxonomic calculation, even when a new edition is activated.

An update error retains the existing catalogue and appears in the next evaluation's
catalogue metadata/report. Without a usable edition, ICA is reported unavailable;
a network failure is never scored as a genus absent from the catalogue.
`state.json` records the last check and update error. Each index records the
release date, source URL, download SHA-256, import timestamp and genus count.
Recording the edition identifies the source of a score; reproducing old scores
also requires retaining that edition and the relevant code version.

### ICA rules and compatibility

`ICA(filepath)` remains supported. Optional keyword arguments are
`catalogue_dir` and `catalogue_check_interval_hours`. Existing score keys remain;
`Hierarchy` is the total of `Kingdom`, `Class/Order` and `Family`, and `Catalogue`
contains provenance metadata and an optional `warning`. `Hierarchy` must not be
added again to the total. The report displays the release and update warnings.

All component percentages use the total number of records as denominator.
Empty strings, whitespace-only strings and nulls are absent. An empty archive is
not evaluable. Contributions are already weighted; ICA is their sum, with no
second weighting and no clipping of negative results.

- **Taxonomic (maximum 45 points):** genus membership 20, presence of
  `specificEpithet` 10, hierarchy 9, presence of `identifiedBy` 6. A populated
  `higherClassification` grants all 9 hierarchy points for that record; otherwise
  kingdom, class OR order, and family each grant one third. The existing three
  hierarchy output keys include that shared credit.
- **Geographic (maximum 35):** both coordinates present 20, valid ISO alpha-2
  country 10, finite positive numeric uncertainty 5, incorrect complete coordinate
  pairs −20. Incomplete pairs neither earn presence credit nor incur the penalty.
  Complete pairs are checked for decimal format, inclusive latitude/longitude
  limits and country containment, using original coordinates. Missing countries
  skip containment; invalid supplied country codes fail it. Boundaries are loaded
  once per evaluation. Missing reference geometry is an evaluation error, not a
  bad record. The existing strict polygon-interior rule and terrestrial/coastal
  boundary data are retained; marine areas and points exactly on a border remain
  limitations of this check.
- **Temporal (maximum 20):** year 11, month 7, day 2, incorrect present dates −15.
  Components come from `eventDate`, with `verbatimEventDate` used only when it is
  absent. Standalone `year`, `month`, `day` fields do not override the date.
  Years must be positive and no later than the current year; months are 1–12 and
  days 1–31. Calendar validity and future dates are checked separately, so an
  impossible date can earn component credits and receive the error penalty.
  Missing dates earn zero and are not penalized.

Supported temporal representations (which may be mixed within one file) are:

- Calendar dates: `YYYY`, `YYYY-MM`, `YYYY-MM-DD`, `YYYYMMDD`.
- Ordinal dates (year and day of year): `YYYY-DDD`, `YYYYDDD`.
- ISO week dates: `YYYY-Www`, `YYYYWww`, `YYYY-Www-D`, `YYYYWwwD`.
- Complete dates followed by `T` and hours, hours/minutes or hours/minutes/seconds,
  with or without time colons. A decimal fraction (`.` or `,`) may follow the
  lowest supplied time component. Optional offsets are `Z`, `±HH`, `±HHMM`,
  or `±HH:MM`. `24:00` denotes midnight at the start of the next day.
- Closed start/end intervals separated by `/`, including partial endpoints
  such as `2024/2025` and `2024-01/2024-03`.

Partial dates earn only supplied component credits. Ordinal dates and week dates
with a weekday resolve to a calendar day and earn year/month/day credits; weeks
without a weekday earn year credit only. Their earliest represented day (Monday
for weeks) is used for future checks and interval ordering. Both interval ends
must be valid, ordered and non-future. Timestamp ordering respects offsets;
timestamps without an offset use UTC for ordering. Future checks use the written
calendar date (the following day for `24:00`) and the evaluator's current date.
Fractional times are truncated to microsecond precision for ordering.

This is not a complete implementation of all ISO 8601 representations. Standalone
hours or durations, duration-based intervals, recurring/open intervals, abbreviated
interval endpoints, expanded/signed years, leap seconds and ISO 8601-2 extensions
are not supported and receive the error penalty. Invalid calendar dates, clock
times, offsets and inverted intervals also receive the error penalty.

### Validation

Run the regression suite using the project's installed dependencies:

```bash
python3 -m unittest discover -s tests -v
```

A real import of Base **2026-08-20** on 2026-09-08 produced:

| Measurement | Result |
| --- | ---: |
| ZIP download | 511,590,750 bytes (487.89 MiB) |
| Uncompressed archive contents | 2,076,483,965 bytes (not extracted) |
| Distinct normalized genus names | 292,254 |
| SQLite index | 5,107,712 bytes (4.87 MiB) |
| Import, hash and ZIP verification | 28.13 seconds, excluding download |
| Import-process peak RSS | 35.20 MiB (28.36 MiB baseline) |

These measurements are environment-dependent and cover the importer, not the
memory required by FAIR EVA or the occurrence dataset. Plan temporary disk space
for the compressed download plus the current, previous and new small indexes.
The ZIP SHA-256 was
`6f558a1d35164afbd48838122d01e044189d20431b702f1d72ee1df0f42b53af`.

### Web-client ICA rendering

`data_01` retains its `(points, html_message)` return contract. Its root HTML
container now carries `data-ica-version="1"` and an HTML-escaped `data-ica` JSON
attribute containing the exact ICA scores and `Catalogue` provenance. Updated
clients can render their own dashboard without scraping rounded table cells;
existing clients continue to show the HTML table. A failure has no score envelope.
