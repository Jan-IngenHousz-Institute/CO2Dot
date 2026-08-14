"""ext_serial_recorder.py — TSV event log sidecar for Serial Scripting.

Writes `<stamp>_<name>_serial.txt` next to the main recording (pyro
pattern): one row per event (command, reply, param change, script
lifecycle). This is the lossless record of what a script did; the main data
file's extra param columns are a projection of it frozen at record start.

Events: connect, disconnect, command, reply, manual_command, param,
script_start, script_end, script_abort, script_error.
"""

from datetime import datetime
from pathlib import Path


class ExtSerialRecorder:
    def __init__(self, data_dir: str | Path = "data"):
        self._data_dir = Path(data_dir)
        self._file = None
        self._recording = False

    @property
    def is_recording(self) -> bool:
        return self._recording

    @property
    def data_dir(self) -> Path:
        return self._data_dir

    @data_dir.setter
    def data_dir(self, value: str | Path) -> None:
        """Ignored mid-recording — see Recorder.data_dir."""
        if self._recording:
            return
        self._data_dir = Path(value)

    def start_recording(self, filename: str) -> Path:
        self._data_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now()
        safe_name = filename.strip() or "DATA"
        stem = now.strftime("%Y-%m-%d_%H-%M-%S") + "_" + safe_name + "_serial"
        path = self._data_dir / (stem + ".txt")
        try:
            self._file = open(path, "w", encoding="utf-8", newline="\n")
            cols = ["timestamp", "event", "name", "value", "detail"]
            self._file.write("\t".join(cols) + "\n")
            self._file.flush()
        except OSError:
            if self._file is not None:
                self._file.close()
                self._file = None
            raise
        self._recording = True
        return path

    @staticmethod
    def _sanitize(text) -> str:
        """Keep the TSV rectangular: no tabs/newlines inside a cell."""
        return (str(text).replace("\t", " ").replace("\r", "")
                .replace("\n", " | "))

    def log_event(self, timestamp: str, event: str, name: str = "",
                  value=None, detail: str = "") -> None:
        if not self._recording or self._file is None:
            return
        row = [
            timestamp,
            event,
            self._sanitize(name),
            "" if value is None else str(value),
            self._sanitize(detail),
        ]
        self._file.write("\t".join(row) + "\n")
        self._file.flush()

    def stop_recording(self) -> None:
        if self._file is not None:
            self._file.flush()
            self._file.close()
            self._file = None
        self._recording = False
