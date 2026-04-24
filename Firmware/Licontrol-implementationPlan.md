# Li-Control GUI Feature — Implementation Plan (v5)

## Context
The project ships a PySide6 GUI ([gui/main_window.py](gui/main_window.py)) that talks to an embedded spectrometer + BME68x board over serial and logs data to TSV. The [Li-Control/](Li-Control/) folder contains a Jupyter workflow that drives a **LI-COR LI-6800** photosynthesis system over SSH, setting environmental parameters (CO₂, T, RH, light) and reading back gas-exchange measurements (`CO2_r/s`, `H2O_r/s`, `Tchamber`, `Tleaf`, `RHcham`, `PPFD_in`).

We bring that workflow into the GUI as an **optional** feature so the user can:
1. Auto-detect and connect to the LI-6800 on the local network, reusing the session across commands.
2. Manually push setpoints and see the LI-6800 readback alongside the spectrometer + BME readings.
3. Run a programmed sequence of setpoint changes, where each step logs **one** combined row (LiCor ACK + a fresh spec+BME triggered after the ACK) to a TSV file.

The on-device script [Li-Control/RemoteEnvMeasure.py](Li-Control/RemoteEnvMeasure.py) is **not** modified — we only implement the client side. When the feature is disabled, the GUI behaves and looks exactly as today (zero runtime cost, no new imports at startup).

## Design decisions (locked)
- **Enable toggle**: `View → Enable Li-Control` checkable menu item, persisted to `gui_config.json` (location: `QStandardPaths.AppConfigLocation` when frozen, `base_dir` in dev).
- **Sequence format**: JSON file loaded via file dialog (no script execution).
- **SSH library**: `paramiko` with a persistent `SSHClient` + `SFTPClient` held by a worker thread. No subprocess / CLI fallback.
- **Auto-detect**: Two complementary paths, both tried on first panel show and on "Scan" click:
  1. **Plain mDNS resolution** via `socket.gethostbyname("licor.local")` and `socket.gethostbyname("licor-6800.local")`. Cheap, synchronous (wrapped in a short-lived `QThread`), works on macOS/Linux natively and Windows 10+ with mDNS enabled. No zeroconf import needed for this path.
  2. **Zeroconf multi-service browse** via `zeroconf.ServiceBrowser` on `_ssh._tcp.local.`, `_sftp-ssh._tcp.local.`, `_http._tcp.local.`, `_workstation._tcp.local.` in parallel; union the results, filter by hostname containing `licor` / `li-6800`.
  **Verification status:** which service type the LI-6800 actually advertises is not documented — the multi-service browse is defensive. Treat auto-discovery as a convenience layer; the editable host combo is the real contract. On first real-hardware contact, log the full Avahi service list and tighten the filter.
  **Firewall fallback:** if both paths return zero hosts, the status bar shows "No LI-6800 found — check firewall or type hostname manually".
- **Spectrometer is optional**: Li-Control must be usable without the embedded spec/BME board connected. The runner degrades gracefully — rows get empty spec/BME fields and a `notes="spec_unavailable"` marker.
- **Sampling model**: exactly **one** combined row per LiCor command. After each ACK, if `SerialWorker` is connected, the runner triggers a fresh `CMD_ENV + CMD_SPEC` and waits for the next `spec_received` (or a short timeout). If the serial worker isn't connected, the runner writes the row immediately with empty spec/BME fields. No per-sample loop.
- **Manual-Send coordination (permissive, option B)**: the manual **Send** button is always allowed. The resulting combined row's spec may be up to one acquisition interval old (if GUI acquisition is running) or empty (if no serial worker) — documented in the `notes` column.
- **Spec-unavailable / spec-timeout during a sequence**: the sequence *advances* with empty spec/BME columns and `notes="spec_unavailable"`. It does **not** abort. Rationale: user explicitly wants to drive the LI-6800 even when the embedded sensors are disconnected.
- **Sequence shared-serial coordination**: when a sequence starts *and* `SerialWorker` is running, `MainWindow._acq_timer` is paused for the duration; the runner is the sole source of spec requests. On finish/abort, the timer is resumed at its previous interval. If `SerialWorker` isn't running, this step is skipped.

## New files in `gui/`

### `gui/li_control.py` — Qt-free protocol layer
```python
REMOTE_DIR = "/home/licor/apps/dynamic"
REMOTE_CMD = f"{REMOTE_DIR}/remote_cmd.json"
REMOTE_TMP = f"{REMOTE_DIR}/remote_cmd.json.tmp"
REMOTE_ACK = f"{REMOTE_DIR}/remote_ack.json"

@dataclass
class LiConfig:
    host: str
    username: str = "licor"
    port: int = 22
    key_filename: str | None = None
    poll_interval_s: float = 0.5
    ack_timeout_s: float = 600.0          # default; per-step override allowed
    keepalive_s: int = 30
    host_key_policy: str = "auto_add"     # "auto_add" | "reject"

@dataclass
class LiSetpoints:
    co2_r: float | None = None
    qin: float | None = None
    flow: float | None = None
    tair: float | None = None
    rh_air: float | None = None
    fan_rpm: float | None = None
    pressure: float | None = None
    wait_for_co2: bool = False
    co2_tol: float = 2.0
    wait_s: float = 0.0
    log: bool = False
    def to_cmd(self, cmd_id: str) -> dict: ...

def build_stop_cmd(cmd_id: str) -> dict: ...
def new_cmd_id() -> str:                   # uuid4().hex[:12]
```

### `gui/li_discovery.py` — mDNS discovery (plain + zeroconf)

**Lazy-import strategy (critical).** `zeroconf` is imported at the **top** of `li_discovery.py`, not inside `start()`. Module-level imports mean a missing package raises `ImportError` *synchronously* at the `from li_discovery import LiDiscovery` call site (in `_init_li_control` / `_on_li_scan`), where the caller's `try/except ImportError` can actually catch it. Importing inside `start()` would raise on whatever thread ran `start()` and the exception would never reach the main-thread handler.

**Split module layout.** The plain-mDNS path uses only stdlib `socket` and must survive a missing `zeroconf`. Structure:
- `li_discovery_plain.py` — stdlib-only, defines `PlainMdnsResolver(QObject)` with the `socket.gethostbyname` logic. Always importable.
- `li_discovery.py` — imports `zeroconf` at the top and defines `LiDiscovery(QObject)` which composes a `PlainMdnsResolver` **and** drives the zeroconf `ServiceBrowser`. Importing this module fails fast if zeroconf is missing; caller falls back to using `PlainMdnsResolver` directly.

```python
LICOR_HOSTNAMES = ("licor.local", "licor-6800.local")
LICOR_SERVICE_TYPES = (
    "_ssh._tcp.local.",
    "_sftp-ssh._tcp.local.",
    "_http._tcp.local.",
    "_workstation._tcp.local.",
)

class LiDiscovery(QObject):
    host_found = Signal(str, str)   # (hostname, ip)
    finished   = Signal(int)        # number of hosts found

    def start(self, duration_s: float = 5.0) -> None
    def stop(self) -> None
```
- **Path 1 (plain mDNS)**: a short-lived worker thread runs `socket.gethostbyname(name)` for each entry in `LICOR_HOSTNAMES`, emits `host_found(name, ip)` on success, swallows `socket.gaierror`. Does not depend on the `zeroconf` package.
- **Path 2 (zeroconf)**: lazy-imports `zeroconf` inside `start()` — on `ImportError`, logs to status bar and proceeds with Path 1 only. Uses `Zeroconf()` + one `ServiceBrowser` per entry in `LICOR_SERVICE_TYPES`, handlers route through `_on_service`. On `ServiceStateChange.Added`, resolves service info and filters `info.server`/`name` by `"licor" in name.lower() or "li-6800" in name.lower()`. Emits `host_found`. Deduplicates against already-emitted hosts.
- `start(duration_s)` uses a `QTimer.singleShot` to call `stop()` and emit `finished(count)`.
- `stop()` closes the Zeroconf instance and joins the plain-mDNS thread.
- Used by `LiControlPanel` on first show (via `_init_li_control`) and on **Scan** clicks.
- Failure modes (zeroconf missing, no services, all `gaierror`): `finished(0)` fires; `_on_li_discovery_finished` surfaces the firewall hint.

### `gui/li_worker.py` — QThread wrapping paramiko
Mirrors [gui/serial_worker.py](gui/serial_worker.py). Holds `_client: SSHClient` and `_sftp: SFTPClient`, created once on first connect and reused across all jobs.

**Lazy-import strategy (critical).** `paramiko` is imported at the **top** of `li_worker.py`, not inside `run()`. The `ImportError` must surface *synchronously* on the main thread at the `from li_worker import LiWorker` call site (inside `_on_li_connect`), where the try/except actually lives. Importing inside `run()` would raise on the worker thread — `_on_li_connect` would return cleanly, the worker would crash silently, no signal would fire, and the user's Connect click would produce nothing visible. The "lazy" story is preserved at the **module** level: `main_window.py` does not import `li_worker` at its top; it imports only when `_on_li_connect` runs for the first time.

```python
class LiWorker(QThread):
    connected      = Signal(str)
    disconnected   = Signal()
    ack_received   = Signal(dict)   # normalized; see _normalize_ack below
    error_received = Signal(str)

    def open_connection(self, cfg: LiConfig) -> None
    def close_connection(self) -> None
    def send_setpoints(self, sp: LiSetpoints, *, timeout_s: float | None = None) -> str
    def send_stop(self) -> str
    def abort_current(self) -> None
```
No `busy_changed` / `status_message` — every state change is covered by `connected` / `disconnected` / `ack_received` / `error_received` plus status-bar messages from the `MainWindow` slots.

**cmd_id generation site (locked):** `send_setpoints` (called on the main thread) invokes `new_cmd_id()`, stuffs the id into the job dict alongside `{"setpoints": sp, "timeout_s": timeout_s}`, enqueues into `queue.Queue`, and returns the id synchronously. The worker never calls `new_cmd_id()`.

**`ack_timeout_s` resolution rule (locked):** inside the worker loop, the timeout used for the poll is `effective = job["timeout_s"] if job.get("timeout_s") is not None else cfg.ack_timeout_s`. Per-step override beats config default; config default beats nothing.

**ACK normalization (locked).** [Li-Control/RemoteEnvMeasure.py](Li-Control/RemoteEnvMeasure.py) writes field names that aren't guaranteed to be stable across firmware tweaks. `LiWorker` normalizes every ack through a single `_normalize_ack(raw: dict) -> dict` function before emitting `ack_received`. The canonical shape is:
```python
{
  "cmd_id":    str,
  "CO2_r":     float | None,   # from raw "CO2_r" / "CO2r" / "co2_r"
  "CO2_s":     float | None,
  "H2O_r":     float | None,
  "H2O_s":     float | None,
  "Tchamber":  float | None,   # from raw "Tchamber" / "Tcham" / "T_chamber"
  "Tleaf":     float | None,
  "RHcham":    float | None,   # from raw "RHcham" / "RH_cham"
  "PPFD_in":   float | None,
  "ts":        float,          # epoch seconds; falls back to time.time() if raw has no ts
  "error":     str,            # "" if no error
}
```
Missing fields are set to `None` (not 0, so downstream code can distinguish "sensor offline" from "reading was zero"). Every other field in the raw ack is dropped. The mapping lives next to `_normalize_ack` as a commented alias dict so firmware changes are one-line edits.

**Host key & auth.** `paramiko.SSHClient.set_missing_host_key_policy(...)` is set **before** `connect()`. Default `AutoAddPolicy` (first-run trust into `~/.ssh/known_hosts`), configurable via `LiConfig.host_key_policy`. Key-only auth via `key_filename`; no password field.

**Keepalive.** After connect:
```python
transport = client.get_transport()
if transport is not None:
    transport.set_keepalive(cfg.keepalive_s)
```

Run loop per job:
1. `json.dumps(cmd)` → `sftp.putfo(BytesIO, REMOTE_TMP)`.
2. `sftp.posix_rename(REMOTE_TMP, REMOTE_CMD)` — atomic, no shell quoting.
3. Poll `REMOTE_ACK` every `poll_interval_s`:
   - `sftp.open(REMOTE_ACK, "r").read()`; `json.loads(...)` wrapped in `try/except JSONDecodeError` — partial-read races from the on-device writer are expected, caught, retried silently.
   - Match `cmd_id` → emit `ack_received`.
4. Exit: match → emit `ack_received`; `_abort` event → emit `error_received("aborted")`; elapsed > job's `timeout_s` (or `cfg.ack_timeout_s` fallback) → emit `error_received("timeout")`.
5. On `paramiko.SSHException` / `socket.error`: emit `error_received`, close handles, one reconnect attempt, else `disconnected`.

### `gui/li_panel.py` — left-panel widget
`QGroupBox("Li-Control")`, hidden unless the menu toggle is on.

**Import rule (hard):** `li_panel.py` must **not** import `paramiko`, `zeroconf`, `li_worker`, or `li_discovery` at module top. It imports `li_control` (Qt-free, pure dataclasses) for type hints. `LiWorker` and `LiDiscovery` are constructed by `MainWindow` and passed in via signal wiring. This keeps the lazy-import story intact: loading `li_panel` at startup does not pull `paramiko` or `zeroconf` into `sys.modules`.

**Constructor signature:** `LiControlPanel(parent=None)` — no `data_dir` argument. `data_dir` is only needed by `LiRecorder`, which is created in `_on_li_sequence_start`. The panel never sees a filesystem path.

Subgroups:
- **SSH**: host `QComboBox` (editable, populated by `LiDiscovery`), **Scan** button, username, key path + Browse, host-key-policy combo, Connect/Disconnect, status label.
- **Manual Setpoints**: `QDoubleSpinBox` for `co2_r`, `tair`, `rh_air`, `qin`, `flow`, `fan_rpm`, `pressure`; checkboxes `wait_for_co2`, `log`; `co2_tol`, `wait_s` spin boxes; **Send** + **Stop** buttons.
- **Readback**: read-only labels for `CO2_r`, `CO2_s`, `H2O_r`, `H2O_s`, `Tchamber`, `Tleaf`, `RHcham`, `PPFD_in`, updated on `ack_received`.
- **Sequence**: **Load sequence…** (`QFileDialog` → `.json`), `QListWidget` of steps, progress label "step i / n", **Run** / **Abort** buttons. Abort button text flips to "Aborting — waiting for current step…" on click and resets to **Abort** (disabled) on `finished` / `aborted`.

Signals:
```python
scan_requested       = Signal()
connect_requested    = Signal(object)   # LiConfig
disconnect_requested = Signal()
setpoints_requested  = Signal(object)   # LiSetpoints
stop_requested       = Signal()
sequence_loaded      = Signal(list)     # list[SequenceStep]
sequence_start       = Signal()
sequence_abort       = Signal()
```

Public slots (called by `MainWindow`):
```python
def on_connected(self, host: str) -> None       # enables Run / Send buttons
def on_disconnected(self) -> None                # disables Run / Send buttons
def on_sequence_started(self) -> None            # Run→disabled, Abort→enabled "Abort"
def on_sequence_aborting(self) -> None           # Abort→"Aborting — waiting for current step…"
def on_sequence_ended(self) -> None              # Run→enabled, Abort→disabled "Abort"
def on_ack_received(self, ack: dict) -> None     # updates readback labels
```
Run and Send are disabled until `on_connected` fires, then enabled. `on_sequence_ended` is called from `_on_li_sequence_start`'s `finished`/`aborted` connections, guaranteeing the button state is always reset regardless of how the sequence ends.

### `gui/li_sequence.py` — sequence loader + runner

```python
@dataclass
class SequenceStep:
    name: str
    setpoints: LiSetpoints
    ack_timeout_s: float | None = None
    post_wait_s: float = 0.0            # optional settle AFTER row is written, before next step

def load_sequence(path: Path) -> list[SequenceStep]:
    """Raises ValueError on JSON parse or schema validation failure."""
    ...

def validate_sequence(raw: dict) -> list[str]   # list of errors, empty = ok

class SequenceRunner(QObject):
    step_started  = Signal(int, object)
    step_finished = Signal(int, dict)
    finished      = Signal()
    aborted       = Signal(str)    # reason
    def __init__(self, li_worker: LiWorker, main_window): ...
    def start(self, steps: list[SequenceStep], recorder: "LiRecorder"): ...
    def abort(self): ...
```

**`load_sequence` contract (locked): raises `ValueError` on parse failure (`json.JSONDecodeError` wrapped) or validation failure (message built from `validate_sequence` errors). The caller catches and surfaces to the status bar.**

**Runner state machine.** All slots live on the main thread. Stores `self._expected_cmd_id` and ignores any `ack_received` whose id doesn't match — prevents stray manual acks from advancing the sequence.

Per step:
1. `main_window._in_sequence = True` (guards `_on_li_ack` against double-writes on the LiRecorder).
2. `step_started(i)` → `self._expected_cmd_id = li_worker.send_setpoints(step.setpoints, timeout_s=step.ack_timeout_s)`.
3. On matching `ack_received(ack)`: stash `ack`. **If `main_window._worker is not None and main_window._worker.isRunning()`**: trigger fresh sensor read via `main_window._worker.send_command(protocol.CMD_ENV)` followed by `CMD_SPEC_FLASH` / `CMD_SPEC` (matching the current mode); then set up the spec wait (see "One-shot spec wait" below). **Else (serial not connected)**: skip straight to step 5 with `spec=None`, `bme=None`, `notes="spec_unavailable"`.
4. On spec arrival (runner's one-shot handler fires): `_disarm_spec_wait()` first (disconnect + cancel timer), then snapshot fresh `spec` + current `main_window._last_bme` + stashed `ack`.
5. Call `recorder.write_row(step_index=i, step_name=step.name, ack=ack, spec=spec, bme=bme, notes=notes)` — `LiRecorder` writes empty strings for every spec/BME column when `spec` or `bme` is None. Emit `step_finished(i, ack)`.
6. `QTimer.singleShot(step.post_wait_s * 1000, self._advance)`.
7. `_advance` **first checks `self._aborting`** — if set, emit `aborted("user")` and return without starting the next step. Otherwise move to step `i+1`, or if last step clear state, resume `_acq_timer` if it was paused, emit `finished`.

**One-shot spec wait (locked mechanism).** Plain `connect` + explicit `disconnect` in a helper — no reliance on `Qt.SingleShotConnection`:
```python
def _arm_spec_wait(self):
    self._spec_slot = self._on_spec_arrived   # bound method, stable reference
    self._main._worker.spec_received.connect(self._spec_slot)
    self._spec_timer = QTimer(self)
    self._spec_timer.setSingleShot(True)
    self._spec_timer.timeout.connect(self._on_spec_timeout)
    self._spec_timer.start(int(self._spec_timeout_s * 1000))

def _disarm_spec_wait(self):
    if self._spec_slot is not None:
        try:
            self._main._worker.spec_received.disconnect(self._spec_slot)
        except (RuntimeError, TypeError):
            pass   # already disconnected
        self._spec_slot = None
    if self._spec_timer is not None:
        self._spec_timer.stop()
        self._spec_timer = None
```
Both `_on_spec_arrived` and `_on_spec_timeout` call `_disarm_spec_wait()` as their **first** statement, so a late-arriving spec after a timeout cannot re-trigger the slot and write a duplicate row for the wrong step.

**Error paths:**
- `error_received` from `LiWorker` during a step (LiCor timeout, disconnect, aborted) → abort the sequence, emit `aborted(reason)`.
- ACK with non-empty `error` field → abort, emit `aborted(ack["error"])`.
- **Spec-timeout does NOT abort.** `_disarm_spec_wait()` is called first, `notes="spec_timeout"` is stamped on the row, the row is written with empty spec/BME, and the runner proceeds to step 5 / 6 / 7 normally. This lets the user drive the LI-6800 even when the embedded sensors are unplugged or hung.

**Abort semantics (best-effort, documented).** The on-device BP [Li-Control/RemoteEnvMeasure.py](Li-Control/RemoteEnvMeasure.py) only checks `action: "stop"` *between* commands — it cannot interrupt an in-flight `SETCONTROL` / `wait_for_co2` loop. So `abort()`:
- Sets `self._aborting = True`.
- Queues `build_stop_cmd` (picked up after current job resolves).
- Panel shows "Aborting — waiting for current step…".
- On the next `ack_received` or `error_received`, emits `aborted`, clears `_in_sequence`, resumes `_acq_timer`.
- Worst-case latency = remainder of in-flight wait + `ack_timeout_s`.

**Refusal conditions before Run:**
- `self._li_worker is None or not connected` → error "connect the LI-6800 first".
- `load_sequence` raised → error with its message.
- **No check on `self._worker`** — a sequence can run with the spectrometer entirely disconnected (rows will carry empty spec/BME fields and `notes="spec_unavailable"`). Only the LI-6800 is required.

### `gui/li_recorder.py` — combined TSV writer
Composes (doesn't subclass) [gui/recorder.py](gui/recorder.py). Constructor takes `data_dir: Path` — `MainWindow._init_li_control()` passes `base_dir / "data"` (same resolution as the existing `self._recorder` at [gui/main_window.py:116](gui/main_window.py#L116)).

Columns:
```
timestamp, step_index, step_name,
co2_r_set, tair_set, rh_air_set, qin_set,
CO2_r, CO2_s, H2O_r, H2O_s, Tchamber, Tleaf, RHcham, PPFD_in,
<spec channels...>, T, P, RH, Gas,
model, mode, gain, atime, astep, led,
notes
```

`LiRecorder.start_recording(filename, model, mode, gain, atime, astep, led, spec_channels)` mirrors the existing [gui/recorder.py:24-61](gui/recorder.py#L24-L61) signature and opens a file named `YYYY-MM-DD_HH-MM-SS_<filename>_licontrol.txt`.

`LiRecorder.write_row(...)` calls `self._file.flush()` after every `\t`-joined row write — matching the normal `Recorder` behavior at [gui/recorder.py:85](gui/recorder.py#L85). A crash mid-sequence leaves all completed rows on disk.

**Spec-column handling when the spectrometer was never connected.** `start_recording` accepts `spec_channels: list[str]`; if `spec_channels == []`, the recorder simply omits the per-channel columns entirely. This means a LiCor-only session produces a narrower TSV without misleading placeholder columns for a spectrometer model that was never in use. A session that had the spec connected even briefly still writes the full model-specific column set with empty strings where spec was unavailable.

The `notes` column values:
- Sequence row, fresh spec OK → `""`.
- Sequence row, spec timed out or serial not connected → `"spec_unavailable"` (or `"spec_timeout"` for the specific case).
- Manual-Send row with GUI acquisition running → `"manual_send,spec_age<=1_acq_interval"`.
- Manual-Send row without serial → `"manual_send,spec_unavailable"`.

`LiRecorder.write_row(step_index, step_name, ack, spec, bme, notes)` accepts `spec=None` / `bme=None` and writes empty strings for the corresponding columns.

## Sequence JSON schema

```json
{
  "name": "co2_ramp_example",
  "steps": [
    {
      "name": "baseline",
      "setpoints": {
        "co2_r": 400, "tair": 25, "rh_air": 60, "qin": 500,
        "wait_for_co2": true, "co2_tol": 2, "wait_s": 30, "log": true
      },
      "ack_timeout_s": 600
    },
    {
      "name": "step_800",
      "setpoints": { "co2_r": 800, "wait_for_co2": true, "co2_tol": 3 },
      "ack_timeout_s": 900
    },
    {
      "name": "recover",
      "setpoints": { "co2_r": 400, "wait_s": 60 }
    }
  ]
}
```

`validate_sequence` checks: top-level `steps` is a non-empty list; each step has `name` (str) and `setpoints` (dict); `setpoints` keys are a subset of `LiSetpoints` fields; `ack_timeout_s > 0` when present; `post_wait_s >= 0` when present.

## Modifications to existing files

### [gui/main_window.py](gui/main_window.py)
- **Imports (~line 35)** — add `import json`, `from PySide6.QtCore import QStandardPaths`. New-module imports are **lazy**, done inside `_init_li_control` and `_on_li_connect`.
- **`__init__` ([gui/main_window.py:102](gui/main_window.py#L102))**:
  ```python
  self._li_worker = None
  self._li_panel = None
  self._li_runner = None
  self._li_discovery = None
  self._li_recorder = None
  self._last_manual_cmd_id: str | None = None   # matched in _on_li_ack
  self._acq_was_running = False
  self._gui_cfg_path = self._resolve_config_path()
  self._gui_cfg = self._load_gui_config()
  self._li_enabled = bool(self._gui_cfg.get("li_control_enabled", False))
  ```
  **Note:** no `_in_sequence` flag — manual-row logging in `_on_li_ack` gates on `cmd_id` match instead (see `_on_li_ack` below), so the two ack-handling paths are naturally disjoint.
- **`_resolve_config_path()`** (new helper): returns `Path(QStandardPaths.writableLocation(QStandardPaths.AppConfigLocation)) / "CO2Dot" / "gui_config.json"` when `sys.frozen`, else `base_dir / "gui_config.json"`. Creates parent directory on write.
- **`_build_ui` ([gui/main_window.py:142](gui/main_window.py#L142))** — call `self._build_menu()`. If `self._li_enabled`, call `self._init_li_control()`.
- **`_build_left_panel` ([gui/main_window.py:164](gui/main_window.py#L164))** — after `layout.addWidget(grp_set)` (line 258) and before `layout.addStretch()`, store the layout on `self._left_layout` so `_init_li_control` can `insertWidget` later. Replace `setFixedWidth(230)` with `setMinimumWidth(230)`; visually verify hidden-state geometry.
- **`_build_menu`** (new) — `menuBar().addMenu("View")` with a checkable `QAction("Enable Li-Control")`; toggled slot calls `self._toggle_li_control(checked)` which shows/hides the panel (creating it lazily on first enable), persists to `gui_config.json`.
- **`_init_li_control`** (new) — lazy-imports `LiControlPanel` from `li_panel` (pulls no heavy deps), instantiates it with no `data_dir`, wires all panel signals to `_on_li_*` slots (including `connected`/`disconnected` → panel's Run-button enabler), inserts into `self._left_layout`. Then tries `from li_discovery import LiDiscovery` inside a `try/except ImportError` — on success, constructs and `start(5.0)`; on `ImportError` (zeroconf missing), falls back to `from li_discovery_plain import PlainMdnsResolver` and runs that alone; on any other failure the panel stays fully functional with manual hostname entry (status bar notes the discovery-disabled state). **`_init_li_control` is itself wrapped in its own try/except at the call site in `_build_ui`** so a broken lazy import cannot prevent the GUI from launching — if it raises, the menu toggle is forced back to unchecked, the flag is persisted, and the user sees a status-bar error.
- **New slots** (all wrapped in try/except for `ImportError` on paramiko/zeroconf with a helpful status-bar message):
  - `_on_li_scan()` — creates `LiDiscovery` on demand, starts a 5 s browse.
  - `_on_li_host_found(name, ip)` — adds to panel host combo if not already present.
  - `_on_li_discovery_finished(count)` — if `count == 0`, status bar: "No LI-6800 found — check firewall or type hostname manually".
  - `_on_li_connect(cfg)` — **lazy-imports the `li_worker` module** (`from li_worker import LiWorker`) inside a `try/except ImportError`. `li_worker.py` imports `paramiko` at its own module top, so the `ImportError` is raised synchronously here on the main thread. On `ImportError`: status bar "Install paramiko (`pip install paramiko>=3.4`) to use Li-Control". On success: creates `LiWorker` if None and calls `open_connection`.
  - `_on_li_disconnect()` — `close_connection`.
  - `_on_li_send(sp)` — `self._last_manual_cmd_id = self._li_worker.send_setpoints(sp)`.
  - `_on_li_stop()` — `self._li_worker.send_stop()`.
  - `_on_li_ack(ack)` — always updates readback labels. **Manual-row logging is gated purely by `cmd_id` match**: if `ack["cmd_id"] == self._last_manual_cmd_id` and `self._li_recorder is not None and self._li_recorder.is_recording`, write one combined row via `LiRecorder.write_row(step_index=-1, step_name="manual", ack=ack, spec=self._last_spec, bme=self._last_bme, notes=...)`. The `notes` value is: `"manual_send,spec_unavailable"` if `self._worker is None or not self._worker.isRunning()`; else `"manual_send,spec_age<=1_acq_interval"` if `self._acq_timer.isActive()`; else `"manual_send"`. Then `self._last_manual_cmd_id = None` so the same ack can't be logged twice. Runner acks carry a different `cmd_id` and are ignored by this path — no `_in_sequence` flag needed.
  - `_on_li_sequence_loaded(steps)` — store and populate the list widget.
  - `_on_li_sequence_start()` — run refusal checks (LI-6800 only); if `self._li_runner is not None`: disconnect its signals and `self._li_runner.deleteLater()` (prevents Qt-object leaks across re-runs); choose `spec_channels = protocol.channels_for_model(self._model) if self._worker is not None else []` (empty list means no spec columns at all when the spectrometer was never connected — see `LiRecorder` notes); start `LiRecorder(base_dir / "data")` with those channels; **if `self._acq_timer.isActive()`**: `self._acq_was_running = True; self._acq_timer.stop()` (one source of truth — don't also check `_worker` or `_running`); construct `SequenceRunner(self._li_worker, self)`; connect `runner.finished` and `runner.aborted` to (a) stop the recorder, (b) resume `_acq_timer` only if `_acq_was_running`, (c) call `self._li_panel.on_sequence_ended()` so the Abort button resets and Run re-enables; call `runner.start(steps, self._li_recorder)`.
  - `_on_li_sequence_abort()` — `self._li_runner.abort()`.
- **`closeEvent`** — shutdown order: abort `SequenceRunner` → `LiDiscovery.stop()` → `LiWorker.close_connection()` + `wait()` → existing `SerialWorker.close_port()`.
- **Config helpers** — `_load_gui_config()` / `_save_gui_config()` read/write `self._gui_cfg_path` as JSON with `encoding="utf-8"`, creating the parent dir on write.

### [gui/recorder.py](gui/recorder.py)
**No changes.**

### `gui/requirements.txt`
Add `paramiko>=3.4` and `zeroconf>=0.131`.

### `.gitignore`
Add `gui/gui_config.json` (only the dev-mode path; the frozen-mode path is outside the repo).

## SSH reuse

`LiWorker` owns a single `SSHClient` + `SFTPClient` created on first `open_connection()` inside `run()`. Policy set **before** `connect()`; keepalive set **after** (with transport None-check). All subsequent jobs reuse the same `_sftp` handle for `putfo` / `posix_rename` / `open(REMOTE_ACK).read()`. On `SSHException` / `socket.error`: emit `error_received`, close handles, attempt one reconnect; on second failure emit `disconnected` and drain the job queue into errors.

## Critical files to modify
- [gui/main_window.py](gui/main_window.py)
- [gui/requirements.txt](gui/requirements.txt)
- `.gitignore`

## Critical files to create
- [gui/li_control.py](gui/li_control.py)
- [gui/li_discovery.py](gui/li_discovery.py)
- [gui/li_worker.py](gui/li_worker.py)
- [gui/li_panel.py](gui/li_panel.py)
- [gui/li_sequence.py](gui/li_sequence.py)
- [gui/li_recorder.py](gui/li_recorder.py)
- `gui/sequences/example_co2_ramp.json`

## Files referenced but unchanged
- [gui/serial_worker.py](gui/serial_worker.py) — pattern reference for `LiWorker`; also exposes `spec_received` / `bme_received` used by the runner's one-shot.
- [gui/recorder.py](gui/recorder.py) — pattern reference for `LiRecorder`.
- [Li-Control/RemoteEnvMeasure.py](Li-Control/RemoteEnvMeasure.py) — on-device protocol (not modified).
- [Li-Control/Example_LiControl.ipynb](Li-Control/Example_LiControl.ipynb) — `send_and_wait_ack` reference.

## Verification

1. **Zero-impact check (feature disabled)** — launch with `li_control_enabled = false`. Confirm: no `paramiko` / `zeroconf` in `sys.modules`, left panel identical, existing spec/BME acquisition and recording unchanged.
2. **Protocol unit test** — `LiSetpoints(co2_r=399, tair=25, rh_air=50, wait_for_co2=True, co2_tol=5, wait_s=1, qin=0, log=True).to_cmd("abc")` produces the dict shape used in [Li-Control/Example_LiControl.ipynb](Li-Control/Example_LiControl.ipynb) cell `6fe6f876`.
3. **Sequence loader test** — good JSON, malformed JSON, and schema-invalid JSON; assert `load_sequence` raises `ValueError` with useful messages for the latter two.
4. **Mock SSH test** — monkey-patch `paramiko.SSHClient` with a fake that stores commands in memory and publishes a matching ACK after a configurable delay. Assert `ack_received` fires with the right `cmd_id`, `abort_current()` halts polling within `poll_interval_s`, and partial-JSON reads are retried silently.
5. **cmd_id filter test** — `SequenceRunner` ignores an `ack_received` whose `cmd_id` doesn't match `_expected_cmd_id` (simulated stray manual Send mid-sequence).
6. **Spec-timeout graceful-advance test** — mock `SerialWorker.spec_received` to never fire; runner must write the row with empty spec/BME and `notes="spec_timeout"` after `spec_timeout_s`, then advance to the next step (not abort).
6a. **Spec-not-connected test** — run a sequence with `self._worker = None`; runner must write rows with `notes="spec_unavailable"` for every step and reach `finished` normally.
6b. **Late-spec-after-timeout test** — mock `spec_received` to fire *after* the spec-timeout has already stamped the row and advanced; assert no second row is written for the prior step and the new spec does not corrupt the next step's row (i.e. `_disarm_spec_wait()` really disconnects first).
6c. **Abort-during-spec-wait test** — start a sequence, let the first ACK arrive, then call `runner.abort()` before the spec-wait resolves; assert the runner emits `aborted("user")` after the current step's row is written and does not advance to step 2.
6d. **ACK normalization test** — feed `_normalize_ack` a raw dict using alias field names (`CO2r`, `Tcham`, `RH_cham`); assert the canonical dict has the right canonical keys and `None` for missing fields.
7. **Discovery test** — mock `zeroconf.ServiceBrowser`, emit a fake service info with `server="licor-123.local."`; assert `LiControlPanel` host combo gets populated.
8. **Toggle round-trip** — enable via menu, close, relaunch; confirm `gui_config.json` persisted the flag and the panel shows on startup. Repeat with a frozen build to confirm the `QStandardPaths` path is used.
9. **Real LI-6800 smoke test** —
   - With `RemoteEnvMeasure.py` running on the instrument and mDNS visible: open the GUI, enable Li-Control, observe the host combo auto-populating.
   - Connect; verify status label turns green.
   - Send `co2_r=450, tair=25, rh_air=50, qin=0, wait_for_co2=false`; verify readback labels update and (if recording) one combined row lands in the TSV.
   - Load `sequences/example_co2_ramp.json`; run it; verify one row per step in the TSV with `step_index` increasing, fresh spec per row, and that `_acq_timer` is paused for the duration and resumed after.
10. **Disconnect recovery** — unplug network mid-session; confirm `error_received` fires, panel shows disconnected state, subsequent Connect re-establishes without restarting.
11. **Abort latency** — during a `wait_for_co2` step, click Abort and verify the button text flips to "Aborting — waiting for current step…" and the runner emits `aborted` only after the on-device BP returns.

## Risks / edge cases

- **Host key policy missing would break first connect** — mandatory `set_missing_host_key_policy` before `connect()`, default `AutoAddPolicy`.
- **Long `wait_for_co2` blocking ACK** — default `ack_timeout_s = 600`; per-step override via `SequenceStep.ack_timeout_s`. On timeout, the sequence aborts.
- **Sequence abort race** — best-effort; on-device BP can't interrupt `SETCONTROL` waits. Documented in the UI.
- **Stray ack advancing the runner** — filtered by `cmd_id`.
- **Partial `REMOTE_ACK` read** — on-device writer rewrites in place; poll catches `JSONDecodeError` and retries.
- **cmd_id collision** — `uuid4().hex[:12]` = 48 bits entropy, collision risk < 10⁻¹².
- **IPv6 link-local scope-id** — zeroconf returns global-scope addresses for instruments on the same LAN; link-local `fe80::...%iface` is not supported (users with link-local-only setups must use a global address or connect the LI-6800 to a router).
- **SSH idle disconnect** — `transport.set_keepalive(keepalive_s)` with None-check.
- **Spec never arrives after a sequence step** — `spec_timeout_s` (default 5 s) lets the runner advance with an empty-spec row and `notes="spec_timeout"`. No abort. Same behavior if the serial worker was never connected in the first place (`notes="spec_unavailable"`).
- **Normal `Recorder` still recording during a sequence** — the two recorders write to two different files; `_acq_timer` is paused during the sequence so the normal `Recorder` sees no new rows until the sequence finishes. No conflict.
- **Manual-Send spec staleness** — permissive model: row's spec may be up to one acquisition interval old, marked in the `notes` column.
- **`ImportError: paramiko` / `zeroconf`** — caught in `_init_li_control` / `_on_li_connect` / `_on_li_scan`; surfaced in the status bar with an install hint.
- **`gui_config.json` not writable in a frozen install** — use `QStandardPaths.AppConfigLocation` when frozen, creating the directory on first write.
- **Thread shutdown ordering** — abort `SequenceRunner` → `LiDiscovery.stop()` → `LiWorker.close_connection()` + `wait()` → existing `SerialWorker.close_port()`.
- **Windows path handling** — all JSON file I/O uses `encoding="utf-8"`; key file inputs accept forward slashes.
- **Crash mid-sequence** — LI-6800 remains at last-applied setpoints. Recommend users include a final "safe" step (baseline CO₂, lights off) in long sequences. Documented in `gui/sequences/example_co2_ramp.json`.
- **zeroconf fails to find the instrument** — user can type the hostname/IP manually into the editable combo; Scan button re-runs discovery on demand. **Discovery auto-runs only once, at `_init_li_control` time.** Toggling the panel off/on does not re-scan — users explicitly click Scan to refresh.
- **LI-6800 mDNS service type is not documented** — the multi-service browse + `licor.local` plain-mDNS fallback is defensive. On first real-hardware contact, log the full Avahi service list and tighten the filter.
- **No crash recovery for partial sequences** — a crash at step 25 of 50 leaves 25 rows on disk (flushed) and the LI-6800 at whatever its last-applied setpoints were. There is no resume path; restart the sequence from step 1, or edit the JSON to skip completed steps. Worth warning users in the example sequence README.
