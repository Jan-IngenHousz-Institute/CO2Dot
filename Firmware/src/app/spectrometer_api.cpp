#include <Arduino.h>
#include <ArduinoJson.h>
#include <Wire.h>

#include "app/as7341_api.h"
#include "app/as7343_api.h"
#include "app/commands.h"
#include "app/spectrometer_api.h"

// ---------------------------------------------------------------------------
// Global state
// ---------------------------------------------------------------------------
bool spectrometer_available = false;
SpectrometerModel spectrometer_model = SpectrometerModel::None;
static uint16_t s_led_current_ma = 0;  // tracks last-set LED current

// Per-channel PAR conversion coefficients, indexed 0..channel_count-1.
// Default values for AS7341 from datasheet.
float par_coefficients[18] = {
  1.0f/55.0f,   // F1
  1.0f/110.0f,  // F2
  1.0f/210.0f,  // F3
  1.0f/390.0f,  // F4
  1.0f/590.0f,  // F5
  1.0f/840.0f,  // F6
  1.0f/1350.0f, // F7
  1.0f/1070.0f, // F8
  1.0f/1750.0f, // clear
  1.0f/112.0f,  // nir
  0,0,0,0,0,0,0,0
};

// ---------------------------------------------------------------------------
// Board-level LED policy
// ---------------------------------------------------------------------------
constexpr uint16_t kSpectrometerLedBoardMaxMa = 20;
constexpr uint16_t kLedSetFailed = 0xFFFF;

// ---------------------------------------------------------------------------
// Channel name tables
// ---------------------------------------------------------------------------
static const char * const kAs7341ChannelNames[10] = {
  "f1_415", "f2_445", "f3_480", "f4_515",
  "f5_555", "f6_590", "f7_630", "f8_680",
  "clear",  "nir"
};

// AS7343 channel names — 13 channels (12 spectral + clear) in wavelength order.
// Matches the remapping in as7343_readInto() (channel_count == 13).
static const char * const kAs7343ChannelNames[13] = {
  "f1_405",   // F1  405nm
  "f2_425",   // F2  425nm
  "fz_450",   // FZ  450nm
  "f3_475",   // F3  475nm
  "f4_515",   // F4  515nm
  "f5_550",   // F5  550nm
  "fy_555",   // FY  555nm
  "fxl_600",  // FXL 600nm
  "f6_640",   // F6  640nm
  "f7_690",   // F7  690nm
  "f8_745",   // F8  745nm
  "nir_855",  // NIR 855nm
  "clear",    // VIS broadband (avg of 6 VIS readings)
};

// AS7343 bringup names — 18 entries matching Adafruit as7343_channel_t DATA register order.
// Used when channel_count == 18 (raw dump mode).
static const char * const kAs7343BringupNames[18] = {
  "fz_450",    // DATA[0]  FZ  450nm
  "fy_555",    // DATA[1]  FY  555nm
  "fxl_600",   // DATA[2]  FXL 600nm
  "nir",       // DATA[3]  NIR 855nm
  "vis_tl_0",  // DATA[4]  VIS clear cycle 1 top-left
  "vis_br_0",  // DATA[5]  VIS clear cycle 1 both-right
  "f2_425",    // DATA[6]  F2  425nm
  "f3_475",    // DATA[7]  F3  475nm
  "f4_515",    // DATA[8]  F4  515nm
  "f6_640",    // DATA[9]  F6  640nm
  "vis_tl_1",  // DATA[10] VIS clear cycle 2 top-left
  "vis_br_1",  // DATA[11] VIS clear cycle 2 both-right
  "f1_405",    // DATA[12] F1  405nm
  "f7_690",    // DATA[13] F7  690nm
  "f8_745",    // DATA[14] F8  745nm
  "f5_550",    // DATA[15] F5  550nm
  "vis_tl_2",  // DATA[16] VIS clear cycle 3 top-left
  "vis_br_2",  // DATA[17] VIS clear cycle 3 both-right
};

// Nominal peak wavelengths per channel (nm). 0 = non-spectral (clear), skipped
// by nearest-wavelength lookup. NIR on AS7341 is broadband; 910 is a usable
// nominal for selecting it by wavelength.
static constexpr uint16_t kAs7341Wavelengths[10] = {
  415, 445, 480, 515, 555, 590, 630, 680, 0, 910,
};
static constexpr uint16_t kAs7343Wavelengths[13] = {
  405, 425, 450, 475, 515, 550, 555, 600, 640, 690, 745, 855, 0,
};

namespace {

// ---------------------------------------------------------------------------
// Detection helpers
// ---------------------------------------------------------------------------
constexpr uint8_t  kSpectrometerI2cAddress    = 0x39;
constexpr uint32_t kProbeRetryWindowUs         = 5000;
constexpr uint32_t kProbeRetryIntervalUs        = 250;

struct DetectionResult {
  SpectrometerModel model = SpectrometerModel::None;
  bool saw_ack = false;
};

struct DetectionDebugInfo {
  uint32_t attempts = 0;
  bool saw_ack = false;
  bool as7343_id_read_ok = false;
  uint8_t as7343_chip_id = 0;
  bool as7341_id_read_ok = false;
  uint8_t as7341_chip_id = 0;
};

void setSpectrometerState(SpectrometerModel model, bool available) {
  spectrometer_model    = model;
  spectrometer_available = available;
}

bool spectrometerAddressAcks() {
  Wire.beginTransmission(kSpectrometerI2cAddress);
  return Wire.endTransmission() == 0;
}

void printHexByte(uint8_t value) {
  if (value < 0x10) Serial.print('0');
  Serial.print(value, HEX);
}

void printDetectionDebug([[maybe_unused]] const char *reason,
                         [[maybe_unused]] const DetectionDebugInfo &debug,
                         [[maybe_unused]] SpectrometerModel result_model,
                         [[maybe_unused]] bool initialized) {
#if DEBUG
  Serial.print(F("[spectrometer-debug] reason="));
  Serial.print(reason);
  Serial.print(F(" attempts="));
  Serial.print(debug.attempts);
  Serial.print(F(" ack="));
  Serial.print(debug.saw_ack ? F("1") : F("0"));
  Serial.print(F(" as7343_id_ok="));
  Serial.print(debug.as7343_id_read_ok ? F("1") : F("0"));
  Serial.print(F(" as7343_id=0x"));
  printHexByte(debug.as7343_chip_id);
  Serial.print(F(" as7341_id_ok="));
  Serial.print(debug.as7341_id_read_ok ? F("1") : F("0"));
  Serial.print(F(" as7341_id=0x"));
  printHexByte(debug.as7341_chip_id);
  Serial.print(F(" result="));
  Serial.print(spectrometerModelName(result_model));
  Serial.print(F(" init_ok="));
  Serial.println(initialized ? F("1") : F("0"));
#endif
}

DetectionResult detectSpectrometerWithinRetryWindow(DetectionDebugInfo *debug) {
  DetectionResult result;
  const uint32_t started_at = micros();

  while ((micros() - started_at) < kProbeRetryWindowUs) {
    if (debug) debug->attempts++;

    if (spectrometerAddressAcks()) {
      result.saw_ack = true;
      if (debug) debug->saw_ack = true;

      // Probe AS7343 first (bank-switch write to 0xBF is safe on AS7341)
      uint8_t as7343_chip_id = 0;
      const bool as7343_read_ok = as7343_readChipId(&as7343_chip_id);
      if (debug) {
        debug->as7343_id_read_ok = as7343_read_ok;
        if (as7343_read_ok) debug->as7343_chip_id = as7343_chip_id;
      }
      if (as7343_read_ok && as7343_chip_id == 0x81) {
        result.model = SpectrometerModel::AS7343;
        return result;
      }

      // Probe AS7341
      uint8_t as7341_chip_id = 0;
      const bool as7341_matched = as7341_readAndValidateChipId(&as7341_chip_id);
      if (debug) {
        debug->as7341_id_read_ok = as7341_matched;
        debug->as7341_chip_id    = as7341_chip_id;
      }
      if (as7341_matched) {
        result.model = SpectrometerModel::AS7341;
        return result;
      }
    }

    delayMicroseconds(kProbeRetryIntervalUs);
  }

  result.model = result.saw_ack ? SpectrometerModel::ProbePendingAt0x39
                                : SpectrometerModel::None;
  return result;
}

bool initializeDetectedSpectrometer(SpectrometerModel model) {
  switch (model) {
  case SpectrometerModel::AS7341: return initAS7341();
  case SpectrometerModel::AS7343: return initAS7343();
  default: return false;
  }
}

bool detectAndInitialize(bool promote_pending_to_unknown, const char *reason) {
  DetectionDebugInfo debug;
  const DetectionResult detection = detectSpectrometerWithinRetryWindow(&debug);

  if (detection.model == SpectrometerModel::AS7341 ||
      detection.model == SpectrometerModel::AS7343) {
    const bool initialized = initializeDetectedSpectrometer(detection.model);
    setSpectrometerState(detection.model, initialized);
    printDetectionDebug(reason, debug, spectrometer_model, initialized);
    return initialized;
  }

  if (promote_pending_to_unknown &&
      detection.model == SpectrometerModel::ProbePendingAt0x39) {
    setSpectrometerState(SpectrometerModel::UnknownAt0x39, false);
    printDetectionDebug(reason, debug, spectrometer_model, false);
    return false;
  }

  setSpectrometerState(detection.model, false);
  printDetectionDebug(reason, debug, spectrometer_model, false);
  return false;
}

// ---------------------------------------------------------------------------
// Internal measurement helpers
// ---------------------------------------------------------------------------

// Populates *out from the active backend.  No printing, no guard checks.
bool spectrometerReadInto(SpectrometerResult *out) {
  if (!out) return false;
  switch (spectrometer_model) {
  case SpectrometerModel::AS7341: return as7341_readInto(out);
  case SpectrometerModel::AS7343: return as7343_readInto(out);
  default: return false;
  }
}

// Fast single-channel dispatch.  Runs the minimum integration sequence the
// active backend supports for out_index (one SMUX pass on AS7341, the
// minimum AUTO_SMUX cycle count on AS7343).
bool spectrometerReadChannelFast(uint8_t out_index, uint16_t *raw_out,
                                 uint16_t *sat_mask_out) {
  switch (spectrometer_model) {
  case SpectrometerModel::AS7341:
    return as7341_readChannelFast(out_index, raw_out, sat_mask_out);
  case SpectrometerModel::AS7343:
    return as7343_readChannelFast(out_index, raw_out, sat_mask_out);
  default:
    return false;
  }
}

// Sets LED current on the active backend, bypassing facade JSON output.
// Returns quantized actual mA, 0 if disabled, 0xFFFF on error.
uint16_t spectrometerSetLedCurrentSilent(uint16_t led_current_ma) {
  switch (spectrometer_model) {
  case SpectrometerModel::AS7341: return as7341_setLEDCurrent(led_current_ma);
  case SpectrometerModel::AS7343: return as7343_setLEDCurrent(led_current_ma);
  default: return kLedSetFailed;
  }
}

uint8_t spectrometerGetAtIME() {
  switch (spectrometer_model) {
  case SpectrometerModel::AS7341: return as7341_getAtIME();
  case SpectrometerModel::AS7343: return as7343_getAtIME();
  default: return 100;
  }
}

uint16_t spectrometerGetAStep() {
  switch (spectrometer_model) {
  case SpectrometerModel::AS7341: return as7341_getAStep();
  case SpectrometerModel::AS7343: return as7343_getAStep();
  default: return 999;
  }
}

// Finds the channel index whose nominal wavelength is closest to target_nm
// for the active spectrometer model. Skips non-spectral entries (clear).
bool resolveWavelengthChannel(SpectrometerModel model, uint16_t target_nm,
                              uint8_t *channel_index, uint16_t *actual_nm) {
  const uint16_t *table = nullptr;
  uint8_t count = 0;
  if (model == SpectrometerModel::AS7341) {
    table = kAs7341Wavelengths;
    count = 10;
  } else if (model == SpectrometerModel::AS7343) {
    table = kAs7343Wavelengths;
    count = 13;
  } else {
    return false;
  }
  int best_idx = -1;
  uint16_t best_diff = 0xFFFF;
  for (uint8_t i = 0; i < count; i++) {
    if (table[i] == 0) continue;
    const uint16_t d = table[i] > target_nm ? table[i] - target_nm
                                            : target_nm - table[i];
    if (d < best_diff) {
      best_diff = d;
      best_idx  = i;
    }
  }
  if (best_idx < 0) return false;
  *channel_index = static_cast<uint8_t>(best_idx);
  *actual_nm     = table[best_idx];
  return true;
}

// Resolves the channel name table and whether to use indexed names ("d0"…)
// for the given result (bringup mode / unexpected count).
const char * const *resolveChannelNames(const SpectrometerResult &r, bool *use_index_names) {
  *use_index_names = false;
  if (r.model == SpectrometerModel::AS7341 && r.channel_count == 10) {
    return kAs7341ChannelNames;
  }
  if (r.model == SpectrometerModel::AS7343 && r.channel_count == 13) {
    return kAs7343ChannelNames;
  }
  if (r.model == SpectrometerModel::AS7343 && r.channel_count == 18) {
    return kAs7343BringupNames;
  }
  // Unexpected channel count — fall back to indexed names
  *use_index_names = true;
  return nullptr;
}

// Prints {"model":"...","channels":{...}} — the inner object used by both the
// single-read and the flash-read (dark/lit/diff) output paths.
void printChannelsObject(const SpectrometerResult &r) {
  bool use_index_names = false;
  const char * const *names = resolveChannelNames(r, &use_index_names);

  Serial.print(F("{\"model\":\""));
  Serial.print(spectrometerModelName(r.model));
  Serial.print(F("\",\"channels\":{"));
  for (uint8_t i = 0; i < r.channel_count; i++) {
    if (i > 0) Serial.print(',');
    Serial.print('"');
    if (use_index_names) {
      Serial.print('d');
      Serial.print(i);
    } else {
      Serial.print(names[i]);
    }
    Serial.print(F("\":"));
    Serial.print(r.channels[i]);
  }
  Serial.print(F("}}"));
}

} // namespace

// ---------------------------------------------------------------------------
// Public API — init and error printers
// ---------------------------------------------------------------------------

const char *spectrometerModelName(SpectrometerModel model) {
  switch (model) {
  case SpectrometerModel::AS7341:            return "AS7341";
  case SpectrometerModel::AS7343:            return "AS7343";
  case SpectrometerModel::ProbePendingAt0x39: return "ProbePendingAt0x39";
  case SpectrometerModel::UnknownAt0x39:     return "UnknownAt0x39";
  case SpectrometerModel::None:
  default:                                   return "None";
  }
}

bool initSpectrometer() {
  const bool initialized = detectAndInitialize(false, "boot");

#if DEBUG
  switch (spectrometer_model) {
  case SpectrometerModel::AS7341:
    Serial.println(F("[init] Spectrometer: AS7341"));
    break;
  case SpectrometerModel::AS7343:
    Serial.println(F("[init] Spectrometer: AS7343"));
    break;
  case SpectrometerModel::ProbePendingAt0x39:
    Serial.println(F("[init] Spectrometer: pending at 0x39"));
    break;
  case SpectrometerModel::None:
  case SpectrometerModel::UnknownAt0x39:
  default:
    Serial.println(F("[init] Spectrometer: not found"));
    break;
  }
#endif
  return initialized;
}

void spectrometerPrintNotAvailableError() {
  Serial.print(F("{\"spectrometer\":{\"error\":\"not_available\"}}"));
  cmdEndLine();
}

void spectrometerPrintUnsupportedDeviceError() {
  Serial.print(F("{\"spectrometer\":{\"error\":\"unsupported_device_at_0x39\"}}"));
  cmdEndLine();
}

bool spectrometerPrepareLegacyCommand() {
  if (spectrometer_model == SpectrometerModel::ProbePendingAt0x39) {
    detectAndInitialize(true, "legacy_cmd_retry");
  }
  if (spectrometer_model == SpectrometerModel::UnknownAt0x39) {
    spectrometerPrintUnsupportedDeviceError();
    return false;
  }
  if (!spectrometer_available) {
    spectrometerPrintNotAvailableError();
    return false;
  }
  return true;
}

// ---------------------------------------------------------------------------
// Public API — spectrometer commands
// ---------------------------------------------------------------------------

bool spectrometer_read_raw() {
  if (!spectrometerPrepareLegacyCommand()) return false;

  SpectrometerResult result;
  if (!spectrometerReadInto(&result)) {
    Serial.print(F("{\"spectrometer\":{\"error\":\"read_failed\"}}"));
    cmdEndLine();
    return false;
  }

  Serial.print(F("{\"spectrometer\":"));
  printChannelsObject(result);
  Serial.print(F("}"));
  cmdEndLine();
  return true;
}

bool spectrometer_read() {
  if (!spectrometerPrepareLegacyCommand()) return false;

  SpectrometerResult result;
  if (!spectrometerReadInto(&result)) {
    Serial.print(F("{\"spectrometer\":{\"error\":\"read_failed\"}}"));
    cmdEndLine();
    return false;
  }

  // Apply per-channel sensitivity correction
  bool use_index_names = false;
  const char * const *names = resolveChannelNames(result, &use_index_names);

  Serial.print(F("{\"spectrometer\":{\"model\":\""));
  Serial.print(spectrometerModelName(result.model));
  Serial.print(F("\",\"channels\":{"));
  for (uint8_t i = 0; i < result.channel_count; i++) {
    if (i > 0) Serial.print(',');
    Serial.print('"');
    if (use_index_names) {
      Serial.print('d');
      Serial.print(i);
    } else {
      Serial.print(names[i]);
    }
    Serial.print(F("\":"));
    float corrected = (float)result.channels[i] * par_coefficients[i];
    Serial.print(corrected, 4);
  }
  Serial.print(F("}}}"));
  cmdEndLine();
  return true;
}

bool spectrometer_set_led_current(uint16_t led_current_ma) {
  if (!spectrometerPrepareLegacyCommand()) return false;

  // Silent clamp to board maximum
  if (led_current_ma > kSpectrometerLedBoardMaxMa) {
    led_current_ma = kSpectrometerLedBoardMaxMa;
  }

  const uint16_t actual_ma = spectrometerSetLedCurrentSilent(led_current_ma);
  if (actual_ma == kLedSetFailed) {
    Serial.print(F("{\"spectrometer\":{\"error\":\"led_set_failed\"}}"));
    cmdEndLine();
    return false;
  }
  s_led_current_ma = actual_ma;

  Serial.print(F("{\"spectrometer\":{\"led_current_ma\":"));
  Serial.print(actual_ma);
  Serial.print(F("}}"));
  cmdEndLine();
  return true;
}

bool spectrometer_read_flash(uint16_t led_current_ma, uint16_t repeat) {
  if (!spectrometerPrepareLegacyCommand()) return false;

  // Silent clamp to board maximum
  if (led_current_ma > kSpectrometerLedBoardMaxMa) {
    led_current_ma = kSpectrometerLedBoardMaxMa;
  }
  if (repeat < 1) repeat = 1;

  // Dark read
  SpectrometerResult dark;
  if (!spectrometerReadInto(&dark)) {
    Serial.print(F("{\"spectrometer\":{\"error\":\"dark_read_failed\"}}"));
    cmdEndLine();
    return false;
  }

  // Output each measurement (envelope handles array wrapping)
  for (uint16_t r = 0; r < repeat; r++) {
    if (r > 0) Serial.print(',');

    // Enable LED and wait for settling
    const uint16_t actual_led_ma = spectrometerSetLedCurrentSilent(led_current_ma);
    if (actual_led_ma == kLedSetFailed) {
      Serial.print(F("{\"spectrometer\":{\"error\":\"led_set_failed\"}}"));
      cmdEndLine();
      return false;
    }

    // LED thermal settling: 50 ms minimum before starting the lit integration
    delay(50);

    // Lit read
    SpectrometerResult lit;
    if (!spectrometerReadInto(&lit)) {
      spectrometerSetLedCurrentSilent(0);
      Serial.print(F("{\"spectrometer\":{\"error\":\"lit_read_failed\"}}"));
      cmdEndLine();
      return false;
    }

    // Disable LED
    spectrometerSetLedCurrentSilent(0);

    // Compute diff per channel
    SpectrometerResult diff;
    diff.model         = dark.model;
    diff.channel_count = dark.channel_count;
    diff.sat_mask      = dark.sat_mask | lit.sat_mask;
    for (uint8_t i = 0; i < dark.channel_count; i++) {
      diff.channels[i] =
          lit.channels[i] > dark.channels[i] ? lit.channels[i] - dark.channels[i] : 0;
    }

    // Single combined JSON object
    Serial.print(F("{\"spectrometer_dark\":"));
    printChannelsObject(dark);
    Serial.print(F(",\"spectrometer_lit\":"));
    printChannelsObject(lit);
    Serial.print(F(",\"spectrometer_diff\":"));
    printChannelsObject(diff);
    Serial.print(F("}"));

    delay(1000); // TODO, pass as arg
  }

  cmdEndLine();
  return true;
}

// Fast single-wavelength flash: dark read, brief LED pulse, lit read, LED off.
// Reads only the target channel via spectrometerReadChannelFast(), which on
// AS7343 reconfigures AUTO_SMUX to the minimum cycle count for that channel
// (6CH / 12CH / 18CH) and on AS7341 runs a single SMUX pass instead of two.
// Cuts LED-on time by 2-3x for cycle-1 channels.
bool spectrometer_read_flash_wave(uint16_t led_current_ma, uint16_t wavelength_nm) {
  if (!spectrometerPrepareLegacyCommand()) return false;

  if (led_current_ma > kSpectrometerLedBoardMaxMa) {
    led_current_ma = kSpectrometerLedBoardMaxMa;
  }

  uint8_t  ch_index  = 0;
  uint16_t actual_nm = 0;
  if (!resolveWavelengthChannel(spectrometer_model, wavelength_nm,
                                &ch_index, &actual_nm)) {
    Serial.print(F("{\"spectrometer_flash_wave\":{\"error\":\"wavelength_unavailable\"}}"));
    cmdEndLine();
    return false;
  }

  uint16_t dark_v   = 0;
  uint16_t dark_sat = 0;
  if (!spectrometerReadChannelFast(ch_index, &dark_v, &dark_sat)) {
    Serial.print(F("{\"spectrometer_flash_wave\":{\"error\":\"dark_read_failed\"}}"));
    cmdEndLine();
    return false;
  }

  const uint16_t actual_led_ma = spectrometerSetLedCurrentSilent(led_current_ma);
  if (actual_led_ma == kLedSetFailed) {
    Serial.print(F("{\"spectrometer_flash_wave\":{\"error\":\"led_set_failed\"}}"));
    cmdEndLine();
    return false;
  }

  // Short LED settling for a fast flash (50 ms in spec_flash → 3 ms here).
  delay(3);

  uint16_t lit_v   = 0;
  uint16_t lit_sat = 0;
  if (!spectrometerReadChannelFast(ch_index, &lit_v, &lit_sat)) {
    spectrometerSetLedCurrentSilent(0);
    Serial.print(F("{\"spectrometer_flash_wave\":{\"error\":\"lit_read_failed\"}}"));
    cmdEndLine();
    return false;
  }

  spectrometerSetLedCurrentSilent(0);

  const uint16_t diff_v = lit_v > dark_v ? lit_v - dark_v : 0;
  const uint8_t  sat    = (dark_sat | lit_sat) ? 1 : 0;

  // Reuse the name-lookup helper by building a probe result.
  SpectrometerResult probe;
  probe.model         = spectrometer_model;
  probe.channel_count = (spectrometer_model == SpectrometerModel::AS7341) ? 10 : 13;
  probe.sat_mask      = 0;
  bool use_index_names = false;
  const char * const *names = resolveChannelNames(probe, &use_index_names);

  Serial.print(F("{\"spectrometer_flash_wave\":{\"model\":\""));
  Serial.print(spectrometerModelName(spectrometer_model));
  Serial.print(F("\",\"wavelength\":"));
  Serial.print(actual_nm);
  Serial.print(F(",\"channel\":\""));
  if (use_index_names) {
    Serial.print('d');
    Serial.print(ch_index);
  } else {
    Serial.print(names[ch_index]);
  }
  Serial.print(F("\",\"dark\":"));
  Serial.print(dark_v);
  Serial.print(F(",\"lit\":"));
  Serial.print(lit_v);
  Serial.print(F(",\"diff\":"));
  Serial.print(diff_v);
  Serial.print(F(",\"sat\":"));
  Serial.print(sat);
  Serial.print(F(",\"led_ma\":"));
  Serial.print(actual_led_ma);
  Serial.print(F("}}"));
  cmdEndLine();
  return true;
}

void fill_spectrometer_status(JsonObject out) {
  out["model"]     = spectrometerModelName(spectrometer_model);
  out["available"] = spectrometer_available;
  if (spectrometer_available) {
    uint8_t gain = 0;
    if (spectrometer_model == SpectrometerModel::AS7341) {
      gain = as7341_getGain();
    } else if (spectrometer_model == SpectrometerModel::AS7343) {
      gain = as7343_getGain();
    }
    out["atime"] = spectrometerGetAtIME();
    out["astep"] = spectrometerGetAStep();
    out["gain"]  = gain;
    out["led"]   = s_led_current_ma;
  }
}

void cmd_spectrometer_status() {
  StaticJsonDocument<256> doc;
  JsonObject obj = doc["spectrometer_status"].to<JsonObject>();
  fill_spectrometer_status(obj);
  serializeJson(doc, Serial);
  cmdEndLine();
}

// ---------------------------------------------------------------------------
// Integration parameter setters
// Command syntax:  spectrometer_set_atime,<0-255>
//                  spectrometer_set_astep,<0-65534>
//                  spectrometer_set_gain,<0-12>   (AS7341 enum ordinal or AS7343 register value)
// Response: {"spectrometer_config":{"atime":N,"astep":N,"gain":N}} on success
//           {"spectrometer_config":{"error":"..."}} on failure
// ---------------------------------------------------------------------------

static void printConfigResponse() {
  Serial.print(F("{\"spectrometer_config\":{\"atime\":"));
  Serial.print(spectrometerGetAtIME());
  Serial.print(F(",\"astep\":"));
  Serial.print(spectrometerGetAStep());
  Serial.print(F(",\"gain\":"));
  uint8_t gain = 0;
  if (spectrometer_model == SpectrometerModel::AS7341) gain = as7341_getGain();
  else if (spectrometer_model == SpectrometerModel::AS7343) gain = as7343_getGain();
  Serial.print(gain);
  Serial.print(F("}}"));
  cmdEndLine();
}

void cmd_spectrometer_set_atime(int argc, const char *argv[]) {
  if (!spectrometerPrepareLegacyCommand()) return;
  if (argc < 1) {
    Serial.print(F("{\"spectrometer_config\":{\"error\":\"missing_arg\"}}"));
    cmdEndLine();
    return;
  }
  const int val = atoi(argv[0]);
  if (val < 0 || val > 255) {
    Serial.print(F("{\"spectrometer_config\":{\"error\":\"atime_out_of_range\"}}"));
    cmdEndLine();
    return;
  }
  bool ok = false;
  if (spectrometer_model == SpectrometerModel::AS7341)
    ok = (as7341_setAtIME(static_cast<uint8_t>(val)) == static_cast<uint8_t>(val));
  else if (spectrometer_model == SpectrometerModel::AS7343)
    ok = as7343_setAtIME(static_cast<uint8_t>(val));
  if (!ok) { Serial.print(F("{\"spectrometer_config\":{\"error\":\"set_failed\"}}")); cmdEndLine(); return; }
  printConfigResponse();
}

void cmd_spectrometer_set_astep(int argc, const char *argv[]) {
  if (!spectrometerPrepareLegacyCommand()) return;
  if (argc < 1) {
    Serial.print(F("{\"spectrometer_config\":{\"error\":\"missing_arg\"}}"));
    cmdEndLine();
    return;
  }
  const long val = atol(argv[0]);
  if (val < 0 || val > 65534) {
    Serial.print(F("{\"spectrometer_config\":{\"error\":\"astep_out_of_range\"}}"));
    cmdEndLine();
    return;
  }
  bool ok = false;
  if (spectrometer_model == SpectrometerModel::AS7341)
    ok = (as7341_setAStep(static_cast<uint16_t>(val)) == static_cast<uint16_t>(val));
  else if (spectrometer_model == SpectrometerModel::AS7343)
    ok = as7343_setAStep(static_cast<uint16_t>(val));
  if (!ok) { Serial.print(F("{\"spectrometer_config\":{\"error\":\"set_failed\"}}")); cmdEndLine(); return; }
  printConfigResponse();
}

void cmd_spectrometer_set_gain(int argc, const char *argv[]) {
  if (!spectrometerPrepareLegacyCommand()) return;
  if (argc < 1) {
    Serial.print(F("{\"spectrometer_config\":{\"error\":\"missing_arg\"}}"));
    cmdEndLine();
    return;
  }
  const int val = atoi(argv[0]);
  // AS7341 gain enum: 0=0.5x, 1=1x, 2=2x, ..., 10=512x (10 max)
  // AS7343 gain register: 0=0.5x, 1=1x, ..., 12=2048x (12 max)
  const int max_gain = (spectrometer_model == SpectrometerModel::AS7341) ? 10 : 12;
  if (val < 0 || val > max_gain) {
    Serial.print(F("{\"spectrometer_config\":{\"error\":\"gain_out_of_range_0_\""));
    Serial.print(max_gain);
    Serial.print(F("\"}}"));
    cmdEndLine();
    return;
  }
  bool ok = false;
  if (spectrometer_model == SpectrometerModel::AS7341)
    ok = as7341_setGain(static_cast<as7341_gain_t>(val));
  else if (spectrometer_model == SpectrometerModel::AS7343)
    ok = as7343_setGain(static_cast<uint8_t>(val));
  if (!ok) { Serial.print(F("{\"spectrometer_config\":{\"error\":\"set_failed\"}}")); cmdEndLine(); return; }
  printConfigResponse();
}
