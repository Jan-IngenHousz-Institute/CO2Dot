"""
recorder.py — TSV file recorder for CO2Dot measurements.

File naming: data/YYYY-MM-DD_HH-MM-SS_<filename>.txt
Columns: timestamp, <spec channels...>, T, P, RH, Gas, model, mode, gain, atime, astep, led
"""

from datetime import datetime
from pathlib import Path


class Recorder:
    def __init__(self, data_dir: str | Path = "data"):
        self._data_dir = Path(data_dir)
        self._file = None
        self._spec_channels: list[str] = []
        self._meta_vals: list[str] = []
        self._extra_cols: list[str] = []
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
        spec_channels: list[str],
        extra_cols: list[str] | None = None,
    ) -> Path:
        """Open a new TSV file and write column headers.

        `extra_cols` (e.g. Serial-Scripting param names) are frozen for the
        whole recording and appended at the END of the header, so parsers of
        old files are unaffected. Row width never changes mid-file."""
        self._data_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now()
        safe_name = filename.strip() or "DATA"
        stem = now.strftime("%Y-%m-%d_%H-%M-%S") + "_" + safe_name
        path = self._data_dir / (stem + ".txt")

        self._spec_channels = list(spec_channels)
        self._meta_vals = [model, mode, str(gain), str(atime), str(astep), str(led)]
        self._extra_cols = [str(c) for c in (extra_cols or [])]
        try:
            self._file = open(path, "w", encoding="utf-8", newline="\n")
            base_cols = (
                ["timestamp"]
                + self._spec_channels
                + ["T", "P", "RH", "Gas"]
                + ["model", "mode", "gain", "atime", "astep", "led"]
            )
            # An extra column must not shadow an existing header name
            # (e.g. a script param called "T"); values are still looked up
            # by the original name in write_row.
            header_extra = [
                ("serial_" + c) if c in base_cols else c
                for c in self._extra_cols
            ]
            cols = base_cols + header_extra
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
        timestamp: str,
        spec: dict | None,
        bme: dict | None,
        extra: dict | None = None,
    ) -> None:
        """Append one TSV data row. Missing values are written as empty strings.

        Only the extra columns frozen at start_recording are emitted; other
        keys in `extra` are ignored so the row width stays constant."""
        if not self._recording or self._file is None:
            return

        spec = spec or {}
        bme = bme or {}
        extra = extra or {}

        spec_vals = [str(spec.get(ch, "")) for ch in self._spec_channels]
        bme_vals = [
            str(bme.get("T", "")),
            str(bme.get("P", "")),
            str(bme.get("RH", "")),
            str(bme.get("Gas", "")),
        ]
        extra_vals = [str(extra.get(c, "")) for c in self._extra_cols]
        row = [timestamp] + spec_vals + bme_vals + self._meta_vals + extra_vals
        self._file.write("\t".join(row) + "\n")
        self._file.flush()

    def stop_recording(self) -> None:
        if self._file is not None:
            self._file.flush()
            self._file.close()
            self._file = None
        self._recording = False
        self._spec_channels = []
        self._meta_vals = []
        self._extra_cols = []
