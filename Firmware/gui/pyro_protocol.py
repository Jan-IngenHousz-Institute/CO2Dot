"""
pyro_protocol.py — Pyroscience O2 meter wire protocol (Qt-free).

Reference: pyroscience_logger.ipynb. Device speaks ASCII over USB-Serial at
115200 baud. Identification with #IDNR\\r; per-sample read with
"MEA <channel> 47\\r". Response is a space-delimited line where columns
3..18 (0-indexed) are the 16 measured floats.

The "47" parameter in the MEA command is a register/option mask copied
verbatim from the vendor notebook — the value is opaque to us.
"""

from __future__ import annotations


BAUD = 115200
CMD_IDNR = b"#IDNR\r"


def cmd_measure(channel: int) -> bytes:
    return f"MEA {channel} 47\r".encode("ascii")


# 16 fields parsed from sr[3:19] in the notebook
FIELDS: tuple[str, ...] = (
    "status", "dphi", "umolar", "mbar", "airSat", "tempSample",
    "tempCase", "signalIntensity", "ambientLight", "pressure",
    "humidity", "resistorTemp", "percentO2", "tempOptical", "pH", "ldev",
)

# Visible by default on the live plot; the rest are dimmed in the legend
DEFAULT_VISIBLE: tuple[str, ...] = ("dphi",)

# Fields the notebook scales by 1e-3 before logging
_SCALE_1E3 = frozenset((
    "dphi", "tempCase", "signalIntensity", "ambientLight", "pressure", "humidity",
))


def parse_meas_line(line: str) -> dict[str, float] | None:
    """Return a dict of named floats, or None for malformed lines."""
    parts = line.strip().split(" ")
    if len(parts) < 19:
        return None
    try:
        vals = [float(x) for x in parts[3:19]]
    except ValueError:
        return None
    out = dict(zip(FIELDS, vals))
    for k in _SCALE_1E3:
        out[k] = round(out[k] * 1e-3, 3)
    return out


def is_idnr_response(line: str) -> bool:
    return "#IDNR" in line


def extract_idnr(line: str) -> str:
    """Pull the serial number out of '#IDNR <serial>'; fall back to the line."""
    s = line.strip()
    parts = s.split()
    if len(parts) >= 2 and parts[0] == "#IDNR":
        return parts[1]
    return s
