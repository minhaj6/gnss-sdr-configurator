# gnss-sdr-configurator

> **Note:** this project is very much a work in progress so far and not ready for use.

A terminal UI for creating and editing [GNSS-SDR](https://gnss-sdr.org) `.conf` files. Pick block implementations from a schema extracted from the GNSS-SDR source, edit parameters with their real defaults at hand, and load existing configs without losing a single comment.

The intention of this program is to make the configuration generation process more user-friendly. From a new-user persepctive, it is hard to keep track all the possible configuration options you have. I have found a similar attempt here - [gnss-sdr-gui](https://github.com/UHaider/gnss_sdr_gui). The reasons behind choosing to start another project from scratch - 
1. The [gnss-sdr-gui](https://github.com/UHaider/gnss_sdr_gui) project seems to be abandoned. 
2. I want a terminal user interface (TUI) since I frequently run GNSS-SDR in headless session (in an SSH session).

There are two parts of the tool:

1. **The TUI** (installed as `gnss-sdr-configurator`) — reads `schema/schema.json` and presents a navigable configurator. Create a new receiver config, or load an existing one (Ctrl+O), edit, and save (Ctrl+S). Loading is non-destructive: comments, ordering, and unknown keys are preserved; saving an unedited file reproduces it. Keys the form does not recognize appear under "Unrecognized options" and are written back untouched.
2. **The scraper** (`scraper/extract_schema.py`, developers only) — walks a GNSS-SDR source checkout and regenerates the schema using tree-sitter-cpp.

## Install (users)

```sh
pipx install .
gnss-sdr-configurator
```

## Developer setup

```sh
python3 -m venv venv
source venv/bin/activate
pip install -e '.[dev]'      # includes pytest and the scraper's tree-sitter deps
pytest
```

Layout: src-style package (`src/gnss_sdr_configurator/`), with a strict
one-way dependency flow `tui -> model <- io`. The scraper depends on
nothing in `src/` and nothing in `src/` imports the scraper; the only
contract between them is `schema/schema.json` (shape documented in
`schema/SCHEMA_SPEC.md`).

## Regenerating the schema

Regenerating `schema/schema.json` is a deliberate act with a review of the
resulting changes, not a build step. It records the GNSS-SDR commit it was
generated from in its `generated_from` header.

```sh
python scraper/extract_schema.py \
    --gnss-sdr-root upstream/gnss-sdr \
    --out schema/schema.json
```

See `scraper/README.md` for details. When the JSON shape itself changes,
`SCHEMA_SPEC.md` changes with it, and `model/schema.py` rejects files that
do not match the documented shape.
