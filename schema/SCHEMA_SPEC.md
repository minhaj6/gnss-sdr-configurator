# SCHEMA_SPEC.md — shape of schema/schema.json

This document and the emitter (scraper/extract_schema.py) change together;
the JSON file must always conform to what is documented here. The loader
(model/schema.py) rejects files that do not match this shape.

## Purpose

`schema/schema.json` is the only interface between the scraper and the TUI.
It records every GNSS-SDR configuration key the scraper could statically
extract from `configuration->property(...)` calls in a specific GNSS-SDR
source checkout.

## Top level

```json
{
  "generated_from": "<gnss-sdr git commit sha, '-dirty' appended if the worktree was not clean>",
  "generated_at": "<UTC ISO-8601 timestamp>",
  "counts": {
    "files_scanned": 0,
    "property_calls_matched": 0,
    "entries": 0,
    "unresolved": 0,
    "implementations": 0
  },
  "implementations": [],
  "adapter_bases": {},
  "adapter_uses": {},
  "adapter_files": {},
  "entries": [],
  "unresolved": []
}
```

Note: build-flag preprocessor conditionals (`#if ENABLE_UHD` …) are blanked
before parsing, so implementations and entries are extracted regardless of
compile-time gating. A schema consumer cannot assume every listed
implementation is available in a given gnss-sdr binary.

`counts` are recomputed at emit time and must equal the actual array
lengths / scan totals.

## What is scanned

- `src/algorithms/**/adapters/*.cc`
- `src/algorithms/**/libs/*.cc` and `src/algorithms/libs/*.cc` (helper
  classes such as `Acq_Conf`, `Dll_Pll_Conf`, `Tlm_Conf`, `Pass_Through`
  read many properties on the adapters' behalf)
- `src/core/receiver/*.cc`

Additionally, `src/core/receiver/gnss_block_factory.cc` is read for the
`implementations` mapping, and the matching `*.h` headers for the
`adapter_bases` / `adapter_uses` maps.

A property call is an expression `R->property(KEY_EXPR, DEFAULT_EXPR)` where
the receiver `R` is one of `configuration`, `configuration_`, `config_`.
Calls on `overrided_` are deliberately excluded: they occur inside the
`ConfigurationInterface` implementations themselves (delegation, variable
keys), not at real configuration-key use sites. Calls inside comments or
string literals are ignored (the source is parsed with a real C++ grammar,
tree-sitter-cpp; see decision D5).

## Implementations and adapter bases

```json
"implementations": [
  {"name": "GPS_L1_CA_PCPS_Acquisition", "adapter": "PcpsAcquisitionAdapter", "signal": "1C"}
],
"adapter_bases": {
  "FileSignalSource": ["FileSourceBase"],
  "FileSourceBase": ["SignalSourceBase"]
},
"adapter_uses": {
  "PcpsAcquisitionAdapter": ["Acq_Conf"]
},
"adapter_files": {
  "SignalConditioner": "src/algorithms/conditioner/adapters/signal_conditioner.cc"
}
```

- `implementations` — every user-facing `implementation=` string accepted by
  `gnss_block_factory.cc`, with the class whose parameters apply. Extracted
  from `if (implementation == "Name")` chains: the branch's
  `std::make_unique<AdapterClass>` if present, else the return type of a
  factory-local helper called in the branch (telemetry decoders →
  `Tlm_Conf`). Comparisons on the factory's `signal_conditioner` variable
  and `!= "Name"` guard clauses are also understood. Sorted by `name`;
  several names may share one class. The OPTIONAL `signal` field is the
  channel signal code (`1C`, `2S`, `1B`, …) taken from the signal constant
  passed to the constructor (`GPS_1C`; band spellings like `GAL_E5a`
  normalize to the channel code `5X`), or inferred from the implementation
  name for branches without a constant (telemetry decoders). Absent when
  the implementation is not signal-specific (sources, filters, PVT, …).
- `adapter_bases` — base classes per adapter class, restricted to bases
  that themselves own schema entries (interface-only bases are omitted).
- `adapter_uses` — configuration helper classes owned as data members
  (e.g. `PcpsAcquisitionAdapter` holds an `Acq_Conf`), same restriction.
- `adapter_files` — the scanned .cc file each class was found in, for every
  scanned file (including classes with zero entries, e.g. the composite
  `SignalConditioner`). This is what consumers should use to categorize a
  class (signal_source / acquisition / …) by its directory.

Consumers must resolve both maps transitively: the parameters applicable
to an implementation are its class's entries plus those of all classes
reachable through `adapter_bases` and `adapter_uses` (e.g.
`File_Signal_Source` → `FileSignalSource` → `FileSourceBase` →
`SignalSourceBase`).

## Entry object

One entry per unique `(key, adapter, file)`:

```json
{
  "key": "<role>.doppler_max",
  "placeholders": ["role"],
  "type": "int",
  "default_expr": "5000",
  "default_value": 5000,
  "adapter": "PcpsAcquisitionAdapter",
  "file": "src/algorithms/acquisition/adapters/pcps_acquisition_adapter.cc",
  "line": 42,
  "occurrences": 1
}
```

- `key` — normalized key pattern. Literal string parts appear verbatim.
  Variable parts become `<name>` placeholders (see normalization below).
  A key with no placeholders is a concrete key (e.g. `GNSS-SDR.enable_FPGA`).
- `placeholders` — placeholder names appearing in `key`, in order. Empty
  list for concrete keys. `role` is the block role the adapter is
  instantiated under (e.g. `SignalSource`, `Acquisition_1C`).
- `type` — `"int" | "float" | "bool" | "string" | "unknown"`, inferred from
  the default expression (see below). `unknown` means the scraper could not
  infer it; the TUI must treat the value as free-form text.
- `default_expr` — the raw C++ second-argument text, always present.
- `default_value` — JSON value when the default was resolved to a literal
  (possibly via a local `const` variable), else `null`.
- `adapter` — class name owning the call, taken from the file's
  `ClassName::ClassName(` constructor definition; if a file has no such
  definition, the CamelCased file stem is used.
- `file` — path relative to the GNSS-SDR root; `line` — first occurrence.
- `occurrences` — number of call sites merged into this entry.
- `other_default_exprs` — OPTIONAL; present only when merged duplicates had
  differing raw default expressions (first one wins `default_expr`).
- `options` — OPTIONAL; the string values this parameter's variable is
  compared against (`==`/`!=`) in the same file, in source order, plus the
  default. Present for enum-like parameters (`item_type`, `filter_type`,
  …); consumers should offer these as a choice list. Not guaranteed
  exhaustive — a file may only compare a subset of accepted values.
- `deprecated` — OPTIONAL; `true` only when every call site in this file
  assigns the property's result to a variable whose name contains
  "deprecat" (upstream convention, e.g. `fs_in_deprecated =
  property("GNSS-SDR.internal_fs_hz", ...)`). Consumers should hide such
  keys from editing UIs; the same key may appear non-deprecated in another
  file's entry, in which case it is live.

Entries are sorted by `(file, line, key)` for stable diffs.

## Key normalization

`KEY_EXPR` is split on top-level `+`. Each part must be one of:

| Part form | Contribution to `key` |
|-----------|----------------------|
| string literal `"..."`, `"..."s`, `std::string{"..."}`, `std::string("...")` | literal text verbatim |
| identifier `role` / `role_` | `<role>` |
| any other simple identifier `x` / `x_` | `<x>` (trailing `_` stripped) |
| `std::to_string(x)` / `std::to_string(x_)` | `<x>` (trailing `_` stripped) |

Any other part form (function calls, ternaries, indexing, arithmetic) makes
the whole key unresolvable: the call goes to `unresolved` instead of
`entries`. Nothing is silently dropped.

## Default-type inference

Tried in order:

1. **Literal**: `true`/`false` → bool; C++ integer literal (decimal or hex,
   with `u`/`l` suffixes) → int; C++ float literal (requires `.`, exponent,
   or `f` suffix) → float; string literal (incl. `"..."s`) → string.
   A literal wrapped in a typed brace/paren initializer — `uint64_t{0UL}`,
   `int64_t(0)`, `std::string("x")` — or in `static_cast<T>(...)` is
   unwrapped first; the wrapping/cast type wins for the schema type.
   `std::string()` / `std::string{}` resolve to the empty string.
2. **Local const variable**: an identifier is looked up in a per-file symbol
   table built from simple one-line `const <type> <name> = <literal>;`
   (or `(<literal>)` / `{<literal>}`) declarations; a default-constructed
   `const std::string name;` resolves to `""`. The declared C++ type
   maps to the schema type (`bool`→bool; `std::string`→string;
   `float`/`double`→float; `int`, `unsigned`, `size_t`, `[u]intN_t`,
   `long` → int).
3. Otherwise: `type: "unknown"`, `default_value: null`, raw `default_expr`
   preserved.

## Unresolved object

```json
{
  "file": "src/algorithms/example/adapters/example.cc",
  "line": 99,
  "key_expr": "role_ + options[i]",
  "default_expr": "defaults[i]",
  "reason": "key part 'options[i]' is not a resolvable form"
}
```

A call lands here only when some key part is not in the normalization
table (e.g. indexing, ternaries, arbitrary function calls). `reason` is
human-readable and non-normative. Note that a bare identifier IS resolvable
(it becomes a placeholder), so e.g. `role_ + option` yields the entry key
`<role><option>` rather than an unresolved record — such odd-looking
placeholders are part of what the D3 human review is for.

Sorted by `(file, line)`. This list exists for human review (decision D3);
the TUI ignores it.

## Known limitations

- Keys read via helpers other than `property()` (if any) are not seen.
- Property calls in non-adapter processing blocks outside the scanned set
  (if any exist) are not seen.
