# gnss-sdr-configurator

> **Note:** this project is very much a work in progress so far and not ready for use.

A terminal UI for creating and editing [GNSS-SDR](https://gnss-sdr.org) `.conf` files. Pick block implementations from a schema extracted from the GNSS-SDR source, edit parameters with their real defaults at hand, and load existing configs without losing a single comment.

The intention of this program is to make the configuration generation process more user-friendly. From a new-user persepctive, it is hard to keep track all the possible configuration options you have. I have found a similar attempt here - [gnss-sdr-gui](https://github.com/UHaider/gnss_sdr_gui). The reasons behind choosing to start another project from scratch - 
1. The [gnss-sdr-gui](https://github.com/UHaider/gnss_sdr_gui) project seems to be abandoned. 
2. I want a terminal user interface (TUI) since I frequently run GNSS-SDR in headless session (in an SSH session).

There are two parts of the tool:

1. **The TUI** (installed as `gnss-sdr-configurator`) — reads `schema/schema.json` and presents a navigable configurator. Create a new receiver config, or load an existing one (Ctrl+O), edit, and save (Ctrl+S). Loading is non-destructive: comments, ordering, and unknown keys are preserved; saving an unedited file reproduces it. Keys the form does not recognize appear under "Unrecognized options" and are written back untouched.
2. **The scraper** (`scraper/extract_schema.py`, developers only) — walks a GNSS-SDR source checkout and regenerates the schema using tree-sitter-cpp.

About the scraper, I am debating myself on what would be a more sensible way of extracting all the configuration options. One way could be manually adding support for all the useful configuration options, start from a few and then adding more as I explore more of the gnss-sdr functionality. The reality is, I will never use all the configuration options there is. The other way is to automate the process somehow. For automation, I could scrape the documentation website (https://gnss-sdr.org/docs/) and go from there. But webscraping is messy. The next option can be parsing the gnss-sdr source code and extract the configuration options from there. There are parser libraries like [tree-sitter](https://tree-sitter.github.io/tree-sitter/). This is what I am going with now. It will require a lot of testing though. However, this is the design choice I am making right now. If you have any suggestion on what should be done instead, please let me know (create an issue or email). 

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
