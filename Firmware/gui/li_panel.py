"""
li_panel.py — Left-panel widget for the Li-Control feature.

Hard import rule: this module must NOT import paramiko, zeroconf, li_worker, or
li_discovery at module top. It only imports li_control (Qt-free dataclasses).
LiWorker and LiDiscovery are constructed by MainWindow and wired via signals.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from li_control import LiConfig, LiSetpoints


class LiControlPanel(QGroupBox):
    scan_requested       = Signal()
    connect_requested    = Signal(object)   # LiConfig
    disconnect_requested = Signal()
    setpoints_requested  = Signal(object)   # LiSetpoints
    stop_requested       = Signal()
    sequence_load_requested = Signal(str)   # path
    sequence_edit_requested = Signal()      # open the form-based editor
    sequence_start       = Signal()
    sequence_abort       = Signal()

    def __init__(self, parent=None):
        super().__init__("Li-Control", parent)
        self._steps = []
        self._connected = False
        self._sequence_running = False
        self._build_ui()
        self._refresh_enabled()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        # Sub-groups are checkable so the user can collapse any section they
        # don't need — important on small screens where 4 stacked group
        # boxes + the existing Connection/Settings groups exceed the window
        # height. Unchecking hides every direct child widget of the group.
        self._ssh_group = self._build_ssh_group()
        self._setpoints_group = self._build_setpoints_group()
        self._readback_group = self._build_readback_group()
        self._sequence_group = self._build_sequence_group()

        for grp, start_checked in (
            (self._ssh_group, True),
            (self._setpoints_group, False),    # collapsed by default
            (self._readback_group, True),
            (self._sequence_group, True),
        ):
            grp.setCheckable(True)
            grp.setChecked(start_checked)
            grp.toggled.connect(
                lambda checked, g=grp: self._set_group_collapsed(g, not checked)
            )
            if not start_checked:
                self._set_group_collapsed(grp, True)
            layout.addWidget(grp)

    @staticmethod
    def _set_group_collapsed(grp: QGroupBox, collapsed: bool) -> None:
        """Show/hide every direct child widget of the group."""
        inner = grp.layout()
        if inner is None:
            return
        for i in range(inner.count()):
            item = inner.itemAt(i)
            w = item.widget() if item is not None else None
            if w is not None:
                w.setVisible(not collapsed)
        # Shrink the groupbox so other groups can reclaim the vertical space.
        grp.setMaximumHeight(16777215 if not collapsed else grp.sizeHint().height())

    def _build_ssh_group(self) -> QGroupBox:
        grp = QGroupBox("SSH")
        form = QFormLayout(grp)
        form.setLabelAlignment(Qt.AlignLeft)

        self._host_combo = QComboBox()
        self._host_combo.setEditable(True)
        self._host_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._host_combo.addItem("licor.local")

        self._scan_btn = QPushButton("Scan")
        self._scan_btn.clicked.connect(self.scan_requested.emit)

        host_row = QWidget()
        host_h = QHBoxLayout(host_row)
        host_h.setContentsMargins(0, 0, 0, 0)
        host_h.addWidget(self._host_combo, stretch=1)
        host_h.addWidget(self._scan_btn)

        self._user_edit = QLineEdit("licor")

        self._key_edit = QLineEdit()
        self._key_edit.setPlaceholderText("(optional)")
        self._key_browse_btn = QPushButton("…")
        self._key_browse_btn.setFixedWidth(28)
        self._key_browse_btn.clicked.connect(self._on_browse_key)
        key_row = QWidget()
        key_h = QHBoxLayout(key_row)
        key_h.setContentsMargins(0, 0, 0, 0)
        key_h.addWidget(self._key_edit, stretch=1)
        key_h.addWidget(self._key_browse_btn)

        self._policy_combo = QComboBox()
        self._policy_combo.addItems(["auto_add", "reject"])

        self._connect_btn = QPushButton("Connect")
        self._connect_btn.clicked.connect(self._on_connect_clicked)

        self._status_lbl = QLabel("Not connected")
        self._status_lbl.setStyleSheet("color: #cdd6f4;")

        form.addRow("Host:", host_row)
        form.addRow("User:", self._user_edit)
        form.addRow("Key:", key_row)
        form.addRow("Host key:", self._policy_combo)
        form.addRow(self._connect_btn)
        form.addRow(self._status_lbl)
        return grp

    def _build_setpoints_group(self) -> QGroupBox:
        grp = QGroupBox("Manual Setpoints")
        form = QFormLayout(grp)
        form.setLabelAlignment(Qt.AlignLeft)

        def spin(val: float, mn: float, mx: float, dec: int, suffix: str = "") -> QDoubleSpinBox:
            w = QDoubleSpinBox()
            w.setRange(mn, mx)
            w.setDecimals(dec)
            w.setValue(val)
            if suffix:
                w.setSuffix(" " + suffix)
            return w

        self._co2_spin = spin(400, 0, 2000, 0, "ppm")
        self._tair_spin = spin(25, 0, 50, 1, "°C")
        self._rh_spin = spin(60, 0, 100, 1, "%")
        self._qin_spin = spin(0, 0, 3000, 0, "μmol/m²/s")
        self._flow_spin = spin(500, 0, 2000, 0, "")
        self._fan_spin = spin(10000, 0, 20000, 0, "rpm")
        self._pressure_spin = spin(0, 0, 10, 2, "kPa")

        self._wait_co2_chk = QCheckBox("wait_for_co2")
        self._co2_tol_spin = spin(2, 0, 100, 1, "ppm")
        self._wait_s_spin = spin(0, 0, 3600, 0, "s")
        self._log_chk = QCheckBox("LI-6800 LOG()")

        form.addRow("CO₂_r:", self._co2_spin)
        form.addRow("Tair:", self._tair_spin)
        form.addRow("RH_air:", self._rh_spin)
        form.addRow("Qin:", self._qin_spin)
        form.addRow("Flow:", self._flow_spin)
        form.addRow("Fan:", self._fan_spin)
        form.addRow("Pressure:", self._pressure_spin)
        form.addRow(self._wait_co2_chk)
        form.addRow("CO₂ tol:", self._co2_tol_spin)
        form.addRow("Wait_s:", self._wait_s_spin)
        form.addRow(self._log_chk)

        btn_row = QWidget()
        btn_h = QHBoxLayout(btn_row)
        btn_h.setContentsMargins(0, 0, 0, 0)
        self._send_btn = QPushButton("Send")
        self._send_btn.clicked.connect(self._on_send_clicked)
        self._stop_send_btn = QPushButton("Stop")
        self._stop_send_btn.clicked.connect(self.stop_requested.emit)
        btn_h.addWidget(self._send_btn)
        btn_h.addWidget(self._stop_send_btn)
        form.addRow(btn_row)
        return grp

    def _build_readback_group(self) -> QGroupBox:
        grp = QGroupBox("Readback")
        form = QFormLayout(grp)
        form.setLabelAlignment(Qt.AlignLeft)
        self._rb_labels: dict[str, QLabel] = {}
        for key in ("CO2_r", "CO2_s", "H2O_r", "H2O_s",
                    "Tchamber", "Tleaf", "RHcham", "PPFD_in"):
            lbl = QLabel("—")
            self._rb_labels[key] = lbl
            form.addRow(key + ":", lbl)
        return grp

    def _build_sequence_group(self) -> QGroupBox:
        grp = QGroupBox("Sequence")
        layout = QVBoxLayout(grp)
        layout.setContentsMargins(6, 6, 6, 6)

        io_row = QWidget()
        io_h = QHBoxLayout(io_row)
        io_h.setContentsMargins(0, 0, 0, 0)
        self._load_btn = QPushButton("Load…")
        self._load_btn.clicked.connect(self._on_load_clicked)
        self._build_btn = QPushButton("Build…")
        self._build_btn.clicked.connect(self.sequence_edit_requested.emit)
        io_h.addWidget(self._load_btn)
        io_h.addWidget(self._build_btn)
        layout.addWidget(io_row)

        self._steps_list = QListWidget()
        self._steps_list.setMaximumHeight(120)
        layout.addWidget(self._steps_list)

        self._progress_lbl = QLabel("step 0 / 0")
        layout.addWidget(self._progress_lbl)

        btn_row = QWidget()
        btn_h = QHBoxLayout(btn_row)
        btn_h.setContentsMargins(0, 0, 0, 0)
        self._run_btn = QPushButton("Run")
        self._run_btn.clicked.connect(self._on_run_clicked)
        self._abort_btn = QPushButton("Abort")
        self._abort_btn.clicked.connect(self._on_abort_clicked)
        self._abort_btn.setEnabled(False)
        btn_h.addWidget(self._run_btn)
        btn_h.addWidget(self._abort_btn)
        layout.addWidget(btn_row)
        return grp

    # ------------------------------------------------------------------
    # Internal click handlers
    # ------------------------------------------------------------------

    def _on_browse_key(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Select SSH private key")
        if path:
            self._key_edit.setText(path)

    def _on_connect_clicked(self) -> None:
        if self._connected:
            self.disconnect_requested.emit()
            return
        host = self._host_combo.currentText().strip()
        if not host:
            return
        cfg = LiConfig(
            host=host,
            username=self._user_edit.text().strip() or "licor",
            key_filename=(self._key_edit.text().strip() or None),
            host_key_policy=self._policy_combo.currentText(),
        )
        self.connect_requested.emit(cfg)

    def _on_send_clicked(self) -> None:
        sp = LiSetpoints(
            co2_r=self._co2_spin.value(),
            tair=self._tair_spin.value(),
            rh_air=self._rh_spin.value(),
            qin=self._qin_spin.value(),
            flow=self._flow_spin.value(),
            fan_rpm=self._fan_spin.value(),
            pressure=self._pressure_spin.value(),
            wait_for_co2=self._wait_co2_chk.isChecked(),
            co2_tol=self._co2_tol_spin.value(),
            wait_s=self._wait_s_spin.value(),
            log=self._log_chk.isChecked(),
        )
        self.setpoints_requested.emit(sp)

    def _on_load_clicked(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Load sequence", "", "JSON files (*.json)"
        )
        if path:
            self.sequence_load_requested.emit(path)

    def _on_run_clicked(self) -> None:
        self.sequence_start.emit()

    def _on_abort_clicked(self) -> None:
        self.on_sequence_aborting()
        self.sequence_abort.emit()

    # ------------------------------------------------------------------
    # Public slots (called by MainWindow)
    # ------------------------------------------------------------------

    def add_discovered_host(self, name: str, ip: str) -> None:
        label = name
        if ip and ip != name:
            label = f"{name}  [{ip}]"
        for i in range(self._host_combo.count()):
            if self._host_combo.itemText(i) == label:
                return
        self._host_combo.addItem(label)

    def set_steps(self, steps: list) -> None:
        self._steps = list(steps)
        self._steps_list.clear()
        for i, step in enumerate(steps):
            name = getattr(step, "name", f"step {i}")
            self._steps_list.addItem(f"{i+1}. {name}")
        self._progress_lbl.setText(f"step 0 / {len(steps)}")
        self._refresh_enabled()

    def set_progress(self, step_idx: int, rep_idx: int = 0, total_reps: int = 1) -> None:
        base = f"step {step_idx + 1} / {len(self._steps)}"
        if total_reps > 1:
            base += f"  (rep {rep_idx + 1}/{total_reps})"
        self._progress_lbl.setText(base)

    def on_connected(self, host: str) -> None:
        self._connected = True
        self._connect_btn.setText("Disconnect")
        self._status_lbl.setText(f"Connected: {host}")
        self._status_lbl.setStyleSheet("color: #a6e3a1;")
        self._refresh_enabled()

    def on_disconnected(self) -> None:
        self._connected = False
        self._connect_btn.setText("Connect")
        self._status_lbl.setText("Not connected")
        self._status_lbl.setStyleSheet("color: #cdd6f4;")
        self._refresh_enabled()

    def on_sequence_started(self) -> None:
        self._sequence_running = True
        self._abort_btn.setText("Abort")
        self._abort_btn.setEnabled(True)
        self._refresh_enabled()

    def on_sequence_aborting(self) -> None:
        self._abort_btn.setText("Aborting — waiting…")
        self._abort_btn.setEnabled(False)

    def on_sequence_ended(self) -> None:
        self._sequence_running = False
        self._abort_btn.setText("Abort")
        self._abort_btn.setEnabled(False)
        self._progress_lbl.setText(f"step 0 / {len(self._steps)}")
        self._refresh_enabled()

    def on_ack_received(self, ack: dict) -> None:
        for key, lbl in self._rb_labels.items():
            val = ack.get(key)
            lbl.setText("—" if val is None else f"{val:.3g}")

    # ------------------------------------------------------------------
    # Enable-state logic
    # ------------------------------------------------------------------

    def _refresh_enabled(self) -> None:
        self._send_btn.setEnabled(self._connected and not self._sequence_running)
        self._stop_send_btn.setEnabled(self._connected)
        self._run_btn.setEnabled(
            self._connected and not self._sequence_running and len(self._steps) > 0
        )
        self._load_btn.setEnabled(not self._sequence_running)
        self._build_btn.setEnabled(not self._sequence_running)
