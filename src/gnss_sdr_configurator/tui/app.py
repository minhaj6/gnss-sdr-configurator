"""Textual App and console-script entry point.

The tree lists the receiver's block roles; the detail panel shows the
implementation choice and parameters for the selected role. All state
lives in model/config.py; widgets only render and dispatch.

Per-signal channel sections (Acquisition/Tracking/Telemetry Decoder) are
built from the signal codes found in the schema's implementations.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

from textual.app import App
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Footer, Header, Label, Select, Tree
from textual_fspicker import FileOpen, FileSave, Filters

from gnss_sdr_configurator import __version__
from gnss_sdr_configurator.io.conf_reader import ConfDocument, load_conf
from gnss_sdr_configurator.io.conf_writer import write_conf
from gnss_sdr_configurator.model.config import ReceiverConfig
from gnss_sdr_configurator.model.schema import (
    Parameter,
    Schema,
    SchemaError,
    load_schema,
)
from gnss_sdr_configurator.tui.widgets.param_row import ParamRow
from gnss_sdr_configurator.tui.widgets.splitter import PaneSplitter

# display names for gnss-sdr channel signal codes (codes come from the
# schema; unknown codes fall back to showing the code itself)
SIGNAL_LABELS = {
    "1C": "GPS L1 C/A",
    "2S": "GPS L2C",
    "L5": "GPS L5",
    "1B": "Galileo E1",
    "5X": "Galileo E5a",
    "7X": "Galileo E5b",
    "E6": "Galileo E6",
    "1G": "GLONASS L1 C/A",
    "2G": "GLONASS L2 C/A",
    "B1": "BeiDou B1I",
    "B3": "BeiDou B3I",
    "J1": "QZSS L1 C/A",
    "J5": "QZSS L5",
}
SIGNAL_ORDER = list(SIGNAL_LABELS)


@dataclass(frozen=True)
class RoleSpec:
    """One tree node. Kinds, by which fields are set:

    - implementation-backed block role: `categories` non-empty (with
      `signal` set, only that signal's implementations are offered)
    - group of concrete keys: `prefix` set, optionally narrowed by
      `include`/`exclude` sub-prefixes (matched after removing `prefix`)
    - per-signal channel group: `signal` set without categories/prefix
      (renders the Channels_<signal>.count parameter)
    - passthrough bucket: `passthrough=True` (read-only keys from a
      loaded file the form does not render)
    - pure container: none of the above, only `children`

    `children` nest under this node in the tree; `label` is the display
    name (defaults to `role`); `collapsed` nodes start folded."""

    role: str
    label: str = ""
    categories: tuple[str, ...] = ()
    prefix: str = ""
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    signal: str = ""
    passthrough: bool = False
    collapsed: bool = False
    children: tuple["RoleSpec", ...] = ()

    @property
    def display(self) -> str:
        return self.label or self.role

    @property
    def is_container(self) -> bool:
        return not (
            self.categories or self.prefix or self.signal or self.passthrough
        )


PASSTHROUGH_SPEC = RoleSpec("Unrecognized options", passthrough=True)

_GLOBAL_GROUPS = ("SUPL_", "AGNSS_", "osnma_")


def build_role_specs(schema: Schema) -> list[RoleSpec]:
    codes = schema.signal_codes()
    ordered = [c for c in SIGNAL_ORDER if c in codes] + [
        c for c in codes if c not in SIGNAL_ORDER
    ]
    signal_nodes = tuple(
        RoleSpec(
            f"Channels_{code}",
            label=f"{SIGNAL_LABELS.get(code, code)} ({code})",
            signal=code,
            collapsed=True,
            children=(
                RoleSpec(
                    f"Acquisition_{code}",
                    label="Acquisition",
                    categories=("acquisition",),
                    signal=code,
                ),
                RoleSpec(
                    f"Tracking_{code}",
                    label="Tracking",
                    categories=("tracking",),
                    signal=code,
                ),
                RoleSpec(
                    f"TelemetryDecoder_{code}",
                    label="Telemetry Decoder",
                    categories=("telemetry_decoder",),
                    signal=code,
                ),
            ),
        )
        for code in ordered
    )
    return [
        RoleSpec(
            "GNSS-SDR",
            label="Global (GNSS-SDR)",
            prefix="GNSS-SDR.",
            exclude=_GLOBAL_GROUPS,
            children=(
                RoleSpec(
                    "GNSS-SDR-SUPL",
                    label="Assisted GNSS — SUPL",
                    prefix="GNSS-SDR.",
                    include=("SUPL_",),
                ),
                RoleSpec(
                    "GNSS-SDR-AGNSS",
                    label="Assisted GNSS — XML (AGNSS)",
                    prefix="GNSS-SDR.",
                    include=("AGNSS_",),
                ),
                RoleSpec(
                    "GNSS-SDR-OSNMA",
                    label="Galileo OSNMA",
                    prefix="GNSS-SDR.",
                    include=("osnma_",),
                ),
            ),
        ),
        RoleSpec(
            "SignalSource",
            label="Signal Source",
            categories=("signal_source", "signal_generator"),
        ),
        RoleSpec(
            "SignalConditioner",
            label="Signal Conditioner",
            categories=("conditioner", "libs"),
            children=(
                RoleSpec(
                    "DataTypeAdapter",
                    label="Data Type Adapter",
                    categories=("data_type_adapter", "libs"),
                ),
                RoleSpec(
                    "InputFilter",
                    label="Input Filter",
                    categories=("input_filter", "libs"),
                ),
                RoleSpec("Resampler", categories=("resampler", "libs")),
            ),
        ),
        RoleSpec("Channels", prefix="Channels.", children=signal_nodes),
        RoleSpec("Observables", categories=("observables",)),
        RoleSpec("PVT", categories=("PVT",)),
        RoleSpec(
            "Monitors",
            children=(
                RoleSpec("Monitor", prefix="Monitor."),
                RoleSpec(
                    "AcquisitionMonitor",
                    label="Acquisition Monitor",
                    prefix="AcquisitionMonitor.",
                ),
                RoleSpec(
                    "TrackingMonitor",
                    label="Tracking Monitor",
                    prefix="TrackingMonitor.",
                ),
            ),
        ),
    ]


def walk_specs(specs):
    for spec in specs:
        yield spec
        yield from walk_specs(spec.children)


class ConfiguratorApp(App):
    TITLE = "gnss-sdr-configurator"
    CSS = """
    #tree { width: 34%; }
    #detail { width: 1fr; padding: 1; }
    #impl-select { margin-bottom: 1; }
    """
    BINDINGS = [
        ("ctrl+o", "open", "Open .conf"),
        ("ctrl+s", "save", "Save .conf"),
        ("ctrl+q", "quit", "Quit"),
    ]

    def __init__(self, schema: Schema | None = None) -> None:
        super().__init__()
        self.schema = schema or load_schema()
        self.role_specs = build_role_specs(self.schema)
        self.config = ReceiverConfig()
        # sensible starting points for a from-scratch config; loading a
        # file replaces the whole config, so these never touch loaded files
        self.config.set("InputFilter.implementation", "Pass_Through")
        self.config.set("Resampler.implementation", "Pass_Through")
        self._current_spec: RoleSpec | None = None
        # set while a file is loaded; save then rewrites the original
        # document (D2: comments/ordering preserved, edits applied in place)
        self.loaded_doc: ConfDocument | None = None
        self.loaded_path: Path | None = None
        self._loaded_values: dict[str, str] = {}

    def compose(self):
        yield Header()
        with Horizontal():
            tree: Tree[RoleSpec] = Tree("Receiver", id="tree")
            self._add_spec_nodes(tree.root, self.role_specs)
            tree.root.expand()
            yield tree
            yield PaneSplitter("#tree")
            yield VerticalScroll(
                Label("Select a block on the left."), id="detail"
            )
        yield Footer()

    @staticmethod
    def _add_spec_nodes(parent, specs) -> None:
        for spec in specs:
            if spec.children:
                node = parent.add(
                    spec.display, data=spec, expand=not spec.collapsed
                )
                ConfiguratorApp._add_spec_nodes(node, spec.children)
            else:
                parent.add_leaf(spec.display, data=spec)

    # -- detail panel -----------------------------------------------------

    async def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        spec = event.node.data
        if spec is not None:
            self._current_spec = spec
            await self._rebuild_detail(spec)

    async def _rebuild_detail(self, spec: RoleSpec) -> None:
        detail = self.query_one("#detail", VerticalScroll)
        await detail.remove_children()
        if spec.passthrough:
            widgets = self._passthrough_widgets()
        elif spec.categories:
            widgets = self._implementation_role_widgets(spec)
        elif spec.prefix:
            widgets = self._concrete_group_widgets(spec)
        elif spec.signal:
            widgets = self._signal_group_widgets(spec)
        else:  # pure container node
            widgets = [
                Label(f"[b]{spec.display}[/b]"),
                Label("Select a sub-section on the left."),
            ]
        await detail.mount(*widgets)

    def _implementation_role_widgets(self, spec: RoleSpec) -> list:
        impls = self.schema.implementations_for_category(
            *spec.categories, signal=spec.signal or None
        )
        impl_key = f"{spec.role}.implementation"
        current = self.config.get(impl_key)
        known = {i.name for i in impls}
        widgets = [
            Label(f"[b]{spec.role}[/b]"),
            Select(
                [(i.name, i.name) for i in impls],
                prompt="(implementation not set)",
                value=current if current in known else Select.NULL,
                id="impl-select",
            ),
        ]
        if current and current not in known:
            widgets.append(
                Label(f"[red]unknown implementation: {current}[/red]")
            )
        if current in known:
            for param in self.schema.params_for_implementation(current):
                if not param.is_role_scoped or param.deprecated:
                    continue
                key = f"{spec.role}.{param.name}"
                widgets.append(self._param_row(key, param))
        return widgets

    def _passthrough_widgets(self) -> list:
        widgets = [
            Label("[b]Unrecognized options[/b] (kept verbatim on save)")
        ]
        widgets += [Label(f"{k}={v}") for k, v in self._passthrough_items()]
        return widgets

    def _recognized_keys(self) -> set[str]:
        keys: set[str] = set()
        for spec in walk_specs(self.role_specs):
            if spec.is_container:
                continue
            if spec.signal and not spec.categories:
                keys |= {key for key, _ in self._signal_group_params(spec)}
            elif spec.categories:
                impl_key = f"{spec.role}.implementation"
                keys.add(impl_key)
                impl = self.config.get(impl_key)
                try:
                    params = (
                        self.schema.params_for_implementation(impl)
                        if impl
                        else []
                    )
                except KeyError:
                    params = []
                keys |= {
                    f"{spec.role}.{p.name}"
                    for p in params
                    if p.is_role_scoped and not p.deprecated
                }
            else:
                for param in self._concrete_group_params(spec):
                    keys.add(param.key)
        return keys

    def _passthrough_items(self) -> list[tuple[str, str]]:
        recognized = self._recognized_keys()
        return [
            (k, v) for k, v in self.config.items() if k not in recognized
        ]

    def _concrete_group_params(self, spec: RoleSpec) -> list[Parameter]:
        params = []
        for param in self.schema.concrete_entries(spec.prefix):
            if param.deprecated:
                continue
            tail = param.key.removeprefix(spec.prefix)
            if spec.include and not tail.startswith(spec.include):
                continue
            if spec.exclude and tail.startswith(spec.exclude):
                continue
            params.append(param)
        return params

    def _concrete_group_widgets(self, spec: RoleSpec) -> list:
        widgets = [Label(f"[b]{spec.display}[/b]")]
        for param in self._concrete_group_params(spec):
            widgets.append(self._param_row(param.key, param))
        return widgets

    def _signal_group_params(self, spec: RoleSpec) -> list[tuple[str, Parameter]]:
        """The per-signal channel parameters (Channels_<code>.count)."""
        return [
            (param.key.replace("<signal_str>", spec.signal), param)
            for param in self.schema.entries
            if param.key == "Channels_<signal_str>.count"
        ]

    def _signal_group_widgets(self, spec: RoleSpec) -> list:
        widgets = [Label(f"[b]{spec.display}[/b]")]
        for key, param in self._signal_group_params(spec):
            widgets.append(self._param_row(key, param))
        return widgets

    def _param_row(self, key: str, param: Parameter) -> ParamRow:
        default = param.default_str
        if param.type == "unknown":
            tooltip_default = f"default (C++): {param.default_expr}"
        else:
            tooltip_default = f"default: {default}"
        browsable = self._is_path_param(param)
        if param.type == "bool":
            options: tuple[str, ...] = ("true", "false")
        elif not browsable:
            options = param.options
        else:
            options = ()  # a path needs free text + Browse, not a dropdown
        return ParamRow(
            key=key,
            param_type=param.type,
            default=default,
            value=self.config.get(key),
            tooltip=f"{tooltip_default}\n{param.file}:{param.line}",
            browsable=browsable,
            options=options,
        )

    @staticmethod
    def _is_path_param(param: Parameter) -> bool:
        name = param.name.rsplit(".", 1)[-1]
        return param.type == "string" and (
            "file" in name or "path" in name or name.endswith("_xml")
        )

    # -- events from widgets ----------------------------------------------

    async def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id != "impl-select":
            return  # ParamRow Selects handle their own events
        spec = self._current_spec
        if spec is None or not spec.categories:
            return
        impl_key = f"{spec.role}.implementation"
        new = None if event.value is Select.NULL else str(event.value)
        if new == (self.config.get(impl_key) or None):
            return  # no-op (e.g. the rebuilt Select announcing its value)
        if new is None:
            self.config.unset(impl_key)
        else:
            self.config.set(impl_key, new)
        await self._rebuild_detail(spec)

    def on_param_row_changed(self, event: ParamRow.Changed) -> None:
        self.config.set_param(event.key, event.value, event.default)

    def on_param_row_browse(self, event: ParamRow.Browse) -> None:
        key = event.key
        self.push_screen(
            FileOpen(title=f"Choose {key}", must_exist=False),
            callback=lambda path: self._on_browse_chosen(key, path),
        )

    def _on_browse_chosen(self, key: str, path: Path | None) -> None:
        if path is None:
            return
        for row in self.query(ParamRow):
            if row.key == key:
                row.set_value(str(path))  # fires Changed -> config update
                return

    # -- opening and saving -------------------------------------------------

    def action_open(self) -> None:
        self.push_screen(
            FileOpen(
                filters=Filters(
                    ("conf", lambda p: p.suffix.lower() == ".conf"),
                    ("All", lambda p: True),
                )
            ),
            callback=self._on_open_chosen,
        )

    async def _on_open_chosen(self, path: Path | None) -> None:
        if path is None:
            self.notify("Open cancelled")
            return
        doc = load_conf(path)
        self.loaded_doc = doc
        self.loaded_path = path
        self._loaded_values = {k: v for k, v in doc.items()}
        self.config = ReceiverConfig()
        for key, value in doc.items():
            self.config.set(key, value)
        await self._refresh_passthrough_node()
        if self._current_spec is not None:
            await self._rebuild_detail(self._current_spec)
        unrecognized = len(self._passthrough_items())
        self.notify(
            f"Loaded {len(self._loaded_values)} keys from {path.name}"
            + (f" ({unrecognized} unrecognized)" if unrecognized else "")
        )

    async def _refresh_passthrough_node(self) -> None:
        tree = self.query_one("#tree", Tree)
        for node in list(tree.root.children):
            if node.data is PASSTHROUGH_SPEC:
                node.remove()
        if self._passthrough_items():
            tree.root.add_leaf(PASSTHROUGH_SPEC.role, data=PASSTHROUGH_SPEC)

    def action_save(self) -> None:
        default = self.loaded_path.name if self.loaded_path else "my_receiver.conf"
        self.push_screen(
            FileSave(default_file=default),
            callback=self._on_save_chosen,
        )

    def _on_save_chosen(self, path: Path | None) -> None:
        if path is None:
            self.notify("Save cancelled")
            return
        path.write_text(self._render_conf(), encoding="utf-8")
        self.notify(f"Wrote {len(self.config)} keys to {path}")

    def _render_conf(self) -> str:
        """New configs render in canonical section order; loaded configs
        rewrite the original document, applying only the edits (D2)."""
        if self.loaded_doc is None:
            return write_conf(self.config, self.schema.generated_from)
        current = dict(self.config.items())
        for key, value in current.items():
            if self._loaded_values.get(key) != value:
                self.loaded_doc.set(key, value)
        for key in self._loaded_values:
            if key not in current:
                self.loaded_doc.remove(key)
        return self.loaded_doc.render()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="gnss-sdr-configurator",
        description="TUI for creating and editing GNSS-SDR .conf files",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.parse_args()
    try:
        app = ConfiguratorApp()
    except SchemaError as exc:
        sys.exit(f"error: {exc}")
    app.run()
