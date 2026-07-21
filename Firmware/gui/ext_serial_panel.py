"""
ext_serial_panel.py — Left-panel widget for the Serial Scripting feature.

Hard import rule (same as pyro_panel): this module must NOT import serial,
the worker, the script runner, or ad3. MainWindow owns those; the panel only
emits request signals and receives state callbacks.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

import device_manager

BAUD_RATES = ["9600", "19200", "38400", "57600", "115200",
              "230400", "460800", "921600"]

DEFAULT_SCRIPT = """\
# Serial script — helpers:
#   send(cmd)            raw serial command
#   wait(s)              abort-interruptible sleep (time.sleep works too)
#   param(name, value)   record/plot a named value
#   pwm(v)               send('PWM v') + param('pwm', v)
#   dc_offset(V)         AD3 LED drive, clamped 0..0.3 V + param('led_V', V)
# Abort stops wait() immediately; `finally:` still runs — park outputs there.
try:
    for led_V in [0, 0.05, 0.1, 0.15]:
        dc_offset(led_V)
        for rep in range(5):
            pwm(2)
            wait(60)
            pwm(0)
            wait(5)
finally:
    pwm(0)
    dc_offset(0)
"""

CONSOLE_DRAIN_MS = 150       # batch console appends to survive reply floods
CONSOLE_MAX_BLOCKS = 2000


class _ScriptEdit(QPlainTextEdit):
    """Plain-text editor that inserts 4 spaces on Tab (avoids TabError)."""

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key_Tab and not ev.modifiers():
            self.insertPlainText("    ")
            return
        super().keyPressEvent(ev)


class ExtSerialPanel(QGroupBox):
    connect_requested    = Signal(str, int)   # port, baud
    disconnect_requested = Signal()
    send_requested       = Signal(str)        # manual one-off command
    run_requested        = Signal(str)        # full script text snapshot
    abort_requested      = Signal()
    park_requested       = Signal()

    def __init__(self, default_script_dir: Path | str = "scripts",
                 busy_ports_provider=None, parent=None):
        super().__init__("Serial Scripting", parent)
        self._default_script_dir = Path(default_script_dir)
        self._busy_ports_provider = busy_ports_provider
        self._connected = False
        self._script_running = False
        self._console_pending: list[str] = []
        self._console_timer = QTimer(self)
        self._console_timer.setInterval(CONSOLE_DRAIN_MS)
        self._console_timer.timeout.connect(self._drain_console)
        self._build_ui()
        self._refresh_ports()
        self._refresh_enabled()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)
        mono = QFontDatabase.systemFont(QFontDatabase.FixedFont)

        # --- Connection ---
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

        self._baud_combo = QComboBox()
        self._baud_combo.setEditable(True)
        for b in BAUD_RATES:
            self._baud_combo.addItem(b)
        self._baud_combo.setCurrentText("115200")

        self._connect_btn = QPushButton("Connect")
        self._connect_btn.clicked.connect(self._on_connect_clicked)

        self._status_lbl = QLabel("Not connected")
        self._status_lbl.setWordWrap(True)
        self._status_lbl.setStyleSheet("color: #cdd6f4;")

        form.addRow("Port:", port_row)
        form.addRow("Baud:", self._baud_combo)
        form.addRow(self._connect_btn)
        form.addRow(self._status_lbl)
        layout.addWidget(conn_grp)

        # --- Console ---
        console_grp = QGroupBox("Console")
        cv = QVBoxLayout(console_grp)
        cv.setContentsMargins(6, 6, 6, 6)
        cv.setSpacing(4)

        self._console_view = QPlainTextEdit()
        self._console_view.setReadOnly(True)
        self._console_view.setMaximumBlockCount(CONSOLE_MAX_BLOCKS)
        self._console_view.setFont(mono)
        self._console_view.setFixedHeight(110)
        cv.addWidget(self._console_view)

        cmd_row = QWidget()
        cmd_h = QHBoxLayout(cmd_row)
        cmd_h.setContentsMargins(0, 0, 0, 0)
        self._cmd_edit = QLineEdit()
        self._cmd_edit.setPlaceholderText("PWM 2  |  dc_offset(0.05)  |  pwm(10)")
        self._cmd_edit.setToolTip(
            "Raw serial command, or a script helper call —\n"
            "send(cmd), pwm(v), dc_offset(V), param(name, value)")
        self._cmd_edit.returnPressed.connect(self._on_send_clicked)
        self._send_btn = QPushButton("Send")
        self._send_btn.clicked.connect(self._on_send_clicked)
        cmd_h.addWidget(self._cmd_edit, stretch=1)
        cmd_h.addWidget(self._send_btn)
        cv.addWidget(cmd_row)
        layout.addWidget(console_grp)

        # --- Script ---
        script_grp = QGroupBox("Script")
        sv = QVBoxLayout(script_grp)
        sv.setContentsMargins(6, 6, 6, 6)
        sv.setSpacing(4)

        self._editor = _ScriptEdit()
        self._editor.setFont(mono)
        fm = self._editor.fontMetrics()
        self._editor.setTabStopDistance(4 * fm.horizontalAdvance(" "))
        self._editor.setMinimumHeight(140)
        self._editor.setPlainText(DEFAULT_SCRIPT)
        sv.addWidget(self._editor)

        btn_row = QWidget()
        btn_h = QHBoxLayout(btn_row)
        btn_h.setContentsMargins(0, 0, 0, 0)
        self._load_btn = QPushButton("Load…")
        self._load_btn.clicked.connect(self._on_load)
        self._save_btn = QPushButton("Save…")
        self._save_btn.clicked.connect(self._on_save)
        self._run_btn = QPushButton("Run ▶")
        self._run_btn.clicked.connect(self._on_run_clicked)
        self._abort_btn = QPushButton("Abort ■")
        self._abort_btn.clicked.connect(self.abort_requested)
        self._park_btn = QPushButton("Park")
        self._park_btn.setToolTip("Emergency: send PWM 0 and drop the AD3 to 0 V")
        self._park_btn.clicked.connect(self.park_requested)
        btn_h.addWidget(self._load_btn)
        btn_h.addWidget(self._save_btn)
        btn_h.addStretch()
        btn_h.addWidget(self._run_btn)
        btn_h.addWidget(self._abort_btn)
        btn_h.addWidget(self._park_btn)
        sv.addWidget(btn_row)
        layout.addWidget(script_grp)

    # ------------------------------------------------------------------
    # Ports
    # ------------------------------------------------------------------

    def _refresh_ports(self) -> None:
        current = self.current_port()
        self._port_combo.clear()
        busy: dict[str, str] = {}
        if self._busy_ports_provider is not None:
            try:
                busy = dict(self._busy_ports_provider())
            except Exception:
                busy = {}
        try:
            infos = device_manager.list_ports_info()
        except AttributeError:
            infos = [(p, "") for p in device_manager.list_ports()]
        added = 0
        for dev, desc in infos:
            if dev in busy:
                continue   # port owned by CO2Dot / Pyroscience
            label = f"{dev} — {desc}" if desc and desc != "n/a" else dev
            self._port_combo.addItem(label, dev)
            added += 1
        if added == 0:
            self._port_combo.addItem("(no ports found)", "")
        if current:
            idx = self._port_combo.findData(current)
            if idx >= 0:
                self._port_combo.setCurrentIndex(idx)

    def current_port(self) -> str:
        return self._port_combo.currentData() or ""

    def current_baud(self) -> int:
        try:
            return int(self._baud_combo.currentText().strip())
        except ValueError:
            return 115200

    def set_port_baud(self, port: str, baud: int) -> None:
        """Restore persisted/detected values; port is locked while connected."""
        if self._connected:
            return
        if port:
            idx = self._port_combo.findData(port)
            if idx < 0:
                self._port_combo.addItem(port, port)
                idx = self._port_combo.findData(port)
            self._port_combo.setCurrentIndex(idx)
        if baud:
            self._baud_combo.setCurrentText(str(int(baud)))

    # ------------------------------------------------------------------
    # Script text
    # ------------------------------------------------------------------

    def script_text(self) -> str:
        return self._editor.toPlainText()

    def set_script_text(self, text: str) -> None:
        self._editor.setPlainText(text)

    # ------------------------------------------------------------------
    # Button handlers
    # ------------------------------------------------------------------

    def _on_connect_clicked(self) -> None:
        if self._connected:
            self.disconnect_requested.emit()
            return
        port = self.current_port()
        if not port:
            return
        self.connect_requested.emit(port, self.current_baud())

    def _on_send_clicked(self) -> None:
        cmd = self._cmd_edit.text().strip()
        if not cmd:
            return
        self.send_requested.emit(cmd)
        self._cmd_edit.clear()

    def _on_run_clicked(self) -> None:
        self.run_requested.emit(self.script_text())

    def _on_load(self) -> None:
        try:
            self._default_script_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        path, _ = QFileDialog.getOpenFileName(
            self, "Load script", str(self._default_script_dir),
            "Python scripts (*.py);;All files (*)")
        if not path:
            return
        try:
            self._editor.setPlainText(Path(path).read_text(encoding="utf-8"))
        except OSError as exc:
            self.append_console(f"load failed: {exc}")

    def _on_save(self) -> None:
        try:
            self._default_script_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        path, _ = QFileDialog.getSaveFileName(
            self, "Save script", str(self._default_script_dir / "script.py"),
            "Python scripts (*.py);;All files (*)")
        if not path:
            return
        try:
            Path(path).write_text(self.script_text(), encoding="utf-8")
        except OSError as exc:
            self.append_console(f"save failed: {exc}")

    # ------------------------------------------------------------------
    # Console (batched appends)
    # ------------------------------------------------------------------

    def append_console(self, line: str) -> None:
        self._console_pending.append(line)
        if not self._console_timer.isActive():
            self._console_timer.start()

    def _drain_console(self) -> None:
        if self._console_pending:
            text = "\n".join(self._console_pending)
            self._console_pending.clear()
            self._console_view.appendPlainText(text)
        else:
            self._console_timer.stop()

    # ------------------------------------------------------------------
    # State callbacks from MainWindow
    # ------------------------------------------------------------------

    def on_connected(self, info: dict) -> None:
        self._connected = True
        self._connect_btn.setText("Disconnect")
        port = info.get("port", "")
        baud = info.get("baud", "")
        self._status_lbl.setText(f"Connected: {port} @ {baud}")
        self._status_lbl.setStyleSheet("color: #a6e3a1;")
        self._refresh_enabled()

    def on_disconnected(self) -> None:
        self._connected = False
        self._connect_btn.setText("Connect")
        self._status_lbl.setText("Not connected")
        self._status_lbl.setStyleSheet("color: #cdd6f4;")
        self._refresh_enabled()

    def on_error(self, msg: str) -> None:
        self._status_lbl.setText(msg)
        self._status_lbl.setStyleSheet("color: #f38ba8;")

    def on_script_started(self) -> None:
        self._script_running = True
        self._refresh_enabled()

    def on_script_finished(self) -> None:
        # Idempotent: also reached via the QThread.finished safety net.
        self._script_running = False
        self._refresh_enabled()

    def _refresh_enabled(self) -> None:
        for w in (self._port_combo, self._baud_combo, self._refresh_btn):
            w.setEnabled(not self._connected)
        # Console and Run need no connection: helper calls (dc_offset(...))
        # act on the PC/AD3 side; raw sends report 'not connected'.
        self._run_btn.setEnabled(not self._script_running)
        self._abort_btn.setEnabled(self._script_running)
        self._connect_btn.setEnabled(not self._script_running)
