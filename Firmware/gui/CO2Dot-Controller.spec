# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller build description for the CO2Dot controller GUI.

Committed so a local build and a CI build produce the same thing, and so the
exclude list below is reviewable rather than buried in a shell command.

    cd Firmware/gui
    pyinstaller --noconfirm CO2Dot-Controller.spec

Layout: one file on Windows (a single .exe is what people expect there), one
directory elsewhere. --onefile unpacks the whole bundle to a temp directory
on every launch, which at this size costs seconds of startup for no benefit
on platforms where the result gets archived anyway. Override with
CO2DOT_ONEFILE=1 / 0.
"""

import os
import sys

APP_NAME = "CO2Dot-Controller"

_default_onefile = "1" if sys.platform == "win32" else "0"
ONEFILE = os.environ.get("CO2DOT_ONEFILE", _default_onefile) == "1"

# Read-only assets. Everything the app writes goes to the user area instead
# (see paths.py), so nothing here needs to be writable.
datas = [
    ("gui_config.default.json", "."),
    ("sequences/example_co2_ramp.json", "sequences"),
]

# PyInstaller follows function-level imports, so the lazily-imported feature
# modules are found on their own. These are the ones it cannot see: a
# per-platform backend chosen at import time, and packages whose real
# contents are compiled submodules.
hiddenimports = [
    "serial.tools.list_ports",
    "serial.tools.list_ports_common",
    "serial.tools.list_ports_linux",
    "serial.tools.list_ports_osx",
    "serial.tools.list_ports_posix",
    "serial.tools.list_ports_windows",
]

excludes = [
    # Qt add-ons. requirements.txt asks for PySide6-Essentials, which does
    # not ship these at all; the list keeps a developer's full-PySide6
    # environment from quietly inflating the build by a few hundred MB.
    "PySide6.Qt3DAnimation",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DExtras",
    "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic",
    "PySide6.Qt3DRender",
    "PySide6.QtBluetooth",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtGraphs",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtNfc",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtPositioning",
    "PySide6.QtQuick3D",
    "PySide6.QtRemoteObjects",
    "PySide6.QtScxml",
    "PySide6.QtSensors",
    "PySide6.QtSpatialAudio",
    "PySide6.QtTextToSpeech",
    "PySide6.QtWebChannel",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebSockets",
    # Essentials modules this GUI has no use for. QtSvg, QtOpenGL and
    # QtPrintSupport stay: pyqtgraph reaches for them.
    "PySide6.QtDesigner",
    "PySide6.QtHelp",
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtQuickControls2",
    "PySide6.QtQuickWidgets",
    "PySide6.QtSql",
    "PySide6.QtUiTools",
    # pyqtgraph probes for every Qt binding it knows and will bind to the
    # first one it finds; only PySide6 is wanted here.
    "PyQt5",
    "PyQt6",
    "PySide2",
    "shiboken2",
    # Plotting/science stacks pyqtgraph will happily pull in if present.
    "IPython",
    "jupyter",
    "matplotlib",
    "notebook",
    "pandas",
    "scipy",
    # Dev-only. numpy.testing needs unittest, so that one has to stay.
    "pytest",
    "tkinter",
]

a = Analysis(
    ["main.py"],
    pathex=[SPECPATH],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

# UPX stays off: it corrupts some Qt DLLs on Windows and invalidates code
# signatures on macOS.
_exe_common = dict(
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

if ONEFILE:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        runtime_tmpdir=None,
        **_exe_common,
    )
else:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, **_exe_common)
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name=APP_NAME,
    )
    if sys.platform == "darwin":
        app = BUNDLE(
            coll,
            name=APP_NAME + ".app",
            icon=None,
            bundle_identifier="org.jan-ingenhousz-institute.co2dot",
            info_plist={
                "NSHighResolutionCapable": True,
                # Serial adapters are plain character devices, but macOS
                # still gates the enumeration behind this prompt.
                "NSSystemAdministrationUsageDescription":
                    "CO2Dot communicates with USB serial instruments.",
            },
        )
