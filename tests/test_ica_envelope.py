"""The API still receives (points, HTML), with exact scores for the web client."""

from configparser import ConfigParser
from html.parser import HTMLParser
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import fair_eva.plugin

fair_eva.plugin.__path__.append(
    str(Path(__file__).resolve().parents[1] / "fair_eva/plugin")
)
from fair_eva.plugin.gbif.plugin import Plugin


class EnvelopeTests(unittest.TestCase):
    def test_versioned_scores_and_safe_provenance(self):
        keys = (
            "ICA Taxonomic Genus Species Kingdom Family Identifiers Geographic "
            "Coordinates Countries CoordinatesUncertainty IncorrectCoordinates "
            "Temporal Years Months Days IncorrectDates"
        ).split()
        scores = {key: 0.0 for key in keys}
        scores["Class/Order"] = 0.0
        scores["Catalogue"] = {
            "version": "2026-08-20",
            "warning": '<script>alert("test")</script>',
        }
        plugin = Plugin.__new__(Plugin)
        plugin.config = ConfigParser()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.zip"
            path.touch()
            with patch.object(
                plugin, "_download_from_metadata", return_value={"path": str(path)}
            ), patch("fair_eva.plugin.gbif.plugin.ICA", return_value=scores):
                points, message = plugin.data_01()
        self.assertEqual(points, 0)
        self.assertIsInstance(message, str)
        self.assertNotIn("<script>", message)
        captured = []

        class Parser(HTMLParser):
            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if attrs.get("data-ica-version") == "1":
                    captured.append(json.loads(attrs["data-ica"]))

        Parser().feed(message)
        self.assertEqual(captured, [scores])
        self.assertIn("ica-table", message)
