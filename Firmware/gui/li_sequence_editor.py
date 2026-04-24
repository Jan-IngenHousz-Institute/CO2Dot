"""
li_sequence_editor.py — Form-based builder for LI-Control sequences.

Two dialogs:
  - StepEditorDialog: edits a single SequenceStep via a form. Each optional
    setpoint field has a "Set …" checkbox that controls whether the value is
    emitted (unchecked → None, matching the sparse-command semantics expected
    by RemoteEnvMeasure.py).
  - SequenceEditorDialog: shows the current step list with Add / Edit /
    Delete / Up / Down, plus Load… / Save As… / Use.

Both dialogs import only li_control and li_sequence (no paramiko / zeroconf).
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from li_control import LiSetpoints
from li_sequence import (
    SequenceStep,
    load_sequence,
    save_sequence,
    validate_sequence,
)


# Optional numeric setpoints shown as "Set …" checkbox + spin box.
# (field name, label, min, max, decimals, default_when_set, suffix)
_OPTIONAL_SETPOINT_FIELDS: tuple[tuple, ...] = (
    ("co2_r",    "CO₂_r",    0.0,  2000.0, 0, 400.0, "ppm"),
    ("tair",     "Tair",     0.0,  50.0,   1, 25.0,  "°C"),
    ("rh_air",   "RH_air",   0.0,  100.0,  1, 60.0,  "%"),
    ("qin",      "Qin",      0.0,  3000.0, 0, 500.0, "μmol/m²/s"),
    ("flow",     "Flow",     0.0,  2000.0, 0, 500.0, ""),
    ("fan_rpm",  "Fan",      0.0, 20000.0, 0, 10000.0, "rpm"),
    ("pressure", "Pressure", 0.0,  10.0,   2, 0.1,   "kPa"),
)


def _step_summary(step: SequenceStep) -> str:
    sp = step.setpoints
    bits: list[str] = []
    if sp.co2_r is not None:
        bits.append(f"CO₂={sp.co2_r:g}")
    if sp.tair is not None:
        bits.append(f"T={sp.tair:g}")
    if sp.rh_air is not None:
        bits.append(f"RH={sp.rh_air:g}")
    if sp.qin is not None:
        bits.append(f"Q={sp.qin:g}")
    if sp.wait_for_co2:
        bits.append(f"wait_for_co2±{sp.co2_tol:g}")
    if sp.wait_s:
        bits.append(f"wait={sp.wait_s:g}s")
    if sp.log:
        bits.append("LOG")
    params = ", ".join(bits) if bits else "(no setpoints)"
    timeout = f"  [timeout={step.ack_timeout_s:g}s]" if step.ack_timeout_s else ""
    repeat = f"  ×{step.repeat}" if step.repeat and step.repeat > 1 else ""
    return f"{step.name} — {params}{timeout}{repeat}"


class StepEditorDialog(QDialog):
    """Edit a single SequenceStep."""

    def __init__(self, step: SequenceStep | None, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Edit step" if step else "Add step")
        self.resize(360, 520)
        self._result_step: SequenceStep | None = None
        self._optional_widgets: dict[str, tuple[QCheckBox, QDoubleSpinBox]] = {}
        self._build_ui()
        self._load_from_step(step or SequenceStep(name="new_step", setpoints=LiSetpoints()))

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        form = QFormLayout()
        layout.addLayout(form)

        self._name_edit = QLineEdit()
        form.addRow("Name:", self._name_edit)

        form.addRow(QLabel("<b>Setpoints</b> — uncheck to leave a field unset"))

        for name, label, mn, mx, dec, default, suffix in _OPTIONAL_SETPOINT_FIELDS:
            chk = QCheckBox(f"Set {label}")
            spin = QDoubleSpinBox()
            spin.setRange(mn, mx)
            spin.setDecimals(dec)
            spin.setValue(default)
            if suffix:
                spin.setSuffix(" " + suffix)
            chk.toggled.connect(spin.setEnabled)
            spin.setEnabled(False)
            row = QWidget()
            row_h = QHBoxLayout(row)
            row_h.setContentsMargins(0, 0, 0, 0)
            row_h.addWidget(chk)
            row_h.addWidget(spin, stretch=1)
            form.addRow(row)
            self._optional_widgets[name] = (chk, spin)

        # Wait / stability / log
        form.addRow(QLabel("<b>Stability & timing</b>"))
        self._wait_co2_chk = QCheckBox("wait_for_co2")
        form.addRow(self._wait_co2_chk)

        self._co2_tol_spin = QDoubleSpinBox()
        self._co2_tol_spin.setRange(0.0, 100.0)
        self._co2_tol_spin.setDecimals(1)
        self._co2_tol_spin.setValue(2.0)
        self._co2_tol_spin.setSuffix(" ppm")
        form.addRow("CO₂ tol:", self._co2_tol_spin)

        self._wait_s_spin = QDoubleSpinBox()
        self._wait_s_spin.setRange(0.0, 3600.0)
        self._wait_s_spin.setDecimals(0)
        self._wait_s_spin.setSuffix(" s")
        form.addRow("wait_s:", self._wait_s_spin)

        self._log_chk = QCheckBox("LI-6800 LOG()")
        form.addRow(self._log_chk)

        # Step-level options
        form.addRow(QLabel("<b>Step options</b>"))
        self._ack_timeout_chk = QCheckBox("override ack_timeout_s")
        self._ack_timeout_spin = QDoubleSpinBox()
        self._ack_timeout_spin.setRange(1.0, 36000.0)
        self._ack_timeout_spin.setDecimals(0)
        self._ack_timeout_spin.setValue(600.0)
        self._ack_timeout_spin.setSuffix(" s")
        self._ack_timeout_spin.setEnabled(False)
        self._ack_timeout_chk.toggled.connect(self._ack_timeout_spin.setEnabled)
        ack_row = QWidget()
        ack_h = QHBoxLayout(ack_row)
        ack_h.setContentsMargins(0, 0, 0, 0)
        ack_h.addWidget(self._ack_timeout_chk)
        ack_h.addWidget(self._ack_timeout_spin, stretch=1)
        form.addRow(ack_row)

        self._post_wait_spin = QDoubleSpinBox()
        self._post_wait_spin.setRange(0.0, 3600.0)
        self._post_wait_spin.setDecimals(0)
        self._post_wait_spin.setSuffix(" s")
        form.addRow("post_wait_s:", self._post_wait_spin)

        self._repeat_spin = QSpinBox()
        self._repeat_spin.setRange(1, 9999)
        self._repeat_spin.setValue(1)
        self._repeat_spin.setToolTip(
            "Number of times this step is executed back-to-back. "
            "Each repetition produces its own row in the TSV "
            "(see repeat_index column)."
        )
        form.addRow("Repeat:", self._repeat_spin)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _load_from_step(self, step: SequenceStep) -> None:
        self._name_edit.setText(step.name)
        sp = step.setpoints
        for name, (chk, spin) in self._optional_widgets.items():
            val = getattr(sp, name)
            if val is None:
                chk.setChecked(False)
            else:
                chk.setChecked(True)
                spin.setValue(float(val))
        self._wait_co2_chk.setChecked(bool(sp.wait_for_co2))
        self._co2_tol_spin.setValue(float(sp.co2_tol))
        self._wait_s_spin.setValue(float(sp.wait_s))
        self._log_chk.setChecked(bool(sp.log))
        if step.ack_timeout_s is not None:
            self._ack_timeout_chk.setChecked(True)
            self._ack_timeout_spin.setValue(float(step.ack_timeout_s))
        else:
            self._ack_timeout_chk.setChecked(False)
        self._post_wait_spin.setValue(float(step.post_wait_s))
        self._repeat_spin.setValue(max(1, int(step.repeat or 1)))

    def _on_accept(self) -> None:
        name = self._name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "Missing name", "Step name is required.")
            return
        kwargs: dict = {
            "wait_for_co2": self._wait_co2_chk.isChecked(),
            "co2_tol": self._co2_tol_spin.value(),
            "wait_s": self._wait_s_spin.value(),
            "log": self._log_chk.isChecked(),
        }
        for field_name, (chk, spin) in self._optional_widgets.items():
            if chk.isChecked():
                kwargs[field_name] = spin.value()
        sp = LiSetpoints(**kwargs)
        self._result_step = SequenceStep(
            name=name,
            setpoints=sp,
            ack_timeout_s=(
                self._ack_timeout_spin.value()
                if self._ack_timeout_chk.isChecked() else None
            ),
            post_wait_s=self._post_wait_spin.value(),
            repeat=int(self._repeat_spin.value()),
        )
        self.accept()

    def result_step(self) -> SequenceStep | None:
        return self._result_step


class SequenceEditorDialog(QDialog):
    """Build/edit a list of SequenceSteps."""

    def __init__(self, steps: list[SequenceStep] | None = None,
                 default_dir: Path | None = None,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Li-Control — Sequence Builder")
        self.resize(560, 520)
        self._steps: list[SequenceStep] = [replace(s) for s in (steps or [])]
        self._default_dir = Path(default_dir) if default_dir else Path.cwd()
        self._last_path: Path | None = None
        self._build_ui()
        self._refresh_list()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Sequence name:"))
        self._name_edit = QLineEdit("new_sequence")
        name_row.addWidget(self._name_edit, stretch=1)
        layout.addLayout(name_row)

        body = QHBoxLayout()
        layout.addLayout(body, stretch=1)

        self._list = QListWidget()
        self._list.itemDoubleClicked.connect(lambda _i: self._edit_selected())
        body.addWidget(self._list, stretch=1)

        col = QVBoxLayout()
        body.addLayout(col)
        self._add_btn = QPushButton("Add…")
        self._edit_btn = QPushButton("Edit…")
        self._dup_btn = QPushButton("Duplicate")
        self._del_btn = QPushButton("Delete")
        self._up_btn = QPushButton("↑ Up")
        self._down_btn = QPushButton("↓ Down")
        for btn, handler in (
            (self._add_btn, self._add_step),
            (self._edit_btn, self._edit_selected),
            (self._dup_btn, self._duplicate_selected),
            (self._del_btn, self._delete_selected),
            (self._up_btn, lambda: self._move_selected(-1)),
            (self._down_btn, lambda: self._move_selected(+1)),
        ):
            btn.clicked.connect(handler)
            col.addWidget(btn)
        col.addStretch()

        io_row = QHBoxLayout()
        layout.addLayout(io_row)
        self._load_btn = QPushButton("Load…")
        self._load_btn.clicked.connect(self._load_from_file)
        self._save_as_btn = QPushButton("Save As…")
        self._save_as_btn.clicked.connect(self._save_as)
        io_row.addWidget(self._load_btn)
        io_row.addWidget(self._save_as_btn)
        io_row.addStretch()

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Use this sequence")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # ------------------------------------------------------------------
    # List management
    # ------------------------------------------------------------------

    def _refresh_list(self, select_row: int | None = None) -> None:
        self._list.clear()
        for i, step in enumerate(self._steps):
            item = QListWidgetItem(f"{i+1}. {_step_summary(step)}")
            self._list.addItem(item)
        if select_row is not None and 0 <= select_row < len(self._steps):
            self._list.setCurrentRow(select_row)

    def _current_row(self) -> int:
        return self._list.currentRow()

    def _add_step(self) -> None:
        dlg = StepEditorDialog(None, self)
        if dlg.exec() == QDialog.Accepted:
            step = dlg.result_step()
            if step is not None:
                self._steps.append(step)
                self._refresh_list(select_row=len(self._steps) - 1)

    def _edit_selected(self) -> None:
        row = self._current_row()
        if row < 0:
            return
        dlg = StepEditorDialog(self._steps[row], self)
        if dlg.exec() == QDialog.Accepted:
            step = dlg.result_step()
            if step is not None:
                self._steps[row] = step
                self._refresh_list(select_row=row)

    def _duplicate_selected(self) -> None:
        row = self._current_row()
        if row < 0:
            return
        src = self._steps[row]
        copy = SequenceStep(
            name=src.name + "_copy",
            setpoints=replace(src.setpoints),
            ack_timeout_s=src.ack_timeout_s,
            post_wait_s=src.post_wait_s,
            repeat=src.repeat,
        )
        self._steps.insert(row + 1, copy)
        self._refresh_list(select_row=row + 1)

    def _delete_selected(self) -> None:
        row = self._current_row()
        if row < 0:
            return
        del self._steps[row]
        self._refresh_list(select_row=min(row, len(self._steps) - 1))

    def _move_selected(self, delta: int) -> None:
        row = self._current_row()
        new_row = row + delta
        if row < 0 or not (0 <= new_row < len(self._steps)):
            return
        self._steps[row], self._steps[new_row] = self._steps[new_row], self._steps[row]
        self._refresh_list(select_row=new_row)

    # ------------------------------------------------------------------
    # Load / Save
    # ------------------------------------------------------------------

    def _load_from_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Load sequence", str(self._default_dir), "JSON files (*.json)"
        )
        if not path:
            return
        try:
            loaded = load_sequence(path)
        except ValueError as exc:
            QMessageBox.critical(self, "Load failed", str(exc))
            return
        self._steps = loaded
        self._last_path = Path(path)
        self._name_edit.setText(self._last_path.stem)
        self._refresh_list(select_row=0 if loaded else None)

    def _save_as(self) -> None:
        if not self._steps:
            QMessageBox.information(self, "Nothing to save", "Add at least one step first.")
            return
        errs = self._validate_current()
        if errs:
            QMessageBox.critical(
                self, "Invalid sequence", "Cannot save:\n\n- " + "\n- ".join(errs)
            )
            return
        start_dir = str(self._last_path.parent if self._last_path else self._default_dir)
        suggested = self._name_edit.text().strip() or "sequence"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save sequence as",
            str(Path(start_dir) / f"{suggested}.json"),
            "JSON files (*.json)",
        )
        if not path:
            return
        try:
            written = save_sequence(
                self._steps, path, name=self._name_edit.text().strip() or Path(path).stem
            )
        except OSError as exc:
            QMessageBox.critical(self, "Save failed", str(exc))
            return
        self._last_path = written

    # ------------------------------------------------------------------
    # Accept path
    # ------------------------------------------------------------------

    def _validate_current(self) -> list[str]:
        # Round-trip through step_to_dict + validate_sequence to catch the
        # same error classes the runtime loader would.
        from li_sequence import step_to_dict
        raw = {
            "name": self._name_edit.text().strip(),
            "steps": [step_to_dict(s) for s in self._steps],
        }
        return validate_sequence(raw)

    def _on_accept(self) -> None:
        if not self._steps:
            QMessageBox.information(self, "Empty sequence", "Add at least one step.")
            return
        errs = self._validate_current()
        if errs:
            QMessageBox.critical(
                self, "Invalid sequence", "Fix these before using:\n\n- " + "\n- ".join(errs)
            )
            return
        self.accept()

    def result_steps(self) -> list[SequenceStep]:
        return list(self._steps)

    def result_name(self) -> str:
        return self._name_edit.text().strip()
