"""
li_recorder.py — TSV writer for Li-Control combined measurements.

Writes one row per LiCor ACK with the LiCor gas-exchange values, the requested
setpoints, and a snapshot of the embedded spec+BME (or empty columns if the
spectrometer is not connected).

Files land in the same data/ directory as the normal Recorder, with filenames
suffixed "_licontrol.txt" to distinguish them from spectroscopy-only sessions.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Iterable

_LI_ACK_COLS = (
    "CO2_r", "CO2_s", "H2O_r", "H2O_s",
    "Tchamber", "Tleaf", "RHcham", "PPFD_in",
)

_SETPOINT_COLS = ("co2_r_set", "tair_set", "rh_air_set", "qin_set")
_BME_COLS = ("T", "P", "RH", "Gas")
_META_COLS = ("model", "mode", "gain", "atime", "astep", "led")


class LiRecorder:
    def __init__(self, data_dir: str | Path = "data"):
        self._data_dir = Path(data_dir)
        self._file = None
        self._spec_channels: list[str] = []
        self._meta_vals: list[str] = []
        self._recording = False

    @property
    def is_recording(self) -> bool:
        return self._recording

    def start_recording(
        self,
        filename: str,
        model: str,
        mode: str,
        gain: int,
        atime: int,
        astep: int,
        led: int,
        spec_channels: Iterable[str],
    ) -> Path:
        self._data_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now()
        safe_name = filename.strip() or "DATA"
        stem = now.strftime("%Y-%m-%d_%H-%M-%S") + "_" + safe_name + "_licontrol"
        path = self._data_dir / (stem + ".txt")

        self._spec_channels = list(spec_channels)
        self._meta_vals = [model, mode, str(gain), str(atime), str(astep), str(led)]

        try:
            self._file = open(path, "w", encoding="utf-8", newline="\n")
            cols: list[str] = ["timestamp", "step_index", "step_name", "repeat_index"]
            cols.extend(_SETPOINT_COLS)
            cols.extend(_LI_ACK_COLS)
            cols.extend(self._spec_channels)
            cols.extend(_BME_COLS)
            cols.extend(_META_COLS)
            cols.append("notes")
            self._file.write("\t".join(cols) + "\n")
            self._file.flush()
        except OSError:
            if self._file is not None:
                self._file.close()
                self._file = None
            raise

        self._recording = True
        return path

    def write_row(
        self,
        *,
        step_index: int,
        step_name: str,
        setpoints: dict | None,
        ack: dict,
        spec: dict | None,
        bme: dict | None,
        notes: str = "",
        repeat_index: int = 0,
    ) -> None:
        if not self._recording or self._file is None:
            return

        timestamp = datetime.now().isoformat(timespec="milliseconds")
        setpoints = setpoints or {}
        spec = spec or {}
        bme = bme or {}

        setpoint_vals = [
            _fmt(setpoints.get("co2_r")),
            _fmt(setpoints.get("tair")),
            _fmt(setpoints.get("rh_air")),
            _fmt(setpoints.get("qin")),
        ]
        ack_vals = [_fmt(ack.get(k)) for k in _LI_ACK_COLS]
        spec_vals = [_fmt(spec.get(ch)) for ch in self._spec_channels]
        bme_vals = [
            _fmt(bme.get("T")),
            _fmt(bme.get("P")),
            _fmt(bme.get("RH")),
            _fmt(bme.get("Gas")),
        ]

        row: list[str] = [
            timestamp,
            str(step_index),
            step_name,
            str(repeat_index),
        ]
        row.extend(setpoint_vals)
        row.extend(ack_vals)
        row.extend(spec_vals)
        row.extend(bme_vals)
        row.extend(self._meta_vals)
        row.append(notes)

        self._file.write("\t".join(row) + "\n")
        self._file.flush()

    def stop_recording(self) -> None:
        if self._file is not None:
            try:
                self._file.flush()
                self._file.close()
            except OSError:
                pass
            self._file = None
        self._recording = False
        self._spec_channels = []
        self._meta_vals = []


def _fmt(value) -> str:
    if value is None:
        return ""
    return str(value)
