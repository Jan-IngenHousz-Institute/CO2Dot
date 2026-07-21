"""
main_window.py — QMainWindow for the CO2Dot controller GUI.
"""

import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pyqtgraph as pg

from PySide6.QtCore import Qt, QStandardPaths, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

import device_manager
import protocol
from data_buffer import BmeBuffer, SpecBuffer
from recorder import Recorder
from serial_worker import SerialWorker

# ---------------------------------------------------------------------------
# Colour palettes for plots
# ---------------------------------------------------------------------------

SPEC_COLORS = [
    "#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4",
    "#42d4f4", "#f032e6", "#bfef45", "#fabed4", "#469990",
    "#dcbeff", "#9A6324", "#fffac8", "#800000", "#aaffc3",
    "#808000", "#ffd8b1", "#000075",
]

BME_COLORS = {
    "T":   "#e6194b",   # red
    "P":   "#4363d8",   # blue
    "RH":  "#3cb44b",   # green
    "Gas": "#f58231",   # orange
}

BME_UNITS = {"T": "°C", "P": "hPa", "RH": "%RH", "Gas": "Ω"}

# Distinct from SPEC_COLORS so the two plots don't collide visually
PYRO_COLORS = [
    "#ff6b6b", "#feca57", "#48dbfb", "#1dd1a1", "#ee5a6f", "#c44569",
    "#5a4f9e", "#f8b500", "#576574", "#10ac84", "#ff9ff3", "#341f97",
    "#01a3a4", "#ee5a24", "#7bed9f", "#ff7f50",
]

# Serial-Scripting parameter step curves (distinct from the other palettes)
EXT_SERIAL_COLORS = [
    "#f9e2af", "#89b4fa", "#f38ba8", "#a6e3a1", "#cba6f7", "#fab387",
    "#94e2d5", "#eba0ac",
]

# Console lines shaped like a script-helper call run through the script
# engine instead of going out raw over serial (dc_offset lives on the PC,
# not on the ESP32). Deliberately narrow so CLI text like "help" stays raw.
EXT_HELPER_RE = re.compile(
    r"^\s*(?:send|wait|param|pwm|dc_offset|print)\s*\(.*\)\s*$")

# Interval dropdown: label → seconds
INTERVALS = [
    ("1 s",    1),
    ("2 s",    2),
    ("5 s",    5),
    ("10 s",  10),
    ("30 s",  30),
    ("1 min",  60),
    ("5 min",  300),
    ("10 min", 600),
    ("30 min", 1800),
    ("1 hour", 3600),
]


class TimeAxisItem(pg.AxisItem):
    """Custom axis that can display elapsed seconds or HH:MM:SS timestamps."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._use_timestamp = False
        self._t0 = 0.0

    def set_timestamp_mode(self, enabled: bool):
        self._use_timestamp = enabled
        self.picture = None
        self.update()

    def set_t0(self, t0: float):
        self._t0 = t0

    def tickStrings(self, values, scale, spacing):
        if self._use_timestamp and self._t0 > 0:
            strings = []
            for v in values:
                try:
                    dt = datetime.fromtimestamp(v + self._t0)
                    strings.append(dt.strftime("%H:%M:%S"))
                except (ValueError, OSError):
                    strings.append("")
            return strings
        return super().tickStrings(values, scale, spacing)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("CO2Dot / MiniPAR Controller")
        self.resize(1280, 800)

        # State
        self._worker: SerialWorker | None = None
        self._spec_buffer = SpecBuffer()
        self._bme_buffer = BmeBuffer()
        # When frozen by PyInstaller, save next to the .exe; otherwise next to this .py
        if getattr(sys, "frozen", False):
            base_dir = Path(sys.executable).parent
        else:
            base_dir = Path(__file__).parent
        self._recorder = Recorder(base_dir / "data")
        self._model = "AS7341"        # updated on status
        self._running = False         # acquisition running
        self._acq_timer = QTimer(self)
        self._acq_timer.timeout.connect(self._on_acquire_tick)
        self._last_spec: dict | None = None
        self._last_bme: dict | None = None
        self._acq_pending = False
        self._gain = 5
        self._atime = 100
        self._astep = 999
        self._led = 10
        self._auto_range = True
        self._show_timestamp = False
        self._device_type = ""        # "CO2Dot" or "MiniPAR"
        self._has_bme = False         # True only for devices with BME sensor

        # Li-Control state (all lazily created)
        self._base_dir = base_dir
        self._li_worker = None
        self._li_panel = None
        self._li_runner = None
        self._li_discovery = None
        self._li_recorder = None
        self._last_manual_cmd_id: str | None = None
        self._acq_was_running = False
        self._gui_cfg_path = self._resolve_config_path()
        self._gui_cfg = self._load_gui_config()
        self._li_enabled = bool(self._gui_cfg.get("li_control_enabled", False))

        # Pyroscience state (all lazily created)
        self._pyro_enabled = bool(self._gui_cfg.get("pyroscience_enabled", False))
        self._pyro_panel = None
        self._pyro_worker = None
        self._pyro_plot = None
        self._pyro_time_axis = None
        self._pyro_legend = None
        self._pyro_curves: dict = {}
        self._pyro_buffer = None
        self._pyro_recorder = None
        self._pyro_idnr = ""
        self._pyro_channel = 1

        # Serial Scripting state (all lazily created)
        self._ext_serial_enabled = bool(
            self._gui_cfg.get("ext_serial_enabled", False))
        self._ext_serial_panel = None
        self._ext_serial_worker = None
        self._ext_serial_runner = None
        self._ext_serial_plot = None
        self._ext_serial_time_axis = None
        self._ext_serial_legend = None
        self._ext_serial_curves: dict[str, pg.PlotDataItem] = {}
        # Params differ wildly in scale (led_V 0-0.3 V vs pwm 0-100), so
        # each gets its own ViewBox + y-axis, mirroring the BME plot.
        self._ext_serial_vbs: dict[str, pg.ViewBox] = {}
        self._ext_serial_axes: dict[str, pg.AxisItem] = {}
        self._ext_serial_buffer = None
        self._ext_serial_recorder = None
        # Main-thread-owned param cache: the ONLY writer is the queued
        # param_set slot, so no locking is needed anywhere.
        self._last_ext_params: dict[str, float] = {}
        self._ext_extra_cols: list[str] = []
        self._ext_missing_warned: set[str] = set()
        self._ext_wrong_port_hinted = False
        self._ext_console_runner = None   # one-shot runner for console helpers
        # Param events alone can't extend step curves to "now" — a slow
        # timer keeps the held levels visually current while enabled.
        self._ext_plot_timer = QTimer(self)
        self._ext_plot_timer.setInterval(1000)
        self._ext_plot_timer.timeout.connect(self._update_ext_serial_plot)

        # Shared port autodetect (Pyroscience + ambyte); one sweep fills
        # whichever panels exist.
        self._port_probe_thread = None

        # Pyqtgraph global style
        pg.setConfigOption("background", "#1e1e2e")
        pg.setConfigOption("foreground", "#cdd6f4")

        self._build_ui()
        self._refresh_ports()

    # ------------------------------------------------------------------
    # UI Construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        # Left panel wrapped in a scroll area so it gracefully handles small
        # windows and the extra Li-Control groups when that feature is enabled.
        left_panel = self._build_left_panel()
        left_scroll = QScrollArea()
        left_scroll.setWidget(left_panel)
        left_scroll.setWidgetResizable(True)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        left_scroll.setFrameShape(QScrollArea.NoFrame)
        left_scroll.setMinimumWidth(250)
        left_scroll.setMaximumWidth(700)

        # Right panel (plots + controls)
        right_panel = self._build_right_panel()

        # Horizontal splitter so the left column can be dragged wider
        # (needed for the Serial Scripting editor/console).
        h_split = QSplitter(Qt.Horizontal)
        h_split.addWidget(left_scroll)
        h_split.addWidget(right_panel)
        h_split.setStretchFactor(0, 0)
        h_split.setStretchFactor(1, 1)
        h_split.setCollapsible(0, False)
        h_split.setCollapsible(1, False)
        h_split.setSizes([320, 960])
        root.addWidget(h_split, stretch=1)

        # Status bar
        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._status_bar.showMessage("Not connected")

        # Menu bar + optional Li-Control init
        self._build_menu()
        if self._li_enabled:
            try:
                self._init_li_control()
            except Exception as exc:
                self._li_enabled = False
                self._gui_cfg["li_control_enabled"] = False
                self._save_gui_config()
                self._li_toggle_action.setChecked(False)
                self._status_bar.showMessage(f"Li-Control disabled: {exc}")
        if self._pyro_enabled:
            try:
                self._init_pyroscience()
            except Exception as exc:
                self._pyro_enabled = False
                self._gui_cfg["pyroscience_enabled"] = False
                self._save_gui_config()
                self._pyro_toggle_action.setChecked(False)
                self._status_bar.showMessage(f"Pyroscience disabled: {exc}")
        if self._ext_serial_enabled:
            try:
                self._init_ext_serial()
            except Exception as exc:
                self._ext_serial_enabled = False
                self._gui_cfg["ext_serial_enabled"] = False
                self._save_gui_config()
                self._ext_serial_toggle_action.setChecked(False)
                self._status_bar.showMessage(f"Serial Scripting disabled: {exc}")

    def _build_left_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # --- Connection group ---
        grp_conn = QGroupBox("Connection")
        form_conn = QFormLayout(grp_conn)
        form_conn.setLabelAlignment(Qt.AlignLeft)

        self._port_combo = QComboBox()
        self._port_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._refresh_btn = QPushButton("⟳")
        self._refresh_btn.setFixedWidth(28)
        self._refresh_btn.setToolTip("Refresh port list")
        self._refresh_btn.clicked.connect(self._refresh_ports)

        self._auto_btn = QPushButton("Auto")
        self._auto_btn.setFixedWidth(44)
        self._auto_btn.setToolTip(
            "Scan ports and auto-connect CO2Dot + Pyroscience")
        self._auto_btn.clicked.connect(self._on_port_autodetect)

        port_row = QWidget()
        port_row_h = QHBoxLayout(port_row)
        port_row_h.setContentsMargins(0, 0, 0, 0)
        port_row_h.addWidget(self._port_combo, stretch=1)
        port_row_h.addWidget(self._refresh_btn)
        port_row_h.addWidget(self._auto_btn)

        self._connect_btn = QPushButton("Connect")
        self._connect_btn.clicked.connect(self._on_connect_clicked)

        self._spec_status_lbl = QLabel("Spectrometer: —")
        self._bme_status_lbl  = QLabel("BME: —")

        form_conn.addRow("Port:", port_row)
        form_conn.addRow(self._connect_btn)
        form_conn.addRow(self._spec_status_lbl)
        form_conn.addRow(self._bme_status_lbl)

        layout.addWidget(grp_conn)

        # --- Settings group ---
        grp_set = QGroupBox("Settings")
        form_set = QFormLayout(grp_set)
        form_set.setLabelAlignment(Qt.AlignLeft)

        # Interval
        self._interval_combo = QComboBox()
        for label, _ in INTERVALS:
            self._interval_combo.addItem(label)
        self._interval_combo.setCurrentIndex(0)  # 1 s default

        # Mode
        mode_widget = QWidget()
        mode_h = QHBoxLayout(mode_widget)
        mode_h.setContentsMargins(0, 0, 0, 0)
        self._mode_ambient = QRadioButton("Ambient")
        self._mode_flash   = QRadioButton("Flash")
        self._mode_flash.setChecked(True)
        mode_h.addWidget(self._mode_ambient)
        mode_h.addWidget(self._mode_flash)

        # Gain
        self._gain_combo = QComboBox()
        self._populate_gain_combo()

        # ATIME
        self._atime_spin = QSpinBox()
        self._atime_spin.setRange(0, 255)
        self._atime_spin.setValue(100)

        # ASTEP
        self._astep_spin = QSpinBox()
        self._astep_spin.setRange(0, 65534)
        self._astep_spin.setValue(999)

        # LED
        self._led_spin = QSpinBox()
        self._led_spin.setRange(0, 20)
        self._led_spin.setValue(10)
        self._led_spin.setSuffix(" mA")

        self._apply_btn = QPushButton("Apply Settings")
        self._apply_btn.clicked.connect(self._on_apply_settings)
        self._apply_btn.setEnabled(False)

        form_set.addRow("Interval:", self._interval_combo)
        form_set.addRow("Mode:", mode_widget)
        form_set.addRow("Gain:", self._gain_combo)
        form_set.addRow("ATIME:", self._atime_spin)
        form_set.addRow("ASTEP:", self._astep_spin)
        form_set.addRow("LED:", self._led_spin)
        self._defaults_btn = QPushButton("Reset to Default")
        self._defaults_btn.clicked.connect(self._on_reset_defaults)

        form_set.addRow(self._apply_btn)
        form_set.addRow(self._defaults_btn)

        layout.addWidget(grp_set)
        self._left_layout = layout   # insertion point for LiControlPanel
        layout.addStretch()
        return panel

    def _build_right_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # Splitter with two plots
        splitter = QSplitter(Qt.Vertical)

        self._spec_time_axis = TimeAxisItem(orientation='bottom')
        self._spec_plot = pg.PlotWidget(title="Spectrometer",
                                        axisItems={'bottom': self._spec_time_axis})
        self._spec_plot.setLabel("left", "Counts")
        self._spec_plot.setLabel("bottom", "Time", units="s")
        self._spec_plot.showGrid(x=True, y=True, alpha=0.3)
        self._spec_legend = self._spec_plot.addLegend(offset=(10, 10))
        self._spec_curves: dict[str, pg.PlotDataItem] = {}

        self._bme_time_axis = TimeAxisItem(orientation='bottom')
        self._bme_plot = pg.PlotWidget(title="BME68x Environment",
                                       axisItems={'bottom': self._bme_time_axis})
        self._bme_plot.setLabel("bottom", "Time", units="s")
        self._bme_plot.showGrid(x=True, y=True, alpha=0.3)
        self._bme_legend = self._bme_plot.addLegend(offset=(10, 10))
        self._bme_curves: dict[str, pg.PlotDataItem] = {}
        self._bme_axes: dict[str, pg.AxisItem] = {}
        self._bme_vbs: dict[str, pg.ViewBox] = {}
        self._build_bme_axes()

        splitter.addWidget(self._spec_plot)
        splitter.addWidget(self._bme_plot)
        splitter.setSizes([400, 300])
        self._right_splitter = splitter

        # Disable auto-range when user manually zooms/pans
        self._spec_plot.getViewBox().sigRangeChangedManually.connect(
            self._on_manual_zoom)
        self._bme_plot.getViewBox().sigRangeChangedManually.connect(
            self._on_manual_zoom)

        layout.addWidget(splitter, stretch=1)

        # Controls bar
        ctrl = self._build_controls_bar()
        layout.addWidget(ctrl)

        return panel

    def _build_bme_axes(self):
        """Add one independent y-axis + ViewBox per BME field."""
        fields = ["T", "P", "RH", "Gas"]
        main_vb = self._bme_plot.getViewBox()

        # First field (T) uses the built-in left axis and main ViewBox
        first = fields[0]
        self._bme_plot.setLabel("left", f"{first} ({BME_UNITS[first]})",
                                color=BME_COLORS[first])
        self._bme_axes[first] = self._bme_plot.getAxis("left")
        self._bme_vbs[first] = main_vb

        # Remaining fields get their own ViewBox + right-side axis
        for col, field in enumerate(fields[1:], start=3):
            ax = pg.AxisItem("right")
            ax.setLabel(f"{field} ({BME_UNITS[field]})", color=BME_COLORS[field])

            vb = pg.ViewBox()
            self._bme_plot.scene().addItem(vb)
            ax.linkToView(vb)
            vb.setXLink(main_vb)

            self._bme_plot.plotItem.layout.addItem(ax, 2, col)
            self._bme_axes[field] = ax
            self._bme_vbs[field] = vb

        # Keep overlay ViewBoxes in sync when the main plot is resized
        main_vb.sigResized.connect(self._sync_bme_viewboxes)

    def _sync_bme_viewboxes(self):
        """Keep overlay ViewBoxes geometry in sync with the main ViewBox."""
        main_vb = self._bme_plot.getViewBox()
        rect = main_vb.sceneBoundingRect()
        if rect.width() == 0 or rect.height() == 0:
            return
        for field, vb in self._bme_vbs.items():
            if vb is not main_vb:
                vb.setGeometry(rect)

    @staticmethod
    def _connect_legend_toggle(legend, curve):
        """Make the last-added legend item clickable to toggle curve visibility."""
        if not legend.items:
            return
        sample, label = legend.items[-1]

        def on_click(ev, c=curve, s=sample, lb=label):
            visible = not c.isVisible()
            c.setVisible(visible)
            s.setOpacity(1.0 if visible else 0.3)
            lb.setOpacity(1.0 if visible else 0.3)

        sample.mousePressEvent = on_click
        label.mousePressEvent = on_click

    def _build_controls_bar(self) -> QWidget:
        bar = QWidget()
        h = QHBoxLayout(bar)
        h.setContentsMargins(0, 0, 0, 0)

        self._start_btn = QPushButton("▶  Start")
        self._start_btn.setEnabled(False)
        self._start_btn.clicked.connect(self._on_start)

        self._stop_btn = QPushButton("■  Stop")
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self._on_stop)

        self._clear_btn = QPushButton("Clear")
        self._clear_btn.clicked.connect(self._on_clear)

        self._yfit_btn = QPushButton("↕ Y-Fit")
        self._yfit_btn.setToolTip("Auto-fit vertical axis only")
        self._yfit_btn.clicked.connect(self._on_y_fit)

        self._xfit_btn = QPushButton("↔ X-Fit")
        self._xfit_btn.setToolTip("Auto-fit horizontal axis only")
        self._xfit_btn.clicked.connect(self._on_x_fit)

        self._resetview_btn = QPushButton("Reset View")
        self._resetview_btn.setToolTip("Reset to full auto-scale view")
        self._resetview_btn.clicked.connect(self._on_reset_view)

        self._time_toggle_btn = QPushButton("Time (s)")
        self._time_toggle_btn.setToolTip("Toggle between elapsed seconds and HH:MM:SS")
        self._time_toggle_btn.clicked.connect(self._on_toggle_time_axis)

        h.addWidget(self._start_btn)
        h.addWidget(self._stop_btn)
        h.addWidget(self._clear_btn)
        h.addWidget(self._yfit_btn)
        h.addWidget(self._xfit_btn)
        h.addWidget(self._resetview_btn)
        h.addWidget(self._time_toggle_btn)
        h.addStretch()

        # Recording controls
        rec_grp = QGroupBox("Record")
        rec_h = QHBoxLayout(rec_grp)
        rec_h.setContentsMargins(4, 4, 4, 4)

        self._filename_edit = QLineEdit("DATA")
        self._filename_edit.setMaximumWidth(150)
        self._filename_edit.setPlaceholderText("filename (no ext)")

        self._record_btn = QPushButton("⏺  Record")
        self._record_btn.setEnabled(False)
        self._record_btn.clicked.connect(self._on_record_start)

        self._stop_rec_btn = QPushButton("⏹  Stop Rec")
        self._stop_rec_btn.setEnabled(False)
        self._stop_rec_btn.clicked.connect(self._on_record_stop)

        rec_h.addWidget(QLabel("File:"))
        rec_h.addWidget(self._filename_edit)
        rec_h.addWidget(self._record_btn)
        rec_h.addWidget(self._stop_rec_btn)

        h.addWidget(rec_grp)
        return bar

    # ------------------------------------------------------------------
    # Gain combo helpers
    # ------------------------------------------------------------------

    def _populate_gain_combo(self):
        self._gain_combo.clear()
        labels = protocol.gain_labels(self._model)
        for idx in sorted(labels):
            self._gain_combo.addItem(f"{idx}  ({labels[idx]})", idx)
        # Select closest to current gain
        for i in range(self._gain_combo.count()):
            if self._gain_combo.itemData(i) == self._gain:
                self._gain_combo.setCurrentIndex(i)
                break

    # ------------------------------------------------------------------
    # Port management
    # ------------------------------------------------------------------

    def _refresh_ports(self):
        self._port_combo.clear()
        ports = device_manager.list_ports()
        for p in ports:
            self._port_combo.addItem(p)
        if not ports:
            self._port_combo.addItem("(no ports found)")

    def _on_connect_clicked(self):
        if self._worker and self._worker.isRunning():
            # Disconnect
            self._running = False
            self._acq_timer.stop()
            self._worker.close_port()
            self._connect_btn.setText("Connect")
            self._start_btn.setEnabled(False)
            self._apply_btn.setEnabled(False)
            self._status_bar.showMessage("Disconnected")
        else:
            port = self._port_combo.currentText()
            if not port or port.startswith("("):
                return
            busy = self._ports_in_use()
            if port in busy:
                self._status_bar.showMessage(
                    f"{port} is in use by {busy[port]} — pick another port")
                return
            self._status_bar.showMessage(f"Checking {port}…")
            detected = device_manager.check_port(port)
            if not detected:
                self._status_bar.showMessage(
                    f"{port}: no known device found (no hello response)"
                )
                return
            self._device_type = detected
            self._has_bme = (detected == "CO2Dot")
            if self._worker is not None:
                self._worker.deleteLater()
            self._worker = SerialWorker(self)
            self._worker.spec_received.connect(self._on_spec)
            self._worker.bme_received.connect(self._on_bme)
            self._worker.status_received.connect(self._on_status)
            self._worker.spec_config_received.connect(self._on_spec_config)
            self._worker.error_received.connect(self._on_error)
            self._worker.connected.connect(self._on_connected)
            self._worker.disconnected.connect(self._on_disconnected)
            self._worker.open_port(port)
            self._connect_btn.setText("Disconnect")
            self._status_bar.showMessage(f"Connecting to {port}…")

    # ------------------------------------------------------------------
    # Worker signal handlers
    # ------------------------------------------------------------------

    def _on_connected(self, info: dict):
        port = info.get("port", "")
        device = info.get("device", "")
        if device:
            self._device_type = device
            self._has_bme = (device == "CO2Dot")
        self._status_bar.showMessage(f"Connected: {port} ({device or 'unknown'})")
        self._start_btn.setEnabled(True)
        self._apply_btn.setEnabled(True)
        # Hide/show BME UI based on device capabilities
        self._bme_plot.setVisible(self._has_bme)
        self._bme_status_lbl.setVisible(self._has_bme)

    def _on_disconnected(self):
        self._running = False
        self._acq_timer.stop()
        # Re-enable Start only if Pyroscience is still around to drive it
        self._start_btn.setEnabled(self._has_any_worker())
        self._stop_btn.setEnabled(False)
        self._apply_btn.setEnabled(False)
        self._connect_btn.setText("Connect")
        self._spec_status_lbl.setText("Spectrometer: —")
        self._bme_status_lbl.setText("BME: —")
        self._bme_status_lbl.setVisible(True)
        self._bme_plot.setVisible(True)
        self._device_type = ""
        self._has_bme = False
        self._status_bar.showMessage("Disconnected")
        if self._recorder.is_recording:
            self._recorder.stop_recording()

    def _on_status(self, data: dict):
        spec_info = data.get("spectrometer", {})
        bme_info  = data.get("bme", {})

        if spec_info:
            model = spec_info.get("model", "Unknown")
            avail = spec_info.get("available", False)
            self._model = model
            icon = "✓" if avail else "✗"
            color = "green" if avail else "red"
            self._spec_status_lbl.setText(
                f'<span style="color:{color}">{icon} {model}</span>'
            )
            if not avail:
                self._status_bar.showMessage(f"Warning: spectrometer {model} not available")
            # Update gain combo range for this model
            self._populate_gain_combo()
            # Update atime/astep defaults
            defs = protocol.defaults_for_model(model)
            self._atime_spin.setValue(spec_info.get("atime", defs["atime"]))
            self._astep_spin.setValue(spec_info.get("astep", defs["astep"]))
            gain_val = spec_info.get("gain", defs["gain"])
            self._gain = gain_val
            for i in range(self._gain_combo.count()):
                if self._gain_combo.itemData(i) == gain_val:
                    self._gain_combo.setCurrentIndex(i)
                    break

        if bme_info:
            self._has_bme = True
            avail = bme_info.get("available", False)
            icon  = "✓" if avail else "✗"
            color = "green" if avail else "red"
            self._bme_status_lbl.setText(
                f'<span style="color:{color}">{icon} BME68x</span>'
            )
            self._bme_plot.setVisible(True)
            self._bme_status_lbl.setVisible(True)
        elif not self._has_bme:
            # Device doesn't report BME — hide related UI
            self._bme_status_lbl.setText("BME: N/A")
            self._bme_plot.setVisible(False)
            self._bme_status_lbl.setVisible(False)

    def _on_spec(self, data: dict):
        self._acq_pending = False
        ts = time.time()
        channels = data.get("channels", {})
        self._last_spec = channels
        self._spec_buffer.append(ts, channels)
        self._update_spec_plot()
        if self._recorder.is_recording:
            self._recorder.write_row(
                datetime.fromtimestamp(ts).isoformat(timespec="milliseconds"),
                channels,
                self._last_bme,
                extra=(self._last_ext_params or None),
            )

    def _on_bme(self, data: dict):
        ts = time.time()
        self._last_bme = data
        self._bme_buffer.append(ts, data)
        self._update_bme_plot()

    def _on_spec_config(self, cfg: dict):
        if "led_current_ma" in cfg:
            self._status_bar.showMessage(f"LED set to {cfg['led_current_ma']} mA")
        else:
            self._status_bar.showMessage(
                f"Config applied — gain={cfg.get('gain')}, "
                f"atime={cfg.get('atime')}, astep={cfg.get('astep')}"
            )

    def _on_error(self, msg: str):
        self._acq_pending = False
        self._status_bar.showMessage(f"Error: {msg}")

    # ------------------------------------------------------------------
    # Acquisition control
    # ------------------------------------------------------------------

    def _on_acquire_tick(self):
        if not self._worker or not self._worker.isRunning():
            return
        if self._acq_pending:
            return  # previous cycle still in progress
        self._acq_pending = True
        # Send env only if the device has a BME sensor
        if self._has_bme:
            self._worker.send_command(protocol.CMD_ENV)
        if self._mode_flash.isChecked():
            self._worker.send_command(protocol.cmd_spec_flash(self._led_spin.value()))
        else:
            self._worker.send_command(protocol.CMD_SPEC)

    def _on_start(self):
        co2_running = bool(self._worker and self._worker.isRunning())
        pyro_running = bool(
            self._pyro_worker and self._pyro_worker.isRunning()
        )
        if not (co2_running or pyro_running):
            return

        if co2_running:
            interval_ms = INTERVALS[self._interval_combo.currentIndex()][1] * 1000
            self._acq_timer.start(interval_ms)

        if pyro_running:
            interval_s = (self._pyro_panel.current_interval_s()
                          if self._pyro_panel is not None else 1.0)
            self._pyro_worker.start_streaming(interval_s)
            if self._pyro_panel is not None:
                self._pyro_panel.on_streaming_started()

        self._running = True
        self._start_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._record_btn.setEnabled(True)
        self._status_bar.showMessage("Acquiring…")
        if co2_running:
            # Immediate first CO2Dot sample (Pyro paces itself)
            self._on_acquire_tick()

    def _on_stop(self):
        self._running = False
        self._acq_timer.stop()
        if self._pyro_worker is not None and self._pyro_worker.isRunning():
            self._pyro_worker.stop_streaming()
            if self._pyro_panel is not None:
                self._pyro_panel.on_streaming_stopped()
        self._start_btn.setEnabled(self._has_any_worker())
        self._stop_btn.setEnabled(False)
        self._record_btn.setEnabled(False)
        if self._recorder.is_recording:
            self._on_record_stop()
        self._status_bar.showMessage("Stopped")

    def _has_any_worker(self) -> bool:
        co2 = bool(self._worker and self._worker.isRunning())
        pyro = bool(self._pyro_worker and self._pyro_worker.isRunning())
        return co2 or pyro

    def _on_clear(self):
        # Clear graph buffers and curves; recording continues to the same file
        self._spec_buffer.clear()
        self._bme_buffer.clear()
        # Remove spec curves and legend
        for curve in self._spec_curves.values():
            self._spec_plot.removeItem(curve)
        self._spec_curves.clear()
        self._spec_legend.clear()
        # Remove BME curves from their ViewBoxes and legend
        for field, curve in self._bme_curves.items():
            self._bme_vbs[field].removeItem(curve)
        self._bme_curves.clear()
        self._bme_legend.clear()
        self._last_spec = None
        self._last_bme = None
        # Pyroscience (if active)
        if self._pyro_buffer is not None:
            self._pyro_buffer.clear()
        if self._pyro_plot is not None:
            for curve in self._pyro_curves.values():
                self._pyro_plot.removeItem(curve)
            self._pyro_curves.clear()
            if self._pyro_legend is not None:
                self._pyro_legend.clear()
        # Serial Scripting (if active). Deliberately keep _last_ext_params:
        # clearing a graph doesn't change device state, and recording rows
        # must keep carrying the last-known values.
        if self._ext_serial_buffer is not None:
            self._ext_serial_buffer.clear()
        if self._ext_serial_plot is not None:
            # Curves live in per-param ViewBoxes (BME pattern); the
            # ViewBoxes/axes themselves persist for reuse.
            for name, curve in self._ext_serial_curves.items():
                vb = self._ext_serial_vbs.get(name)
                if vb is not None:
                    vb.removeItem(curve)
            self._ext_serial_curves.clear()
            if self._ext_serial_legend is not None:
                self._ext_serial_legend.clear()

    # ------------------------------------------------------------------
    # Recording control
    # ------------------------------------------------------------------

    def _on_record_start(self):
        filename = self._filename_edit.text().strip() or "DATA"
        mode = "flash" if self._mode_flash.isChecked() else "ambient"

        # Serial Scripting: freeze the extra param columns for this file.
        # Names come from a scan of the running script's source snapshot
        # (or the editor text) plus any params already set this session, so
        # the very first row carries the current state.
        extra_cols: list[str] = []
        if self._ext_serial_panel is not None:
            from ext_serial_script import scan_param_names
            code = None
            if (self._ext_serial_runner is not None
                    and self._ext_serial_runner.isRunning()):
                code = self._ext_serial_runner.source_code
            if not code:
                code = self._ext_serial_panel.script_text()
            extra_cols = list(dict.fromkeys(
                scan_param_names(code) + list(self._last_ext_params.keys())))
        self._ext_extra_cols = extra_cols
        self._ext_missing_warned.clear()

        path = self._recorder.start_recording(
            filename=filename,
            model=self._model,
            mode=mode,
            gain=self._gain,
            atime=self._atime_spin.value(),
            astep=self._astep_spin.value(),
            led=self._led_spin.value(),
            spec_channels=protocol.channels_for_model(self._model),
            extra_cols=extra_cols or None,
        )
        self._record_btn.setEnabled(False)
        self._stop_rec_btn.setEnabled(True)
        self._status_bar.showMessage(f"Recording → {path.name}")

        # If Pyroscience is already streaming, start a parallel pyro file.
        if (self._pyro_worker is not None
                and self._pyro_worker.isRunning()):
            self._open_pyro_recorder_lazy()

        # Serial Scripting: open the event-log sidecar when the feature has
        # anything to log (live port, running script, or pre-set params).
        if self._ext_serial_panel is not None and (
                (self._ext_serial_worker is not None
                 and self._ext_serial_worker.isRunning())
                or (self._ext_serial_runner is not None
                    and self._ext_serial_runner.isRunning())
                or self._last_ext_params):
            self._open_ext_serial_recorder_lazy()

    def _on_record_stop(self):
        self._recorder.stop_recording()
        if self._pyro_recorder is not None and self._pyro_recorder.is_recording:
            self._pyro_recorder.stop_recording()
        if (self._ext_serial_recorder is not None
                and self._ext_serial_recorder.is_recording):
            self._ext_serial_recorder.stop_recording()
        self._record_btn.setEnabled(self._running)
        self._stop_rec_btn.setEnabled(False)
        self._status_bar.showMessage("Recording stopped")

    # ------------------------------------------------------------------
    # Settings application
    # ------------------------------------------------------------------

    def _on_apply_settings(self):
        if not self._worker or not self._worker.isRunning():
            return
        gain_val = self._gain_combo.currentData()
        atime_val = self._atime_spin.value()
        astep_val = self._astep_spin.value()
        led_val   = self._led_spin.value()
        self._gain  = gain_val
        self._atime = atime_val
        self._astep = astep_val
        self._led   = led_val
        self._worker.send_command(protocol.cmd_set_gain(gain_val))
        self._worker.send_command(protocol.cmd_set_atime(atime_val))
        self._worker.send_command(protocol.cmd_set_astep(astep_val))
        self._worker.send_command(protocol.cmd_set_led(led_val))

    # ------------------------------------------------------------------
    # View control handlers
    # ------------------------------------------------------------------

    def _on_manual_zoom(self):
        """Called when the user manually zooms/pans a plot."""
        self._auto_range = False

    def _on_y_fit(self):
        """Auto-fit vertical axis only (keep horizontal range)."""
        self._spec_plot.enableAutoRange(axis='y')
        for vb in self._bme_vbs.values():
            vb.enableAutoRange(axis='y')
        if self._pyro_plot is not None:
            self._pyro_plot.enableAutoRange(axis='y')
        if self._ext_serial_plot is not None:
            for vb in (self._ext_serial_vbs.values()
                       or [self._ext_serial_plot.getViewBox()]):
                vb.enableAutoRange(axis='y')

    def _on_x_fit(self):
        """Fit the x-axis so ALL plots show the same wall-clock window.

        Each pane plots against its own t0, so a plain per-plot x-autorange
        never lines the panes up in time; here the union of all data spans
        is applied to every pane in its local coordinates."""
        now = time.time()
        panes = []   # (main ViewBox, its wall-clock t0)
        spans = []   # (wall_start, wall_end) of that pane's data
        if len(self._spec_buffer):
            t = self._spec_buffer.times()
            panes.append((self._spec_plot.getViewBox(), t[0]))
            spans.append((t[0], t[-1]))
        if len(self._bme_buffer):
            t = self._bme_buffer.times()
            panes.append((self._bme_plot.getViewBox(), t[0]))
            spans.append((t[0], t[-1]))
        if (self._pyro_plot is not None and self._pyro_buffer is not None
                and len(self._pyro_buffer)):
            t = self._pyro_buffer.times()
            panes.append((self._pyro_plot.getViewBox(), t[0]))
            spans.append((t[0], t[-1]))
        if (self._ext_serial_plot is not None
                and self._ext_serial_buffer is not None
                and len(self._ext_serial_buffer)):
            t0 = self._ext_serial_buffer.t0()
            panes.append((self._ext_serial_plot.getViewBox(), t0))
            spans.append((t0, now))    # step curves hold-extend to "now"
        if not panes:
            self._spec_plot.enableAutoRange(axis='x')
            return
        start = min(s for s, _ in spans)
        end = max(e for _, e in spans)
        if end <= start:
            end = start + 1.0
        # Freeze live auto-range so the aligned window sticks; Reset View
        # returns to live auto-scaling. (Overlay ViewBoxes follow via XLink.)
        self._auto_range = False
        for vb, t0 in panes:
            vb.setXRange(start - t0, end - t0, padding=0.02)

    def _on_reset_view(self):
        """Reset to full auto-scale on both axes, re-enable live auto-range."""
        self._auto_range = True
        self._spec_plot.enableAutoRange()
        for vb in self._bme_vbs.values():
            vb.enableAutoRange()
        if self._pyro_plot is not None:
            self._pyro_plot.enableAutoRange()
        if self._ext_serial_plot is not None:
            for vb in (self._ext_serial_vbs.values()
                       or [self._ext_serial_plot.getViewBox()]):
                vb.enableAutoRange()

    def _on_toggle_time_axis(self):
        """Toggle x-axis between elapsed seconds and HH:MM:SS."""
        self._show_timestamp = not self._show_timestamp
        if self._show_timestamp:
            self._time_toggle_btn.setText("HH:MM:SS")
            self._spec_plot.setLabel("bottom", "Timestamp")
            self._bme_plot.setLabel("bottom", "Timestamp")
            if self._pyro_plot is not None:
                self._pyro_plot.setLabel("bottom", "Timestamp")
            if self._ext_serial_plot is not None:
                self._ext_serial_plot.setLabel("bottom", "Timestamp")
        else:
            self._time_toggle_btn.setText("Time (s)")
            self._spec_plot.setLabel("bottom", "Time", units="s")
            self._bme_plot.setLabel("bottom", "Time", units="s")
            if self._pyro_plot is not None:
                self._pyro_plot.setLabel("bottom", "Time", units="s")
            if self._ext_serial_plot is not None:
                self._ext_serial_plot.setLabel("bottom", "Time", units="s")
        self._spec_time_axis.set_timestamp_mode(self._show_timestamp)
        self._bme_time_axis.set_timestamp_mode(self._show_timestamp)
        if self._pyro_time_axis is not None:
            self._pyro_time_axis.set_timestamp_mode(self._show_timestamp)
        if self._ext_serial_time_axis is not None:
            self._ext_serial_time_axis.set_timestamp_mode(self._show_timestamp)
        # Force redraw
        self._update_spec_plot()
        self._update_bme_plot()
        self._update_pyro_plot()
        self._update_ext_serial_plot()

    def _on_reset_defaults(self):
        """Reset spectrometer settings to model defaults."""
        defs = protocol.defaults_for_model(self._model)
        self._atime_spin.setValue(defs["atime"])
        self._astep_spin.setValue(defs["astep"])
        self._led_spin.setValue(defs["led"])
        self._gain = defs["gain"]
        for i in range(self._gain_combo.count()):
            if self._gain_combo.itemData(i) == self._gain:
                self._gain_combo.setCurrentIndex(i)
                break

    # ------------------------------------------------------------------
    # Plot update helpers
    # ------------------------------------------------------------------

    def _update_spec_plot(self):
        if len(self._spec_buffer) == 0:
            return

        times = self._spec_buffer.times()
        t0 = times[0]
        t_rel = times - t0
        self._spec_time_axis.set_t0(t0)

        channels = self._spec_buffer.channel_names()
        ordered = [c for c in protocol.channels_for_model(self._model) if c in channels]
        ordered += [c for c in channels if c not in ordered]

        for i, ch in enumerate(ordered):
            color = SPEC_COLORS[i % len(SPEC_COLORS)]
            vals = self._spec_buffer.channel(ch)
            if len(vals) != len(t_rel):
                continue
            if ch not in self._spec_curves:
                label = protocol.channel_display_name(ch)
                pen = pg.mkPen(color=color, width=1.5)
                curve = self._spec_plot.plot(pen=pen, name=label)
                self._spec_curves[ch] = curve
                self._connect_legend_toggle(self._spec_legend, curve)
            self._spec_curves[ch].setData(t_rel, vals)

        if self._auto_range:
            self._spec_plot.enableAutoRange()

    def _update_bme_plot(self):
        if len(self._bme_buffer) == 0:
            return

        times = self._bme_buffer.times()
        t0 = times[0]
        t_rel = times - t0
        self._bme_time_axis.set_t0(t0)

        for field in BmeBuffer.FIELDS:
            vals = self._bme_buffer.field(field)
            if len(vals) != len(t_rel):
                continue
            color = BME_COLORS[field]
            vb = self._bme_vbs[field]
            if field not in self._bme_curves:
                label = f"{field} ({BME_UNITS[field]})"
                pen = pg.mkPen(color=color, width=1.5)
                curve = pg.PlotDataItem(pen=pen, name=label)
                vb.addItem(curve)
                self._bme_legend.addItem(curve, label)
                self._bme_curves[field] = curve
                self._connect_legend_toggle(self._bme_legend, curve)
            self._bme_curves[field].setData(t_rel, vals)

        # Ensure overlay ViewBoxes have correct geometry (needed on first data)
        self._sync_bme_viewboxes()

        if self._auto_range:
            for vb in self._bme_vbs.values():
                vb.enableAutoRange()

    # ------------------------------------------------------------------
    # Li-Control: menu, config, init
    # ------------------------------------------------------------------

    def _resolve_config_path(self) -> Path:
        if getattr(sys, "frozen", False):
            loc = QStandardPaths.writableLocation(QStandardPaths.AppConfigLocation)
            return Path(loc) / "co2dot" / "gui_config.json"
        return self._base_dir / "gui_config.json"

    def _load_gui_config(self) -> dict:
        try:
            return json.loads(self._gui_cfg_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_gui_config(self) -> None:
        try:
            self._gui_cfg_path.parent.mkdir(parents=True, exist_ok=True)
            self._gui_cfg_path.write_text(
                json.dumps(self._gui_cfg, indent=2), encoding="utf-8"
            )
        except OSError:
            pass

    def _build_menu(self) -> None:
        view_menu = self.menuBar().addMenu("View")
        self._li_toggle_action = QAction("Enable Li-Control", self)
        self._li_toggle_action.setCheckable(True)
        self._li_toggle_action.setChecked(self._li_enabled)
        self._li_toggle_action.toggled.connect(self._toggle_li_control)
        view_menu.addAction(self._li_toggle_action)

        self._pyro_toggle_action = QAction("Enable Pyroscience", self)
        self._pyro_toggle_action.setCheckable(True)
        self._pyro_toggle_action.setChecked(self._pyro_enabled)
        self._pyro_toggle_action.toggled.connect(self._toggle_pyroscience)
        view_menu.addAction(self._pyro_toggle_action)

        self._ext_serial_toggle_action = QAction("Enable Serial Scripting", self)
        self._ext_serial_toggle_action.setCheckable(True)
        self._ext_serial_toggle_action.setChecked(self._ext_serial_enabled)
        self._ext_serial_toggle_action.toggled.connect(self._toggle_ext_serial)
        view_menu.addAction(self._ext_serial_toggle_action)

    def _toggle_li_control(self, enabled: bool) -> None:
        self._li_enabled = enabled
        self._gui_cfg["li_control_enabled"] = enabled
        self._save_gui_config()
        if enabled:
            if self._li_panel is None:
                try:
                    self._init_li_control()
                except Exception as exc:
                    self._li_toggle_action.setChecked(False)
                    self._status_bar.showMessage(f"Li-Control init failed: {exc}")
                    return
            self._li_panel.setVisible(True)
        else:
            if self._li_panel is not None:
                self._li_panel.setVisible(False)

    def _init_li_control(self) -> None:
        # Lazy imports confined to this method so a broken install doesn't
        # prevent the GUI from launching when Li-Control is disabled.
        from li_panel import LiControlPanel

        panel = LiControlPanel()
        panel.connect_requested.connect(self._on_li_connect)
        panel.disconnect_requested.connect(self._on_li_disconnect)
        panel.setpoints_requested.connect(self._on_li_send)
        panel.stop_requested.connect(self._on_li_stop)
        panel.scan_requested.connect(self._on_li_scan)
        panel.sequence_load_requested.connect(self._on_li_load_sequence)
        panel.sequence_edit_requested.connect(self._on_li_sequence_edit)
        panel.sequence_start.connect(self._on_li_sequence_start)
        panel.sequence_abort.connect(self._on_li_sequence_abort)

        insert_at = self._left_layout.count() - 1  # before the addStretch
        self._left_layout.insertWidget(insert_at, panel)
        self._li_panel = panel

        # Try zeroconf discovery; fall back to plain-mDNS resolver alone.
        try:
            from li_discovery import LiDiscovery
            self._li_discovery = LiDiscovery(self)
        except ImportError:
            from li_discovery_plain import PlainMdnsResolver
            self._li_discovery = PlainMdnsResolver(self)
            self._status_bar.showMessage(
                "Li-Control: zeroconf missing — using plain mDNS only"
            )
        except Exception as exc:
            self._li_discovery = None
            self._status_bar.showMessage(f"Li-Control discovery unavailable: {exc}")

        if self._li_discovery is not None:
            self._li_discovery.host_found.connect(self._on_li_host_found)
            self._li_discovery.finished.connect(self._on_li_discovery_finished)
            self._li_discovery.start(5.0)

    # ------------------------------------------------------------------
    # Li-Control: discovery slots
    # ------------------------------------------------------------------

    def _on_li_scan(self) -> None:
        if self._li_discovery is None:
            self._status_bar.showMessage("Li-Control: discovery unavailable")
            return
        self._li_discovery.start(5.0)
        self._status_bar.showMessage("Li-Control: scanning…")

    def _on_li_host_found(self, name: str, ip: str) -> None:
        if self._li_panel is not None:
            self._li_panel.add_discovered_host(name, ip)

    def _on_li_discovery_finished(self, count: int) -> None:
        if count == 0:
            self._status_bar.showMessage(
                "No LI-6800 found — check firewall or type hostname manually"
            )
        else:
            self._status_bar.showMessage(f"Li-Control: found {count} host(s)")

    # ------------------------------------------------------------------
    # Li-Control: SSH session slots
    # ------------------------------------------------------------------

    def _on_li_connect(self, cfg) -> None:
        if self._li_worker is not None and self._li_worker.isRunning():
            return
        try:
            from li_worker import LiWorker
        except ImportError:
            self._status_bar.showMessage(
                "Install paramiko (pip install paramiko>=3.4) to use Li-Control"
            )
            return

        # Accept "host  [ip]" format from discovered entries.
        host = cfg.host
        if "[" in host and host.endswith("]"):
            host = host.split("[", 1)[1].rstrip("]").strip() or host
            cfg.host = host

        if self._li_worker is None:
            self._li_worker = LiWorker(self)
            self._li_worker.connected.connect(self._on_li_connected)
            self._li_worker.disconnected.connect(self._on_li_disconnected)
            self._li_worker.ack_received.connect(self._on_li_ack)
            self._li_worker.error_received.connect(self._on_li_error)
        self._li_worker.open_connection(cfg)
        self._status_bar.showMessage(f"Li-Control: connecting to {cfg.host}…")

    def _on_li_disconnect(self) -> None:
        if self._li_worker is not None and self._li_worker.isRunning():
            self._li_worker.close_connection()

    def _on_li_connected(self, host: str) -> None:
        if self._li_panel is not None:
            self._li_panel.on_connected(host)
        self._status_bar.showMessage(f"Li-Control connected: {host}")

    def _on_li_disconnected(self) -> None:
        if self._li_panel is not None:
            self._li_panel.on_disconnected()
        self._status_bar.showMessage("Li-Control disconnected")

    def _on_li_send(self, sp) -> None:
        if self._li_worker is None or not self._li_worker.isRunning():
            self._status_bar.showMessage("Li-Control: not connected")
            return
        self._last_manual_cmd_id = self._li_worker.send_setpoints(sp)

    def _on_li_stop(self) -> None:
        if self._li_worker is None or not self._li_worker.isRunning():
            return
        self._li_worker.send_stop()

    def _on_li_ack(self, ack: dict) -> None:
        if self._li_panel is not None:
            self._li_panel.on_ack_received(ack)
        # Manual-row logging gates on cmd_id match only.
        if (
            self._li_recorder is not None
            and self._li_recorder.is_recording
            and self._last_manual_cmd_id is not None
            and ack.get("cmd_id") == self._last_manual_cmd_id
        ):
            spec = self._last_spec
            bme = self._last_bme
            if self._worker is None or not self._worker.isRunning():
                notes = "manual_send,spec_unavailable"
                spec = None
                bme = None
            elif self._acq_timer.isActive():
                notes = "manual_send,spec_age<=1_acq_interval"
            else:
                notes = "manual_send"
            sp_dict = {
                "co2_r": None, "tair": None, "rh_air": None, "qin": None,
            }
            self._li_recorder.write_row(
                step_index=-1,
                step_name="manual",
                setpoints=sp_dict,
                ack=ack,
                spec=spec,
                bme=bme,
                notes=notes,
            )
            self._last_manual_cmd_id = None

    def _on_li_error(self, msg: str) -> None:
        self._status_bar.showMessage(f"Li-Control: {msg}")

    # ------------------------------------------------------------------
    # Li-Control: sequence slots
    # ------------------------------------------------------------------

    def _on_li_load_sequence(self, path: str) -> None:
        try:
            from li_sequence import load_sequence
            steps = load_sequence(path)
        except Exception as exc:
            self._status_bar.showMessage(f"Sequence load failed: {exc}")
            return
        if self._li_panel is not None:
            self._li_panel.set_steps(steps)
        self._status_bar.showMessage(
            f"Loaded sequence: {len(steps)} step(s) from {Path(path).name}"
        )

    def _on_li_sequence_edit(self) -> None:
        try:
            from li_sequence_editor import SequenceEditorDialog
        except Exception as exc:
            self._status_bar.showMessage(f"Sequence builder unavailable: {exc}")
            return
        sequences_dir = self._base_dir / "sequences"
        initial = list(self._li_panel._steps) if self._li_panel is not None else []
        dlg = SequenceEditorDialog(
            steps=initial, default_dir=sequences_dir, parent=self
        )
        if dlg.exec() and self._li_panel is not None:
            steps = dlg.result_steps()
            self._li_panel.set_steps(steps)
            self._status_bar.showMessage(
                f"Sequence builder: {len(steps)} step(s) ready to run"
            )

    def _on_li_sequence_start(self) -> None:
        if self._li_worker is None or not self._li_worker.isRunning():
            self._status_bar.showMessage("Li-Control: connect the LI-6800 first")
            return
        if self._li_panel is None or not self._li_panel._steps:
            self._status_bar.showMessage("Li-Control: load a sequence first")
            return

        from li_sequence import SequenceRunner
        from li_recorder import LiRecorder

        # Clean up previous runner if any
        if self._li_runner is not None:
            try:
                self._li_runner.finished.disconnect()
                self._li_runner.aborted.disconnect()
                self._li_runner.step_started.disconnect()
                self._li_runner.repetition_started.disconnect()
            except (RuntimeError, TypeError):
                pass
            self._li_runner.deleteLater()
            self._li_runner = None

        # Choose spec channels (empty when spectrometer never connected)
        if self._worker is not None and self._worker.isRunning():
            spec_channels = protocol.channels_for_model(self._model)
        else:
            spec_channels = []

        self._li_recorder = LiRecorder(self._base_dir / "data")
        try:
            path = self._li_recorder.start_recording(
                filename="DATA",
                model=self._model,
                mode=("flash" if self._mode_flash.isChecked() else "ambient"),
                gain=self._gain,
                atime=self._atime_spin.value(),
                astep=self._astep_spin.value(),
                led=self._led_spin.value(),
                spec_channels=spec_channels,
            )
        except OSError as exc:
            self._status_bar.showMessage(f"Li recorder open failed: {exc}")
            return

        # Pause the main acquisition timer for the duration of the sequence.
        self._acq_was_running = self._acq_timer.isActive()
        if self._acq_was_running:
            self._acq_timer.stop()

        self._li_runner = SequenceRunner(self._li_worker, self, parent=self)
        self._li_runner.step_started.connect(self._on_li_step_started)
        self._li_runner.repetition_started.connect(self._on_li_repetition_started)
        self._li_runner.finished.connect(self._on_li_sequence_finished)
        self._li_runner.aborted.connect(self._on_li_sequence_aborted)

        self._li_panel.on_sequence_started()
        self._li_runner.start(self._li_panel._steps, self._li_recorder)
        self._status_bar.showMessage(
            f"Li-Control sequence running → {Path(path).name}"
        )

    def _on_li_step_started(self, index: int, step) -> None:
        # Repetition_started fires next and updates the panel progress label.
        self._status_bar.showMessage(
            f"Li-Control step {index + 1}: {getattr(step, 'name', '')}"
        )

    def _on_li_repetition_started(self, step_idx: int, rep_idx: int, total_reps: int) -> None:
        if self._li_panel is not None:
            self._li_panel.set_progress(step_idx, rep_idx, total_reps)
        if total_reps > 1:
            self._status_bar.showMessage(
                f"Li-Control step {step_idx + 1} rep {rep_idx + 1}/{total_reps}"
            )

    def _on_li_sequence_abort(self) -> None:
        if self._li_runner is not None:
            self._li_runner.abort()

    def _on_li_sequence_finished(self) -> None:
        self._end_li_sequence("Li-Control sequence finished")

    def _on_li_sequence_aborted(self, reason: str) -> None:
        self._end_li_sequence(f"Li-Control sequence aborted: {reason}")

    def _end_li_sequence(self, message: str) -> None:
        if self._li_recorder is not None:
            self._li_recorder.stop_recording()
        if self._acq_was_running:
            interval_ms = INTERVALS[self._interval_combo.currentIndex()][1] * 1000
            self._acq_timer.start(interval_ms)
        self._acq_was_running = False
        if self._li_panel is not None:
            self._li_panel.on_sequence_ended()
        self._status_bar.showMessage(message)

    # ------------------------------------------------------------------
    # Pyroscience: optional feature
    # ------------------------------------------------------------------

    def _toggle_pyroscience(self, enabled: bool) -> None:
        self._pyro_enabled = enabled
        self._gui_cfg["pyroscience_enabled"] = enabled
        self._save_gui_config()
        if enabled:
            if self._pyro_panel is None:
                try:
                    self._init_pyroscience()
                except Exception as exc:
                    self._pyro_toggle_action.setChecked(False)
                    self._status_bar.showMessage(f"Pyroscience init failed: {exc}")
                    return
            self._pyro_panel.setVisible(True)
            if self._pyro_plot is not None:
                self._pyro_plot.setVisible(True)
                self._rebalance_right_splitter()
        else:
            # Disconnect first if a session is live, then hide
            if self._pyro_worker is not None and self._pyro_worker.isRunning():
                self._pyro_worker.close_port()
            if self._pyro_recorder is not None and self._pyro_recorder.is_recording:
                self._pyro_recorder.stop_recording()
            if self._pyro_panel is not None:
                self._pyro_panel.setVisible(False)
            if self._pyro_plot is not None:
                self._pyro_plot.setVisible(False)
                self._rebalance_right_splitter()

    def _init_pyroscience(self) -> None:
        # Lazy imports so a broken install doesn't keep the GUI from launching
        # when the feature is disabled.
        from pyro_panel import PyroPanel
        from pyro_buffer import PyroBuffer

        # Buffer
        if self._pyro_buffer is None:
            self._pyro_buffer = PyroBuffer()

        # Plot pane (created once, inserted into the right splitter)
        if self._pyro_plot is None:
            self._pyro_time_axis = TimeAxisItem(orientation='bottom')
            self._pyro_plot = pg.PlotWidget(
                title="Pyroscience",
                axisItems={'bottom': self._pyro_time_axis},
            )
            self._pyro_plot.setLabel("left", "Value")
            self._pyro_plot.setLabel(
                "bottom",
                "Timestamp" if self._show_timestamp else "Time",
                units=None if self._show_timestamp else "s",
            )
            self._pyro_time_axis.set_timestamp_mode(self._show_timestamp)
            self._pyro_plot.showGrid(x=True, y=True, alpha=0.3)
            self._pyro_legend = self._pyro_plot.addLegend(
                offset=(10, 10), colCount=2
            )
            self._pyro_plot.getViewBox().sigRangeChangedManually.connect(
                self._on_manual_zoom)
            self._right_splitter.addWidget(self._pyro_plot)
            self._rebalance_right_splitter()

        # Left-panel widget
        panel = PyroPanel(busy_ports_provider=self._ports_in_use)
        panel.connect_requested.connect(self._on_pyro_connect)
        panel.disconnect_requested.connect(self._on_pyro_disconnect)
        insert_at = self._left_layout.count() - 1   # before trailing addStretch
        self._left_layout.insertWidget(insert_at, panel)
        self._pyro_panel = panel

    # ---- Slots --------------------------------------------------------

    def _on_pyro_connect(self, port: str, channel: int, interval_s: float) -> None:
        if self._pyro_worker is not None and self._pyro_worker.isRunning():
            return
        busy = self._ports_in_use()
        if port in busy:
            # Guard against opening another feature's device — PyroWorker
            # opens with DTR/RTS asserted and would reset an ESP32 board.
            if self._pyro_panel is not None:
                self._pyro_panel.on_error(
                    f"{port} is in use by {busy[port]} — pick another port")
            return
        try:
            from pyro_worker import PyroWorker
        except ImportError as exc:
            self._status_bar.showMessage(
                f"Pyroscience: pyserial missing ({exc})"
            )
            return

        self._pyro_channel = int(channel)
        if self._pyro_worker is None:
            self._pyro_worker = PyroWorker(self)
            self._pyro_worker.connected.connect(self._on_pyro_connected)
            self._pyro_worker.disconnected.connect(self._on_pyro_disconnected)
            self._pyro_worker.sample_received.connect(self._on_pyro_sample)
            self._pyro_worker.error_received.connect(self._on_pyro_error)
        self._pyro_worker.open_port(port, channel, interval_s)
        self._status_bar.showMessage(f"Pyroscience: connecting to {port}…")

    def _on_pyro_disconnect(self) -> None:
        if self._pyro_worker is not None and self._pyro_worker.isRunning():
            self._pyro_worker.close_port()

    def _on_pyro_connected(self, info: dict) -> None:
        self._pyro_idnr = info.get("idnr", "")
        if self._pyro_panel is not None:
            self._pyro_panel.on_connected(info)
        # Allow the global Start button to drive Pyroscience too
        if not self._running:
            self._start_btn.setEnabled(True)
        else:
            # Acquisition already in progress — auto-join the running session
            interval_s = (self._pyro_panel.current_interval_s()
                          if self._pyro_panel is not None else 1.0)
            self._pyro_worker.start_streaming(interval_s)
            if self._pyro_panel is not None:
                self._pyro_panel.on_streaming_started()
        self._status_bar.showMessage(
            f"Pyroscience connected: {info.get('port', '')}"
        )

    def _on_pyro_disconnected(self) -> None:
        if self._pyro_panel is not None:
            self._pyro_panel.on_disconnected()
        if self._pyro_recorder is not None and self._pyro_recorder.is_recording:
            self._pyro_recorder.stop_recording()
        # Disable Start unless CO2Dot is still around to drive it
        if not self._running:
            self._start_btn.setEnabled(self._has_any_worker())
        self._status_bar.showMessage("Pyroscience disconnected")

    def _on_pyro_sample(self, data: dict) -> None:
        ts = float(data.get("timestamp", time.time()))
        ch = int(data.get("channel", self._pyro_channel))
        # Field-only dict for buffer/recorder
        sample = {k: v for k, v in data.items()
                  if k not in ("timestamp", "channel")}
        if self._pyro_buffer is not None:
            self._pyro_buffer.append(ts, sample)
        self._update_pyro_plot()

        # Late-Record edge case: open a pyro recorder lazily if the main
        # recorder is already running and we just got our first sample.
        if (self._recorder.is_recording
                and (self._pyro_recorder is None
                     or not self._pyro_recorder.is_recording)):
            self._open_pyro_recorder_lazy()

        if (self._pyro_recorder is not None
                and self._pyro_recorder.is_recording):
            self._pyro_recorder.write_row(
                datetime.fromtimestamp(ts).isoformat(timespec="milliseconds"),
                ch,
                sample,
            )

    def _on_pyro_error(self, msg: str) -> None:
        if self._pyro_panel is not None:
            self._pyro_panel.on_error(msg)
        self._status_bar.showMessage(f"Pyroscience: {msg}")

    # ---- Plot update --------------------------------------------------

    def _update_pyro_plot(self) -> None:
        if (self._pyro_buffer is None
                or self._pyro_plot is None
                or len(self._pyro_buffer) == 0):
            return
        import pyro_protocol

        times = self._pyro_buffer.times()
        t0 = times[0]
        t_rel = times - t0
        if self._pyro_time_axis is not None:
            self._pyro_time_axis.set_t0(t0)

        # Materialize curves on first sample
        if not self._pyro_curves:
            for i, field in enumerate(pyro_protocol.FIELDS):
                color = PYRO_COLORS[i % len(PYRO_COLORS)]
                pen = pg.mkPen(color=color, width=1.5)
                curve = self._pyro_plot.plot(pen=pen, name=field)
                self._pyro_curves[field] = curve
                self._connect_legend_toggle(self._pyro_legend, curve)
                if field not in pyro_protocol.DEFAULT_VISIBLE:
                    curve.setVisible(False)
                    if self._pyro_legend.items:
                        sample_item, label_item = self._pyro_legend.items[-1]
                        sample_item.setOpacity(0.3)
                        label_item.setOpacity(0.3)

        for field, curve in self._pyro_curves.items():
            vals = self._pyro_buffer.field(field)
            if len(vals) == len(t_rel):
                curve.setData(t_rel, vals)

        if self._auto_range:
            self._pyro_plot.enableAutoRange()

    # ---- Recorder helpers --------------------------------------------

    def _open_pyro_recorder_lazy(self) -> None:
        from pyro_recorder import PyroRecorder
        if self._pyro_recorder is None:
            self._pyro_recorder = PyroRecorder(self._base_dir / "data")
        try:
            filename = self._filename_edit.text().strip() or "DATA"
            path = self._pyro_recorder.start_recording(
                filename=filename,
                idnr=self._pyro_idnr,
                channel=self._pyro_channel,
            )
            self._status_bar.showMessage(f"Pyroscience recording → {path.name}")
        except OSError as exc:
            self._status_bar.showMessage(f"Pyroscience record failed: {exc}")

    # ------------------------------------------------------------------
    # Serial Scripting: optional feature
    # ------------------------------------------------------------------

    def _rebalance_right_splitter(self) -> None:
        """Distribute pane heights over the non-hidden plots.

        The spec plot gets 1.5 shares, every other visible pane 1 share.
        isHidden() (not isVisible()) so this also works during __init__,
        before the window itself is shown."""
        total = sum(self._right_splitter.sizes()) or 700
        widgets = [self._right_splitter.widget(i)
                   for i in range(self._right_splitter.count())]
        shares = [
            0.0 if (w is None or w.isHidden())
            else (1.5 if w is self._spec_plot else 1.0)
            for w in widgets
        ]
        denom = sum(shares) or 1.0
        self._right_splitter.setSizes(
            [int(total * s / denom) for s in shares])

    def _ports_in_use(self) -> dict[str, str]:
        """Ports currently owned by a running worker → owner name."""
        busy: dict[str, str] = {}
        if self._worker is not None and self._worker.isRunning():
            busy[self._worker.port] = "CO2Dot"
        if self._pyro_worker is not None and self._pyro_worker.isRunning():
            busy[self._pyro_worker.port] = "Pyroscience"
        if (self._ext_serial_worker is not None
                and self._ext_serial_worker.isRunning()):
            busy[self._ext_serial_worker.port] = "Serial Scripting"
        return busy

    # ---- Port autodetect: one button connects CO2Dot + Pyroscience ------

    def _on_port_autodetect(self) -> None:
        if (self._port_probe_thread is not None
                and self._port_probe_thread.isRunning()):
            self._status_bar.showMessage("Port scan already running…")
            return
        try:
            from port_probe import PortProbeThread
        except ImportError as exc:
            self._status_bar.showMessage(f"Autodetect unavailable: {exc}")
            return
        # Never probe ports that a worker holds open (probing is safe —
        # DTR/RTS stay low — but an owned port can't be opened anyway).
        busy = set(self._ports_in_use())
        ports = [p for p in device_manager.list_ports() if p not in busy]
        if not ports:
            self._status_bar.showMessage("Autodetect: no free ports to scan")
            return
        self._auto_btn.setEnabled(False)
        self._port_probe_thread = PortProbeThread(ports, self)
        self._port_probe_thread.port_checked.connect(
            self._on_port_probe_progress)
        self._port_probe_thread.finished_scan.connect(
            self._on_port_autodetect_done)
        self._port_probe_thread.start()
        self._status_bar.showMessage(
            f"Scanning {len(ports)} port(s) for CO2Dot / Pyroscience…")

    def _on_port_probe_progress(self, port: str, kind: str) -> None:
        if kind:
            self._status_bar.showMessage(f"Autodetect: {port} → {kind}")
        else:
            self._status_bar.showMessage(f"Autodetect: checked {port}")

    def _on_port_autodetect_done(self, found: dict) -> None:
        if self._port_probe_thread is not None:
            self._port_probe_thread.deleteLater()
            self._port_probe_thread = None
        self._auto_btn.setEnabled(True)
        msgs = []

        co2 = found.get("co2dot")
        if co2:
            port, device = co2
            if self._worker is not None and self._worker.isRunning():
                msgs.append(f"{device} on {port} (already connected)")
            else:
                idx = self._port_combo.findText(port)
                if idx < 0:
                    self._port_combo.addItem(port)
                    idx = self._port_combo.findText(port)
                self._port_combo.setCurrentIndex(idx)
                self._on_connect_clicked()
                msgs.append(f"{device} on {port} — connecting")

        pyro = found.get("pyro")
        if pyro:
            port, idnr = pyro
            label = f"Pyroscience on {port}" + (f" (IDNR {idnr})" if idnr else "")
            if self._pyro_panel is None:
                msgs.append(label + " — enable the Pyroscience feature to use it")
            elif (self._pyro_worker is not None
                    and self._pyro_worker.isRunning()):
                msgs.append(label + " (already connected)")
            else:
                self._pyro_panel.set_port(port)
                # Reuse the panel's own click path so channel/interval come
                # from its spin boxes.
                self._pyro_panel._on_connect_clicked()
                msgs.append(label + " — connecting")

        amb = found.get("ambyte")
        if amb:
            # Deliberately not auto-selected: the user picks the leftover
            # port in Serial Scripting manually.
            msgs.append(f"ambyte on {amb} — select it in Serial Scripting")

        self._status_bar.showMessage(
            "Autodetect: " + ("; ".join(msgs) if msgs else "no devices found"))

    def _toggle_ext_serial(self, enabled: bool) -> None:
        if (not enabled
                and self._ext_serial_runner is not None
                and self._ext_serial_runner.isRunning()):
            # A running script is an experiment — refuse to tear it down on
            # a menu mis-click (deliberate divergence from Pyroscience).
            self._ext_serial_toggle_action.setChecked(True)
            self._status_bar.showMessage(
                "Abort the running script before disabling Serial Scripting")
            return
        self._ext_serial_enabled = enabled
        self._gui_cfg["ext_serial_enabled"] = enabled
        self._save_gui_config()
        if enabled:
            if self._ext_serial_panel is None:
                try:
                    self._init_ext_serial()
                except Exception as exc:
                    self._ext_serial_toggle_action.setChecked(False)
                    self._status_bar.showMessage(
                        f"Serial Scripting init failed: {exc}")
                    return
            self._ext_serial_panel.setVisible(True)
            if self._ext_serial_plot is not None:
                self._ext_serial_plot.setVisible(True)
            self._rebalance_right_splitter()
            self._ext_plot_timer.start()
        else:
            if (self._ext_serial_worker is not None
                    and self._ext_serial_worker.isRunning()):
                self._ext_serial_worker.close_port()
            if (self._ext_serial_recorder is not None
                    and self._ext_serial_recorder.is_recording):
                self._ext_serial_recorder.stop_recording()
            self._ext_plot_timer.stop()
            if self._ext_serial_panel is not None:
                self._ext_serial_panel.setVisible(False)
            if self._ext_serial_plot is not None:
                self._ext_serial_plot.setVisible(False)
            self._rebalance_right_splitter()

    def _init_ext_serial(self) -> None:
        # Lazy imports so a broken install doesn't keep the GUI from
        # launching when the feature is disabled.
        from ext_serial_panel import ExtSerialPanel
        from ext_serial_buffer import ParamBuffer

        if self._ext_serial_buffer is None:
            self._ext_serial_buffer = ParamBuffer()

        # Plot pane (created once, inserted into the right splitter)
        if self._ext_serial_plot is None:
            self._ext_serial_time_axis = TimeAxisItem(orientation='bottom')
            self._ext_serial_plot = pg.PlotWidget(
                title="Serial Script Parameters",
                axisItems={'bottom': self._ext_serial_time_axis},
            )
            self._ext_serial_plot.setLabel("left", "Value")
            self._ext_serial_plot.setLabel(
                "bottom",
                "Timestamp" if self._show_timestamp else "Time",
                units=None if self._show_timestamp else "s",
            )
            self._ext_serial_time_axis.set_timestamp_mode(self._show_timestamp)
            self._ext_serial_plot.showGrid(x=True, y=True, alpha=0.3)
            self._ext_serial_legend = self._ext_serial_plot.addLegend(
                offset=(10, 10))
            self._ext_serial_plot.getViewBox().sigRangeChangedManually.connect(
                self._on_manual_zoom)
            self._ext_serial_plot.getViewBox().sigResized.connect(
                self._sync_ext_viewboxes)
            self._right_splitter.addWidget(self._ext_serial_plot)
            self._rebalance_right_splitter()

        # Left-panel widget
        panel = ExtSerialPanel(
            default_script_dir=self._base_dir / "scripts",
            busy_ports_provider=self._ports_in_use,
        )
        saved_script = self._gui_cfg.get("ext_serial_script", "")
        if saved_script:
            panel.set_script_text(saved_script)
        panel.set_port_baud(
            self._gui_cfg.get("ext_serial_port", ""),
            int(self._gui_cfg.get("ext_serial_baud", 115200) or 115200),
        )
        panel.connect_requested.connect(self._on_ext_serial_connect)
        panel.disconnect_requested.connect(self._on_ext_serial_disconnect)
        panel.send_requested.connect(self._on_ext_serial_send)
        panel.run_requested.connect(self._on_ext_serial_run)
        panel.abort_requested.connect(self._on_ext_serial_abort)
        panel.park_requested.connect(self._on_ext_serial_park)
        insert_at = self._left_layout.count() - 1   # before trailing addStretch
        self._left_layout.insertWidget(insert_at, panel)
        self._ext_serial_panel = panel
        self._ext_plot_timer.start()

    # ---- Connection slots ----------------------------------------------

    def _on_ext_serial_connect(self, port: str, baud: int) -> None:
        if (self._ext_serial_worker is not None
                and self._ext_serial_worker.isRunning()):
            return
        busy = self._ports_in_use()
        if port in busy:
            if self._ext_serial_panel is not None:
                self._ext_serial_panel.on_error(
                    f"{port} is in use by {busy[port]} — pick another port")
            return
        try:
            from ext_serial_worker import ExtSerialWorker
        except ImportError as exc:
            self._status_bar.showMessage(
                f"Serial Scripting: pyserial missing ({exc})")
            return
        if self._ext_serial_worker is None:
            self._ext_serial_worker = ExtSerialWorker(self)
            self._ext_serial_worker.connected.connect(
                self._on_ext_serial_connected)
            self._ext_serial_worker.disconnected.connect(
                self._on_ext_serial_disconnected)
            self._ext_serial_worker.line_sent.connect(
                self._on_ext_serial_line_sent)
            self._ext_serial_worker.line_received.connect(
                self._on_ext_serial_line)
            self._ext_serial_worker.error_received.connect(
                self._on_ext_serial_error)
        self._ext_serial_worker.open_port(port, baud)
        self._status_bar.showMessage(f"Serial Scripting: connecting to {port}…")

    def _on_ext_serial_disconnect(self) -> None:
        if (self._ext_serial_worker is not None
                and self._ext_serial_worker.isRunning()):
            self._ext_serial_worker.close_port()

    def _on_ext_serial_connected(self, info: dict) -> None:
        self._ext_wrong_port_hinted = False   # fresh port, fresh hint
        if self._ext_serial_panel is not None:
            self._ext_serial_panel.on_connected(info)
        self._gui_cfg["ext_serial_port"] = info.get("port", "")
        self._gui_cfg["ext_serial_baud"] = int(info.get("baud", 115200))
        self._save_gui_config()
        self._log_ext_event(
            "connect", detail=f"{info.get('port', '')} @ {info.get('baud', '')}")
        self._status_bar.showMessage(
            f"Serial Scripting connected: {info.get('port', '')}")

    def _on_ext_serial_disconnected(self) -> None:
        # Deliberately no auto-abort: an AD3-only script may keep running;
        # a serial script fails loudly at its next send().
        if self._ext_serial_panel is not None:
            self._ext_serial_panel.on_disconnected()
        self._log_ext_event("disconnect")
        self._status_bar.showMessage("Serial Scripting disconnected")

    def _on_ext_serial_error(self, msg: str) -> None:
        if self._ext_serial_panel is not None:
            self._ext_serial_panel.on_error(msg)
        self._status_bar.showMessage(msg)

    # ---- Console / manual command slots ----------------------------------

    def _on_ext_serial_send(self, cmd: str) -> None:
        # Script-helper calls typed in the console (dc_offset(0.05),
        # pwm(10), param('x', 1), …) run through the script engine — they
        # act on the PC/AD3 side, not on the serial device.
        if EXT_HELPER_RE.match(cmd):
            self._run_ext_console_command(cmd)
            return
        if (self._ext_serial_worker is None
                or not self._ext_serial_worker.isRunning()):
            if self._ext_serial_panel is not None:
                self._ext_serial_panel.append_console("not connected")
            return
        try:
            self._ext_serial_worker.send_command(cmd)
        except RuntimeError as exc:
            if self._ext_serial_panel is not None:
                self._ext_serial_panel.append_console(f"send failed: {exc}")
            return
        self._log_ext_event("manual_command", detail=cmd)

    def _run_ext_console_command(self, code: str) -> None:
        """Execute one console line as a mini script (own runner, so it
        never disturbs the Run/Abort state of the main script)."""
        from ext_serial_script import ScriptRunner
        if (self._ext_console_runner is not None
                and self._ext_console_runner.isRunning()):
            self._on_ext_serial_info("previous console command still running")
            return
        if self._ext_console_runner is not None:
            self._ext_console_runner.deleteLater()
            self._ext_console_runner = None
        runner = ScriptRunner(lambda: self._ext_serial_worker, parent=self)
        runner.param_set.connect(self._on_ext_serial_param)
        runner.info.connect(self._on_ext_serial_info)
        runner.finished_run.connect(self._on_ext_console_finished)
        self._ext_console_runner = runner
        self._on_ext_serial_info(code)   # echo what is being executed
        self._log_ext_event("manual_command", detail=code)
        try:
            runner.run_script(code)
        except SyntaxError as exc:
            self._ext_console_runner = None
            runner.deleteLater()
            if self._ext_serial_panel is not None:
                self._ext_serial_panel.append_console(f"syntax error: {exc}")

    def _on_ext_console_finished(self, ok: bool, msg: str) -> None:
        if ok or self._ext_serial_panel is None:
            return
        for ln in msg.splitlines():
            self._ext_serial_panel.append_console(ln)

    def _on_ext_serial_line_sent(self, ts: float, cmd: str) -> None:
        if self._ext_serial_panel is not None:
            self._ext_serial_panel.append_console(
                f"{datetime.fromtimestamp(ts).strftime('%H:%M:%S')} → {cmd}")
        self._log_ext_event("command", detail=cmd, ts=ts)

    def _on_ext_serial_line(self, ts: float, text: str) -> None:
        if self._ext_serial_panel is not None:
            self._ext_serial_panel.append_console(
                f"{datetime.fromtimestamp(ts).strftime('%H:%M:%S')} ← {text}")
            # '#ERRO'/'#IDNR' replies are PyroScience protocol — the classic
            # wrong-port mistake when the optode shares the USB hub.
            if text.startswith("#") and not self._ext_wrong_port_hinted:
                self._ext_wrong_port_hinted = True
                self._ext_serial_panel.append_console(
                    "⚠ '#…' replies are PyroScience protocol — this looks "
                    "like the O2 optode, not the ESP32. Check the port.")
        self._log_ext_event("reply", detail=text, ts=ts)

    # ---- Script slots -----------------------------------------------------

    def _on_ext_serial_run(self, code: str) -> None:
        if (self._ext_serial_runner is not None
                and self._ext_serial_runner.isRunning()):
            self._status_bar.showMessage(
                "Serial Scripting: a script is already running")
            return
        # Persist the script so an app crash can't lose it.
        self._gui_cfg["ext_serial_script"] = code
        self._save_gui_config()

        from ext_serial_script import ScriptRunner

        # Fresh runner per run; the old one has finished and is safe to drop.
        if self._ext_serial_runner is not None:
            try:
                self._ext_serial_runner.started_run.disconnect()
                self._ext_serial_runner.finished_run.disconnect()
                self._ext_serial_runner.param_set.disconnect()
                self._ext_serial_runner.info.disconnect()
                self._ext_serial_runner.finished.disconnect()
            except (RuntimeError, TypeError):
                pass
            self._ext_serial_runner.deleteLater()
            self._ext_serial_runner = None

        runner = ScriptRunner(lambda: self._ext_serial_worker, parent=self)
        runner.started_run.connect(self._on_ext_serial_script_started)
        runner.finished_run.connect(self._on_ext_serial_script_finished)
        runner.param_set.connect(self._on_ext_serial_param)
        runner.info.connect(self._on_ext_serial_info)
        # Safety net: unlock the panel even if finished_run was lost.
        runner.finished.connect(self._on_ext_serial_thread_finished)
        self._ext_serial_runner = runner
        try:
            runner.run_script(code)   # compiles first, on this thread
        except SyntaxError as exc:
            self._ext_serial_runner = None
            runner.deleteLater()
            if self._ext_serial_panel is not None:
                self._ext_serial_panel.append_console(f"syntax error: {exc}")
            self._status_bar.showMessage(f"Script syntax error: {exc}")
        # NOTE: the main acquisition timer is deliberately left running —
        # watching the spectrometer respond to the script is the point.

    def _on_ext_serial_abort(self) -> None:
        runner = self._ext_serial_runner
        if runner is None or not runner.isRunning():
            return
        runner.abort()
        # Escalate to an async exception only if the script is still alive
        # after a grace period, so `finally:` cleanup isn't interrupted in
        # the normal wait()-dominated case.
        QTimer.singleShot(
            1500,
            lambda r=runner: r.force_abort() if r.isRunning() else None,
        )
        self._status_bar.showMessage("Aborting script…")

    def _on_ext_serial_script_started(self) -> None:
        if self._ext_serial_panel is not None:
            self._ext_serial_panel.on_script_started()
        self._log_ext_event("script_start")
        self._status_bar.showMessage("Serial script running…")

    def _on_ext_serial_script_finished(self, ok: bool, msg: str) -> None:
        if self._ext_serial_panel is not None:
            self._ext_serial_panel.on_script_finished()
        if ok:
            event = "script_end"
            self._status_bar.showMessage("Serial script finished")
        elif msg == "aborted by user":
            event = "script_abort"
            self._status_bar.showMessage(
                self._ext_state_message("Serial script aborted"))
        else:
            event = "script_error"
            if self._ext_serial_panel is not None:
                for ln in msg.splitlines():
                    self._ext_serial_panel.append_console(ln)
            self._status_bar.showMessage(
                self._ext_state_message("Serial script ended with error"))
        self._log_ext_event(event, detail=msg)

    def _on_ext_serial_thread_finished(self) -> None:
        if self._ext_serial_panel is not None:
            self._ext_serial_panel.on_script_finished()

    def _ext_state_message(self, prefix: str) -> str:
        """Append the last-known param state so nobody misses a stuck ON."""
        if self._last_ext_params:
            state = ", ".join(
                f"{k}={v:g}" for k, v in self._last_ext_params.items())
            return f"{prefix} — last state: {state}"
        return prefix

    def _on_ext_serial_info(self, text: str) -> None:
        if self._ext_serial_panel is not None:
            self._ext_serial_panel.append_console(f"· {text}")

    def _on_ext_serial_param(self, ts: float, name: str, value: float) -> None:
        self._last_ext_params[name] = value
        if self._ext_serial_buffer is not None:
            self._ext_serial_buffer.append(ts, name, value)
        self._update_ext_serial_plot()
        self._log_ext_event("param", name=name, value=value, ts=ts)
        if (self._recorder.is_recording
                and name not in self._ext_extra_cols
                and name not in self._ext_missing_warned):
            self._ext_missing_warned.add(name)
            self._status_bar.showMessage(
                f"param '{name}' not in record header — kept in the "
                "_serial.txt log and plot only")

    def _on_ext_serial_park(self) -> None:
        """Emergency knob: PWM 0 + AD3 to 0 V, recorded as real state."""
        parked = []
        if (self._ext_serial_worker is not None
                and self._ext_serial_worker.isRunning()):
            try:
                self._ext_serial_worker.send_command("PWM 0")
                self._on_ext_serial_param(time.time(), "pwm", 0.0)
                parked.append("PWM 0")
            except RuntimeError:
                pass
        if "ad3" in sys.modules and sys.modules["ad3"].is_open():
            try:
                sys.modules["ad3"].park()
                self._on_ext_serial_param(time.time(), "led_V", 0.0)
                parked.append("AD3 0 V")
            except Exception as exc:
                self._status_bar.showMessage(f"AD3 park failed: {exc}")
        if parked:
            self._log_ext_event("manual_command",
                                detail="park: " + ", ".join(parked))
            self._status_bar.showMessage("Outputs parked: " + ", ".join(parked))
        else:
            self._status_bar.showMessage(
                "Nothing to park (no serial connection, AD3 not in use)")

    # ---- Plot update ------------------------------------------------------

    def _ensure_ext_param_viewbox(self, name: str, color: str) -> pg.ViewBox:
        """One independent y-axis + ViewBox per param (BME plot pattern) —
        params differ wildly in scale, a shared axis flattens the small ones."""
        if name in self._ext_serial_vbs:
            return self._ext_serial_vbs[name]
        main_vb = self._ext_serial_plot.getViewBox()
        if not self._ext_serial_vbs:
            # First param uses the built-in left axis and main ViewBox
            self._ext_serial_plot.setLabel("left", name, color=color)
            self._ext_serial_axes[name] = self._ext_serial_plot.getAxis("left")
            self._ext_serial_vbs[name] = main_vb
            return main_vb
        ax = pg.AxisItem("right")
        ax.setLabel(name, color=color)
        vb = pg.ViewBox()
        self._ext_serial_plot.scene().addItem(vb)
        ax.linkToView(vb)
        vb.setXLink(main_vb)
        col = 2 + len(self._ext_serial_vbs)   # first right axis lands at col 3
        self._ext_serial_plot.plotItem.layout.addItem(ax, 2, col)
        self._ext_serial_axes[name] = ax
        self._ext_serial_vbs[name] = vb
        return vb

    def _sync_ext_viewboxes(self) -> None:
        """Keep overlay ViewBoxes geometry in sync with the main ViewBox."""
        if self._ext_serial_plot is None:
            return
        main_vb = self._ext_serial_plot.getViewBox()
        rect = main_vb.sceneBoundingRect()
        if rect.width() == 0 or rect.height() == 0:
            return
        for vb in self._ext_serial_vbs.values():
            if vb is not main_vb:
                vb.setGeometry(rect)

    def _update_ext_serial_plot(self) -> None:
        if (self._ext_serial_buffer is None
                or self._ext_serial_plot is None
                or len(self._ext_serial_buffer) == 0):
            return

        t0 = self._ext_serial_buffer.t0()
        if self._ext_serial_time_axis is not None:
            self._ext_serial_time_axis.set_t0(t0)
        now = time.time()

        for i, name in enumerate(self._ext_serial_buffer.names()):
            t, v = self._ext_serial_buffer.series(name)
            if len(t) == 0:
                continue
            if name not in self._ext_serial_curves:
                color = EXT_SERIAL_COLORS[i % len(EXT_SERIAL_COLORS)]
                vb = self._ensure_ext_param_viewbox(name, color)
                pen = pg.mkPen(color=color, width=1.5)
                curve = pg.PlotDataItem(pen=pen, name=name, stepMode="right")
                vb.addItem(curve)
                self._ext_serial_legend.addItem(curve, name)
                self._ext_serial_curves[name] = curve
                self._connect_legend_toggle(self._ext_serial_legend, curve)
            # Hold each level to "now" with a synthetic end point (never
            # stored in the buffer) so the step curve reads correctly.
            x = np.append(t, now) - t0
            y = np.append(v, v[-1])
            self._ext_serial_curves[name].setData(x, y)

        # Overlay ViewBoxes need their geometry set on first data / resize
        self._sync_ext_viewboxes()

        if self._auto_range:
            for vb in self._ext_serial_vbs.values():
                vb.enableAutoRange()

    # ---- Recorder helpers --------------------------------------------------

    def _log_ext_event(self, event: str, name: str = "", value=None,
                       detail: str = "", ts: float | None = None) -> None:
        """Sidecar event log; mirrors the main recording (pyro convention).
        Outside a recording, events reach the console only."""
        if not self._recorder.is_recording:
            return
        if (self._ext_serial_recorder is None
                or not self._ext_serial_recorder.is_recording):
            self._open_ext_serial_recorder_lazy()
        if (self._ext_serial_recorder is None
                or not self._ext_serial_recorder.is_recording):
            return
        stamp = datetime.fromtimestamp(
            ts if ts is not None else time.time()
        ).isoformat(timespec="milliseconds")
        self._ext_serial_recorder.log_event(
            stamp, event, name=name, value=value, detail=detail)

    def _open_ext_serial_recorder_lazy(self) -> None:
        from ext_serial_recorder import ExtSerialRecorder
        if self._ext_serial_recorder is None:
            self._ext_serial_recorder = ExtSerialRecorder(self._base_dir / "data")
        if self._ext_serial_recorder.is_recording:
            return
        try:
            filename = self._filename_edit.text().strip() or "DATA"
            path = self._ext_serial_recorder.start_recording(filename=filename)
            self._status_bar.showMessage(f"Serial event log → {path.name}")
        except OSError as exc:
            self._status_bar.showMessage(f"Serial event log failed: {exc}")
            return
        # Opening snapshot: current param state, so a recording started
        # mid-script has full context from its first row.
        now = datetime.fromtimestamp(time.time()).isoformat(
            timespec="milliseconds")
        for pname, pval in self._last_ext_params.items():
            self._ext_serial_recorder.log_event(
                now, "param", name=pname, value=pval,
                detail="snapshot at record start")

    # ------------------------------------------------------------------
    # Close
    # ------------------------------------------------------------------

    def closeEvent(self, event):
        # Serial-script confirmation first — if the user cancels, nothing
        # may have been torn down yet.
        if (self._ext_serial_runner is not None
                and self._ext_serial_runner.isRunning()):
            resp = QMessageBox.question(
                self, "Script running",
                "A serial script is running — abort it and exit?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if resp != QMessageBox.Yes:
                event.ignore()
                return
            try:
                self._ext_serial_runner.abort()
                if not self._ext_serial_runner.wait(1500):
                    # Still alive: escalate, but NEVER terminate() exec'd
                    # Python — if it survives this too, keep the thread
                    # object referenced and let process teardown handle it.
                    self._ext_serial_runner.force_abort()
                    self._ext_serial_runner.wait(1500)
            except Exception:
                pass
        if (self._port_probe_thread is not None
                and self._port_probe_thread.isRunning()):
            # Probe sweeps are short (~1 s/port); let the current one finish.
            self._port_probe_thread.wait(2000)
        if (self._ext_console_runner is not None
                and self._ext_console_runner.isRunning()):
            try:
                self._ext_console_runner.abort()
                self._ext_console_runner.wait(500)
            except Exception:
                pass
        if self._recorder.is_recording:
            self._recorder.stop_recording()
        if self._li_runner is not None:
            try:
                self._li_runner.abort()
            except Exception:
                pass
        if self._li_discovery is not None:
            try:
                self._li_discovery.stop()
            except Exception:
                pass
        if self._li_worker is not None and self._li_worker.isRunning():
            try:
                self._li_worker.close_connection()
            except Exception:
                pass
        if self._li_recorder is not None and self._li_recorder.is_recording:
            self._li_recorder.stop_recording()
        if self._pyro_worker is not None and self._pyro_worker.isRunning():
            try:
                self._pyro_worker.close_port()
            except Exception:
                pass
        if self._pyro_recorder is not None and self._pyro_recorder.is_recording:
            self._pyro_recorder.stop_recording()
        if (self._ext_serial_worker is not None
                and self._ext_serial_worker.isRunning()):
            try:
                self._ext_serial_worker.close_port()
            except Exception:
                pass
        if (self._ext_serial_recorder is not None
                and self._ext_serial_recorder.is_recording):
            self._ext_serial_recorder.stop_recording()
        if self._ext_serial_panel is not None:
            # Persist the script text and connection settings.
            self._gui_cfg["ext_serial_script"] = \
                self._ext_serial_panel.script_text()
            port = self._ext_serial_panel.current_port()
            if port:
                self._gui_cfg["ext_serial_port"] = port
            self._gui_cfg["ext_serial_baud"] = \
                self._ext_serial_panel.current_baud()
            self._save_gui_config()
        if "ad3" in sys.modules:
            # Park the LED at 0 V and release the AD3. Never import ad3
            # here — only act if a script already loaded it.
            try:
                sys.modules["ad3"].close()
            except Exception:
                pass
        if self._worker and self._worker.isRunning():
            self._worker.close_port()
        super().closeEvent(event)
