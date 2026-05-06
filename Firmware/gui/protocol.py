"""
protocol.py — Serial command strings and JSON response parsers for CO2Dot.

Serial config: 115200 baud, 8N1, no parity.
All commands are plain text terminated with '\n'.
All responses are JSON lines (one JSON object per line).
"""

import json

# ---------------------------------------------------------------------------
# Command strings
# ---------------------------------------------------------------------------

CMD_HELLO = "hello"
CMD_STATUS = "status"
CMD_SPEC = "spec"
CMD_SPEC_FLASH = "spec_flash"


def cmd_spec_flash(led_current: int) -> str:
    return f"spec_flash,{led_current}"
CMD_ENV = "env"


def cmd_set_gain(value: int) -> str:
    return f"spec_set_gain,{value}"


def cmd_set_atime(value: int) -> str:
    return f"spec_set_atime,{value}"


def cmd_set_astep(value: int) -> str:
    return f"spec_set_astep,{value}"


def cmd_set_led(value: int) -> str:
    return f"set_led,{value}"


# ---------------------------------------------------------------------------
# Gain label tables
# ---------------------------------------------------------------------------

AS7341_GAIN_LABELS = {
    0: "0.5x", 1: "1x", 2: "2x", 3: "4x", 4: "8x",
    5: "16x", 6: "32x", 7: "64x", 8: "128x", 9: "256x", 10: "512x",
}

AS7343_GAIN_LABELS = {
    0: "0.5x", 1: "1x", 2: "2x", 3: "4x", 4: "8x",
    5: "16x", 6: "32x", 7: "64x", 8: "128x", 9: "256x",
    10: "512x", 11: "1024x", 12: "2048x",
}


def gain_labels(model: str) -> dict:
    if model == "AS7343":
        return AS7343_GAIN_LABELS
    return AS7341_GAIN_LABELS


def gain_max(model: str) -> int:
    return 12 if model == "AS7343" else 10


# ---------------------------------------------------------------------------
# Channel ordering / display labels
# ---------------------------------------------------------------------------

AS7341_CHANNELS = [
    "f1_415", "f2_445", "f3_480", "f4_515", "f5_555",
    "f6_590", "f7_630", "f8_680", "clear", "nir",
]

AS7343_CHANNELS = [
    "f1_405", "f2_425", "fz_450", "f3_475", "f4_515", "f5_550",
    "fy_555", "fxl_600", "f6_640", "f7_690", "f8_745", "nir_855",
    "clear",
]

# Human-readable display names for legend
_CHANNEL_DISPLAY = {
    # AS7341
    "f1_415": "F1 415nm", "f2_445": "F2 445nm", "f3_480": "F3 480nm",
    "f5_555": "F5 555nm", "f6_590": "F6 590nm",
    "f7_630": "F7 630nm", "f8_680": "F8 680nm",
    "nir": "NIR",
    # AS7343
    "f1_405": "F1 405nm", "f2_425": "F2 425nm", "fz_450": "FZ 450nm",
    "f3_475": "F3 475nm", "f5_550": "F5 550nm",
    "fy_555": "FY 555nm", "fxl_600": "FXL 600nm", "f6_640": "F6 640nm",
    "f7_690": "F7 690nm", "f8_745": "F8 745nm", "nir_855": "NIR 855nm",
    # Shared
    "f4_515": "F4 515nm", "clear": "Clear",
}


def channel_display_name(ch: str) -> str:
    return _CHANNEL_DISPLAY.get(ch, ch)


def channels_for_model(model: str) -> list:
    if model == "AS7343":
        return AS7343_CHANNELS
    return AS7341_CHANNELS


# ---------------------------------------------------------------------------
# Default settings per model
# ---------------------------------------------------------------------------

MODEL_DEFAULTS = {
    "AS7341": {"atime": 100, "astep": 999, "gain": 5, "led": 10},
    "AS7343": {"atime": 29,  "astep": 599, "gain": 1, "led": 10},
}


def defaults_for_model(model: str) -> dict:
    return MODEL_DEFAULTS.get(model, MODEL_DEFAULTS["AS7341"])


# ---------------------------------------------------------------------------
# JSON parsers
# ---------------------------------------------------------------------------

def _try_parse(line: str):
    """Return parsed JSON object or None."""
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


def classify_line(line: str) -> tuple[str, object]:
    """
    Classify an incoming serial line and return (kind, parsed) where kind is
    one of: 'hello', 'status', 'spec', 'spec_flash', 'bme', 'spec_config',
    'led_set', 'error', 'unknown'.
    """
    stripped = line.strip()

    # Hello detection — accept any known device
    obj = _try_parse(stripped)
    if obj is not None and "device" in obj:
        return ("hello", obj)

    if obj is None:
        return ("unknown", stripped)

    # Status
    if "spectrometer_status" in obj or "bme_status" in obj:
        result = {}
        if "spectrometer_status" in obj:
            result["spectrometer"] = obj["spectrometer_status"]
        if "bme_status" in obj:
            result["bme"] = obj["bme_status"]
        return ("status", result) if result else ("unknown", stripped)

    # Spec flash (check before ambient because both have 'spectrometer' key)
    if "spectrometer_diff" in obj:
        diff = obj["spectrometer_diff"]
        if isinstance(diff, dict) and "channels" in diff and "model" in diff:
            if "error" not in diff:
                return ("spec_flash", {"model": diff["model"], "channels": diff["channels"]})

    # Spectrometer responses (ambient read or LED set)
    if "spectrometer" in obj:
        spec = obj["spectrometer"]
        if isinstance(spec, dict):
            # LED set response
            if "led_current_ma" in spec:
                return ("led_set", spec)
            # Error response
            if "error" in spec:
                return ("error", f"spectrometer: {spec['error']}")
            # Ambient spec read
            channels = spec.get("channels")
            model = spec.get("model", "")
            if isinstance(channels, dict) and model:
                return ("spec", {"model": model, "channels": channels})

    # BME
    if "bme_read" in obj:
        bme = obj["bme_read"]
        if isinstance(bme, dict) and all(k in bme for k in ("T", "P", "RH", "Gas")):
            return ("bme", {
                "T": float(bme["T"]), "P": float(bme["P"]),
                "RH": float(bme["RH"]), "Gas": int(bme["Gas"]),
            })

    # Config ack
    if "spectrometer_config" in obj:
        cfg = obj["spectrometer_config"]
        if isinstance(cfg, dict):
            if "error" in cfg:
                return ("error", f"spectrometer_config: {cfg['error']}")
            return ("spec_config", cfg)

    return ("unknown", stripped)
