#!/usr/bin/python
# -*- coding: utf-8 -*-
import logging
import os
from pathlib import Path
import sys
import time
import requests

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
SHP_PATH = STATIC_DIR / "ne_110m_admin_0_countries.shp"

logging.basicConfig(
    stream=sys.stdout, level=logging.DEBUG, format="'%(name)s:%(lineno)s' | %(message)s"
)

# Configura el nivel de registro para GeoPandas y Fiona
logging.getLogger("geopandas").setLevel(logging.ERROR)

logger = logging.getLogger(os.path.basename(__file__))


def gbif_doi_search(doi):
    """Realiza una búsqueda en GBIF utilizando un DOI y devuelve la información del
    conjunto de datos.

    Args:
    - doi (str): El DOI (Digital Object Identifier) del conjunto de datos que se va a buscar.

    Returns:
    - dict: Un diccionario que contiene información sobre el conjunto de datos encontrado.
    """
    # Realiza una solicitud para obtener la informacion del conjunto de datos desde la API de GBIF
    search_request = requests.get(f"https://api.gbif.org/v1/dataset/doi/{doi}").json()[
        "results"
    ][0]

    # Imprime información relevante sobre el conjunto de datos
    logger.debug(f"TITLE: {search_request['title']}")
    logger.debug(f"DOI: {doi}")
    logger.debug(f"UUID: {search_request['key']}")

    # Devuelve el resultado de la búsqueda, que es un diccionario con información sobre el conjunto de datos
    return search_request


def gbif_download_request(uuid, timeout, api_mail, api_user, api_pass):
    """Realiza una solicitud de descarga de datos de ocurrencias desde GBIF y devuelve
    el estado de la solicitud.

    Args:
    - uuid (str): El UUID (identificador único universal) del conjunto de datos para el cual se solicita la descarga.
    - timeout (int): El tiempo máximo (en minutos) que se espera para que la solicitud de descarga se complete.

    Returns:
    - dict: Un diccionario que contiene el estado de la solicitud de descarga y otra información relevante.
    """
    # Convierte el tiempo de espera a segundos
    timeout = timeout * 60

    # Configuración de la solicitud de descarga
    download_query = {
        "notificationAddresses": [api_mail],
        "sendNotification": True,
        "format": "DWCA",  # Puedes cambiar el formato según tus necesidades
        # "format": "SIMPLE_CSV",
        "DATASET_KEY": uuid,
    }

    # Realiza la solicitud de la clave de descarga
    download_key_request = requests.get(
        f"https://api.gbif.org/v1/occurrence/download/request",
        params=download_query,
        auth=(api_user, api_pass),
    )
    # Verifica el estado de la solicitud de clave de descarga
    if download_key_request.status_code == 200:
        download_key = download_key_request.text
    else:
        download_key = f"Error {download_key_request.status_code}"

    # Imprime la clave de la solicitud de descarga
    # logger.debug(f"Download Request Key: {download_key}")

    # Monitorea el progreso de la descarga
    status = "PREPARING"
    t0 = time.time()
    while status in ("PREPARING", "RUNNING"):
        t1 = time.time()

        # Obtiene el estado actual de la solicitud de descarga
        download_request = requests.get(
            f"https://api.gbif.org/v1/occurrence/download/{download_key}"
        ).json()
        status = download_request["status"]

        # Maneja el caso en el que la descarga ha tenido éxito
        if status == "SUCCEEDED":
            logger.debug(f"Download Request Status: {status} [{time.time() - t0:.0f}s]")
            continue
        # Maneja el caso en el que el tiempo de espera ha sido superado
        elif timeout > 0 and (time.time() - t0) >= timeout:
            status = "TIMEOUT"
            logger.debug(f"Download Request Status: {status} [{timeout:.0f}s]")
            continue

        # Espera 20 segundos antes de realizar la siguiente verificación
        time.sleep(10 - (time.time() - t1))

        # Imprime el estado actual de la descarga
        logger.debug(f"Download Request Status: {status} [{time.time() - t0:.0f}s]")

    # Devuelve el resultado final de la solicitud de descarga
    return download_request


def gbif_doi_download(doi: str, timeout=-1, auth=None):
    """Busca un conjunto de datos en GBIF usando un DOI, realiza una solicitud de
    descarga y descarga los datos.

    Args:
    - doi (str): El DOI (Digital Object Identifier) del conjunto de datos que se va a descargar.
    - timeout (int, optional): El tiempo máximo (en minutos) que se espera para que la solicitud de descarga se complete. Por defecto, es -1, lo que significa sin límite de tiempo.

    Returns:
    - dict: Un diccionario que contiene información sobre el conjunto de datos descargado.
    """
    download_dict = {"doi": doi}
    # Búsqueda del conjunto de datos utilizando el DOI
    logger.debug("Busqueda de DOI")
    try:
        search_request = gbif_doi_search(doi)
        uuid = search_request["key"]
        download_dict["uuid"] = uuid
        download_dict["path"] = f"{BASE_DIR}/downloads/{uuid}.zip"
        download_dict["title"] = search_request["title"]
    except Exception as e:
        logger.debug(f"ERROR Searching Data: {e}")
        return download_dict

    logger.debug("Intentando IPT")
    endpoints = search_request["endpoints"]
    logger.debug(endpoints)
    for ep in endpoints:
        logger.debug("Probando endpoints")
        if ep.get("type") == "DWC_ARCHIVE" and ep.get("url"):
            url = ep["url"]
            logger.debug(f"Intentando descarga directa desde endpoint DWC_ARCHIVE: {url}")
            try:
                os.makedirs(os.path.dirname(download_dict["path"]), exist_ok=True)
                # Hacemos el GET con timeout razonable (por ejemplo 60s)
                with requests.get(url, stream=True, timeout=60) as resp:
                    resp.raise_for_status()
                    with open(download_dict["path"], "wb") as f:
                        for chunk in resp.iter_content(chunk_size=8192):
                            if chunk:
                                f.write(chunk)
                # Si llegamos aquí, la descarga fue exitosa
                download_dict.update({
                    "download_url": url,
                    "download_method": "endpoint",
                    # opcionalmente, capturamos size si viene en headers
                    "size": int(resp.headers.get("content-length", 0))
                })
                logger.debug("Descarga directa exitosa.")
                return download_dict
            except Exception as e:
                logger.debug(f"ERROR descarga directa desde endpoint: {e}")
                # si falla, seguimos al siguiente endpoint o al fallback
                continue


    # Genera la solicitud de descarga
    logger.debug("Solicitud de Descarga")
    try:
        download_request = gbif_download_request(
            uuid, timeout, api_mail=auth[0], api_user=auth[1], api_pass=auth[2]
        )
        download_dict["size"] = download_request["size"]
    except Exception as e:
        logger.debug(f"ERROR Requesting Download: {e}")
        return download_dict

    # Descarga los datos del conjunto
    logger.debug("Descarga")
    try:
        os.makedirs(f"{BASE_DIR}/downloads", exist_ok=True)
        with open(download_dict["path"], "wb") as f:
            # Itera sobre los bloques del archivo descargado
            for data in requests.get(
                f"https://api.gbif.org/v1/occurrence/download/request/{download_request['key']}",
                stream=True,
            ).iter_content(chunk_size=1024):
                f.write(data)

        logger.debug(f"File size: {download_dict['size']:.0f}b")
    except Exception as e:
        logger.debug(f"ERROR Downloading Data: {e}")
        return download_dict

    return download_dict


# Keep the established imports available to callers of this module.
from .quality import (  # noqa: E402,F401
    ICA,
    geographic_percentajes,
    taxonomic_percentajes,
    temporal_percentajes,
)
