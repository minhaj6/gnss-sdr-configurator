"""Load and validate schema/schema.json into typed dataclasses.

The JSON shape is documented in schema/SCHEMA_SPEC.md.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path

class SchemaError(Exception):
    """The schema file is missing, malformed, or an unsupported version."""


def default_schema_path() -> Path:
    """Locate schema.json: bundled inside the installed package (hatchling
    force-include, see pyproject.toml), or at schema/schema.json in a
    development checkout of this repo."""
    package_root = Path(__file__).resolve().parent.parent
    bundled = package_root / "schema.json"
    if bundled.is_file():
        return bundled
    repo_copy = package_root.parents[1] / "schema" / "schema.json"
    if repo_copy.is_file():
        return repo_copy
    raise SchemaError(
        f"schema.json not found (looked at {bundled} and {repo_copy})"
    )


@dataclass(frozen=True)
class Parameter:
    key: str
    placeholders: tuple[str, ...]
    type: str
    default_expr: str
    default_value: object
    adapter: str
    file: str
    line: int
    occurrences: int
    other_default_exprs: tuple[str, ...] = ()
    deprecated: bool = False
    options: tuple[str, ...] = ()

    @property
    def is_role_scoped(self) -> bool:
        return self.key.startswith("<role>.")

    @property
    def name(self) -> str:
        """Key without the `<role>.` prefix (for display next to a block)."""
        return self.key.removeprefix("<role>.")

    @property
    def default_str(self) -> str:
        """The default as it would appear in a .conf file, or '' if the
        default is not statically known."""
        if self.default_value is None:
            return ""
        if isinstance(self.default_value, bool):
            return "true" if self.default_value else "false"
        return str(self.default_value)


@dataclass(frozen=True)
class Implementation:
    name: str
    adapter: str
    signal: str | None = None


@dataclass
class Schema:
    generated_from: str
    generated_at: str
    entries: list[Parameter]
    implementations: list[Implementation]
    adapter_bases: dict[str, list[str]]
    adapter_uses: dict[str, list[str]]
    adapter_files: dict[str, str]
    _by_adapter: dict[str, list[Parameter]] = field(default_factory=dict)
    _links: dict[str, list[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for entry in self.entries:
            self._by_adapter.setdefault(entry.adapter, []).append(entry)
        for graph in (self.adapter_bases, self.adapter_uses):
            for cls, targets in graph.items():
                self._links.setdefault(cls, []).extend(targets)

    def params_for_adapter(self, adapter: str) -> list[Parameter]:
        """Parameters of an adapter class including everything reachable
        through base classes (adapter_bases) and configuration helper
        classes it owns (adapter_uses), transitively."""
        params: list[Parameter] = []
        seen_keys: set[str] = set()
        stack, visited = [adapter], set()
        while stack:
            cls = stack.pop(0)
            if cls in visited:
                continue
            visited.add(cls)
            for p in self._by_adapter.get(cls, []):
                if p.key not in seen_keys:
                    seen_keys.add(p.key)
                    params.append(p)
            stack.extend(self._links.get(cls, []))
        return params

    def params_for_implementation(self, name: str) -> list[Parameter]:
        for impl in self.implementations:
            if impl.name == name:
                return self.params_for_adapter(impl.adapter)
        raise KeyError(name)

    def category_of_adapter(self, adapter: str) -> str | None:
        """Category directory the adapter lives in (e.g. 'signal_source',
        'acquisition'), or None if the adapter is unknown."""
        file = self.adapter_files.get(adapter)
        if file is None:
            for linked in self._links.get(adapter, []):
                category = self.category_of_adapter(linked)
                if category is not None:
                    return category
            return None
        parts = Path(file).parts
        if "adapters" in parts:
            return parts[parts.index("adapters") - 1]
        if "libs" in parts:
            parent = parts[parts.index("libs") - 1]
            # src/algorithms/libs itself (e.g. Pass_Through) -> "libs";
            # src/algorithms/<category>/libs -> that category
            return "libs" if parent == "algorithms" else parent
        if "receiver" in parts:
            return "receiver"
        return None

    def implementations_for_category(
        self, *categories: str, signal: str | None = None
    ) -> list[Implementation]:
        """Implementations in the given categories; with `signal`, only
        those for that channel signal code (implementations without a known
        signal are kept — better offered everywhere than nowhere)."""
        return [
            impl
            for impl in self.implementations
            if self.category_of_adapter(impl.adapter) in categories
            and (signal is None or impl.signal in (None, signal))
        ]

    def signal_codes(self) -> list[str]:
        """All channel signal codes seen on implementations, sorted."""
        return sorted({i.signal for i in self.implementations if i.signal})

    def concrete_entries(self, prefix: str) -> list[Parameter]:
        """Placeholder-free entries whose key starts with `prefix`, one per
        key. The same key is often read in many files (internal_fs_sps is
        read in 16); the representative with a known default wins, and a
        key counts as deprecated only if every reader deprecates it."""
        by_key: dict[str, list[Parameter]] = {}
        for e in self.entries:
            if not e.placeholders and e.key.startswith(prefix):
                by_key.setdefault(e.key, []).append(e)
        result = []
        for key in sorted(by_key):
            group = by_key[key]
            best = next(
                (e for e in group if e.default_value is not None), group[0]
            )
            deprecated = all(e.deprecated for e in group)
            if deprecated != best.deprecated:
                best = replace(best, deprecated=deprecated)
            result.append(best)
        return result


def _parameter_from_json(raw: dict) -> Parameter:
    return Parameter(
        key=raw["key"],
        placeholders=tuple(raw["placeholders"]),
        type=raw["type"],
        default_expr=raw["default_expr"],
        default_value=raw["default_value"],
        adapter=raw["adapter"],
        file=raw["file"],
        line=raw["line"],
        occurrences=raw["occurrences"],
        other_default_exprs=tuple(raw.get("other_default_exprs", [])),
        deprecated=raw.get("deprecated", False),
        options=tuple(raw.get("options", [])),
    )


def load_schema(path: Path | None = None) -> Schema:
    path = path or default_schema_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SchemaError(f"schema file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SchemaError(f"schema file is not valid JSON: {path}") from exc
    try:
        return _schema_from_json(raw)
    except (KeyError, TypeError) as exc:
        raise SchemaError(
            f"schema file {path} does not match the shape documented in "
            f"schema/SCHEMA_SPEC.md ({exc!r}); regenerate it with "
            "scraper/extract_schema.py"
        ) from exc


def _schema_from_json(raw: dict) -> Schema:
    return Schema(
        generated_from=raw["generated_from"],
        generated_at=raw["generated_at"],
        entries=[_parameter_from_json(e) for e in raw["entries"]],
        implementations=[
            Implementation(
                name=i["name"],
                adapter=i["adapter"],
                signal=i.get("signal"),
            )
            for i in raw["implementations"]
        ],
        adapter_bases=raw["adapter_bases"],
        adapter_uses=raw["adapter_uses"],
        adapter_files=raw["adapter_files"],
    )
