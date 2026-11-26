# firmware-CO2Dot
Repo for the firmware of the CO2Dot

## Identity

openJII registers a CO2Dot by its family and hardware MAC. The MAC is the
ESP32-C3 eFuse MAC, uppercase and colon-separated, byte zero first, the same
form the Ambit prints.

`hello` (line mode):

```
{"device":"CO2Dot","version":"0.1.0","mac":"58:CF:79:12:34:56"}
```

`device` and `version` are unchanged in meaning; `mac` is new. `version` is now
the build version instead of a fixed `1.0`.

`{"command":"INFO"}` (JSON mode, the openJII generic-device identity probe)
answers one bare line:

```
{"status":"success","data":{"device_name":"CO2Dot","device_type":"co2dot","device_id":"58:CF:79:12:34:56","firmware_version":"0.1.0"}}
```

Any other `{"command":...}` object answers
`{"status":"error","error":"unknown_command"}`. Line mode `info` returns the
same `data` object.

## Versions and releases

`tools/version.py` injects the version at build time: `CO2DOT_RELEASE_VERSION`
in CI, otherwise `git describe` over firmware tags (`fw/v*`).

- `Build firmware` (`.github/workflows/firmware.yml`) builds the `usb` and
  `uart` variants on pull requests and on `main`, and uploads release-shaped
  assets as an artifact.
- `Release firmware` (`.github/workflows/release-firmware.yml`) runs when a tag
  `fw/vX.Y.Z` on `main` is pushed. It publishes `co2dot-fw-vX.Y.Z.bin`,
  `co2dot-uart-fw-vX.Y.Z.bin`, `bootloader.bin`, `partitions.bin`,
  `boot_app0.bin` and `manifest.json` (offsets and sha256) as a GitHub release.
  GUI (`gui/v*`) and hardware (`hw/v*`) tags never trigger it.

To release: `git tag fw/v0.1.0 <main commit> && git push origin fw/v0.1.0`.
Build locally with `pio run -e usb -e uart`, then
`python3 tools/assemble_release.py <version>`.
