"""
pyro_panel.py — Left-panel widget for the Pyroscience optional feature.

Hard import rule: this module must NOT import serial or pyro_worker at top
level. The worker is owned by MainWindow; the panel only emits signals.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

import device_manager


class PyroPanel(QGroupBox):
    connect_requested    = Signal(str, int, float)  # port, channel, interval_s
    disconnect_requested = Signal()

    def __init__(self, parent=None):
        super().__init__("Pyroscience", parent)
        self._connected = False
        self._streaming = False
        self._build_ui()
        self._refresh_ports()
        self._refresh_enabled()

    # The global Start/Stop button needs the configured sample interval.
    def current_interval_s(self) -> float:
        return float(self._interval_spin.value())

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        conn_grp = QGroupBox("Connection")
        form = QFormLayout(conn_grp)
        form.setLabelAlignment(Qt.AlignLeft)

        self._port_combo = QComboBox()
        self._port_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._refresh_btn = QPushButton("⟳")
        self._refresh_btn.setFixedWidth(28)
        self._refresh_btn.setToolTip("Refresh port list")
        self._refresh_btn.clicked.connect(self._refresh_ports)

        port_row = QWidget()
        port_h = QHBoxLayout(port_row)
        port_h.setContentsMargins(0, 0, 0, 0)
        port_h.addWidget(self._port_combo, stretch=1)
        port_h.addWidget(self._refresh_btn)

        self._channel_spin = QSpinBox()
        self._channel_spin.setRange(1, 4)
        self._channel_spin.setValue(1)

        self._interval_spin = QDoubleSpinBox()
        self._interval_spin.setRange(0.2, 60.0)
        self._interval_spin.setSingleStep(0.5)
        self._interval_spin.setDecimals(1)
        self._interval_spin.setValue(1.0)
        self._interval_spin.setSuffix(" s")

        self._connect_btn = QPushButton("Connect")
        self._connect_btn.clicked.connect(self._on_connect_clicked)

        self._status_lbl = QLabel("Not connected")
        self._status_lbl.setWordWrap(True)
        self._status_lbl.setStyleSheet("color: #cdd6f4;")

        form.addRow("Port:", port_row)
        form.addRow("Channel:", self._channel_spin)
        form.addRow("Interval:", self._interval_spin)
        form.addRow(self._connect_btn)
        form.addRow(self._status_lbl)

        layout.addWidget(conn_grp)

    def _refresh_ports(self) -> None:
        current = self._port_combo.currentText()
        self._port_combo.clear()
        ports = device_manager.list_ports()
        for p in ports:
            self._port_combo.addItem(p)
        if not ports:
            self._port_combo.addItem("(no ports found)")
        idx = self._port_combo.findText(current)
        if idx >= 0:
            self._port_combo.setCurrentIndex(idx)

    def _on_connect_clicked(self) -> None:
        if self._connected:
            self.disconnect_requested.emit()
            return
        port = self._port_combo.currentText().strip()
        if not port or port.startswith("("):
            return
        self.connect_requested.emit(
            port, self._channel_spin.value(), float(self._interval_spin.value())
        )

    # Public slots called by MainWindow
    def on_connected(self, info: dict) -> None:
        self._connected = True
        self._connect_btn.setText("Disconnect")
        idnr = info.get("idnr", "")
        port = info.get("port", "")
        if idnr:
            self._status_lbl.setText(f"Connected: {port}\nIDNR: {idnr}")
        else:
            self._status_lbl.setText(f"Connected: {port}")
        self._status_lbl.setStyleSheet("color: #a6e3a1;")
        self._refresh_enabled()

    def on_disconnected(self) -> None:
        self._connected = False
        self._streaming = False
        self._connect_btn.setText("Connect")
        self._status_lbl.setText("Not connected")
        self._status_lbl.setStyleSheet("color: #cdd6f4;")
        self._refresh_enabled()

    def on_streaming_started(self) -> None:
        self._streaming = True
        self._refresh_enabled()

    def on_streaming_stopped(self) -> None:
        self._streaming = False
        self._refresh_enabled()

    def on_error(self, msg: str) -> None:
        self._status_lbl.setText(msg)
        self._status_lbl.setStyleSheet("color: #f38ba8;")

    def _refresh_enabled(self) -> None:
        for w in (self._port_combo, self._channel_spin, self._refresh_btn):
            w.setEnabled(not self._connected)
        # Lock interval while actively streaming so the displayed cadence
        # matches the worker's loop.
        self._interval_spin.setEnabled(not self._streaming)
