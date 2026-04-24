# Fix Plan: firmware-CO2Dot

Revise the existing fix plan so the firmware can work with either an AS7341 or an AS7343 spectrometer on the same I2C address (`0x39`), while still addressing the previously identified robustness and command-path issues.

The current codebase is AS7341-only:
- `src/app/as7341_api.cpp` instantiates `Adafruit_AS7341` directly.
- `initAS7341()` assumes the device is an AS7341.
- command output is hard-coded to the AS7341 12-value layout.

That is not sufficient for AS7343 support because:
- both sensors use I2C address `0x39`;
- AS7341 and AS7343 use different identification registers and different register maps;
- the bundled Adafruit AS7341 driver validates the AS7341 chip ID and will reject AS7343;
- AS7343 exposes a different channel set and supports automatic 12-channel / 18-channel readout.

## Datasheet Constraints That Must Drive the Design

### Shared bus address, different identification
- **AS7341:** I2C address `0x39`; part identification is read via register `0x92`
- **AS7343:** I2C address `0x39`; part identification is read via register `0x5A` and the datasheet lists value `0x81`
- Consequence: `i2c_scan` can only prove that *something* exists at `0x39`; firmware must probe ID registers to know which spectrometer is populated

### Different register maps
- **AS7341:** LED register at `0x74`; ASTEP at `0xCA/0xCB`
- **AS7343:** LED register at `0xCD`; ASTEP at `0xD4/0xD5`
- Consequence: AS7343 cannot be supported by extending the AS7341 register code in-place without a device-specific backend

### Different spectral result shapes
- **AS7341:** 8 VIS bands plus Clear and NIR, acquired via SMUX sequencing
- **AS7343:** 11 VIS bands plus Clear and NIR; the datasheet recommends automatic 12-channel or 18-channel processing, with measurements stored in 18 data registers
- Consequence: the current fixed `uint16_t[12]` printing path is too narrow for AS7343

### LED driver capability versus board policy
- Both datasheets describe a programmable LED driver with 4 mA minimum and 2 mA steps up to 258 mA
- This board has a real hardware limit of **20 mA maximum**
- Consequence: firmware must enforce `20 mA` as a **hard board-level safety limit** on both AS7341 and AS7343, even though the sensor LED drivers support more

### Compatibility contract
- **Command-name compatibility only**
- Legacy command names `read_spectro`, `read_spectro_flash`, and `set_led` must continue to dispatch
- Response payload compatibility is **not** required
- Consequence: legacy command aliases may return the new generic `{"spectrometer":{...}}` payload rather than old `{"as7341":{...}}` keys
- Spectrometer errors must use the same complete JSON object shape in both LINE mode and JSON/openJII mode so the error contract is not mode-dependent

### Spectral output contract
- Expose a **normalized semantic channel set only**
- Do **not** expose raw AS7343 auto-read slots such as `DATA_0..DATA_17`, `2xVIS`, `FD`, or ADC channel numbers in the public API
- Consequence: the facade must convert backend-specific raw reads into stable, named spectral channels before command output

---

## Phase 0: Spectrometer Detection And Abstraction

### Step 0.1 - Introduce a generic spectrometer facade
- **Add files:** `include/app/spectrometer_api.h`, `src/app/spectrometer_api.cpp`
- **Keep existing file:** `src/app/as7341_api.cpp` as the AS7341-specific backend
- **Add files:** `include/app/as7343_api.h`, `src/app/as7343_api.cpp` for the AS7343-specific backend
- Define (initially in `spectrometer_api.h`; will be moved to `spectrometer_types.h` in Step 2.3 — do not add cross-includes before that move):
  - `enum class SpectrometerModel { None, AS7341, AS7343, ProbePendingAt0x39, UnknownAt0x39 };`
  - `extern bool spectrometer_available;`
  - `extern SpectrometerModel spectrometer_model;`
  - generic entry points such as:
    - `bool initSpectrometer();`
    - `bool spectrometer_read();`
    - `bool spectrometer_set_led_current(uint16_t ma);`
    - `bool spectrometer_read_flash(uint16_t led_current_ma);`
- **Phase sequencing rule:** introduce the generic facade first, but keep the existing AS7341 symbols and command flow temporarily until Phase 3 moves the command layer over
- Move `main.cpp` init onto the generic facade in Phase 0; move command handling in Phase 3

### Step 0.2 - Detect the populated spectrometer before backend init
- **Files:** `src/app/spectrometer_api.cpp`, `src/main.cpp`
- After `Wire.begin(3, 4)`, run the full detect-and-identify sequence inside a bounded startup retry window rather than doing a single immediate probe
- Retry policy:
  - retry for up to `5 ms`
  - use a short retry interval in the `200 us` to `500 us` range
  - within each retry attempt:
    - check whether `0x39` ACKs
    - if it ACKs, try the AS7343 ID read
    - if that does not match, try the AS7341 ID read
  - only classify the device as `None` or `ProbePendingAt0x39` after the retry window expires
- If the retry window ends with successful AS7343 ID match, select `SpectrometerModel::AS7343`
- Otherwise, if the retry window ends with successful AS7341 masked WHOAMI match, select `SpectrometerModel::AS7341`
- Otherwise, if `0x39` ACKed during the retry window but no supported ID matched, set `SpectrometerModel::ProbePendingAt0x39`
- If `0x39` does not ACK, set `SpectrometerModel::None`
- Print a precise boot message:
  - `Spectrometer detected: AS7341`
  - `Spectrometer detected: AS7343`
  - `Unidentified spectrometer at 0x39, will retry on first command`
  - `No spectrometer detected at 0x39`
- Runtime command contract:
  - if `spectrometer_model == SpectrometerModel::None`, spectrometer commands return `{"spectrometer":{"error":"not_available"}}`
  - if `spectrometer_model == SpectrometerModel::ProbePendingAt0x39`, the first spectrometer command reruns the full detect-and-identify sequence before deciding availability
  - if that on-demand retry succeeds, proceed normally
  - if that on-demand retry still sees `0x39` with no supported ID match, set `spectrometer_model = SpectrometerModel::UnknownAt0x39` and return `{"spectrometer":{"error":"unsupported_device_at_0x39"}}`
  - if `spectrometer_model == SpectrometerModel::UnknownAt0x39`, subsequent spectrometer commands return `{"spectrometer":{"error":"unsupported_device_at_0x39"}}`
  - use the same complete JSON object shape in both LINE mode and JSON/openJII mode; do not emit free-text spectrometer errors
- **AS7343 bank-switch write — verified safe:** the AS7343 chip ID read writes to register `0xBF` (CFG0 bank-select) before identity is confirmed; the Adafruit AS7341 library header lists every defined AS7341 register and `0xBF` does not appear — it falls in an undefined/reserved gap between `0xBE` (GPIO2) and `0xCA` (ASTEP_L); writing to a reserved register and restoring the prior value is safe; the AS7343-first detection order is frozen

### Step 0.3 - Use separate backends, not a forced shared driver
- **AS7341 backend:** continue using `Adafruit_AS7341`
- **AS7343 backend:** implement a small raw-register backend from the datasheet
- Do **not** try to initialize AS7343 through `Adafruit_AS7341::begin()`; the library validates the AS7341 chip ID and will reject AS7343

### Step 0.4 - Guard AS7343 commands as `not_yet_implemented` until Phase 2.2
- **File:** `src/app/spectrometer_api.cpp`
- Once Phase 0.2 detection is active, a detected AS7343 device will pass the `spectrometer_available` guard inside the facade functions but then fail the `as7341_available` guard, silently returning `{"spectrometer":{"error":"not_available"}}` even though the device is present and identified — this is a misleading error
- After `spectrometerPrepareLegacyCommand()` succeeds, if `spectrometer_model == SpectrometerModel::AS7343`, return `{"spectrometer":{"error":"not_yet_implemented"}}` immediately from `spectrometer_read()`, `spectrometer_set_led_current()`, and `spectrometer_read_flash()`
- Remove this guard when Phase 2.2 wires the AS7343 spectral read path into the facade
- **Phase sequencing rule:** this guard must be in place before Phase 2.2 lands; it prevents a silent wrong-error response during incremental development and testing

### >>> HARDWARE TEST CHECKPOINT 0 <<<
- [ ] With AS7341 hardware fitted, boot detects `AS7341`
- [ ] With AS7343 hardware fitted, boot detects `AS7343`
- [ ] With no device at `0x39`, boot reports no spectrometer and does not hang
- [ ] With an unexpected or not-yet-identified device at `0x39`, boot leaves spectrometer unavailable, reports retry-on-first-command behavior, and does not hang
- [ ] Cold-power-up and rapid reboot both still detect AS7343 reliably, confirming the startup retry window works
- [ ] With AS7343 hardware fitted, a spectrometer read command returns `{"spectrometer":{"error":"not_yet_implemented"}}` — not `{"spectrometer":{"error":"not_available"}}`

---

## Phase 1: Safety-Critical And Bus-Level Fixes

### Step 1.1 - Keep spectrometer init non-blocking
- **Files:** `src/app/as7341_api.cpp`, `src/app/as7343_api.cpp`, `src/app/spectrometer_api.cpp`
- Every init path must return `false` on failure and must never stall in an infinite loop
- `setup()` already has the right pattern for graceful failure handling; keep that behavior

### Step 1.2 - Centralize I2C init and prevent bus reconfiguration
- **File:** `src/main.cpp` - keep a single `Wire.begin(3, 4);` in `setup()` after `Serial.begin(115200)` and before any sensor init
- **File:** `src/app/debug_api.cpp` - keep `i2c_scan()` as a pure scanner with no `Wire.begin(...)` or bus clock reset
- Ensure no spectrometer backend reinitializes `Wire`

### Step 1.3 - Add include guards to all API headers
- **Files:** `include/app/as7341_api.h`, `include/app/as7343_api.h`, `include/app/bme68x_api.h`, `include/app/spectrometer_api.h`
- Add `#pragma once`

### Step 1.4 - Replace AS7341-specific availability flags with generic spectrometer state
- **Files:** `src/main.cpp`, `src/app/spectrometer_api.cpp`, `include/app/spectrometer_api.h`
  - keep `bme_available` defined in `main.cpp`
  - define `spectrometer_available` and `spectrometer_model` in `spectrometer_api.cpp` and expose as `extern` via `spectrometer_api.h`; `main.cpp` accesses them through the header — do **not** move them into `main.cpp`
- **Transitional rule for compile-stable phases:** until Phase 3 removes the old AS7341-only command path, keep `as7341_available` as a derived compatibility shim defined in `spectrometer_api.cpp`:
  - `as7341_available = spectrometer_available && spectrometer_model == SpectrometerModel::AS7341`
- **Files:** `include/app/as7341_api.h`, `include/app/as7343_api.h`, `include/app/spectrometer_api.h`
  - expose only the state each layer truly needs
- Guard at the generic API layer, not at the command layer:
  - `spectrometer_read()`
  - `spectrometer_set_led_current(uint16_t ma)`
  - `spectrometer_read_flash(uint16_t led_current_ma)`
- Backends may assume the correct model has already been selected by the facade

### >>> HARDWARE TEST CHECKPOINT 1 <<<
- [ ] Device boots with BME68x plus AS7341
- [ ] Device boots with BME68x plus AS7343
- [ ] Device boots with spectrometer disconnected and prints a warning, not a hang
- [ ] `i2c_scan`, then a spectrometer read command, still works on the same bus

---

## Phase 2: Spectrometer Backend Implementation

> **Implementation order within Phase 2:** Step 2.3 defines the `SpectrometerResult` struct that Steps 2.1 and 2.2 must populate. Implement Step 2.3 first, then Step 2.1 (AS7341 backend), then Step 2.2 (AS7343 backend), then Step 2.4 (facade payload emission).

### Step 2.1 - Stabilize the AS7341 backend
- **File:** `src/app/as7341_api.cpp`
- **Prerequisite:** define the `SpectrometerResult` struct in Step 2.3 before making the backend changes below
- Keep the current Adafruit-based path, but limit it to AS7341-only responsibilities
- Fix the LED disable path:
  - `0 mA` means disable LED and return success without trying to write a 0 mA setpoint
- Fix current variable truncation:
  - change `requested_current` from `uint8_t` to `uint16_t`
- Remove JSON printing from the low-level backend read function; backend reads should populate a `SpectrometerResult`, not print directly
- **Phase-stable compatibility rule:** when backend printing is removed in Phase 2, `spectrometer_read()` in `spectrometer_api.cpp` must immediately take over: call the backend to get a `SpectrometerResult`, then serialize and print the complete JSON object `{"spectrometer":{"model":"...","channels":{...}}}` directly from the facade function; this keeps LINE mode output valid while Phase 3 command migration is still pending
- Decouple the facade from the Adafruit library:
  - `spectrometer_api.cpp` currently includes `<Adafruit_AS7341.h>` to access `AS7341_WHOAMI` and `AS7341_CHIP_ID`; the facade must not include backend library headers
  - Define the AS7341 WHOAMI register address and chip-ID mask as file-scope `constexpr` values in `as7341_api.cpp`, not in `as7341_api.h`; they are internal implementation details and must not leak into the public header; the facade accesses them only through `as7341_readAndValidateChipId()`
- Resolve the backend–facade layering inversion:
  - `as7341_api.cpp` reads `as7341_available` which is defined in `spectrometer_api.cpp`; a backend must not depend on facade state
  - Remove the `as7341_available` availability guards from `as7341_readAll()` and `as7341_setLEDCurrent()`; the facade enforces `spectrometer_available` before calling into the backend, so the backend can assume it is only called when valid
  - The `as7341_available` shim may remain defined in `spectrometer_api.cpp` for the transitional period but must not be read by the backend
  - Remove the `extern bool as7341_available` declaration from `as7341_api.h`; the backend no longer references it, so the extern is a false dependency
- Move `readAs7341WhoAmI()` from `spectrometer_api.cpp` into the AS7341 backend; expose it in `as7341_api.h` as `bool as7341_readAndValidateChipId(uint8_t *raw_out)` so the detection helper in the facade calls a backend function rather than performing a raw I2C read directly against AS7341 registers

### Step 2.2 - Implement a minimal AS7343 backend from the datasheet
- **Files:** `include/app/as7343_api.h`, `src/app/as7343_api.cpp`
- Scope this backend to only what the firmware needs now:
  - init
  - configure gain / ATIME / ASTEP
  - read spectral data
  - switch LED on/off and set LED current
- Prefer the AS7343 automatic readout path instead of hand-recreating AS7341 SMUX logic:
  - after power-up, wait out the datasheet initialization window before assuming I2C access is valid; the AS7343 datasheet specifies this duration in the power-on sequence section — use that value explicitly, do not guess
  - set `PON=1`
  - explicitly keep flicker detection disabled: set `FDEN=0` during init and verify it remains disabled for spectral reads
  - configure gain / `ATIME` / `ASTEP` / `auto_smux` **while `SP_EN=0`**
  - use `auto_smux = 3` for automatic 18-channel readout
  - start one measurement by setting `SP_EN=1`
  - poll `STATUS2.AVALID` (`0x90`) with a timeout derived from the configured integration time plus a conservative guard margin
  - read the latched ASTATUS plus spectral data block in one burst (`0x94` through `0xB8`) so all returned values are concurrent
  - return the device to idle by clearing `SP_EN` after the read sequence
  - if polling times out or any read fails, clear `SP_EN` before returning failure so the device does not remain stuck in `ACTIVE`
- Treat saturation/status bits as part of the backend read result so the facade can decide whether to expose, warn, or reject a sample
- LED control must use the AS7343 LED register at `0xCD`
- **Register address verification:** all register addresses listed in this step (`0x90`, `0x94`–`0xB8`, `0xCD`, etc.) are plan-time references; verify every address against the AS7343 datasheet before coding; do not treat plan-listed addresses as authoritative
- Remove the `not_yet_implemented` guard introduced in Step 0.4 from `spectrometer_read()`, `spectrometer_set_led_current()`, and `spectrometer_read_flash()` in `spectrometer_api.cpp` once the AS7343 spectral read path is wired into the facade

### Step 2.3 - Define a generic spectrometer result model
- **Files:** `include/app/spectrometer_types.h` (new), `include/app/spectrometer_api.h`, `src/app/spectrometer_api.cpp`

#### Step 2.3a — Move `SpectrometerModel` to `spectrometer_types.h`
- Create `include/app/spectrometer_types.h` with `#pragma once` at the top
- Move `enum class SpectrometerModel` out of `spectrometer_api.h` and into `spectrometer_types.h`
- Move the `SpectrometerResult` struct definition (below) into the same new file
- Both the backends and the facade include `spectrometer_types.h` for type definitions; this breaks the circular include chain `backend → spectrometer_api.h → backend`
- Update `spectrometer_api.h` to include `spectrometer_types.h` instead of re-declaring the enum

#### Step 2.3b — Define `SpectrometerResult`
- `SpectrometerResult` must be defined concretely — not "for example"; the required definition is:
  ```cpp
  struct SpectrometerResult {
      SpectrometerModel model;
      uint8_t  channel_count;   // number of valid entries in channels[]
      uint16_t channels[13];    // indexed 0..channel_count-1; 13 = AS7343 max
  };
  ```
- Set `channel_count = 10` for AS7341; set `channel_count = 13` for AS7343
- Channel names are **not** stored in `SpectrometerResult`; use per-model static `const char * const[]` lookup tables defined in `spectrometer_api.cpp`; on ESP32, `const char *` string literals are stored in flash via XIP — `PROGMEM` is an AVR concept with no effect here; no extra annotation is needed

#### Step 2.3c — AS7341 channel mapping
- The Adafruit `readAllChannels(uint16_t[12])` layout has two SMUX passes; indices 4 and 5 are intermediate clear/NIR readings from the first pass and must be **skipped**; the mapping to `SpectrometerResult.channels[]` is:

  | Adafruit index | Semantic name | `channels[]` index |
  |---|---|---|
  | 0 | `f1_415` | 0 |
  | 1 | `f2_445` | 1 |
  | 2 | `f3_480` | 2 |
  | 3 | `f4_515` | 3 |
  | 4 | *(skip — SMUX pass 1 clear)* | — |
  | 5 | *(skip — SMUX pass 1 NIR)* | — |
  | 6 | `f5_555` | 4 |
  | 7 | `f6_590` | 5 |
  | 8 | `f7_630` | 6 |
  | 9 | `f8_680` | 7 |
  | 10 | `clear` | 8 |
  | 11 | `nir` | 9 |

- The AS7341 channel name lookup table must match this order exactly

#### Step 2.3d — Output contracts
- Public output must be **semantic**, not raw-register-oriented
- Minimum required normalized output contract:
  - **AS7341:** `f1_415`, `f2_445`, `f3_480`, `f4_515`, `f5_555`, `f6_590`, `f7_630`, `f8_680`, `clear`, `nir`
  - **AS7343:** `f1_405`, `f2_425`, `fz_450`, `f3_475`, `f4_515`, `fy_555`, `f5_550`, `fxl_600`, `f6_640`, `f7_690`, `f8_745`, `clear`, `nir`
  - **Channel name verification required:** `fy_555` and `f5_550` are only 5 nm apart; verify both are distinct physical filters in the AS7343 datasheet and that the names are not a copy-paste error before encoding them; the final names must match the datasheet band labels exactly
- AS7343 bring-up rule:
  - do **not** freeze a `DATA_0..DATA_17 -> semantic band` table purely from plan review
  - first implement a temporary bring-up path that can dump raw `DATA_0..DATA_17` for AS7343 while flicker is disabled and auto 18-channel mode is active; gate this path behind a compile-time flag `AS7343_BRINGUP_RAW_DUMP` (default: disabled, never set in release builds)
  - use that bring-up output on real hardware to confirm the band ordering under known illumination
  - only after that confirmation, encode the final fixed `DATA_0..DATA_17 -> semantic band` table in the backend and **delete** the raw bring-up path entirely; do not leave it behind a disabled flag
- Until the final table is frozen from bring-up evidence, AS7343 support is considered incomplete
- For AS7343, derive the public `clear` value from the automatic-readout `2xVIS` clear/VIS data using a fixed normalization rule:
  - collect all non-saturated, valid `2xVIS` values produced across the full automatic 18-channel sequence
  - compute the integer average of those valid `2xVIS` values
  - publish that average as the single semantic `clear` output
  - if every `2xVIS` sample in the sequence is invalid or saturated, fail the spectrometer sample rather than publishing a fabricated `clear`
- **Normalization note:** for AS7343, exported `clear` is a firmware-normalized broadband visible proxy derived from the default `2xVIS` readout. It is not asserted to be physically identical to AS7341 `clear`; it is provided to keep a stable cross-device semantic contract
- **Contract status:** the precise `DATA_n` slot positions that carry `2xVIS` values depend on `auto_smux = 3` hardware behavior and are not verifiable from the datasheet alone; treat the AS7343 `clear` derivation contract as **explicitly incomplete** until the bring-up data from Step 2.2 confirms which slots to average; encode the final slot selection only after that hardware confirmation
- Do not expose `FD`, duplicated `2xVIS`, or `DATA_n` slots in the command payload
- Do not force AS7343 data into the AS7341 array indexing scheme

### Step 2.4 - Normalize output naming at the facade layer
- **File:** `src/app/spectrometer_api.cpp`
- The command layer should emit a complete JSON object:
  - `{"spectrometer": { "model": "...", "channels": { ... } }}`
- Move payload emission into Phase 2 together with the facade so output behavior stays valid before Phase 3 finishes the dispatch-table migration
- Do **not** keep the old `"as7341"` payload key, even when the hardware is AS7341

### Step 2.5 - Make LED current policy explicit
- **Files:** `src/app/spectrometer_api.cpp`, `src/app/as7343_api.cpp`
- Introduce a board-level constant such as `kSpectrometerLedBoardMaxMa`
- Set `kSpectrometerLedBoardMaxMa = 20`
- Enforce this limit with **silent clamping** in the facade before passing the value to either backend: `if (led_current_ma > kSpectrometerLedBoardMaxMa) led_current_ma = kSpectrometerLedBoardMaxMa;`
- After the backend applies sensor quantization (minimum 4 mA, 2 mA steps), `spectrometer_set_led_current()` prints the clamped and quantized actual value:
  - `{"spectrometer":{"led_current_ma":<actual_ma>}}` for any non-zero request
  - `{"spectrometer":{"led_current_ma":0}}` when LED is disabled
- Preserve the sensor-level quantization behavior:
  - minimum 4 mA when enabling
  - 2 mA steps
  - `0 mA` means off — call `enableLED(false)` and return without writing a current setpoint
- Remove the free-text `Serial.println("AS7341: LED current must be <= 20 mA")` and the `led_current_ma = 20` cap code from `as7341_setLEDCurrent()`; once the facade enforces the cap, this backend code is dead and its free-text output violates the structured-error contract (`as7341_api.cpp` was already addressed in Step 2.1)
- Do not add a cap inside the AS7343 LED function either; all clamping is the facade's responsibility

### Step 2.6 - Unify dark / lit / diff measurement flow
- **File:** `src/app/spectrometer_api.cpp`
- Replace `cmd_read_as7341_flash()` with a generic `spectrometer_read_flash(...)`
- Sequence:
  1. read dark
  2. enable LED at requested current
  3. wait a settling delay: `delay(max((ATIME + 1) * (ASTEP + 1) * 2.78 / 1000 + 2, 5))` ms — formula gives one full integration period (in ms) plus a 2 ms guard margin, with a hard floor of 5 ms; retrieve `ATIME` and `ASTEP` from the active backend before computing; applies to both AS7341 and AS7343
  4. read lit
  5. disable LED
  6. compute diff
- Output payload: a single complete JSON object containing all three results:
  - `{"spectrometer_dark":{"model":"...","channels":{...}},"spectrometer_lit":{"model":"...","channels":{...}},"spectrometer_diff":{"model":"...","channels":{...}}}`
- Diff is computed per named channel: `diff[name] = lit[name] > dark[name] ? lit[name] - dark[name] : 0`
- Keep `read_spectro_flash` as a compatibility alias for the canonical `spectrometer_read_flash`

### Step 2.7 - Add diagnostic CLI commands
- **Files:** `src/app/spectrometer_api.cpp`, `include/app/spectrometer_api.h`, `src/app/bme68x_api.cpp`, `include/app/bme68x_api.h`, `src/app/debug_api.cpp`, `include/app/debug_api.h`, `src/app/commands.cpp`

#### `spectrometer_status`
- Reports current spectrometer model, availability, and active configuration
- **Output when spectrometer is available and fully configured:**
  ```
  {"spectrometer_status":{"model":"AS7341","available":true,"atime":NNN,"astep":NNN,"gain":N}}
  ```
- **Output when detected but commands not yet implemented (AS7343 with Phase 0.4 guard active):**
  ```
  {"spectrometer_status":{"model":"AS7343","available":false}}
  ```
- **Output when not available:**
  ```
  {"spectrometer_status":{"model":"None","available":false}}
  ```
- Emit `atime`, `astep`, and `gain` fields only when `spectrometer_available == true`; omit them in all unavailable states to keep the output unambiguous
- `gain` is emitted as an integer (the enum ordinal stored in the device register); this is a debug command — raw values are acceptable
- Add `as7341_gain_t as7341_getGain()` to `as7341_api.cpp` and `as7341_api.h` using `as7341.getGain()`
- When `spectrometer_model == SpectrometerModel::AS7343` and Phase 2.2 is not yet implemented, emit only `model` and `available`; once Phase 2.2 adds AS7343 getters (`as7343_getAtIME()`, `as7343_getAStep()`, `as7343_getGain()`), add those fields too
- Add `void cmd_spectrometer_status();` to `spectrometer_api.h`; implement in `spectrometer_api.cpp`

#### `bme_status`
- Reports BME availability only; does not perform a measurement
- **Output:**
  ```
  {"bme_status":{"available":true}}
  ```
  or
  ```
  {"bme_status":{"available":false}}
  ```
- Add `void cmd_bme_status();` to `bme68x_api.h`; implement in `bme68x_api.cpp` reading `bme_available`

#### `reboot`
- Initiates a software reset of the ESP32
- **Output before reset:**
  ```
  {"reboot":"initiated"}
  ```
- Implementation: print the JSON object, call `Serial.flush()`, then `esp_restart()` (from `<esp_system.h>`)
- Add `void cmd_reboot();` to `debug_api.h`; implement in `debug_api.cpp`

#### Wiring for Phase 2
- For Phase 2 (before the dispatch-table migration in Step 3.1): add all three commands to the if-else chain in `handleCommandText()` in `src/app/commands.cpp`
- Step 3.1 will move them into `kCmds[]` as canonical entries; no aliases needed for debug commands

### >>> HARDWARE TEST CHECKPOINT 2 <<<
- [ ] On AS7341 hardware, generic spectrometer read returns AS7341 model plus valid channels
- [ ] On AS7343 hardware, generic spectrometer read returns AS7343 model plus valid channels
- [ ] On both sensors, `set_led,0` disables the LED
- [ ] On both sensors, `set_led,25` clamps to the hard `20 mA` board limit and still turns the LED on
- [ ] On both sensors, non-zero LED current turns the LED on and readback is consistent with the hard board cap and device quantization
- [ ] Flash-read dark / lit / diff works on both sensor variants; output is a single valid JSON object with `spectrometer_dark`, `spectrometer_lit`, and `spectrometer_diff` keys each containing `model` and `channels`
- [ ] AS7343 timeout path clears `SP_EN` and the next measurement can still succeed
- [ ] AS7343 saturated or invalid `2xVIS` samples are excluded from `clear` averaging; if all are invalid, the sample fails cleanly
- [ ] `spectrometer_status` on AS7341 returns `available:true` with `atime`, `astep`, and `gain` matching the values set in `initAS7341()`
- [ ] `spectrometer_status` on AS7343 after Phase 2.2 returns `available:true` with `atime`, `astep`, and `gain` matching AS7343 init values
- [ ] `spectrometer_status` with no spectrometer fitted returns `{"spectrometer_status":{"model":"None","available":false}}`
- [ ] `bme_status` returns `available:true` when BME is fitted; `available:false` when not
- [ ] `reboot` emits `{"reboot":"initiated"}`, device restarts, boot banner reprints on reconnect

---

## Phase 3: Command Infrastructure And JSON Path

### Step 3.1 - Add generic command wrappers and populate the dispatch table
- **File:** `src/app/commands.cpp`
- Create `CmdFn` wrappers for:
  - `cmd_bme_read` — emits `{"bme":{"temp_c":...,"humidity_pct":...,"pressure_hpa":...,"gas_resistance_ohm":...}}`
  - `cmd_spectrometer_read` — delegates to `spectrometer_read()`
  - `cmd_spectrometer_read_flash` — parses `argv[0]` as LED current with `atoi()`; if `argc == 0` or `atoi(argv[0]) < 0`, use default of 10 mA; delegates to `spectrometer_read_flash(led_ma)` — document the zero/negative→default behavior in a comment
  - `cmd_spectrometer_set_led` — parses `argv[0]` as LED current with `atoi()`; negative values clamp to 0 (LED off); delegates to `spectrometer_set_led_current(led_ma)`
  - `cmd_i2c_scan` — delegates to `i2c_scan()`; always returns `true`
  - `cmd_hello` — emits `{"hello":"ready"}`; always returns `true`
  - `cmd_battery` — emits `{"battery":0}`; always returns `true`
- `atoi()` returns `0` silently for non-numeric input — treat as intentional (LED off or default); do not add extra validation; document this at the call site
- Commands with no failure mode (`cmd_hello`, `cmd_battery`, `cmd_i2c_scan`) return `true` unconditionally
- Fill `kCmds[]` with all canonical commands and compatibility aliases:
  - `{"spectrometer_read",  cmd_spectrometer_read}`
  - `{"spectrometer_read_flash", cmd_spectrometer_read_flash}`
  - `{"spectrometer_set_led", cmd_spectrometer_set_led}`
  - `{"bme_read", cmd_bme_read}`
  - `{"i2c_scan", cmd_i2c_scan}`
  - `{"hello", cmd_hello}`
  - `{"battery", cmd_battery}`
  - `{"spectrometer_status", cmd_spectrometer_status}`
  - `{"bme_status", cmd_bme_status}`
  - `{"reboot", cmd_reboot}`
  - `{"read_spectro", cmd_spectrometer_read}` — compatibility alias
  - `{"read_spectro_flash", cmd_spectrometer_read_flash}` — compatibility alias
  - `{"set_led", cmd_spectrometer_set_led}` — compatibility alias
- **Compatibility rule:** alias commands preserve command names only; they return the same payload as the canonical names
- **`CMD_BUF_LEN` check:** verify the longest expected input (`spectrometer_read_flash,NNN` = 28 chars) fits well within `CMD_BUF_LEN = 96`; no change needed unless new commands are added

### Step 3.3 - Migrate `handleCommandText()` to the dispatch table
- **File:** `src/app/commands.cpp`
- Replace the if-else chain with `dispatchCommand(...)`
- Change `handleCommandText()` return type from `void` to `bool`
- Update declaration in `include/app/commands.h`
- **Coordinate with Step 3.6:** both steps modify the `handleCommandText()` signature (return type here, argument type in 3.6); implement them together in a single commit to avoid an intermediate broken state
- In `main.cpp`, consume the return value: a `false` return in LINE mode should print `{"error":"unknown_command"}` to Serial (complete JSON object, consistent with Option B)
- At the end of Phase 3, delete the `as7341_available` transitional shim from `spectrometer_api.cpp` once the old AS7341-only command path no longer exists; verify no remaining code references it before deleting

### Step 3.4 - Fix and enable the JSON/openJII path
- **Files:** `src/main.cpp`, `src/app/commands.cpp`, `include/app/commands.h`
- Replace the JSON TODO in `main.cpp` with a real call to `HandleJson(...)`
- Remove `static` from `HandleJson(...)`
- Fix the `"set"` array structure: `"set"` is a JSON array and each element must be a complete JSON object; command handlers emit complete objects (e.g. `{"spectrometer":{...}}`), so `HandleJson()` emits the comma separator between calls and does **not** add extra `{}`/`}` wrapping; advance the `firstOut` / comma flag **only when `dispatchCommand()` returns `true`**; a failed dispatch prints nothing and must not leave a dangling comma or empty slot in the array
- Fix the stray `"` in `serial_string_init()`: the second `Serial.print()` call currently starts with `"\",\"device_battery\"...` which prematurely closes the `device_id` string value; remove the leading `"` so the two print calls concatenate into a single valid JSON prefix
- Fix `serial_string_end()` so the frame delimiter (`7A1E3AA1`) is emitted after the closing `}` of the JSON document, not inside it
- Remove the redundant `commandExists()` pre-check; `dispatchCommand()` already determines success

### Step 3.5 - Make sensor output consistently JSON-shaped
- **Files:** `src/app/bme68x_api.cpp`, `src/app/commands.cpp`, `src/app/spectrometer_api.cpp`
- Ensure all command handlers emit complete JSON objects (not fragments); see envelope contract below
- Ensure the spectrometer payload shape matches the contract defined in Step 2.4: `{"spectrometer":{"model":"...","channels":{...}}}`; do not re-specify the shape here
- Ensure both canonical and alias command names return the same normalized semantic spectrometer payload
- Ensure spectrometer errors in both LINE mode and JSON/openJII mode emit complete JSON objects:
  - `{"spectrometer":{"error":"not_available"}}`
  - `{"spectrometer":{"error":"unsupported_device_at_0x39"}}`
  - `{"spectrometer":{"error":"not_yet_implemented"}}` (transitional, removed when AS7343 read path is complete)
- **Envelope contract (Option B — selected):** every command handler emits a complete, self-contained JSON object: `{"spectrometer":{...}}`, `{"bme":{...}}`, etc.; this applies in both LINE mode and JSON/openJII mode; LINE mode output is independently valid JSON; `HandleJson()` requires no mode-specific wrapping logic

### Step 3.6 - Replace the `String` receive buffer with a fixed buffer
- **File:** `src/main.cpp`
- **Coordinate with Step 3.3:** both steps modify the `handleCommandText()` signature; implement them together in a single commit
- Replace `String rx` with `char rx[2048]` plus `size_t rxLen`
- Check bounds before appending; on overflow emit `{"error":"rx_overflow"}` (complete JSON object, consistent with Option B) and call `resetRx()`
- Remove unused `String line`
- Change:
  - `HandleJson(const String &)` -> `HandleJson(const char *)`
  - `handleCommandText(const String &)` -> `handleCommandText(const char *)`

### >>> HARDWARE TEST CHECKPOINT 3 <<<
- [ ] Plain-text commands work on AS7341 hardware:
  - `spectrometer_read`
  - `spectrometer_read_flash,15`
  - `spectrometer_set_led,10`
  - `set_led,10`
  - `read_spectro` and `read_spectro_flash,15` aliases dispatch correctly
- [ ] Plain-text commands work on AS7343 hardware with the same command names
- [ ] JSON payload works and returns the detected model in the spectrometer object
- [ ] Alias commands `read_spectro` and `read_spectro_flash` return the same generic `{"spectrometer":{...}}` payload as the canonical names
- [ ] `hello` returns `{"hello":"ready"}`
- [ ] `battery` returns `{"battery":0}`
- [ ] `spectrometer_set_led,10` returns `{"spectrometer":{"led_current_ma":10}}`
- [ ] `spectrometer_set_led,25` clamps to 20 mA and returns `{"spectrometer":{"led_current_ma":20}}`
- [ ] An unknown command returns `{"error":"unknown_command"}`
- [ ] In JSON/openJII mode, `None` and `UnknownAt0x39` produce distinct structured spectrometer error objects
- [ ] In LINE mode, spectrometer errors emit complete JSON objects, not free text
- [ ] Sending more than 2048 bytes of garbage returns `{"error":"rx_overflow"}` and does not crash
- [ ] JSON/openJII output is valid JSON from `serial_string_init()` through `serial_string_end()`

---

## Phase 4: BME68x Robustness And Cleanup

### Step 4.1 - Fix `bme_read()` robustness
- **File:** `src/app/bme68x_api.cpp`
- Use the correct Bosch library call sequence:
  1. Call `bme.fetchData()`; if the return value is `0`, no data was ready — return `false` immediately
  2. Call `bme.getAllData(data)`; if the returned count is `0`, the fetch produced no valid records — return `false`
  3. Only then access `data[0]` fields
- `getAllData()` returns the number of records fetched, not a pointer — do not null-check it

### Step 4.2 - Make BME output consistently JSON-shaped
- **File:** `src/app/bme68x_api.cpp`
- Unify both `#ifdef BME68X_USE_FPU` branches to emit a complete JSON object using the same key names; the value types differ by build:
  - With `BME68X_USE_FPU` defined: `gas_resistance` is `float` — use `%f` format
  - Without `BME68X_USE_FPU`: `gas_resistance` is `uint32_t` — use `%lu` format
- Canonical output shape (same key names in both branches):
  - `{"bme":{"temp_c":<val>,"humidity_pct":<val>,"pressure_hpa":<val>,"gas_resistance_ohm":<val>}}`
- This is the canonical `bme_read` output in both LINE mode and JSON/openJII mode

### Step 4.3 - Remove invalid `platformio.ini` options
- **File:** `platformio.ini`
- Delete the `board_build.arduino.earlephilhower.*` lines; they are not relevant to ESP32 Arduino

### Step 4.4 - Pin library versions deliberately
- **File:** `platformio.ini`
- Replace bare Git URLs with explicit versions where possible
- Keep `Adafruit_AS7341` because it remains the AS7341 backend
- AS7343 support is implemented locally in the firmware; do not assume an external AS7343 Arduino library

### Step 4.5 - Clean up `bme68x_api.h`
- **File:** `include/app/bme68x_api.h`
- Review every declaration; keep only functions called from outside `bme68x_api.cpp` (i.e. from `commands.cpp` or `main.cpp`); remove anything that has become internal
- Ensure `#pragma once` is present

---

## Files Affected

| File | Purpose |
|------|---------|
| `src/main.cpp` | Generic spectrometer init state, JSON path, fixed RX buffer |
| `src/app/commands.cpp` | Generic spectrometer commands, aliases, dispatch wiring |
| `include/app/commands.h` | Updated command signatures and JSON handler declaration |
| `src/app/as7341_api.cpp` | AS7341-only backend cleanup |
| `include/app/as7341_api.h` | AS7341 backend declarations only |
| `src/app/as7343_api.cpp` | New AS7343 backend |
| `include/app/as7343_api.h` | New AS7343 backend header |
| `src/app/spectrometer_api.cpp` | Generic spectrometer facade and shared policy |
| `include/app/spectrometer_api.h` | Generic spectrometer facade API |
| `include/app/spectrometer_types.h` | `SpectrometerModel` enum and `SpectrometerResult` struct (new, shared by backends and facade) |
| `src/app/bme68x_api.cpp` | BME robustness and JSON consistency |
| `include/app/bme68x_api.h` | Header cleanup (Step 4.5) |
| `src/app/debug_api.cpp` | Pure bus scan, no reinit |
| `platformio.ini` | Cleanup and dependency pinning |

## Final Verification Matrix

- [ ] `pio run` compiles cleanly after each phase
- [ ] AS7341 board variant:
  - boot detects `AS7341`
  - `spectrometer_read` returns model plus valid channels
  - `spectrometer_read_flash,10` returns a single JSON object with `spectrometer_dark`, `spectrometer_lit`, `spectrometer_diff`
  - LED control works
- [ ] AS7343 board variant:
  - boot detects `AS7343`
  - `spectrometer_read` returns model plus valid channels
  - `spectrometer_read_flash,10` returns a single JSON object with `spectrometer_dark`, `spectrometer_lit`, `spectrometer_diff`
  - LED control works
- [ ] No spectrometer fitted:
  - boot reports no spectrometer
  - spectrometer commands return a clear availability error
- [ ] Unknown device at `0x39`:
  - boot leaves spectrometer unavailable and indicates retry-on-first-command behavior
  - first spectrometer command retries detection
  - if still unsupported, spectrometer commands return a distinct unsupported-device error, not the no-device error
  - firmware remains responsive
- [ ] Compatibility aliases `read_spectro` and `read_spectro_flash` still work and return the generic normalized spectrometer payload
- [ ] `hello` → `{"hello":"ready"}`; `battery` → `{"battery":0}`; `spectrometer_set_led,10` → `{"spectrometer":{"led_current_ma":10}}`
- [ ] An unknown command returns `{"error":"unknown_command"}` — not plain text
- [ ] Long-running command loop for 10+ minutes shows no hangs and no obvious heap growth

## Reference Datasheets

- AS7341 datasheet: `AS7341-DS000504`
- AS7343 datasheet: `AS7343-14-Channel-Multi-Spectral-Sensor`, version dated 2023-06-07
