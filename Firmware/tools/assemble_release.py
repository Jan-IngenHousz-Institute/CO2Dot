"""Collect built CO2Dot firmware into release assets.

Usage: python3 tools/assemble_release.py <version> [out_dir]

Run from Firmware/ after `pio run -e usb -e uart`. Writes, for version X:
  co2dot-fw-vX.bin        usb (native USB-CDC) application, flashed at 0x10000
  co2dot-uart-fw-vX.bin   uart variant application
  bootloader.bin, partitions.bin, boot_app0.bin
  manifest.json           names, offsets, sizes and sha256 for flashers

The naming follows the Ambit firmware releases (ambit-fw-vX.bin plus a
manifest), which openJII lists per device family from GitHub releases.
"""

import glob
import hashlib
import json
import os
import shutil
import sys


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    version = sys.argv[1].removeprefix("fw/").lstrip("vV")
    out = sys.argv[2] if len(sys.argv) > 2 else "dist"
    os.makedirs(out, exist_ok=True)

    usb = os.path.join(".pio", "build", "usb")
    uart = os.path.join(".pio", "build", "uart")
    app = f"co2dot-fw-v{version}.bin"
    uart_app = f"co2dot-uart-fw-v{version}.bin"

    # boot_app0.bin ships with the Arduino-ESP32 core, not the build.
    boot_app0 = sorted(
        glob.glob(
            os.path.expanduser(
                "~/.platformio/packages/framework-arduinoespressif32*/tools/partitions/boot_app0.bin"
            )
        )
    )
    if not boot_app0:
        print("boot_app0.bin not found in the PlatformIO Arduino-ESP32 package", file=sys.stderr)
        return 1

    copies = {
        os.path.join(usb, "firmware.bin"): app,
        os.path.join(uart, "firmware.bin"): uart_app,
        os.path.join(usb, "bootloader.bin"): "bootloader.bin",
        os.path.join(usb, "partitions.bin"): "partitions.bin",
        boot_app0[0]: "boot_app0.bin",
    }
    for source, name in copies.items():
        if not os.path.isfile(source):
            print(f"missing build product: {source}", file=sys.stderr)
            return 1
        shutil.copyfile(source, os.path.join(out, name))

    # The build-injected version must be embedded verbatim: `hello` and INFO
    # report it after flashing.
    for name in (app, uart_app):
        with open(os.path.join(out, name), "rb") as handle:
            if version.encode() + b"\0" not in handle.read():
                print(f"{name} does not embed version {version}", file=sys.stderr)
                return 1

    def entry(name: str, offset: str) -> dict:
        data = open(os.path.join(out, name), "rb").read()
        return {
            "file": name,
            "offset": offset,
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }

    manifest = {
        "name": "co2dot",
        "version": version,
        "chip": "esp32c3",
        "flash": [
            entry("bootloader.bin", "0x0"),
            entry("partitions.bin", "0x8000"),
            entry("boot_app0.bin", "0xe000"),
            entry(app, "0x10000"),
        ],
        "ota": {"file": app},
        "variants": {"uart": entry(uart_app, "0x10000")},
    }
    with open(os.path.join(out, "manifest.json"), "w") as handle:
        json.dump(manifest, handle, indent=2)
        handle.write("\n")
    print(f"assembled {len(copies) + 1} assets for v{version} in {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
