"""
ad3.py — minimal Digilent AD3 (WaveForms SDK) DC-offset driver. Qt-free.

Lazy: the dwf library is loaded and the device opened on the first
dc_offset() call, so the GUI runs fine on machines without WaveForms
installed until a script actually needs the AD3.

The device handle is held for the whole app lifetime: WaveForms stops all
outputs when a device handle is closed (DwfParamOnClose default), so an
open/close-per-call style would drop the LED to 0 V between calls.

Lifted from Scripts/CO2Dot-leafTest.ipynb (open_wv / _switch_variable_ /
dc_offset / wf_park). The few SDK constants needed are inlined so we do not
depend on the dwfconstants.py sample shipped inside the WaveForms install
directory.
"""

from __future__ import annotations

import ctypes
import sys
import threading

VMAX = 0.300   # 300 mV hard ceiling on the VCCS input


class AD3Error(RuntimeError):
    """Raised when the WaveForms SDK is missing or the AD3 is unavailable."""


_lock = threading.Lock()
_dwf = None      # ctypes CDLL once loaded
_handle = None   # ctypes.c_int device handle once opened

# WaveForms SDK constants (values from WaveFormsSDK samples dwfconstants.py)
_FUNC_DC = ctypes.c_ubyte(0)       # funcDC
_CH_W1 = ctypes.c_int(0)           # analog-out channel 0 (W1)
_NODE_CARRIER = ctypes.c_int(0)    # AnalogOutNodeCarrier


def _load_dwf():
    """Load the WaveForms dynamic library (OS-specific)."""
    global _dwf
    if _dwf is not None:
        return _dwf
    try:
        if sys.platform.startswith("win"):
            _dwf = ctypes.cdll.dwf
        elif sys.platform.startswith("darwin"):
            _dwf = ctypes.cdll.LoadLibrary(
                "/Library/Frameworks/dwf.framework/dwf")
        else:
            _dwf = ctypes.cdll.LoadLibrary("libdwf.so")
    except (OSError, AttributeError) as exc:
        raise AD3Error(
            "WaveForms SDK (dwf) not found — install Digilent WaveForms "
            f"to use dc_offset() ({exc})"
        ) from exc
    return _dwf


def _open():
    """Open the first device, enable the +5 V supply, park W1 at 0 V.

    Must be called with _lock held."""
    global _handle
    if _handle is not None:
        return
    dwf = _load_dwf()
    handle = ctypes.c_int()
    dwf.FDwfDeviceOpen(ctypes.c_int(-1), ctypes.byref(handle))
    if handle.value == 0:
        err = ctypes.create_string_buffer(512)
        dwf.FDwfGetLastErrorMsg(err)
        msg = err.value.decode(errors="replace").strip() or "unknown error"
        if "busy" in msg.lower():
            raise AD3Error(
                f"AD3 open failed: {msg} — another process holds the AD3. "
                "Close the WaveForms app and/or shut down the Jupyter "
                "kernel that ran open_wv() (e.g. CO2Dot-leafTest.ipynb), "
                "then Run again."
            )
        raise AD3Error(
            f"AD3 open failed: {msg} — is the device attached and not in "
            "use by the WaveForms application?"
        )
    _handle = handle
    # Enable the +5 V supply for the VCCS (notebook _switch_variable_ with
    # master=True, positive=True at 5.0 V, negative=False at 0.0 V):
    dwf.FDwfAnalogIOChannelNodeSet(
        _handle, ctypes.c_int(0), ctypes.c_int(1), ctypes.c_double(5.0))
    dwf.FDwfAnalogIOChannelNodeSet(
        _handle, ctypes.c_int(1), ctypes.c_int(1), ctypes.c_double(0.0))
    dwf.FDwfAnalogIOChannelNodeSet(
        _handle, ctypes.c_int(0), ctypes.c_int(0), ctypes.c_int(1))
    dwf.FDwfAnalogIOChannelNodeSet(
        _handle, ctypes.c_int(1), ctypes.c_int(0), ctypes.c_int(0))
    dwf.FDwfAnalogIOEnableSet(_handle, ctypes.c_int(1))
    _park_unlocked()   # start in the safe (0 V) state


def is_open() -> bool:
    with _lock:
        return _handle is not None


def dc_offset(volts: float) -> float:
    """Hold W1 at a constant DC level, clamped to 0..VMAX.

    Opens the AD3 lazily on first use. Returns the clamped voltage that is
    actually driven, so callers can record the true hardware state.
    """
    with _lock:
        volt = float(min(max(float(volts), 0.0), VMAX))
        if _handle is None and volt == 0.0:
            # Device never opened → W1 isn't driving anything; a cleanup
            # dc_offset(0) must not fail (or open the device) just to park.
            return 0.0
        _open()
        _dwf.FDwfAnalogOutNodeEnableSet(
            _handle, _CH_W1, _NODE_CARRIER, ctypes.c_int(1))
        _dwf.FDwfAnalogOutNodeFunctionSet(
            _handle, _CH_W1, _NODE_CARRIER, _FUNC_DC)
        _dwf.FDwfAnalogOutNodeOffsetSet(
            _handle, _CH_W1, _NODE_CARRIER, ctypes.c_double(volt))
        _dwf.FDwfAnalogOutConfigure(_handle, _CH_W1, ctypes.c_int(1))
        return volt


def _park_unlocked() -> None:
    """Drop W1 to 0 V and stop the output (notebook wf_park). Best-effort.

    Must be called with _lock held."""
    if _handle is None or _dwf is None:
        return
    try:
        _dwf.FDwfAnalogOutNodeEnableSet(
            _handle, _CH_W1, _NODE_CARRIER, ctypes.c_int(1))
        _dwf.FDwfAnalogOutNodeFunctionSet(
            _handle, _CH_W1, _NODE_CARRIER, _FUNC_DC)
        _dwf.FDwfAnalogOutNodeOffsetSet(
            _handle, _CH_W1, _NODE_CARRIER, ctypes.c_double(0.0))
        _dwf.FDwfAnalogOutConfigure(_handle, _CH_W1, ctypes.c_int(1))
        _dwf.FDwfAnalogOutReset(_handle, _CH_W1)
        _dwf.FDwfAnalogOutConfigure(_handle, _CH_W1, ctypes.c_int(0))
    except Exception:
        pass


def park() -> None:
    """Public park: LED to 0 V, output stopped. Safe to call any time."""
    with _lock:
        _park_unlocked()


def close() -> None:
    """Park W1 and close the device. Safe to call when never opened."""
    global _handle
    with _lock:
        if _handle is None:
            return
        _park_unlocked()
        try:
            _dwf.FDwfDeviceClose(_handle)
        except Exception:
            pass
        _handle = None
