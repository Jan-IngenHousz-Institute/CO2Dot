"""
main.py — Entry point for the CO2Dot GUI.

Usage:
    python main.py
    python main.py --selftest    # import + construct check, exits non-zero on failure

Requirements:
    pip install -r requirements.txt
"""

import importlib
import os
import sys
import traceback
from pathlib import Path

# Ensure imports resolve correctly when run from any working directory
sys.path.insert(0, os.path.dirname(__file__))

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication


# Every module a packaged build has to contain. Most are imported lazily at
# runtime (optional features), and the third-party ones below resolve their
# real implementation dynamically — pyserial picks a per-platform backend,
# zeroconf and cryptography load compiled submodules — which PyInstaller's
# static analysis cannot follow. Listing them here turns "works on my
# machine" into a build-time gate.
SELFTEST_MODULES = [
    # Third party with dynamic imports
    "numpy",
    "pyqtgraph",
    "serial",
    "serial.tools.list_ports",
    "zeroconf",
    "paramiko",
    # Application: core
    "ad3",
    "data_buffer",
    "device_manager",
    "paths",
    "port_probe",
    "protocol",
    "recorder",
    "serial_worker",
    # Application: optional features, all lazily imported by main_window
    "ext_serial_buffer",
    "ext_serial_panel",
    "ext_serial_recorder",
    "ext_serial_script",
    "ext_serial_worker",
    "li_control",
    "li_discovery",
    "li_discovery_plain",
    "li_panel",
    "li_recorder",
    "li_sequence",
    "li_sequence_editor",
    "li_worker",
    "pyro_buffer",
    "pyro_panel",
    "pyro_protocol",
    "pyro_recorder",
    "pyro_worker",
]

SELFTEST_LOG = "selftest.log"


def _configure_app(app: QApplication) -> None:
    app.setApplicationName("CO2Dot Controller")
    app.setOrganizationName("JII")


def _check_resources() -> list[str]:
    """Confirm the read-only assets really are inside the bundle.

    Both of these are read through code that falls back to a default when
    the file is missing, so a spec that dropped its `datas` entry would pass
    every import check and only surface as an install with no settings and
    no example sequence. Worth asserting explicitly.
    """
    import json

    import paths

    problems: list[str] = []

    config = paths.default_config_path()
    if not config.is_file():
        problems.append(f"missing bundled asset: {config}")
    else:
        try:
            data = json.loads(config.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            problems.append(f"{config.name} is unreadable: {exc}")
        else:
            if "version" not in data:
                problems.append(f"{config.name} has no version key")

    example = paths.resource_path(f"sequences/{paths.EXAMPLE_SEQUENCE}")
    if not example.is_file():
        problems.append(f"missing bundled asset: {example}")

    return problems


def _selftest() -> int:
    """Import every bundled module and build the window once, then quit.

    Also proves the Qt platform plugin loaded, which no amount of
    hidden-import guessing can establish. Output goes to both stdout and
    SELFTEST_LOG because a windowed Windows build has no stdout at all.
    """
    # Suppress the first-run data-directory dialog: a modal would block the
    # run forever on a headless CI machine.
    os.environ["CO2DOT_SELFTEST"] = "1"

    report: list[str] = []
    failures: list[str] = []
    checks = 0

    for name in SELFTEST_MODULES:
        checks += 1
        try:
            importlib.import_module(name)
        except Exception:
            failures.append(name)
            report.append(f"FAIL  {name}\n{traceback.format_exc()}")
        else:
            report.append(f"ok    {name}")

    checks += 1
    problems = _check_resources()
    if problems:
        failures.append("resources")
        report.append("FAIL  resources\n      " + "\n      ".join(problems))
    else:
        report.append("ok    resources")

    checks += 1
    try:
        app = QApplication(sys.argv[:1])
        _configure_app(app)
        from main_window import MainWindow

        window = MainWindow()
        window.show()
        QTimer.singleShot(0, app.quit)
        app.exec()
    except Exception:
        failures.append("MainWindow")
        report.append(f"FAIL  MainWindow\n{traceback.format_exc()}")
    else:
        report.append("ok    MainWindow")

    report.append(
        f"\n{checks - len(failures)} passed, {len(failures)} failed"
        + (f": {', '.join(failures)}" if failures else "")
    )
    text = "\n".join(report)

    print(text)
    try:
        Path(SELFTEST_LOG).write_text(text, encoding="utf-8")
    except OSError:
        pass
    return 1 if failures else 0


def main():
    if "--selftest" in sys.argv:
        sys.exit(_selftest())

    app = QApplication(sys.argv)
    _configure_app(app)

    from main_window import MainWindow

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
