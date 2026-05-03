"""
data_buffer.py — In-memory circular buffers for live plot data.
"""

from collections import deque
import numpy as np


class SpecBuffer:
    """Stores timestamped spectrometer channel readings."""

    def __init__(self, maxlen: int = 3600):
        self._maxlen = maxlen
        self._times: deque[float] = deque(maxlen=maxlen)
        self._channels: dict[str, deque[float]] = {}

    def append(self, timestamp: float, channels: dict[str, float]) -> None:
        self._times.append(timestamp)
        existing_len = len(self._times) - 1  # length before this sample
        for ch, val in channels.items():
            if ch not in self._channels:
                d = deque(maxlen=self._maxlen)
                d.extend([float("nan")] * existing_len)
                self._channels[ch] = d
            self._channels[ch].append(float(val))

    def times(self) -> np.ndarray:
        return np.array(self._times, dtype=np.float64)

    def channel(self, name: str) -> np.ndarray:
        if name not in self._channels:
            return np.array([], dtype=np.float64)
        return np.array(self._channels[name], dtype=np.float64)

    def channel_names(self) -> list[str]:
        return list(self._channels.keys())

    def clear(self) -> None:
        self._times.clear()
        self._channels.clear()

    def __len__(self) -> int:
        return len(self._times)


class BmeBuffer:
    """Stores timestamped BME68x readings (T, P, RH, Gas)."""

    FIELDS = ("T", "P", "RH", "Gas")

    def __init__(self, maxlen: int = 3600):
        self._maxlen = maxlen
        self._times: deque[float] = deque(maxlen=maxlen)
        self._data: dict[str, deque[float]] = {f: deque(maxlen=maxlen) for f in self.FIELDS}

    def append(self, timestamp: float, reading: dict) -> None:
        self._times.append(timestamp)
        for f in self.FIELDS:
            self._data[f].append(float(reading.get(f, float("nan"))))

    def times(self) -> np.ndarray:
        return np.array(self._times, dtype=np.float64)

    def field(self, name: str) -> np.ndarray:
        return np.array(self._data.get(name, []), dtype=np.float64)

    def clear(self) -> None:
        self._times.clear()
        for d in self._data.values():
            d.clear()

    def __len__(self) -> int:
        return len(self._times)
