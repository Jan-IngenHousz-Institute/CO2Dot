# Plan: CLI `spec_group` command — single SMUX group measurement

## TL;DR
Add a `spec_group,<N>` CLI command that selects a single SMUX channel group, performs one integration pass, and prints only the channels belonging to that group. Reuses existing single-pass register-level code from `as7341_readChannelFast` / `as7343_readChannelFast` patterns but reads ALL 6 ADC channels from the selected pass instead of one.

## Background — SMUX group structure

**AS7341 (2 groups, 6 ADC channels each):**
| Group | SMUX config | ADC channels | Spectral output |
|-------|-------------|--------------|-----------------|
| 1 | `setup_F1F4_Clear_NIR()` | ADC0–5 | F1(415), F2(445), F3(480), F4(515), Clear, NIR |
| 2 | `setup_F5F8_Clear_NIR()` | ADC0–5 | F5(555), F6(590), F7(630), F8(680), Clear, NIR |

**AS7343 (3 groups / AUTO_SMUX cycles):**
| Group | AUTO_SMUX mode | DATA registers | Spectral output | Integration time |
|-------|----------------|----------------|-----------------|------------------|
| 1 | 6CH (bits[6:5]=0b00, value=0) | DATA[0..5] | FZ(450), FY(555), FXL(600), NIR(855), VIS_TL, VIS_BR | 1× t_int |
| 2 | 12CH (bits[6:5]=0b10, value=2) | DATA[6..11] | F2(425), F3(475), F4(515), F6(640), VIS_TL, VIS_BR | 2× t_int |
| 3 | 18CH (bits[6:5]=0b11, value=3) | DATA[12..17] | F1(405), F7(690), F8(745), F5(550), VIS_TL, VIS_BR | 3× t_int |

Note: AS7343 AUTO_SMUX is cumulative (12CH runs cycles 1+2, 18CH runs 1+2+3). We only *print* the channels belonging to the requested group, but for groups 2/3 the hardware inherently integrates earlier cycles too — so measurement time scales linearly with group number.

**Critical: AS7343 AUTO_SMUX encoding (CFG20 bits[6:5]):**
- 6CH  = 0 (0b00) → byte shifted = 0x00
- 12CH = 2 (0b10) → byte shifted = 0x40
- 18CH = 3 (0b11) → byte shifted = 0x60

The Adafruit library confirms: `AS7343_SMUX_12CH = 2`. (Note: existing `as7343_readChannelFast` has a bug using `smux_bits = 1` for 12CH — must be fixed to `smux_bits = 2`.)

## Steps

### Phase 0 — Bug fix (pre-existing)

0. **Fix `as7343_readChannelFast` 12CH encoding bug** in `as7343_api.cpp`:
   - Change `smux_bits = 1` → `smux_bits = 2` for cycle-2 channels (out_index 1, 3, 4, 8).
   - Lines 286–289: all four cases currently set `smux_bits = 1` which produces byte `0x20` (undefined). Correct value is `smux_bits = 2` → byte `0x40` (12CH per Adafruit `AS7343_SMUX_12CH = 2`).

### Phase 1 — Backend group-read functions

1. **`as7341_api.h/.cpp`** — add `bool as7341_readGroup(uint8_t group, SpectrometerResult *out)`
   - `group` is 1 or 2.
   - Reuses the SMUX-load + single-integration + burst-read pattern from `as7341_readChannelFast`:
     1. `as7341.enableSpectralMeasurement(false)`
     2. Write CFG6 = SMUX_CMD_WRITE (`writeReg8(0xAF, 0x10)`)
     3. Call `as7341.setup_F1F4_Clear_NIR()` or `as7341.setup_F5F8_Clear_NIR()`
     4. Trigger SMUX load (set ENABLE bit 4, poll until clear)
     5. `as7341.enableSpectralMeasurement(true)`
     6. Poll AVALID in STATUS2
     7. Burst-read 12 bytes from CH0_DATA_L (0x95) → 6 × uint16_t
   - Populates `out->channels[0..5]`, sets `out->channel_count = 6`, `out->model = AS7341`.
   - Sets `out->sat_mask` from STATUS2 ASAT bit.

2. **`as7343_api.h/.cpp`** — add `bool as7343_readGroup(uint8_t group, SpectrometerResult *out)`
   - `group` is 1, 2, or 3.
   - Sets AUTO_SMUX to minimum mode covering the group:
     - Group 1: value 0 (6CH, bits[6:5]=0b00)
     - Group 2: value 2 (12CH, bits[6:5]=0b10)  ← corrected
     - Group 3: value 3 (18CH, bits[6:5]=0b11)
   - Runs measurement (SP_EN=1, poll AVALID).
   - Reads ASTATUS to latch, then burst-reads only the 12 bytes (6 channels) for the target cycle:
     - Group 1: DATA_0_L + 0  (offsets 0–11)
     - Group 2: DATA_0_L + 12 (offsets 12–23)
     - Group 3: DATA_0_L + 24 (offsets 24–35)
   - Populates `out->channels[0..5]`, `out->channel_count = 6`, `out->model = AS7343`.
   - Sets `out->sat_mask` from STATUS2 analog/digital sat bits.
   - Restores AUTO_SMUX=18CH (value 3) afterwards.

### Phase 2 — Spectrometer facade

3. **`spectrometer_api.h/.cpp`** — add `bool spectrometer_read_group(uint8_t group)`
   - Guards (model check via `spectrometerPrepareLegacyCommand()`).
   - Validates group number against model (AS7341: 1–2; AS7343: 1–3). Returns error JSON if invalid.
   - Dispatches to `as7341_readGroup` or `as7343_readGroup`.
   - Prints JSON output with per-group channel-name tables and saturation info.

4. **Channel name tables for groups** — define static const arrays:
   - AS7341 group 1: `{"f1_415","f2_445","f3_480","f4_515","clear","nir"}`
   - AS7341 group 2: `{"f5_555","f6_590","f7_630","f8_680","clear","nir"}`
   - AS7343 group 1: `{"fz_450","fy_555","fxl_600","nir_855","vis_tl","vis_br"}`
   - AS7343 group 2: `{"f2_425","f3_475","f4_515","f6_640","vis_tl","vis_br"}`
   - AS7343 group 3: `{"f1_405","f7_690","f8_745","f5_550","vis_tl","vis_br"}`

### Phase 3 — CLI command registration

5. **`commands.cpp`** — add handler for `spec_group,<N>`:
   - Parse the integer group argument after the comma.
   - Call `spectrometer_read_group(group)`.
   - Follows the same pattern as `spec_flash,<led_mA>` for argument parsing.

### Phase 4 — Output format

6. JSON output (matches existing `printChannelsObject` style, adds `sat` field):
   ```
   {"spectrometer_group":{"model":"AS7341","group":1,"sat":0,"channels":{"f1_415":1234,"f2_445":5678,...}}}
   ```

## Relevant files
- `Firmware/include/app/as7341_api.h` — add `as7341_readGroup` declaration
- `Firmware/src/app/as7341_api.cpp` — implement; reuse register helpers `writeReg8`, `readReg8`, SMUX-load sequence from `as7341_readChannelFast`
- `Firmware/include/app/as7343_api.h` — add `as7343_readGroup` declaration
- `Firmware/src/app/as7343_api.cpp` — implement + fix `smux_bits` bug at lines 286–289; reuse `readRegister8`, `writeRegister8`, AUTO_SMUX swap
- `Firmware/include/app/spectrometer_api.h` — add `spectrometer_read_group` declaration
- `Firmware/src/app/spectrometer_api.cpp` — implement facade, group channel name tables, JSON printer with `sat` field
- `Firmware/src/app/commands.cpp` — add `spec_group` command parsing

## Verification
1. Build with `pio run -e esp32-c3-devkitm-1` — must compile without errors/warnings.
2. Flash and send `spec_group,1` over serial → verify JSON output contains only group 1 channels + `"sat"` field.
3. Send `spec_group,2` → verify only group 2 channels are returned.
4. (AS7343 only) Send `spec_group,3` → verify group 3.
5. Send `spec_group,0` or `spec_group,4` → verify error JSON `{"spectrometer_group":{"error":"invalid_group"}}`.
6. Cross-check: `spec_group,1` + `spec_group,2` channel values should match corresponding channels from `spec` (full read).
7. Regression: verify `spec_flash_wave` still works correctly after the 12CH bug fix.

## Decisions
- Group numbering is 1-based (matches human-friendly "SMUX pass 1/2/3").
- VIS_TL and VIS_BR are included in AS7343 group output (they're physically measured in each cycle).
- Clear/NIR appear in both AS7341 groups — they're independent measurements per-pass (expected to differ slightly).
- The command does NOT toggle the LED — it's a bare-sensor read. Use `spec_flash` variants for LED-assisted measurements.
- `spectrometer_read_group` restores full-range mode (18CH / both-pass) after each call so other commands stay unaffected.
- AS7343 group 2 takes ~2× integration time and group 3 takes ~3× vs group 1 (hardware limitation of cumulative AUTO_SMUX).
- The pre-existing 12CH encoding bug (`smux_bits=1` instead of `2`) is fixed as part of this work since the group-read feature depends on the same code path.
