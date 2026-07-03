"""ParamRow: one editable configuration parameter.

Dumb widget: renders a key, its type, and an Input; emits Changed messages.
All config mutation happens in the app via model/config.py.
"""

from __future__ import annotations

from textual.containers import Horizontal
from textual.message import Message
from textual.widgets import Button, Input, Label, Select


class ParamRow(Horizontal):
    """A parameter renders as a Select when it has a known value set
    (bool -> true/false; enum-like params -> their scraped options),
    otherwise as an Input. Blank/empty means "use the default" and the key
    is not written."""

    DEFAULT_CSS = """
    ParamRow { height: 3; }
    ParamRow Label { padding-top: 1; width: 36; }
    ParamRow Input { width: 1fr; }
    ParamRow Select { width: 1fr; }
    ParamRow Button { width: auto; }
    """

    class Changed(Message):
        def __init__(self, key: str, value: str, default: str) -> None:
            super().__init__()
            self.key = key
            self.value = value
            self.default = default

    class Browse(Message):
        def __init__(self, key: str) -> None:
            super().__init__()
            self.key = key

    def __init__(
        self,
        key: str,
        param_type: str,
        default: str,
        value: str = "",
        tooltip: str = "",
        browsable: bool = False,
        options: tuple[str, ...] = (),
    ) -> None:
        super().__init__()
        self.key = key
        self.param_type = param_type
        self.default = default
        self.value = value
        self.row_tooltip = tooltip
        self.browsable = browsable
        self.options = options

    def compose(self):
        name = self.key.rsplit(".", 1)[-1]
        label = Label(f"{name} [{self.param_type}]")
        if self.row_tooltip:
            label.tooltip = self.row_tooltip
        yield label
        if self.options:
            choices = list(self.options)
            if self.value and self.value not in choices:
                choices.append(self.value)  # keep odd loaded values visible
            yield Select(
                [(c, c) for c in choices],
                prompt=f"(default: {self.default})"
                if self.default
                else "(not set)",
                value=self.value if self.value in choices else Select.NULL,
            )
        else:
            yield Input(
                value=self.value, placeholder=self.default or "(no default)"
            )
        if self.browsable:
            yield Button("Browse…")

    def set_value(self, value: str) -> None:
        self.query_one(Input).value = value  # Input.Changed then fires

    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()
        self.post_message(self.Changed(self.key, event.value, self.default))

    def on_select_changed(self, event: Select.Changed) -> None:
        event.stop()
        value = "" if event.value is Select.NULL else str(event.value)
        self.post_message(self.Changed(self.key, value, self.default))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.post_message(self.Browse(self.key))
