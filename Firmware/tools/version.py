# version.py - PlatformIO pre-script: inject the firmware version at build time.
#
# One source, in priority order:
#   1. $CO2DOT_RELEASE_VERSION - set by CI (release tag or PR build label)
#   2. git describe            - local builds ("0.1.0-3-gabc123-dirty"), firmware
#                                tags only: this repo also tags gui/v* and hw/v*
#   3. "0.0.0-dev"             - no git / no tags
#
# It feeds the `hello` reply and the openJII INFO identity. Modeled on
# Jan-IngenHousz-Institute/ambit tools/version.py.
Import("env")

import os
import subprocess


def _git_describe():
    try:
        return (
            subprocess.check_output(
                ["git", "describe", "--tags", "--match", "fw/v*", "--always", "--dirty"],
                cwd=env["PROJECT_DIR"],
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
        )
    except Exception:
        return ""


version = os.environ.get("CO2DOT_RELEASE_VERSION", "").strip() or _git_describe() or "0.0.0-dev"
version = version.removeprefix("fw/").lstrip("vV")

env.Append(CPPDEFINES=[("CO2DOT_FW_VERSION", env.StringifyMacro(version))])
print("co2dot fw version: %s" % version)
