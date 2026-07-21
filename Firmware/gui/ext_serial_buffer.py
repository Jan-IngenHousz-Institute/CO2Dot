"""ext_serial_buffer.py — per-parameter event series for the script plot."""

from collections import deque

import numpy as np


class ParamBuffer:
    """Sparse per-name (times, values) event series.

    Unlike SpecBuffer, every parameter keeps its own timestamp series:
    script params change at unrelated instants, so a shared time base would
    desynchronize the columns. Events are sparse by design; maxlen bounds a
    runaway param loop, and the recorded files remain the source of truth.
    """

    def __init__(self, maxlen: int = 10000):
        self._maxlen = maxlen
        self._series: dict[str, tuple[deque, deque]] = {}

    def append(self, timestamp: float, name: str, value: float) -> None:
        if name not in self._series:
            self._series[name] = (
                deque(maxlen=self._maxlen),
                deque(maxlen=self._maxlen),
            )
        t, v = self._series[name]
        t.append(float(timestamp))
        v.append(float(value))

    def names(self) -> list[str]:
        """Parameter names in first-appearance order."""
        return list(self._series.keys())

    def series(self, name: str) -> tuple[np.ndarray, np.ndarray]:
        t, v = self._series.get(name, ((), ()))
        return (np.array(t, dtype=np.float64),
                np.array(v, dtype=np.float64))

    def t0(self) -> float:
        firsts = [t[0] for t, _ in self._series.values() if len(t)]
        return min(firsts) if firsts else 0.0

    def clear(self) -> None:
        self._series.clear()

    def __len__(self) -> int:
        return sum(len(t) for t, _ in self._series.values())
