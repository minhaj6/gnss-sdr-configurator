# scraper

Developer-facing tool that walks a GNSS-SDR source checkout and emits `schema/schema.json` (shape documented in `schema/SCHEMA_SPEC.md`). Not part of the installed package.

## Dependencies

`tree-sitter` and `tree-sitter-cpp` (decision D5 in DECISIONS.md). Install
into the project venv:

```sh
source venv/bin/activate
pip install tree-sitter tree-sitter-cpp   # or: pip install -e '.[dev]'
```

## How it works

Each target `.cc` file (`src/algorithms/*/adapters/*.cc` and `src/core/receiver/*.cc`) is parsed into a real C++ syntax tree with the tree-sitter-cpp grammar. From the tree the scraper reads:

1. **Property calls** — `call_expression` nodes whose function is `<receiver>->property` with receiver `configuration`, `configuration_`, or `config_`. Comments and string literals can never produce false matches because the parser already classified them.
2. **Key expressions** — the first argument, flattened across `+` concatenation. String literals contribute verbatim text; identifiers and `std::to_string(x)` become `<placeholder>` segments. Anything else sends
   the call to the `unresolved` list.
3. **Default expressions** — the second argument, resolved to a typed value when it is a literal (including `static_cast<T>(lit)`, `T{lit}`,
   `T(lit)`, `std::string()`), or an identifier declared in the same file as a simple `const <type> name = <literal>;`.
4. **Adapter class** — from the `ClassName::ClassName(` constructor definition in the file (CamelCased file stem as fallback).

## Regenerating schema.json

Regeneration is a deliberate act with a diff review, not a build step:

```sh
python scraper/extract_schema.py \
    --gnss-sdr-root upstream/gnss-sdr \
    --out schema/schema.json
git diff schema/schema.json   # review before committing
```

The header records the GNSS-SDR commit the schema was generated from (`-dirty` appended if that checkout had local modifications).
