"""
ui_helpers.py — small UI building blocks shared by the main window and the
optional panels.

Each one exists because a Qt default differs between the Windows, Linux and
macOS styles and the difference is visible in this GUI.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QFormLayout, QLabel, QToolButton, QWidget


def form_layout(parent: QWidget | None = None) -> QFormLayout:
    """A QFormLayout that lays out the same under every style.

    Qt takes these three settings from the platform style. On macOS the
    defaults are right-aligned labels, a centred form and fields that stay at
    their size hint, so combo boxes and full-row buttons stop short of the
    panel edge instead of filling it."""
    form = QFormLayout(parent)
    form.setLabelAlignment(Qt.AlignLeft)
    form.setFormAlignment(Qt.AlignLeft | Qt.AlignTop)
    form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
    return form


def compact_button(text: str, tooltip: str = "") -> QToolButton:
    """A button exactly as wide as its label, for use beside an input.

    QPushButton cannot do this portably: the Windows styles enforce a ~75 px
    minimum, and the macOS style puts wide rounded caps around the text, so a
    pixel width that looks right on Windows clips the label on a Mac.
    QToolButton sizes to its contents under every style."""
    btn = QToolButton()
    btn.setText(text)
    btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
    if tooltip:
        btn.setToolTip(tooltip)
    return btn


class StatusLabel(QLabel):
    """One-line connection status whose colour follows the widget theme.

    The app does not impose a theme, so the window is dark on a dark desktop
    and light on a light one. A single hard-coded colour is readable on only
    one of them — the pale lavender that suits a dark window is close to
    invisible on a light one."""

    # kind: (colour on a dark theme, colour on a light theme)
    # "" means the palette's own text colour.
    _COLOURS = {
        "idle":  ("#cdd6f4", ""),
        "ok":    ("#a6e3a1", "#2e7d32"),
        "error": ("#f38ba8", "#c62828"),
    }

    def __init__(self, text: str = "", parent: QWidget | None = None):
        super().__init__(text, parent)
        self._kind = "idle"
        self._apply()

    def set_status(self, text: str, kind: str = "idle") -> None:
        """Show `text` as "idle", "ok" or "error"."""
        self._kind = kind if kind in self._COLOURS else "idle"
        self.setText(text)
        self._apply()

    def changeEvent(self, event) -> None:
        # The desktop can switch between light and dark while the app runs.
        if event.type() in (QEvent.PaletteChange,
                            QEvent.ApplicationPaletteChange):
            self._apply()
        super().changeEvent(event)

    def _apply(self) -> None:
        dark = self.palette().color(QPalette.Window).lightness() < 128
        colour = self._COLOURS[self._kind][0 if dark else 1]
        sheet = f"color: {colour};" if colour else ""
        # Re-applying an unchanged sheet would re-polish the widget and send
        # another PaletteChange straight back here.
        if sheet != self.styleSheet():
            self.setStyleSheet(sheet)
