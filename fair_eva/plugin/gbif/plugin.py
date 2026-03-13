# SPDX-FileCopyrightText: Copyright contributors to the FAIR eva project
# SPDX-FileContributor: Fernando Aguilar <aguilarf@ifca.unican.es>
#
# SPDX-License-Identifier: GPL-3.0-or-later

# coding: utf-8
from configparser import ConfigParser
import logging
from pathlib import Path
import sys
from tempfile import NamedTemporaryFile
from typing import Dict, Optional, Tuple
import xml.etree.ElementTree as ET

from fair_eva.api.evaluator import EvaluatorBase
from fair_eva.plugin.gbif.gbif_data import ICA, gbif_doi_download, gbif_doi_search
import idutils
import pandas as pd
from pandas import DataFrame
import requests

logging.basicConfig(
    stream=sys.stdout, level=logging.DEBUG, format="'%(name)s:%(lineno)s' | %(message)s"
)
logger = logging.getLogger("fair_eva.api.plugin.evaluation_steps")

PLUGIN_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = PLUGIN_DIR / "downloads"


def _strip_namespace(tag: str) -> str:
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


class Plugin(EvaluatorBase):
    """GBIF plugin implementation for FAIR EVA."""

    def __init__(
        self,
        item_id: str,
        api_endpoint: str = "https://api.gbif.org/v1/",
        lang: str = "en",
        config: ConfigParser = ConfigParser(),
        name: str = "gbif",
    ):
        logger.debug("Initiating FAIR-EVA's <%s> plugin", name)
        super().__init__(item_id, api_endpoint, lang, config, name)

        self.metadata = self.get_metadata()
        if self.metadata.empty:
            error_message = f"Problem accessing (meta)data from repository <{api_endpoint}>"
            logger.error(error_message)
            raise RuntimeError(error_message)

        logger.debug("Successfully obtained metadata from repository")

    def _document_url(self) -> str:
        try:
            dataset = gbif_doi_search(self.item_id)
            return f"{self.api_endpoint.rstrip('/')}/dataset/{dataset['key']}/document"
        except Exception:
            pass

        schemes = idutils.detect_identifier_schemes(self.item_id)
        if not schemes:
            raise ValueError(f"Unsupported identifier: {self.item_id}")

        item_url = idutils.to_url(self.item_id, schemes[0], url_scheme="https")
        response = requests.get(item_url, allow_redirects=True, timeout=30)
        response.raise_for_status()
        final_url = response.url

        if "/resource?" in final_url:
            final_url = final_url.replace("/resource?", "/eml.do?")
        if "www.gbif.org/" in final_url:
            final_url = final_url.replace("www.gbif.org/", "api.gbif.org/v1/")
        if not final_url.endswith("/document") and "api.gbif.org/v1/" in final_url:
            final_url = final_url.rstrip("/") + "/document"
        return final_url

    def get_metadata(self) -> DataFrame:
        document_url = self._document_url()
        response = requests.get(document_url, timeout=60)
        response.raise_for_status()

        tree = ET.fromstring(response.content)
        rows = []

        def walk(node: ET.Element, path: str = "") -> None:
            tag = _strip_namespace(node.tag)
            current_path = f"{path}.{tag}" if path else tag
            text = (node.text or "").strip()
            if len(node) == 0 and text:
                parent_path, _, qualifier = current_path.rpartition(".")
                rows.append(
                    {
                        "metadata_schema": _strip_namespace(tree.tag),
                        "element": parent_path or tag,
                        "text_value": text,
                        "qualifier": qualifier or tag,
                    }
                )
                return

            for child in node:
                walk(child, current_path)

        walk(tree)
        return pd.DataFrame(
            rows, columns=["metadata_schema", "element", "text_value", "qualifier"]
        )

    def rda_a1_01m(self) -> Tuple[int, str]:
        try:
            data_res = gbif_doi_search(self.item_id)
        except Exception as exc:
            logger.debug("GBIF search failed for %s: %s", self.item_id, exc)
            data_res = {}

        if data_res:
            return (100, "Data found in GBIF")
        return (0, "Data is not accessible")

    def rda_a1_02m(self) -> Tuple[int, str]:
        return self.rda_a1_01m()

    @staticmethod
    def get_color(value: float) -> str:
        if value >= 75:
            return "#d1e7dd"
        if value >= 50:
            return "#fff3cd"
        if value >= 25:
            return "#f8d7da"
        return "#f5c2c7"

    def _download_from_metadata(self) -> Optional[Dict[str, str]]:
        identifier_rows = self.metadata[self.metadata["qualifier"] == "alternateIdentifier"]
        for _, row in identifier_rows.iterrows():
            text_value = row.get("text_value", "")
            if "ipt.gbif" not in text_value or "r=" not in text_value:
                continue

            short_name = text_value.split("r=", 1)[1]
            url = f"https://ipt.gbif.es/archive.do?r={short_name}"
            DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

            with NamedTemporaryFile(
                prefix=f"{short_name}_", suffix=".zip", dir=DOWNLOAD_DIR, delete=False
            ) as tmp_file:
                response = requests.get(url, stream=True, timeout=120)
                response.raise_for_status()
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        tmp_file.write(chunk)

            return {"path": tmp_file.name, "download_url": url}

        return None

    def _gbif_auth(self) -> Optional[Tuple[str, str, str]]:
        if not self.config.has_section("gbif"):
            return None

        values = tuple(
            self.config.get("gbif", key, fallback="").strip()
            for key in ("api_mail", "api_user", "api_pass")
        )
        if all(values):
            return values  # type: ignore[return-value]
        return None

    def data_01(self) -> Tuple[float, str]:
        download_dict: Optional[Dict[str, str]] = None

        try:
            download_dict = self._download_from_metadata()
        except Exception as exc:
            logger.debug("Direct IPT download failed: %s", exc)

        if not download_dict:
            try:
                download_dict = gbif_doi_download(self.item_id, auth=self._gbif_auth())
            except Exception as exc:
                logger.debug("GBIF download failed: %s", exc)
                return (0, "Unable to download the Darwin Core Archive from GBIF")

        download_path = download_dict.get("path") if download_dict else None
        if not download_path or not Path(download_path).exists():
            return (0, "Unable to locate a downloaded Darwin Core Archive")

        try:
            ica = ICA(download_path)
        except Exception as exc:
            logger.debug("ICA calculation failed: %s", exc)
            return (0, "Downloaded file could not be analysed")
        finally:
            try:
                Path(download_path).unlink(missing_ok=True)
            except Exception:
                logger.debug("Could not remove temporary file %s", download_path)

        points = round(ica["ICA"], 2)
        msg = f"""
        <div class="card shadow-sm my-3">
          <div class="card-header py-2"><strong>ICA summary</strong></div>
          <div class="card-body p-0">
            <table class="table table-sm mb-0 ica-table">
              <thead class="table-light">
                <tr>
                  <th scope="col">Dimension</th>
                  <th scope="col" class="text-end">Score</th>
                </tr>
              </thead>
              <tbody>
                <tr class="table-primary fw-bold">
                  <td>ICA total</td>
                  <td class="text-end">{ica['ICA']:.2f}%</td>
                </tr>
                <tr class="table-secondary fw-bold">
                  <td>Taxonomic</td>
                  <td class="text-end" style="background-color:{self.get_color(ica['Taxonomic'])}">{ica['Taxonomic']:.2f}%</td>
                </tr>
                <tr>
                  <td>Genus</td>
                  <td class="text-end" style="background-color:{self.get_color(ica['Genus'])}">{ica['Genus']:.2f}%</td>
                </tr>
                <tr>
                  <td>Species</td>
                  <td class="text-end" style="background-color:{self.get_color(ica['Species'])}">{ica['Species']:.2f}%</td>
                </tr>
                <tr>
                  <td>Kingdom</td>
                  <td class="text-end" style="background-color:{self.get_color(ica['Kingdom'])}">{ica['Kingdom']:.2f}%</td>
                </tr>
                <tr>
                  <td>Class / Order</td>
                  <td class="text-end" style="background-color:{self.get_color(ica['Class/Order'])}">{ica['Class/Order']:.2f}%</td>
                </tr>
                <tr>
                  <td>Family</td>
                  <td class="text-end" style="background-color:{self.get_color(ica['Family'])}">{ica['Family']:.2f}%</td>
                </tr>
                <tr>
                  <td>Identifiers</td>
                  <td class="text-end" style="background-color:{self.get_color(ica['Identifiers'])}">{ica['Identifiers']:.2f}%</td>
                </tr>
                <tr class="table-secondary fw-bold">
                  <td>Geographic</td>
                  <td class="text-end" style="background-color:{self.get_color(ica['Geographic'])}">{ica['Geographic']:.2f}%</td>
                </tr>
                <tr>
                  <td>Coordinates</td>
                  <td class="text-end" style="background-color:{self.get_color(ica['Coordinates'])}">{ica['Coordinates']:.2f}%</td>
                </tr>
                <tr>
                  <td>Countries</td>
                  <td class="text-end" style="background-color:{self.get_color(ica['Countries'])}">{ica['Countries']:.2f}%</td>
                </tr>
                <tr>
                  <td>CoordinatesUncertainty</td>
                  <td class="text-end" style="background-color:{self.get_color(ica['CoordinatesUncertainty'])}">{ica['CoordinatesUncertainty']:.2f}%</td>
                </tr>
                <tr>
                  <td>IncorrectCoordinates</td>
                  <td class="text-end" style="background-color:{self.get_color(ica['IncorrectCoordinates'])}">{ica['IncorrectCoordinates']:.2f}%</td>
                </tr>
                <tr class="table-secondary fw-bold">
                  <td>Temporal</td>
                  <td class="text-end" style="background-color:{self.get_color(ica['Temporal'])}">{ica['Temporal']:.2f}%</td>
                </tr>
                <tr>
                  <td>Years</td>
                  <td class="text-end" style="background-color:{self.get_color(ica['Years'])}">{ica['Years']:.2f}%</td>
                </tr>
                <tr>
                  <td>Months</td>
                  <td class="text-end" style="background-color:{self.get_color(ica['Months'])}">{ica['Months']:.2f}%</td>
                </tr>
                <tr>
                  <td>Days</td>
                  <td class="text-end" style="background-color:{self.get_color(ica['Days'])}">{ica['Days']:.2f}%</td>
                </tr>
                <tr>
                  <td>IncorrectDates</td>
                  <td class="text-end" style="background-color:{self.get_color(ica['IncorrectDates'])}">{ica['IncorrectDates']:.2f}%</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
        """
        return (points, msg)
