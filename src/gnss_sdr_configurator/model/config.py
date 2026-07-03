"""In-memory receiver configuration: key=value pairs for the .conf file.

only values that differ from their schema default are kept 
(set_param drops a value equal to its default).
"""

from __future__ import annotations


class ReceiverConfig:
    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    def set(self, key: str, value: str) -> None:
        """Set a key unconditionally (used for implementation= choices)."""
        self._values[key] = value

    def set_param(self, key: str, value: str, default: str) -> None:
        """Set a parameter value; a value equal to its default (or empty)
        is removed instead, so only intentional overrides are written."""
        if value == "" or value == default:
            self._values.pop(key, None)
        else:
            self._values[key] = value

    def unset(self, key: str) -> None:
        self._values.pop(key, None)

    def get(self, key: str, default: str = "") -> str:
        return self._values.get(key, default)

    def __contains__(self, key: str) -> bool:
        return key in self._values

    def items(self) -> list[tuple[str, str]]:
        return list(self._values.items())

    def __len__(self) -> int:
        return len(self._values)
