"""PaneSplitter: a draggable vertical divider between two panes.

Dumb widget: dragging it resizes the widget selected by `target` (the pane
to its left); the pane to the right must use a flexible width (1fr).
"""

from __future__ import annotations

from textual import events
from textual.widget import Widget


class PaneSplitter(Widget):
    DEFAULT_CSS = """
    PaneSplitter {
        width: 1;
        height: 1fr;
        background: $panel-lighten-1;
        color: $text-muted;
        content-align: center middle;
    }
    PaneSplitter:hover { background: $accent; color: $text; }
    """

    def render(self) -> str:
        return "⋮"

    def __init__(
        self,
        target: str,
        min_width: int = 16,
        min_remainder: int = 30,
    ) -> None:
        super().__init__()
        self.target = target
        self.min_width = min_width
        self.min_remainder = min_remainder
        self._dragging = False

    def on_mouse_down(self, event: events.MouseDown) -> None:
        event.stop()
        self._dragging = True
        self.capture_mouse()

    def on_mouse_up(self, event: events.MouseUp) -> None:
        event.stop()
        self._dragging = False
        self.release_mouse()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if self._dragging:
            self.resize_to(event.screen_x)

    def resize_to(self, screen_x: int) -> None:
        """Resize the target pane so its right edge lands at `screen_x`,
        clamped so both panes keep a usable width."""
        pane = self.screen.query_one(self.target)
        left_edge = pane.region.x
        max_width = self.screen.size.width - left_edge - self.min_remainder
        width = max(self.min_width, min(screen_x - left_edge, max_width))
        pane.styles.width = width
