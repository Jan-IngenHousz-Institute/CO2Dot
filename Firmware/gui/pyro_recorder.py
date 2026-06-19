"""pyro_recorder.py — TSV recorder for Pyroscience samples.

Writes a parallel `<stem>_pyro.txt` next to the main CO2Dot recording, with
columns aligned with pyroscience_logger.ipynb's logger output (timestamp,
channel, status, dphi, umolar, mbar, airSat, tempSample, tempCase,
signalIntensity, ambientLight, pressure, humidity, resistorTemp, percentO2,
tempOptical, pH, ldev) plus an `idnr` column for the device serial number.
"""

from datetime import datetime
from pathlib import Path

import pyro_protocol


class PyroRecorder:
    def __init__(self, data_dir: str | Path = "data"):
        self._data_dir = Path(data_dir)
        self._file = None
        self._idnr = ""
        self._recording = False

    @property
    def is_recording(self) -> bool:
        return self._recording

    def start_recording(
        self,
        filename: str,
        idnr: str = "",
        channel: int = 1,
    ) -> Path:
        self._data_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now()
        safe_name = filename.strip() or "DATA"
        stem = now.strftime("%Y-%m-%d_%H-%M-%S") + "_" + safe_name + "_pyro"
        path = self._data_dir / (stem + ".txt")

        self._idnr = str(idnr)
        try:
            self._file = open(path, "w", encoding="utf-8", newline="\n")
            cols = ["timestamp", "channel", *pyro_protocol.FIELDS, "idnr"]
            self._file.write("\t".join(cols) + "\n")
            self._file.flush()
        except OSError:
            if self._file is not None:
                self._file.close()
                self._file = None
            raise

        self._recording = True
        return path

    def write_row(self, timestamp: str, channel: int, sample: dict) -> None:
        if not self._recording or self._file is None:
            return
        vals = [str(sample.get(f, "")) for f in pyro_protocol.FIELDS]
        row = [timestamp, str(channel), *vals, self._idnr]
        self._file.write("\t".join(row) + "\n")
        self._file.flush()

    def stop_recording(self) -> None:
        if self._file is not None:
            self._file.flush()
            self._file.close()
            self._file = None
        self._recording = False
        self._idnr = ""
