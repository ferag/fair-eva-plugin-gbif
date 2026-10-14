"""Versioned, streaming Catalogue of Life Base genus index (POSIX hosts)."""

import csv
import datetime as dt
import fcntl
import hashlib
from html.parser import HTMLParser
import io
import json
import logging
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import time
import unicodedata
from urllib.parse import urljoin
import xml.etree.ElementTree as ET
import zipfile

import requests

ARCHIVE = "https://download.checklistbank.org/col/monthly/"
RELEASE = re.compile(r"^(\d{4}-\d{2}-\d{2})_dwca\.zip$")
logger = logging.getLogger(__name__)


class CatalogueUnavailable(RuntimeError):
    """No trustworthy local index could be opened or built."""


def normalize_name(value):
    return " ".join(unicodedata.normalize("NFKC", str(value)).split()).casefold()


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.names = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            name = dict(attrs).get("href", "")
            if RELEASE.fullmatch(name):
                dt.date.fromisoformat(name[:10])
                self.names.append(name)


class Snapshot:
    def __init__(self, path):
        self.connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            if self.connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("Catalogue integrity check failed")
            self.metadata = json.loads(
                self.connection.execute(
                    "SELECT value FROM metadata WHERE key='release'"
                ).fetchone()[0]
            )
            dt.date.fromisoformat(self.metadata["version"])
            if not self.connection.execute("SELECT 1 FROM genera LIMIT 1").fetchone():
                raise ValueError("Empty genus index")
        except Exception:
            self.close()
            raise

    def contains(self, genus):
        return (
            self.connection.execute(
                "SELECT 1 FROM genera WHERE name=?", (normalize_name(genus),)
            ).fetchone()
            is not None
        )

    def close(self):
        self.connection.close()


class Catalogue:
    def __init__(self, directory=None, check_interval_hours=24):
        cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
        self.directory = Path(directory).expanduser() if directory else cache / "fair-eva-gbif/col"
        self.interval = float(check_interval_hours) * 3600
        if not math.isfinite(self.interval) or self.interval <= 0:
            raise ValueError("Catalogue check interval must be positive and finite")

    def _state(self):
        try:
            state = json.loads((self.directory / "state.json").read_text())
            if not isinstance(state, dict) or not math.isfinite(float(state.get("checked_at", 0))):
                return {}
            state["checked_at"] = float(state.get("checked_at", 0))
            return state
        except (OSError, ValueError, TypeError):
            return {}

    def _save_state(self, state):
        path = self.directory / "state.tmp"
        path.write_text(json.dumps(state))
        os.replace(path, self.directory / "state.json")

    def _open(self):
        for name in ("current.sqlite", "previous.sqlite"):
            try:
                snapshot = Snapshot(self.directory / name)
                if name == "previous.sqlite":
                    snapshot.metadata["warning"] = (
                        "Current catalogue unavailable; using previous release"
                    )
                return snapshot
            except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
                continue
        return None

    def acquire(self):
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise CatalogueUnavailable("Cannot access catalogue directory: " + str(exc)) from exc
        snapshot = self._open()
        if snapshot is None:
            try:
                self.refresh(blocking=True)
            except OSError as exc:
                raise CatalogueUnavailable("Cannot prepare catalogue: " + str(exc)) from exc
            snapshot = self._open()
            if snapshot is None:
                raise CatalogueUnavailable(
                    self._state().get("error") or "No local catalogue available"
                )
        else:
            if time.time() - self._state().get("checked_at", 0) >= self.interval:
                threading.Thread(target=self._background_refresh, daemon=True).start()
        error = self._state().get("error")
        if error:
            snapshot.metadata["warning"] = error
        return snapshot

    def _background_refresh(self):
        try:
            self.refresh()
        except OSError as exc:
            # A filesystem failure can prevent writing state.json itself.
            logger.warning("Cannot update catalogue cache: %s", exc)

    def refresh(self, blocking=False):
        self.directory.mkdir(parents=True, exist_ok=True)
        with (self.directory / "update.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError:
                return
            state = self._state()
            if time.time() - state.get("checked_at", 0) < self.interval:
                return
            state = {"checked_at": time.time()}
            self._save_state(state)
            try:
                with requests.get(ARCHIVE, timeout=(15, 60)) as response:
                    response.raise_for_status()
                    links = Links()
                    links.feed(response.text)
                if not links.names:
                    raise ValueError("No Base DwC-A releases in official archive")
                name = max(links.names)
                release = name[:10]
                current = self._open()
                if current:
                    up_to_date = current.metadata["version"] >= release
                    current.close()
                    if up_to_date:
                        self._save_state(state)
                        return
                url = urljoin(ARCHIVE, name)
                with tempfile.TemporaryDirectory(prefix="import-", dir=self.directory) as temp:
                    archive = Path(temp) / "release.zip"
                    digest = hashlib.sha256()
                    with requests.get(url, stream=True, timeout=(15, 120)) as response:
                        response.raise_for_status()
                        with archive.open("wb") as output:
                            for chunk in response.iter_content(1024 * 1024):
                                if chunk:
                                    digest.update(chunk)
                                    output.write(chunk)
                        expected = response.headers.get("Content-Length")
                        if expected and archive.stat().st_size != int(expected):
                            raise ValueError("Truncated catalogue download")
                    metadata = {
                        "version": release,
                        "source": url,
                        "sha256": digest.hexdigest(),
                        "imported_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                        "edition": "Base",
                        "format_version": 1,
                    }
                    index = Path(temp) / "index.sqlite"
                    build_index(archive, index, metadata)
                    verified = Snapshot(index)
                    verified.close()
                    # Copy a verified old snapshot before publishing the replacement.
                    old = self._open()
                    if old:
                        backup = sqlite3.connect(Path(temp) / "previous.sqlite")
                        try:
                            old.connection.backup(backup)
                        finally:
                            backup.close()
                            old.close()
                        os.replace(
                            Path(temp) / "previous.sqlite", self.directory / "previous.sqlite"
                        )
                    os.replace(index, self.directory / "current.sqlite")
                self._save_state(state)
            except Exception as exc:
                state["error"] = "Catalogue update failed: {}".format(exc)
                self._save_state(state)
                logger.warning(state["error"])


def build_index(archive, target, metadata):
    """Read the declared DwC-A taxon core in batches; never extract the archive."""
    # Real CoL rows contain long reference/remarks fields unrelated to the name.
    csv.field_size_limit(max(csv.field_size_limit(), 16 * 1024 * 1024))
    with zipfile.ZipFile(archive) as zipped:
        corrupt = zipped.testzip()
        if corrupt:
            raise ValueError("Corrupt archive member: " + corrupt)
        meta_names = [n for n in zipped.namelist() if n == "meta.xml" or n.endswith("/meta.xml")]
        if len(meta_names) != 1:
            raise ValueError("Missing or ambiguous DwC-A meta.xml")
        meta_name = meta_names[0]
        root = ET.fromstring(zipped.read(meta_name))
        ns = {"d": "http://rs.tdwg.org/dwc/text/"}
        core = root.find("d:core", ns)
        if core is None or not core.get("rowType", "").endswith("Taxon"):
            raise ValueError("Expected a taxon core")
        fields = {
            field.get("term", "").rsplit("/", 1)[-1]: field
            for field in core.findall("d:field", ns)
        }
        if not {"taxonRank", "scientificName"}.issubset(fields):
            raise ValueError("Missing rank or scientific name")

        def value(row, name):
            field = fields.get(name)
            if field is None:
                return ""
            index = field.get("index")
            return row[int(index)] if index is not None else field.get("default", "")

        delimiter = core.get("fieldsTerminatedBy", "\t").replace("\\t", "\t")
        quote = core.get("fieldsEnclosedBy", '"')
        locations = core.findall("d:files/d:location", ns)
        if not locations:
            raise ValueError("Missing taxon core file")
        connection = sqlite3.connect(target)
        try:
            connection.execute("CREATE TABLE genera (name TEXT PRIMARY KEY) WITHOUT ROWID")
            connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT)")
            batch = []
            for location in locations:
                member = str(Path(meta_name).parent / location.text)
                with zipped.open(member) as raw, io.TextIOWrapper(
                    raw, encoding=core.get("encoding", "UTF-8")
                ) as stream:
                    reader = csv.reader(
                        stream,
                        delimiter=delimiter,
                        quotechar=quote or None,
                        quoting=csv.QUOTE_MINIMAL if quote else csv.QUOTE_NONE,
                    )
                    for _ in range(int(core.get("ignoreHeaderLines", "0"))):
                        next(reader)
                    for row in reader:
                        if normalize_name(value(row, "taxonRank")) != "genus":
                            continue
                        # A genus is uninomial; scientificName can include authorship.
                        name = value(row, "genericName")
                        if not name:
                            parts = value(row, "scientificName").strip().split()
                            if not parts:
                                raise ValueError("Genus row has no scientific name")
                            name = parts[0]
                        name = normalize_name(name)
                        if name:
                            batch.append((name,))
                        if len(batch) >= 5000:
                            connection.executemany(
                                "INSERT OR IGNORE INTO genera VALUES (?)", batch
                            )
                            connection.commit()
                            batch.clear()
            connection.executemany("INSERT OR IGNORE INTO genera VALUES (?)", batch)
            metadata["genus_count"] = connection.execute("SELECT count(*) FROM genera").fetchone()[
                0
            ]
            if not metadata["genus_count"]:
                raise ValueError("Catalogue contains no genera")
            connection.execute(
                "INSERT INTO metadata VALUES ('release', ?)", (json.dumps(metadata),)
            )
            connection.commit()
        finally:
            connection.close()
