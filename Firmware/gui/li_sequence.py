"""
li_sequence.py — Sequence loader + runner for Li-Control programmed sequences.

A sequence is a JSON file with a list of steps. Each step carries a LiSetpoints
payload and optional wait/timeout values. The runner sends one command per step,
waits for the matching ACK, triggers a fresh spec+BME read on the existing
SerialWorker (if it's running), and writes one combined row to the LiRecorder.

Spec unavailability is non-fatal: rows are stamped notes="spec_unavailable" or
notes="spec_timeout" and the sequence keeps going. LI-6800 errors DO abort.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QObject, QTimer, Signal

from li_control import LI_SETPOINT_FIELDS, LiSetpoints
import protocol

if TYPE_CHECKING:
    from li_worker import LiWorker


@dataclass
class SequenceStep:
    name: str
    setpoints: LiSetpoints
    ack_timeout_s: float | None = None
    post_wait_s: float = 0.0
    repeat: int = 1   # number of times the step is executed back-to-back


def validate_sequence(raw: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(raw, dict):
        errors.append("top-level must be an object")
        return errors
    steps = raw.get("steps")
    if not isinstance(steps, list) or len(steps) == 0:
        errors.append("'steps' must be a non-empty list")
        return errors
    for i, step in enumerate(steps):
        prefix = f"step[{i}]"
        if not isinstance(step, dict):
            errors.append(f"{prefix}: must be an object")
            continue
        if not isinstance(step.get("name"), str) or not step["name"]:
            errors.append(f"{prefix}: missing 'name'")
        sp = step.get("setpoints")
        if not isinstance(sp, dict):
            errors.append(f"{prefix}: 'setpoints' must be an object")
        else:
            bad_keys = set(sp.keys()) - LI_SETPOINT_FIELDS
            if bad_keys:
                errors.append(
                    f"{prefix}: unknown setpoint keys {sorted(bad_keys)}"
                )
        if "ack_timeout_s" in step:
            try:
                if float(step["ack_timeout_s"]) <= 0:
                    errors.append(f"{prefix}: ack_timeout_s must be > 0")
            except (TypeError, ValueError):
                errors.append(f"{prefix}: ack_timeout_s must be numeric")
        if "post_wait_s" in step:
            try:
                if float(step["post_wait_s"]) < 0:
                    errors.append(f"{prefix}: post_wait_s must be >= 0")
            except (TypeError, ValueError):
                errors.append(f"{prefix}: post_wait_s must be numeric")
        if "repeat" in step:
            try:
                r = int(step["repeat"])
                if r < 1:
                    errors.append(f"{prefix}: repeat must be >= 1")
            except (TypeError, ValueError):
                errors.append(f"{prefix}: repeat must be an integer")
    return errors


def step_to_dict(step: SequenceStep) -> dict:
    """Inverse of the per-step parsing in load_sequence."""
    sp = step.setpoints
    sp_out: dict[str, Any] = {}
    for field_name in LI_SETPOINT_FIELDS:
        val = getattr(sp, field_name)
        if field_name in ("wait_for_co2", "log"):
            sp_out[field_name] = bool(val)
            continue
        if field_name in ("co2_tol", "wait_s"):
            # Always emit these so round-trips stay stable.
            sp_out[field_name] = float(val)
            continue
        if val is not None:
            sp_out[field_name] = float(val)
    out: dict[str, Any] = {"name": step.name, "setpoints": sp_out}
    if step.ack_timeout_s is not None:
        out["ack_timeout_s"] = float(step.ack_timeout_s)
    if step.post_wait_s:
        out["post_wait_s"] = float(step.post_wait_s)
    if step.repeat and step.repeat != 1:
        out["repeat"] = int(step.repeat)
    return out


def save_sequence(steps: list[SequenceStep], path: Path | str, *, name: str = "") -> Path:
    """Write steps back to JSON. Raises OSError on I/O failure."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "name": name or p.stem,
        "steps": [step_to_dict(s) for s in steps],
    }
    p.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return p


def load_sequence(path: Path | str) -> list[SequenceStep]:
    """Load a sequence JSON file. Raises ValueError on parse or validation failure."""
    p = Path(path)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {p.name}: {exc}") from exc
    except OSError as exc:
        raise ValueError(f"cannot read {p.name}: {exc}") from exc

    errs = validate_sequence(raw)
    if errs:
        raise ValueError("sequence validation failed:\n  - " + "\n  - ".join(errs))

    steps: list[SequenceStep] = []
    for entry in raw["steps"]:
        sp_dict = entry["setpoints"]
        sp = LiSetpoints(**{k: v for k, v in sp_dict.items() if k in LI_SETPOINT_FIELDS})
        steps.append(SequenceStep(
            name=entry["name"],
            setpoints=sp,
            ack_timeout_s=(float(entry["ack_timeout_s"]) if "ack_timeout_s" in entry else None),
            post_wait_s=float(entry.get("post_wait_s", 0.0)),
            repeat=int(entry.get("repeat", 1)),
        ))
    return steps


class SequenceRunner(QObject):
    step_started       = Signal(int, object)       # base step index, SequenceStep (fires once per step)
    repetition_started = Signal(int, int, int)     # step_idx, rep_idx, total_reps (fires every repetition)
    step_finished      = Signal(int, dict)         # base step index, ack
    finished           = Signal()
    aborted            = Signal(str)               # reason

    def __init__(self, li_worker: "LiWorker", main_window, parent: QObject | None = None):
        super().__init__(parent or main_window)
        self._li = li_worker
        self._main = main_window
        self._steps: list[SequenceStep] = []
        self._recorder = None
        self._idx = -1
        self._rep_idx = 0
        self._running = False
        self._aborting = False
        self._expected_cmd_id: str | None = None
        self._pending_ack: dict | None = None
        self._spec_slot = None
        self._spec_timer: QTimer | None = None
        self._spec_timeout_s = 5.0

        self._li.ack_received.connect(self._on_ack)
        self._li.error_received.connect(self._on_li_error)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, steps: list[SequenceStep], recorder) -> None:
        self._steps = list(steps)
        self._recorder = recorder
        self._idx = -1
        self._rep_idx = 0
        self._running = True
        self._aborting = False
        self._advance()

    def abort(self) -> None:
        if not self._running:
            return
        self._aborting = True
        try:
            self._li.send_stop()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # State machine
    # ------------------------------------------------------------------

    def _advance(self) -> None:
        if self._aborting:
            self._finish_aborted("user")
            return

        # Still inside a repeating step?
        if 0 <= self._idx < len(self._steps):
            cur = self._steps[self._idx]
            reps = max(1, int(cur.repeat or 1))
            if self._rep_idx + 1 < reps:
                self._rep_idx += 1
                self._start_current()
                return

        # Move on to the next base step.
        self._idx += 1
        self._rep_idx = 0
        if self._idx >= len(self._steps):
            self._running = False
            self.finished.emit()
            return
        step = self._steps[self._idx]
        self.step_started.emit(self._idx, step)
        self._start_current()

    def _start_current(self) -> None:
        step = self._steps[self._idx]
        reps = max(1, int(step.repeat or 1))
        self.repetition_started.emit(self._idx, self._rep_idx, reps)
        try:
            self._expected_cmd_id = self._li.send_setpoints(
                step.setpoints, timeout_s=step.ack_timeout_s
            )
        except Exception as exc:
            self._finish_aborted(f"send failed: {exc}")

    def _on_ack(self, ack: dict) -> None:
        if not self._running:
            return
        if ack.get("cmd_id") != self._expected_cmd_id:
            return
        self._expected_cmd_id = None
        if ack.get("error"):
            self._write_row_with(ack, spec=None, bme=None,
                                 notes=f"li_error:{ack['error']}")
            self._finish_aborted(ack["error"])
            return
        self._pending_ack = ack

        worker = getattr(self._main, "_worker", None)
        if worker is None or not worker.isRunning():
            self._write_row_with(ack, spec=None, bme=None, notes="spec_unavailable")
            self._after_row_written()
            return

        self._arm_spec_wait()
        try:
            worker.send_command(protocol.CMD_ENV)
            mode_flash = bool(getattr(self._main, "_mode_flash", None) and
                              self._main._mode_flash.isChecked())
            if mode_flash:
                led_ma = getattr(self._main, "_led_spin", None)
                led_val = led_ma.value() if led_ma is not None else 10
                worker.send_command(protocol.cmd_spec_flash(led_val))
            else:
                worker.send_command(protocol.CMD_SPEC)
        except Exception:
            self._disarm_spec_wait()
            self._write_row_with(ack, spec=None, bme=None, notes="spec_unavailable")
            self._after_row_written()

    def _on_spec_arrived(self, data: dict) -> None:
        self._disarm_spec_wait()
        ack = self._pending_ack
        if ack is None or not self._running:
            return
        channels = data.get("channels") if isinstance(data, dict) else None
        bme = getattr(self._main, "_last_bme", None)
        self._write_row_with(ack, spec=channels, bme=bme, notes="")
        self._after_row_written()

    def _on_spec_timeout(self) -> None:
        self._disarm_spec_wait()
        ack = self._pending_ack
        if ack is None or not self._running:
            return
        self._write_row_with(ack, spec=None, bme=None, notes="spec_timeout")
        self._after_row_written()

    def _after_row_written(self) -> None:
        ack = self._pending_ack or {}
        idx = self._idx
        self._pending_ack = None
        self.step_finished.emit(idx, ack)
        if self._aborting:
            self._finish_aborted("user")
            return
        step = self._steps[idx] if 0 <= idx < len(self._steps) else None
        delay_ms = int(step.post_wait_s * 1000) if step else 0
        QTimer.singleShot(delay_ms, self._advance)

    def _on_li_error(self, msg: str) -> None:
        if not self._running:
            return
        if self._expected_cmd_id is None and self._pending_ack is None:
            return
        self._disarm_spec_wait()
        self._finish_aborted(msg)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _arm_spec_wait(self) -> None:
        worker = self._main._worker
        self._spec_slot = self._on_spec_arrived
        worker.spec_received.connect(self._spec_slot)
        self._spec_timer = QTimer(self)
        self._spec_timer.setSingleShot(True)
        self._spec_timer.timeout.connect(self._on_spec_timeout)
        self._spec_timer.start(int(self._spec_timeout_s * 1000))

    def _disarm_spec_wait(self) -> None:
        if self._spec_slot is not None:
            try:
                self._main._worker.spec_received.disconnect(self._spec_slot)
            except (RuntimeError, TypeError):
                pass
            self._spec_slot = None
        if self._spec_timer is not None:
            try:
                self._spec_timer.stop()
            except Exception:
                pass
            self._spec_timer = None

    def _write_row_with(self, ack: dict, spec, bme, notes: str) -> None:
        if self._recorder is None:
            return
        step = self._steps[self._idx] if 0 <= self._idx < len(self._steps) else None
        step_name = step.name if step else ""
        setpoint_dict = {}
        if step is not None:
            setpoint_dict = {
                "co2_r": step.setpoints.co2_r,
                "tair": step.setpoints.tair,
                "rh_air": step.setpoints.rh_air,
                "qin": step.setpoints.qin,
            }
        self._recorder.write_row(
            step_index=self._idx,
            step_name=step_name,
            repeat_index=self._rep_idx,
            setpoints=setpoint_dict,
            ack=ack,
            spec=spec,
            bme=bme,
            notes=notes,
        )

    def _finish_aborted(self, reason: str) -> None:
        self._running = False
        self._disarm_spec_wait()
        self._expected_cmd_id = None
        self._pending_ack = None
        self.aborted.emit(reason)
