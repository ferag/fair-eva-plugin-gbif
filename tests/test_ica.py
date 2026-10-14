import datetime as dt
import hashlib
import multiprocessing
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

# FAIR EVA is a regular installed package; include this checkout's plugin namespace.
import fair_eva.plugin

fair_eva.plugin.__path__.append(str(Path(__file__).resolve().parents[1] / "fair_eva/plugin"))

import geopandas as gpd
import pandas as pd
from shapely.geometry import box
from fair_eva.plugin.gbif.catalogue import Catalogue, CatalogueUnavailable, Snapshot, build_index
from fair_eva.plugin.gbif.quality import (
    ICA,
    geographic_percentajes,
    taxonomic_percentajes,
    temporal_percentajes,
)

META = """<archive xmlns="http://rs.tdwg.org/dwc/text/">
<core encoding="UTF-8" fieldsTerminatedBy="\\t" fieldsEnclosedBy="" ignoreHeaderLines="1" rowType="http://rs.tdwg.org/dwc/terms/Taxon">
<files><location>Taxon.tsv</location></files>
<field index="0" term="http://rs.tdwg.org/dwc/terms/scientificName"/>
<field index="1" term="http://rs.tdwg.org/dwc/terms/taxonRank"/>
<field index="2" term="http://rs.tdwg.org/dwc/terms/taxonomicStatus"/>
</core></archive>"""


def archive(path):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("meta.xml", META)
        z.writestr(
            "Taxon.tsv",
            "name\trank\tstatus\nAbies Mill.\tgenus\taccepted\n"
            "Picea Link\tgenus\tsynonym\nAbies alba Mill.\tspecies\taccepted\n",
        )


def index(directory, version="2026-07-14", name="current.sqlite"):
    source = directory / "source.zip"
    archive(source)
    build_index(source, directory / name, {"version": version, "source": "fixture"})


class Response:
    def __init__(self, text="", body=b"", headers=None):
        self.text, self.body, self.headers = text, body, headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def raise_for_status(self):
        pass

    def iter_content(self, size):
        for start in range(0, len(self.body), size):
            yield self.body[start : start + size]


def held_refresh(directory, queue):
    with patch(
        "fair_eva.plugin.gbif.catalogue.requests.get", side_effect=AssertionError("network")
    ):
        Catalogue(directory).refresh()
    queue.put(True)


class CatalogueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.catalogue = Catalogue(self.root)

    def test_stream_index_and_taxonomic_weighting(self):
        index(self.root)
        snapshot = Snapshot(self.root / "current.sqlite")
        self.addCleanup(snapshot.close)
        self.assertTrue(snapshot.contains("  ＡＢＩＥＳ  "))
        self.assertTrue(snapshot.contains("picea"))
        self.assertFalse(snapshot.contains("Abies alba"))
        df = pd.DataFrame({
            "genus": ["Abies", "ABIES", "Picea", "Unknown"],
            "specificEpithet": ["alba", "", " ", None],
            "higherClassification": ["tree", None, None, None],
            "kingdom": [None, "Plantae", None, None],
            "class": [None, None, "Pinopsida", None],
            "order": [None, None, "Pinales", None],
            "identifiedBy": ["A", "", " ", None],
        })
        with patch(
            "fair_eva.plugin.gbif.catalogue.requests.get", side_effect=AssertionError("network")
        ):
            result = taxonomic_percentajes(df, catalogue=snapshot)
        self.assertEqual(result["Genus"], 15)
        self.assertEqual(result["Species"], 2.5)
        self.assertEqual(result["Hierarchy"], 3.75)
        self.assertEqual(result["Identifiers"], 1.5)
        self.assertEqual(result["Taxonomic"], 22.75)
        self.assertEqual(
            taxonomic_percentajes(pd.DataFrame({"genus": [None]}), catalogue=snapshot)[
                "Taxonomic"
            ],
            0,
        )

    def test_update_is_atomic_and_snapshot_stays_pinned(self):
        index(self.root)
        self.catalogue._save_state({"checked_at": time.time()})
        old = self.catalogue.acquire()
        self.addCleanup(old.close)
        # Suppress the automatic thread; refresh below is controlled explicitly.
        self.catalogue._save_state({"checked_at": 0})
        listing = '<a href="2026-08-20_dwca.zip">Base</a><a href="2026-09-01_xr_dwca.zip">XR</a>'
        payload = (self.root / "source.zip").read_bytes()
        with patch(
            "fair_eva.plugin.gbif.catalogue.requests.get",
            side_effect=[Response(listing), Response(body=payload)],
        ):
            self.catalogue.refresh(blocking=True)
        new = self.catalogue.acquire()
        self.addCleanup(new.close)
        self.assertEqual(new.metadata["version"], "2026-08-20")
        self.assertEqual(new.metadata["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(old.metadata["version"], "2026-07-14")
        self.assertTrue(old.contains("Abies"))
        previous = Snapshot(self.root / "previous.sqlite")
        self.addCleanup(previous.close)
        self.assertEqual(previous.metadata["version"], "2026-07-14")

    def test_no_change_daily_throttle_and_offline(self):
        index(self.root)
        with patch(
            "fair_eva.plugin.gbif.catalogue.requests.get",
            return_value=Response('<a href="2026-07-14_dwca.zip">x</a>'),
        ) as get:
            self.catalogue.refresh()
            self.catalogue.refresh()
            self.assertEqual(get.call_count, 1)
        with patch(
            "fair_eva.plugin.gbif.catalogue.requests.get", side_effect=AssertionError("network")
        ):
            snapshot = self.catalogue.acquire()
            snapshot.close()

    def test_download_failures_retain_current(self):
        index(self.root)
        for body, headers in [(b"bad zip", {}), (b"partial", {"Content-Length": "999"})]:
            self.catalogue._save_state({"checked_at": 0})
            with patch(
                "fair_eva.plugin.gbif.catalogue.requests.get",
                side_effect=[
                    Response('<a href="2026-08-20_dwca.zip">x</a>'),
                    Response(body=body, headers=headers),
                ],
            ):
                self.catalogue.refresh()
            snapshot = self.catalogue.acquire()
            self.assertEqual(snapshot.metadata["version"], "2026-07-14")
            self.assertIn("warning", snapshot.metadata)
            snapshot.close()
            self.assertFalse(list(self.root.glob("import-*")))

    def test_failed_bootstrap_and_corrupt_index(self):
        with patch("fair_eva.plugin.gbif.catalogue.requests.get", side_effect=OSError("offline")):
            with self.assertRaises(CatalogueUnavailable):
                self.catalogue.acquire()
        index(self.root, name="previous.sqlite")
        (self.root / "current.sqlite").write_bytes(b"broken")
        snapshot = self.catalogue.acquire()
        self.addCleanup(snapshot.close)
        self.assertTrue(snapshot.contains("Abies"))
        self.assertIn("warning", snapshot.metadata)

    def test_interprocess_lock(self):
        import fcntl

        with (self.root / "update.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            context = multiprocessing.get_context("spawn")
            queue = context.Queue()
            process = context.Process(target=held_refresh, args=(self.root, queue))
            process.start()
            process.join(5)
            self.assertFalse(process.is_alive())
            self.assertEqual(process.exitcode, 0)
            self.assertTrue(queue.get(timeout=1))
            self.assertFalse((self.root / "state.json").exists())

    def test_background_check_does_not_block_evaluation(self):
        index(self.root)
        started, finish = threading.Event(), threading.Event()
        threads = []
        thread_class = threading.Thread

        def thread_factory(*args, **kwargs):
            thread = thread_class(*args, **kwargs)
            threads.append(thread)
            return thread

        def request(*args, **kwargs):
            started.set()
            if not finish.wait(5):
                raise RuntimeError("Test update timed out")
            return Response('<a href="2026-07-14_dwca.zip">x</a>')

        with patch("fair_eva.plugin.gbif.catalogue.requests.get", side_effect=request), patch(
            "fair_eva.plugin.gbif.catalogue.threading.Thread", side_effect=thread_factory
        ):
            try:
                snapshot = self.catalogue.acquire()
                self.assertTrue(started.wait(5))
                self.assertTrue(snapshot.contains("Abies"))
                snapshot.close()
            finally:
                finish.set()
                for thread in threads:
                    thread.join(5)
                    self.assertFalse(thread.is_alive())

    def test_bootstrap_and_large_fields(self):
        path = self.root / "source.zip"
        with zipfile.ZipFile(path, "w") as zipped:
            # Real releases have remarks/reference fields beyond csv's default limit.
            zipped.writestr("meta.xml", META)
            zipped.writestr(
                "Taxon.tsv",
                "name\trank\tstatus\tremarks\nAbies\tgenus\taccepted\t" + "x" * 200000 + "\n",
            )
        payload = path.read_bytes()
        with patch(
            "fair_eva.plugin.gbif.catalogue.requests.get",
            side_effect=[Response('<a href="2026-08-20_dwca.zip">x</a>'), Response(body=payload)],
        ) as get:
            snapshot = self.catalogue.acquire()
            self.assertEqual(snapshot.metadata["version"], "2026-08-20")
            self.assertEqual(get.call_count, 2)
            snapshot.close()

    def test_valid_zip_with_invalid_index_is_not_activated(self):
        index(self.root)
        path = self.root / "invalid.zip"
        with zipfile.ZipFile(path, "w") as zipped:
            zipped.writestr("meta.xml", META)
            zipped.writestr("Taxon.tsv", "name\trank\tstatus\nAbies alba\tspecies\taccepted\n")
        with patch(
            "fair_eva.plugin.gbif.catalogue.requests.get",
            side_effect=[
                Response('<a href="2026-08-20_dwca.zip">x</a>'),
                Response(body=path.read_bytes()),
            ],
        ):
            self.catalogue.refresh()
        snapshot = self.catalogue.acquire()
        self.assertEqual(snapshot.metadata["version"], "2026-07-14")
        self.assertIn("no genera", snapshot.metadata["warning"])
        snapshot.close()


class QualityTests(unittest.TestCase):
    def setUp(self):
        self.borders = gpd.GeoDataFrame(
            {"ADM0_A3": ["ESP"], "geometry": [box(-10, -10, 10, 10)]}, crs="EPSG:4326"
        )

    def test_geographic_scores(self):
        df = pd.DataFrame({
            "decimalLatitude": [0, 90, 91, "x", None, 1],
            "decimalLongitude": [0, 180, 0, 0, 0, 1],
            "countryCode": ["ES", "", "", "", "", "ES"],
            "coordinateUncertaintyInMeters": ["1", "0", "bad", None, -1, "2"],
        })
        result = geographic_percentajes(df, self.borders)
        self.assertAlmostEqual(result["Coordinates"], 20 * 5 / 6)
        self.assertAlmostEqual(result["IncorrectCoordinates"], -20 * 2 / 6)
        self.assertAlmostEqual(result["Countries"], 10 * 2 / 6)
        self.assertAlmostEqual(result["CoordinatesUncertainty"], 5 * 2 / 6)

    def test_original_coordinates_and_missing_geometry(self):
        result = geographic_percentajes(
            pd.DataFrame(
                {"decimalLatitude": [0], "decimalLongitude": [9.9999], "countryCode": ["ES"]}
            ),
            self.borders,
        )
        self.assertEqual(result["IncorrectCoordinates"], 0)
        with patch("fair_eva.plugin.gbif.quality.gpd.read_file", side_effect=OSError("missing")):
            with self.assertRaises(OSError):
                geographic_percentajes(pd.DataFrame({"decimalLatitude": [1]}))

    def test_temporal_cases(self):
        cases = {
            "2024-02-29": (11, 7, 2, 0),
            "2023-02-29": (11, 7, 2, -15),
            "2024": (11, 0, 0, 0),
            "2024-02": (11, 7, 0, 0),
            "2024-12-31/2025-01-02": (11, 7, 2, 0),
            "2025-02-02/2025-01-01": (11, 7, 2, -15),
            "2025-07-01": (11, 7, 2, -15),
            "2026-01-01": (0, 7, 2, -15),
            "2024-02-30": (11, 7, 2, -15),
            "2024-13-01": (11, 0, 2, -15),
            "2024/2025": (11, 0, 0, -15),
            "2024-01-01/": (0, 0, 0, -15),
            "garbage": (0, 0, 0, -15),
            "": (0, 0, 0, 0),
            "2024-01-01T12:30:00Z": (11, 7, 2, 0),
            "2024-02-30T12:30:00Z": (11, 7, 2, -15),
            "2024-01-01T99:30:00Z": (11, 7, 2, -15),
            "2024-01-01T12:30:00Z/2024-01-01T11:30:00Z": (11, 7, 2, -15),
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                result = temporal_percentajes(
                    pd.DataFrame({"eventDate": [value]}), today=dt.date(2025, 6, 1)
                )
                self.assertEqual(
                    tuple(result[k] for k in ("Years", "Months", "Days", "IncorrectDates")),
                    expected,
                )
                self.assertEqual(result["Temporal"], sum(expected))
        result = temporal_percentajes(
            pd.DataFrame(
                {"eventDate": [" ", "bad"], "verbatimEventDate": ["2024-01-01", "2024-01-01"]}
            )
        )
        self.assertEqual(result["Temporal"], 2.5)

    def test_empty_datasets(self):
        for function in (temporal_percentajes, geographic_percentajes, taxonomic_percentajes):
            with self.assertRaises(ValueError):
                function(pd.DataFrame())

    def test_dwca_end_to_end_and_error_propagation(self):
        import csv
        import io

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            index(root)
            Catalogue(root)._save_state({"checked_at": time.time()})
            path = root / "occurrences.zip"
            fields = [
                "genus",
                "specificEpithet",
                "higherClassification",
                "identifiedBy",
                "decimalLatitude",
                "decimalLongitude",
                "countryCode",
                "coordinateUncertaintyInMeters",
                "eventDate",
            ]
            meta = (
                '<archive xmlns="http://rs.tdwg.org/dwc/text/"><core encoding="UTF-8"'
                ' linesTerminatedBy="\\n" rowType="http://rs.tdwg.org/dwc/terms/Occurrence"'
                ' fieldsTerminatedBy="\\t" fieldsEnclosedBy=""'
                ' ignoreHeaderLines="0"><files><location>occurrence.tsv</location></files><id'
                ' index="0"/>'
            )
            meta += "".join(
                '<field index="{}" term="http://rs.tdwg.org/dwc/terms/{}"/>'.format(i + 1, field)
                for i, field in enumerate(fields)
            )
            meta += "</core></archive>"
            stream = io.StringIO()
            csv.writer(stream, delimiter="\t").writerow(
                ["1", "Abies", "alba", "Plantae", "A", "0", "0", "ES", "1", "2024-01-01"]
            )
            with zipfile.ZipFile(path, "w") as z:
                z.writestr("meta.xml", meta)
                z.writestr("occurrence.tsv", stream.getvalue())
            with patch(
                "fair_eva.plugin.gbif.catalogue.requests.get",
                side_effect=AssertionError("network"),
            ):
                with patch(
                    "fair_eva.plugin.gbif.quality.gpd.read_file", return_value=self.borders
                ) as read:
                    result = ICA(path, catalogue_dir=root)
                    self.assertEqual(result["ICA"], 100)
                    self.assertEqual(read.call_count, 1)
                    self.assertEqual(result["Catalogue"]["version"], "2026-07-14")
                with patch(
                    "fair_eva.plugin.gbif.quality.gpd.read_file", side_effect=OSError("missing")
                ):
                    with self.assertRaises(OSError):
                        ICA(path, catalogue_dir=root)

    def test_missing_columns_and_all_blank(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            index(root)
            Catalogue(root)._save_state({"checked_at": time.time()})
            path = root / "occurrences.zip"
            with zipfile.ZipFile(path, "w") as z:
                z.writestr(
                    "meta.xml",
                    '<archive xmlns="http://rs.tdwg.org/dwc/text/"><core encoding="UTF-8"'
                    ' linesTerminatedBy="\\n" rowType="http://rs.tdwg.org/dwc/terms/Occurrence"'
                    ' fieldsTerminatedBy="\\t"'
                    ' fieldsEnclosedBy=""><files><location>occurrence.tsv</location></files><id'
                    ' index="0"/></core></archive>',
                )
                z.writestr("occurrence.tsv", "1\n2\n")
            with patch("fair_eva.plugin.gbif.quality.gpd.read_file", return_value=self.borders):
                self.assertEqual(ICA(path, catalogue_dir=root)["ICA"], 0)


if __name__ == "__main__":
    unittest.main()
