"""pyro_buffer.py — Circular buffer for Pyroscience live plot."""

from collections import deque
import numpy as np

import pyro_protocol


class PyroBuffer:
    """Stores timestamped Pyroscience samples (16 fields)."""

    FIELDS = pyro_protocol.FIELDS

    def __init__(self, maxlen: int = 3600):
        self._maxlen = maxlen
        self._times: deque[float] = deque(maxlen=maxlen)
        self._data: dict[str, deque[float]] = {
            f: deque(maxlen=maxlen) for f in self.FIELDS
        }

    def append(self, timestamp: float, sample: dict) -> None:
        self._times.append(timestamp)
        for f in self.FIELDS:
            self._data[f].append(float(sample.get(f, float("nan"))))

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
