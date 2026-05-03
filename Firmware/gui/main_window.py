"""
main_window.py — QMainWindow for the CO2Dot controller GUI.
"""

import json
import sys
import time
from datetime import datetime
from pathlib import Path

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
        self.setWindowTitle("CO2Dot Controller")
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
        left_scroll.setMaximumWidth(340)

        # Right panel (plots + controls)
        right_panel = self._build_right_panel()

        root.addWidget(left_scroll)
        root.addWidget(right_panel, stretch=1)

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

        port_row = QWidget()
        port_row_h = QHBoxLayout(port_row)
        port_row_h.setContentsMargins(0, 0, 0, 0)
        port_row_h.addWidget(self._port_combo, stretch=1)
        port_row_h.addWidget(self._refresh_btn)

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
            self._status_bar.showMessage(f"Checking {port}…")
            if not device_manager.check_port(port):
                self._status_bar.showMessage(
                    f"{port}: no CO2Dot device found (no hello response)"
                )
                return
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

    def _on_connected(self, port: str):
        self._status_bar.showMessage(f"Connected: {port}")
        self._start_btn.setEnabled(True)
        self._apply_btn.setEnabled(True)

    def _on_disconnected(self):
        self._running = False
        self._acq_timer.stop()
        self._start_btn.setEnabled(False)
        self._stop_btn.setEnabled(False)
        self._apply_btn.setEnabled(False)
        self._connect_btn.setText("Connect")
        self._spec_status_lbl.setText("Spectrometer: —")
        self._bme_status_lbl.setText("BME: —")
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
            avail = bme_info.get("available", False)
            icon  = "✓" if avail else "✗"
            color = "green" if avail else "red"
            self._bme_status_lbl.setText(
                f'<span style="color:{color}">{icon} BME68x</span>'
            )

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
        # Send env first so BME data arrives before spec (fixes data alignment)
        self._worker.send_command(protocol.CMD_ENV)
        if self._mode_flash.isChecked():
            self._worker.send_command(protocol.cmd_spec_flash(self._led_spin.value()))
        else:
            self._worker.send_command(protocol.CMD_SPEC)

    def _on_start(self):
        if not self._worker:
            return
        interval_ms = INTERVALS[self._interval_combo.currentIndex()][1] * 1000
        self._running = True
        self._acq_timer.start(interval_ms)
        self._start_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._record_btn.setEnabled(True)
        self._status_bar.showMessage("Acquiring…")
        # Immediate first sample
        self._on_acquire_tick()

    def _on_stop(self):
        self._running = False
        self._acq_timer.stop()
        self._start_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        self._record_btn.setEnabled(False)
        if self._recorder.is_recording:
            self._on_record_stop()
        self._status_bar.showMessage("Stopped")

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

    # ------------------------------------------------------------------
    # Recording control
    # ------------------------------------------------------------------

    def _on_record_start(self):
        filename = self._filename_edit.text().strip() or "DATA"
        mode = "flash" if self._mode_flash.isChecked() else "ambient"
        path = self._recorder.start_recording(
            filename=filename,
            model=self._model,
            mode=mode,
            gain=self._gain,
            atime=self._atime_spin.value(),
            astep=self._astep_spin.value(),
            led=self._led_spin.value(),
            spec_channels=protocol.channels_for_model(self._model),
        )
        self._record_btn.setEnabled(False)
        self._stop_rec_btn.setEnabled(True)
        self._status_bar.showMessage(f"Recording → {path.name}")

    def _on_record_stop(self):
        self._recorder.stop_recording()
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

    def _on_x_fit(self):
        """Auto-fit horizontal axis only (keep vertical range)."""
        self._spec_plot.enableAutoRange(axis='x')
        for vb in self._bme_vbs.values():
            vb.enableAutoRange(axis='x')

    def _on_reset_view(self):
        """Reset to full auto-scale on both axes, re-enable live auto-range."""
        self._auto_range = True
        self._spec_plot.enableAutoRange()
        for vb in self._bme_vbs.values():
            vb.enableAutoRange()

    def _on_toggle_time_axis(self):
        """Toggle x-axis between elapsed seconds and HH:MM:SS."""
        self._show_timestamp = not self._show_timestamp
        if self._show_timestamp:
            self._time_toggle_btn.setText("HH:MM:SS")
            self._spec_plot.setLabel("bottom", "Timestamp")
            self._bme_plot.setLabel("bottom", "Timestamp")
        else:
            self._time_toggle_btn.setText("Time (s)")
            self._spec_plot.setLabel("bottom", "Time", units="s")
            self._bme_plot.setLabel("bottom", "Time", units="s")
        self._spec_time_axis.set_timestamp_mode(self._show_timestamp)
        self._bme_time_axis.set_timestamp_mode(self._show_timestamp)
        # Force redraw
        self._update_spec_plot()
        self._update_bme_plot()

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
    # Close
    # ------------------------------------------------------------------

    def closeEvent(self, event):
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
        if self._worker and self._worker.isRunning():
            self._worker.close_port()
        super().closeEvent(event)
