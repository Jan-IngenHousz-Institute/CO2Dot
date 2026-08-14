"""
paths.py — resource and user-data path resolution.

A packaged build must not write next to its own executable: a macOS .app
bundle is code-signed and quarantined, and a Windows install under Program
Files is read-only for a normal user. So two kinds of path are kept strictly
apart:

    resource_path()   read-only assets shipped inside the bundle
    user_root()       everything the app writes (data, sequences, scripts)

Running from source both collapse to this directory, so the development
layout (gui/data, gui/sequences) behaves exactly as before.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from PySide6.QtCore import QStandardPaths

APP_DIR_NAME = "CO2Dot"

# Committed baseline settings, shipped as a bundled asset. User state is
# layered on top of it at load time (see MainWindow._load_gui_config), so a
# key added here reaches existing installs instead of reading back as None.
DEFAULT_CONFIG_NAME = "gui_config.default.json"
# Written by the app. Kept out of git in both layouts.
USER_CONFIG_NAME = "gui_config.json"
DEV_CONFIG_NAME = "gui_config.local.json"

EXAMPLE_SEQUENCE = "example_co2_ramp.json"


def is_frozen() -> bool:
    """True when running from a PyInstaller bundle."""
    return bool(getattr(sys, "frozen", False))


def source_dir() -> Path:
    """Directory holding the GUI sources."""
    return Path(__file__).resolve().parent


def resource_path(rel: str) -> Path:
    """Locate a read-only asset shipped with the application."""
    if is_frozen():
        base = Path(getattr(sys, "_MEIPASS", "") or Path(sys.executable).parent)
    else:
        base = source_dir()
    return base / rel


def user_root() -> Path:
    """Root of everything the app writes."""
    if not is_frozen():
        return source_dir()
    loc = QStandardPaths.writableLocation(QStandardPaths.DocumentsLocation)
    return Path(loc or Path.home()) / APP_DIR_NAME


def default_data_dir() -> Path:
    """Where recordings go until the user picks somewhere else."""
    return user_root() / "data"


def sequences_dir() -> Path:
    """User-editable Li-Control sequences."""
    return user_root() / "sequences"


def scripts_dir() -> Path:
    """User-editable Serial Scripting scripts."""
    return user_root() / "scripts"


def config_dir() -> Path:
    if not is_frozen():
        return source_dir()
    loc = QStandardPaths.writableLocation(QStandardPaths.AppConfigLocation)
    return Path(loc) / "co2dot" if loc else user_root()


def config_path() -> Path:
    """The file live settings are written to."""
    name = USER_CONFIG_NAME if is_frozen() else DEV_CONFIG_NAME
    return config_dir() / name


def default_config_path() -> Path:
    return resource_path(DEFAULT_CONFIG_NAME)


def is_writable(directory: Path) -> bool:
    """Can we create `directory` and write inside it? Never raises.

    ValueError is caught alongside OSError: a settings file carrying a
    malformed path (an embedded null, say) must fail this check rather than
    take the application down during startup."""
    try:
        probe = Path(directory) / ".co2dot_write_test"
        Path(directory).mkdir(parents=True, exist_ok=True)
        probe.write_text("", encoding="utf-8")
    except (OSError, ValueError):
        return False
    else:
        return True
    finally:
        try:
            probe.unlink()
        except (OSError, ValueError, NameError):
            pass


def seed_user_file(bundled_rel: str, target: Path) -> None:
    """Copy a shipped template into the user area once, if it is missing.

    A no-op when running from source, where the two paths are the same file.
    """
    try:
        target = Path(target)
        source = resource_path(bundled_rel)
        if target.exists() or not source.is_file() or source == target:
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    except (OSError, ValueError):
        pass
