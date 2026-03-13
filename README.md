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

Create a virtual environment and install the plugin in editable mode:

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

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
