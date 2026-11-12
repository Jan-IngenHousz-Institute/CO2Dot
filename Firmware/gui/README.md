# CO2Dot Controller GUI

Desktop application for the CO2Dot / MiniPAR: live spectrometer and BME plots,
recording, and three optional panels (Li-Control for the LI-6800, Pyroscience
optode, Serial Scripting).

It runs on Windows, Linux and macOS from the same sources.

| Platform | Status |
|---|---|
| Windows 10/11 | In daily use with the hardware. |
| Linux (Ubuntu 22.04 / 24.04) | Window, serial handling and packaged build are tested on Ubuntu 24.04. Not yet run against the hardware. |
| macOS (Apple Silicon) | Built and self-tested in CI. Not yet run on a desktop or against the hardware. |

## Run from source

Python 3.10 or newer.

```
cd Firmware/gui
python -m venv .venv
.venv\Scripts\activate          # Windows
source .venv/bin/activate       # Linux, macOS
pip install -r requirements.txt
python main.py
```

`python main.py --selftest` imports every module, builds the window once and
exits non-zero if anything is missing.

## Packaged builds

`.github/workflows/buildGUI.yml` builds all three on every push that touches
the GUI and attaches them to the workflow run:

| Artifact | Contents |
|---|---|
| `CO2Dot-Controller-windows-x64.exe` | Single executable. |
| `CO2Dot-Controller-linux-x64.tar.gz` | Folder; run `CO2Dot-Controller/CO2Dot-Controller`. |
| `CO2Dot-Controller-macos-arm64.zip` | `CO2Dot-Controller.app`. |

To build locally, on the operating system you are building for:

```
pip install pyinstaller
pyinstaller --noconfirm CO2Dot-Controller.spec
```

## Per-platform setup

### Windows

Nothing to install. Ports appear as `COM3`, `COM4`, …

### Linux

**Serial port access.** A normal user may not open serial devices until they
are in the group that owns them:

```
sudo usermod -aG dialout $USER
```

Log out and back in afterwards. If this step is missing, Connect and Auto
report `permission denied` together with this command (and the right group
name, on distributions where it is not `dialout`).

**X11 libraries, from source only.** On an X11 session Qt needs these, and a
stock Ubuntu does not have them; without them the program stops at start-up
with "Could not load the Qt platform plugin xcb":

```
sudo apt install libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 \
    libxcb-render-util0 libxcb-shape0 libxcb-util1 libxcb-xkb1 libxkbcommon-x11-0
```

The packaged build carries its own copies. A Wayland session does not need
them.

**Ports** appear as `/dev/ttyACM0` (CO2Dot, native USB) and `/dev/ttyUSB0`
(USB-serial adapters).

**`licor.local`** only resolves if the system has mDNS name resolution
(`libnss-mdns`, present on desktop Ubuntu). The Scan button does not depend on
it and fills in the IP address.

### macOS

**First launch.** The packaged app is not signed with an Apple developer
certificate, so macOS blocks it once. Either allow it under System Settings →
Privacy & Security → Open Anyway, or clear the download flag:

```
xattr -dr com.apple.quarantine CO2Dot-Controller.app
```

**Intel Macs.** The packaged build is Apple Silicon only; run from source.

**Local network prompt.** The first Li-Control scan or connection makes macOS
ask for permission to reach devices on the local network. It has to be
allowed for the LI-6800 to be found.

**Ports** appear as `/dev/cu.usbmodem…` (CO2Dot) and `/dev/cu.usbserial-…`
(USB-serial adapters).

### AD3 (`dc_offset()` in serial scripts), all platforms

Needs Digilent WaveForms installed; it provides the `dwf` library the GUI
loads. Everything else works without it.

## Where files go

| | From source | Packaged build |
|---|---|---|
| Recordings | `gui/data/` | `Documents/CO2Dot/data/`, or the folder chosen under File → Data directory… |
| Sequences, scripts | `gui/sequences/`, `gui/scripts/` | `Documents/CO2Dot/sequences/`, `…/scripts/` |
| Settings | `gui/gui_config.local.json` | `co2dot/gui_config.json` inside the per-user settings folder: `%LOCALAPPDATA%\JII\CO2Dot Controller` on Windows, `~/.config/JII/CO2Dot Controller` on Linux, under `~/Library/Preferences` on macOS |

Recording names are cleaned of characters that Windows does not allow in a
file name (`< > : " / \ | ? *`), on every platform, so a file recorded on
Linux or macOS can be copied to a Windows machine or a OneDrive folder.

## If the window looks wrong

The GUI uses the native widget style of each platform. To rule the style out
as the cause of a layout problem, start with Qt's own style, which looks the
same everywhere:

```
python main.py -style fusion
```
