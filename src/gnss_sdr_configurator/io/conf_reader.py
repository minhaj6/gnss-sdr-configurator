"""Parse an existing GNSS-SDR .conf file
Comments and ordering survive load -> edit -> save workflow.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# splits an after-the-= remainder into value and optional inline comment
# (inline comments require whitespace before ; or #, like gnss-sdr's reader)
_VALUE_RE = re.compile(r"^(?P<value>.*?)(?P<comment>\s+[;#].*)?$")


@dataclass
class ConfLine:
    """One physical line. Non-key lines keep only `raw`; key lines also
    record the parsed key/value and the pieces needed to rewrite the value
    without disturbing spacing or an inline comment."""

    raw: str
    key: str | None = None
    value: str | None = None
    _pre: str = ""   # everything up to and including '='
    _post: str = ""  # inline comment tail (with its leading whitespace)

    def set_value(self, value: str) -> None:
        assert self.key is not None
        self.value = value
        self.raw = f"{self._pre}{value}{self._post}"


@dataclass
class ConfDocument:
    lines: list[ConfLine] = field(default_factory=list)
    trailing_newline: bool = True

    def items(self) -> list[tuple[str, str]]:
        """key=value pairs in file order; for duplicate keys the last
        occurrence wins (matching gnss-sdr's reader)."""
        seen: dict[str, tuple[str, str]] = {}
        for line in self.lines:
            if line.key is not None:
                seen[line.key.lower()] = (line.key, line.value or "")
        return list(seen.values())

    def _last_line_for(self, key: str) -> ConfLine | None:
        found = None
        for line in self.lines:
            if line.key is not None and line.key.lower() == key.lower():
                found = line
        return found

    def get(self, key: str) -> str | None:
        line = self._last_line_for(key)
        return None if line is None else line.value

    def set(self, key: str, value: str) -> None:
        """Update the last line holding `key` in place, or append a new
        `key=value` line at the end of the file."""
        line = self._last_line_for(key)
        if line is not None:
            line.set_value(value)
        else:
            self.lines.append(
                ConfLine(
                    raw=f"{key}={value}",
                    key=key,
                    value=value,
                    _pre=f"{key}=",
                )
            )

    def remove(self, key: str) -> None:
        self.lines = [
            l
            for l in self.lines
            if l.key is None or l.key.lower() != key.lower()
        ]

    def render(self) -> str:
        text = "\n".join(line.raw for line in self.lines)
        return text + ("\n" if self.trailing_newline else "")


def _parse_line(raw: str) -> ConfLine:
    stripped = raw.strip()
    if (
        not stripped
        or stripped.startswith((";", "#", "["))
        or "=" not in raw
    ):
        return ConfLine(raw=raw)
    eq = raw.index("=")
    key = raw[:eq].strip()
    if not key:
        return ConfLine(raw=raw)
    m = _VALUE_RE.match(raw[eq + 1 :])
    value_part = m.group("value")
    comment = m.group("comment") or ""
    lead_ws = len(value_part) - len(value_part.lstrip())
    pre = raw[: eq + 1 + lead_ws]
    return ConfLine(
        raw=raw,
        key=key,
        value=value_part.strip(),
        _pre=pre,
        _post=comment,
    )


def parse_conf(text: str) -> ConfDocument:
    doc = ConfDocument(trailing_newline=text.endswith("\n"))
    body = text[:-1] if text.endswith("\n") else text
    if body:
        doc.lines = [_parse_line(raw) for raw in body.split("\n")]
    return doc


def load_conf(path: Path) -> ConfDocument:
    return parse_conf(path.read_text(encoding="utf-8"))
