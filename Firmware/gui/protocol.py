"""
protocol.py — Serial command strings and response parsers for CO2Dot.

Serial config: 115200 baud, 8N1, no parity.
All commands are plain text terminated with '\n'.
Responses follow the command-as-root openJII LINE protocol: one newline-
terminated payload per command, with no wrapper keys (see
/CommunicationProtocolOpenJIISerial.md). classify_line() routes each response
by its top-level shape.
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
    """Return parsed JSON value (object or array) or None."""
    line = line.strip()
    if not (line.startswith("{") or line.startswith("[")):
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

    Responses follow the command-as-root protocol (see
    /CommunicationProtocolOpenJIISerial.md): there are no wrapper keys, so each
    kind is detected from its top-level shape.
    """
    stripped = line.strip()
    obj = _try_parse(stripped)
    if not isinstance(obj, dict):
        return ("unknown", stripped)

    # Error — reserved top-level key (e.g. {"error":"not_available"})
    if "error" in obj:
        return ("error", obj.get("error", "error"))

    # Hello — {"device":..,"version":..}
    if "device" in obj:
        return ("hello", obj)

    # Combined status — {"spectrometer":{...},"bme":{...}}
    if "spectrometer" in obj or "bme" in obj:
        result = {}
        if "spectrometer" in obj:
            result["spectrometer"] = obj["spectrometer"]
        if "bme" in obj:
            result["bme"] = obj["bme"]
        return ("status", result) if result else ("unknown", stripped)

    # Spec flash — {"model":..,"dark":{...},"lit":{...},"diff":{...}}
    # (check before ambient: a flash object has no top-level "channels")
    if "diff" in obj and "model" in obj:
        diff = obj["diff"]
        if isinstance(diff, dict):
            return ("spec_flash", {"model": obj["model"], "channels": diff})

    # Ambient spec read — {"model":..,"channels":{...}}
    if "channels" in obj and "model" in obj:
        channels = obj["channels"]
        if isinstance(channels, dict):
            return ("spec", {"model": obj["model"], "channels": channels})

    # LED set ack — {"led_current_ma":N}
    if "led_current_ma" in obj:
        return ("led_set", obj)

    # BME read — {"T":..,"P":..,"RH":..,"Gas":..}
    if all(k in obj for k in ("T", "P", "RH", "Gas")):
        return ("bme", {
            "T": float(obj["T"]), "P": float(obj["P"]),
            "RH": float(obj["RH"]), "Gas": int(obj["Gas"]),
        })

    # Config ack — {"atime":..,"astep":..,"gain":..}
    if all(k in obj for k in ("atime", "astep", "gain")):
        return ("spec_config", obj)

    return ("unknown", stripped)
