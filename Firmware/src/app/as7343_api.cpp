#include <Arduino.h>
#include <Wire.h>

#include "app/as7343_api.h"

namespace {

// ---------------------------------------------------------------------------
// I2C address and identity
// ---------------------------------------------------------------------------
constexpr uint8_t kAs7343I2cAddress     = 0x39;
constexpr uint8_t kAs7343Cfg0Register   = 0xBF;  // bank-select; verified Phase 0
constexpr uint8_t kAs7343RegBankBitMask = 0x10;   // bit 4 = REG_BANK
constexpr uint8_t kAs7343ChipIdRegister = 0x5A;   // bank 1
constexpr uint8_t kAs7343ChipId         = 0x81;

// ---------------------------------------------------------------------------
// Control registers (bank 0)
// ---------------------------------------------------------------------------
constexpr uint8_t kAs7343Enable  = 0x80;  // PON=bit0, SP_EN=bit1, FDEN=bit6
constexpr uint8_t kAs7343Atime   = 0x81;
constexpr uint8_t kAs7343Status  = 0x93;  // self-clear by write-back
constexpr uint8_t kAs7343Status2 = 0x90;  // AVALID=bit6 (hardware verified)
constexpr uint8_t kAs7343Astatus = 0x94;  // read to latch spectral data; ASAT=bit7
// DATA_0_L = 0x95, hardware-confirmed.
// (0x94=ASTATUS, 0x95=DATA_0_L, ..., 0xB4=DATA_17_H)
// The Adafruit .cpp comment says 0x96 but the register map shows 0x95.
// Original burst-from-0x94 code validated this: buf[1]=0x95 gave correct values.
constexpr uint8_t kAs7343Data0L  = 0x95;
constexpr uint8_t kAs7343Cfg1    = 0xC6;  // GAIN field bits[4:0]
constexpr uint8_t kAs7343Led     = 0xCD;  // LED_ACT=bit7, LED_DRIVE=bits[6:0]
constexpr uint8_t kAs7343AstepL  = 0xD4;
constexpr uint8_t kAs7343AstepH  = 0xD5;

// CFG20 (0xD6): AUTO_SMUX at bits[6:5]
// Hardware confirmed address from register scan.
// Bit positions confirmed from Adafruit AS7343 library: bits[6:5], 2-bit field.
// value 0 = 6CH, value 1 = 12CH, value 3 = 18CH
constexpr uint8_t kAs7343Cfg20        = 0xD6;
constexpr uint8_t kAs7343AutoSmuxMask = 0x60;  // bits[6:5]
constexpr uint8_t kAs7343AutoSmux18   = 0x60;  // bits[6:5]=11 = 18-channel mode

// Bit masks
constexpr uint8_t kAs7343PonBit      = 0x01;
constexpr uint8_t kAs7343SpEnBit     = 0x02;
constexpr uint8_t kAs7343AvalidBit   = 0x40;  // bit 6 of STATUS2; hardware verified
constexpr uint8_t kAs7343AsatAnalog  = 0x04;  // bit 2 of STATUS2; verified 0x44=AVALID|ASAT_ANA
constexpr uint8_t kAs7343AsatDigital = 0x08;  // bit 3 of STATUS2
constexpr uint8_t kAs7343AstatusAsat = 0x80;  // bit 7 of ASTATUS

// Default configuration (low gain for bringup; raise after band mapping confirmed)
constexpr uint8_t  kAs7343DefaultAtime = 29;
constexpr uint16_t kAs7343DefaultAstep = 599;
constexpr uint8_t  kAs7343DefaultGain  = 1;   // 1x

// Burst: 18 channels × 2 bytes starting from DATA_0_L (ASTATUS read separately)
constexpr uint8_t kBurstLen = 36;

// ---------------------------------------------------------------------------
// Stored configuration
// ---------------------------------------------------------------------------
uint8_t  s_atime = kAs7343DefaultAtime;
uint16_t s_astep = kAs7343DefaultAstep;
uint8_t  s_gain  = kAs7343DefaultGain;

// ---------------------------------------------------------------------------
// Low-level I2C helpers
// ---------------------------------------------------------------------------
bool writeRegister8(uint8_t reg, uint8_t value) {
  Wire.beginTransmission(kAs7343I2cAddress);
  Wire.write(reg);
  Wire.write(value);
  return Wire.endTransmission() == 0;
}

bool readRegister8(uint8_t reg, uint8_t *value) {
  if (!value) return false;
  Wire.beginTransmission(kAs7343I2cAddress);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom(static_cast<int>(kAs7343I2cAddress), 1) != 1) return false;
  *value = Wire.read();
  return true;
}

bool setRegisterBank1(uint8_t *original_cfg0) {
  if (!original_cfg0) return false;
  if (!readRegister8(kAs7343Cfg0Register, original_cfg0)) return false;
  const uint8_t bank1_cfg0 = *original_cfg0 | kAs7343RegBankBitMask;
  if (bank1_cfg0 == *original_cfg0) return true;
  return writeRegister8(kAs7343Cfg0Register, bank1_cfg0);
}

bool restoreRegisterBank(uint8_t original_cfg0) {
  return writeRegister8(kAs7343Cfg0Register, original_cfg0);
}

bool readBank1Register8(uint8_t reg, uint8_t *value) {
  uint8_t original_cfg0 = 0;
  if (!setRegisterBank1(&original_cfg0)) return false;
  const bool ok = readRegister8(reg, value);
  const bool restore_ok = restoreRegisterBank(original_cfg0);
  return ok && restore_ok;
}

} // namespace

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

bool as7343_readChipId(uint8_t *chip_id) {
  return readBank1Register8(kAs7343ChipIdRegister, chip_id);
}

bool initAS7343() {
  uint8_t chip_id = 0;
  if (!as7343_readChipId(&chip_id) || chip_id != kAs7343ChipId) {
    return false;
  }

  // Power on: PON=1, SP_EN=0, FDEN=0 (flicker detection disabled)
  if (!writeRegister8(kAs7343Enable, kAs7343PonBit)) return false;
  delay(2);  // AS7343 power-on oscillator stabilization (2 ms per datasheet)

  if (!writeRegister8(kAs7343Atime, s_atime)) return false;
  if (!writeRegister8(kAs7343AstepL, static_cast<uint8_t>(s_astep & 0xFF))) return false;
  if (!writeRegister8(kAs7343AstepH, static_cast<uint8_t>(s_astep >> 8)))   return false;
  if (!writeRegister8(kAs7343Cfg1,   s_gain)) return false;

  // Configure AUTO_SMUX=3 (18-channel) in CFG20 bits[6:5]
  // Confirmed: kAs7343Cfg20=0xD6, bits[6:5] from Adafruit AS7343 library source
  {
    uint8_t cfg20 = 0;
    if (!readRegister8(kAs7343Cfg20, &cfg20)) return false;
    const uint8_t new_cfg20 = (cfg20 & ~kAs7343AutoSmuxMask) | kAs7343AutoSmux18;
    if (!writeRegister8(kAs7343Cfg20, new_cfg20)) return false;
    #if DEBUG
    uint8_t readback = 0;
    readRegister8(kAs7343Cfg20, &readback);
    Serial.print(F("[as7343-init] CFG20(0xD6) was=0x")); Serial.print(cfg20, HEX);
    Serial.print(F(" wrote=0x")); Serial.print(new_cfg20, HEX);
    Serial.print(F(" readback=0x")); Serial.println(readback, HEX);
    #endif
  }

  return true;
}

bool as7343_readInto(SpectrometerResult *out) {
  if (!out) return false;

  // Read ENABLE; ensure SP_EN is clear before starting a fresh measurement
  uint8_t enable_val = 0;
  if (!readRegister8(kAs7343Enable, &enable_val)) return false;
  writeRegister8(kAs7343Enable, enable_val & ~kAs7343SpEnBit);

  // Clear stale STATUS flags (self-clear by write-back)
  uint8_t status_val = 0;
  if (readRegister8(kAs7343Status, &status_val)) {
    writeRegister8(kAs7343Status, status_val);
  }

  // Read ASTATUS to clear any stale data latch
  uint8_t astatus_stale = 0;
  readRegister8(kAs7343Astatus, &astatus_stale);

  // Start measurement: set SP_EN=1
  if (!writeRegister8(kAs7343Enable, enable_val | kAs7343SpEnBit)) return false;

  // Poll AVALID (STATUS2 bit 6) with 1000 ms timeout.
  // AVALID fires once after all 18-channel cycles complete.
  const uint32_t deadline = millis() + 1000ul;
  bool avalid = false;
  uint8_t status2 = 0;
  while (millis() < deadline) {
    if (readRegister8(kAs7343Status2, &status2) && (status2 & kAs7343AvalidBit)) {
      avalid = true;
      break;
    }
    delay(1);
  }
  #if DEBUG
  Serial.print(F("[as7343-debug] STATUS2=0x")); Serial.print(status2, HEX);
  Serial.println(avalid ? F(" AVALID") : F(" TIMEOUT"));
  #endif

  if (!avalid) {
    writeRegister8(kAs7343Enable, enable_val & ~kAs7343SpEnBit);
    return false;
  }

  // Read ASTATUS to latch spectral data (required before burst read)
  uint8_t astatus_val = 0;
  readRegister8(kAs7343Astatus, &astatus_val);

  // Burst read: DATA_0_L (0x96) through DATA_17_H = 36 bytes
  // SP_EN remains set during burst — clearing it first resets DATA registers
  Wire.beginTransmission(kAs7343I2cAddress);
  Wire.write(kAs7343Data0L);
  if (Wire.endTransmission(false) != 0) {
    writeRegister8(kAs7343Enable, enable_val & ~kAs7343SpEnBit);
    return false;
  }
  if (Wire.requestFrom(static_cast<int>(kAs7343I2cAddress),
                       static_cast<int>(kBurstLen)) != kBurstLen) {
    writeRegister8(kAs7343Enable, enable_val & ~kAs7343SpEnBit);
    return false;
  }
  uint8_t buf[kBurstLen];
  for (uint8_t i = 0; i < kBurstLen; i++) {
    buf[i] = Wire.read();
  }

  // Safe to stop measurement now
  writeRegister8(kAs7343Enable, enable_val & ~kAs7343SpEnBit);

  out->model = SpectrometerModel::AS7343;

  // Per-channel saturation: ASTATUS bit7 = global ASAT,
  // STATUS2 bit2 = analog sat, bit3 = digital sat.
  // When saturated, mark all spectral channels (bits 0..12).
  const bool analog_sat  = (status2 & kAs7343AsatAnalog) != 0;
  const bool digital_sat = (status2 & kAs7343AsatDigital) != 0;
  const bool global_sat  = (astatus_val & kAs7343AstatusAsat) != 0;
  out->sat_mask = (analog_sat || digital_sat || global_sat) ? 0x1FFF : 0;  // bits 0-12

  // Decode all 18 raw DATA registers (little-endian 16-bit each)
  uint16_t raw[18];
  for (uint8_t i = 0; i < 18; i++) {
    raw[i] = static_cast<uint16_t>(buf[i * 2]) |
             (static_cast<uint16_t>(buf[i * 2 + 1]) << 8);
  }

  // Remap 18 raw DATA registers to 13 output channels (12 spectral + clear).
  //
  // DATA register order (Adafruit as7343_channel_t / AS7343 datasheet):
  //   Cycle 1: [0]=FZ/450  [1]=FY/555  [2]=FXL/600  [3]=NIR/855  [4]=VIS_TL [5]=VIS_BR
  //   Cycle 2: [6]=F2/425  [7]=F3/475  [8]=F4/515   [9]=F6/640   [10]=VIS_TL [11]=VIS_BR
  //   Cycle 3: [12]=F1/405 [13]=F7/690 [14]=F8/745  [15]=F5/550  [16]=VIS_TL [17]=VIS_BR
  static constexpr uint8_t kRemap[12] = {
    12,  // f1_405   ← DATA[12]
     6,  // f2_425   ← DATA[6]
     0,  // fz_450   ← DATA[0]
     7,  // f3_475   ← DATA[7]
     8,  // f4_515   ← DATA[8]
    15,  // f5_550   ← DATA[15]
     1,  // fy_555   ← DATA[1]
     2,  // fxl_600  ← DATA[2]
     9,  // f6_640   ← DATA[9]
    13,  // f7_690   ← DATA[13]
    14,  // f8_745   ← DATA[14]
     3,  // nir_855  ← DATA[3]
  };
  for (uint8_t i = 0; i < 12; i++) {
    out->channels[i] = raw[kRemap[i]];
  }

  // Clear channel: average the 6 VIS broadband readings (VIS_TL + VIS_BR × 3 cycles)
  static constexpr uint8_t kVisIndices[6] = { 4, 5, 10, 11, 16, 17 };
  uint32_t vis_sum = 0;
  for (uint8_t i = 0; i < 6; i++) {
    vis_sum += raw[kVisIndices[i]];
  }
  out->channels[12] = static_cast<uint16_t>(vis_sum / 6);
  out->channel_count = 13;

  return true;
}

bool as7343_readChannelFast(uint8_t out_index, uint16_t *raw_out,
                            uint16_t *sat_mask_out) {
  if (!raw_out) return false;

  // Map SpectrometerResult index to (required AUTO_SMUX mode, raw DATA index).
  // Cycle 1 (AUTO_SMUX=6CH, 1 integration):  DATA[0..5]
  // Cycle 2 (AUTO_SMUX=12CH, 2 integrations): DATA[6..11]
  // Cycle 3 (AUTO_SMUX=18CH, 3 integrations): DATA[12..17]
  uint8_t smux_bits = 0;   // bits[6:5] value: 0=6CH, 1=12CH, 3=18CH
  uint8_t raw_idx   = 0;
  switch (out_index) {
  case 2:  smux_bits = 0; raw_idx = 0;  break;  // fz_450  (cycle 1)
  case 6:  smux_bits = 0; raw_idx = 1;  break;  // fy_555  (cycle 1)
  case 7:  smux_bits = 0; raw_idx = 2;  break;  // fxl_600 (cycle 1)
  case 11: smux_bits = 0; raw_idx = 3;  break;  // nir_855 (cycle 1)
  case 1:  smux_bits = 1; raw_idx = 6;  break;  // f2_425  (cycle 2)
  case 3:  smux_bits = 1; raw_idx = 7;  break;  // f3_475  (cycle 2)
  case 4:  smux_bits = 1; raw_idx = 8;  break;  // f4_515  (cycle 2)
  case 8:  smux_bits = 1; raw_idx = 9;  break;  // f6_640  (cycle 2)
  case 0:  smux_bits = 3; raw_idx = 12; break;  // f1_405  (cycle 3)
  case 9:  smux_bits = 3; raw_idx = 13; break;  // f7_690  (cycle 3)
  case 10: smux_bits = 3; raw_idx = 14; break;  // f8_745  (cycle 3)
  case 5:  smux_bits = 3; raw_idx = 15; break;  // f5_550  (cycle 3)
  default: return false;                         // clear (12) needs all cycles
  }
  const uint8_t auto_smux_value = static_cast<uint8_t>(smux_bits << 5);

  // 1. Read CFG20 and swap in the minimum-cycle AUTO_SMUX mode.
  uint8_t cfg20_orig = 0;
  if (!readRegister8(kAs7343Cfg20, &cfg20_orig)) return false;
  const uint8_t cfg20_new = (cfg20_orig & ~kAs7343AutoSmuxMask) | auto_smux_value;
  if (cfg20_new != cfg20_orig) {
    if (!writeRegister8(kAs7343Cfg20, cfg20_new)) return false;
  }

  // 2. Ensure SP_EN is clear and stale status/data latches are flushed.
  uint8_t enable_val = 0;
  if (!readRegister8(kAs7343Enable, &enable_val)) {
    writeRegister8(kAs7343Cfg20, cfg20_orig);
    return false;
  }
  writeRegister8(kAs7343Enable, enable_val & ~kAs7343SpEnBit);
  uint8_t status_val = 0;
  if (readRegister8(kAs7343Status, &status_val)) {
    writeRegister8(kAs7343Status, status_val);
  }
  uint8_t astatus_stale = 0;
  readRegister8(kAs7343Astatus, &astatus_stale);

  // 3. Start the spectral measurement.
  if (!writeRegister8(kAs7343Enable, enable_val | kAs7343SpEnBit)) {
    writeRegister8(kAs7343Cfg20, cfg20_orig);
    return false;
  }

  // 4. Poll AVALID.  At ATIME=29,ASTEP=599 each cycle is ~50 ms.
  const uint32_t deadline = millis() + 1000ul;
  uint8_t status2 = 0;
  bool avalid = false;
  while (millis() < deadline) {
    if (readRegister8(kAs7343Status2, &status2) && (status2 & kAs7343AvalidBit)) {
      avalid = true;
      break;
    }
    delay(1);
  }
  if (!avalid) {
    writeRegister8(kAs7343Enable, enable_val & ~kAs7343SpEnBit);
    writeRegister8(kAs7343Cfg20, cfg20_orig);
    return false;
  }

  // 5. Latch spectral data and read only the two bytes for the target channel.
  uint8_t astatus_val = 0;
  readRegister8(kAs7343Astatus, &astatus_val);

  const uint8_t target_reg = kAs7343Data0L + raw_idx * 2;
  Wire.beginTransmission(kAs7343I2cAddress);
  Wire.write(target_reg);
  if (Wire.endTransmission(false) != 0) {
    writeRegister8(kAs7343Enable, enable_val & ~kAs7343SpEnBit);
    writeRegister8(kAs7343Cfg20, cfg20_orig);
    return false;
  }
  if (Wire.requestFrom(static_cast<int>(kAs7343I2cAddress), 2) != 2) {
    writeRegister8(kAs7343Enable, enable_val & ~kAs7343SpEnBit);
    writeRegister8(kAs7343Cfg20, cfg20_orig);
    return false;
  }
  const uint8_t lo = Wire.read();
  const uint8_t hi = Wire.read();
  *raw_out = static_cast<uint16_t>(lo) | (static_cast<uint16_t>(hi) << 8);

  // 6. Stop measurement and restore 18CH mode so as7343_readInto() keeps working.
  writeRegister8(kAs7343Enable, enable_val & ~kAs7343SpEnBit);
  if (cfg20_new != cfg20_orig) {
    writeRegister8(kAs7343Cfg20, cfg20_orig);
  }

  if (sat_mask_out) {
    const bool analog_sat  = (status2 & kAs7343AsatAnalog) != 0;
    const bool digital_sat = (status2 & kAs7343AsatDigital) != 0;
    const bool global_sat  = (astatus_val & kAs7343AstatusAsat) != 0;
    *sat_mask_out = (analog_sat || digital_sat || global_sat) ? 0x1 : 0x0;
  }
  return true;
}

// Returns quantized actual LED current in mA, 0 if disabled, 0xFFFF on error.
// TODO: verify AS7343 LED register format against datasheet.
uint16_t as7343_setLEDCurrent(uint16_t led_current_ma) {
  if (led_current_ma == 0) {
    uint8_t reg = 0;
    if (!readRegister8(kAs7343Led, &reg)) return 0xFFFF;
    if (!writeRegister8(kAs7343Led, reg & ~0x80u)) return 0xFFFF;
    return 0;
  }
  uint16_t normalized = led_current_ma < 4 ? 4 : led_current_ma;
  normalized = 4 + (((normalized - 4) / 2) * 2);
  const uint8_t drive = static_cast<uint8_t>((normalized - 4) / 2);
  if (!writeRegister8(kAs7343Led, static_cast<uint8_t>(0x80u | (drive & 0x7Fu)))) {
    return 0xFFFF;
  }
  return normalized;
}

uint8_t  as7343_getAtIME() { return s_atime; }
uint16_t as7343_getAStep() { return s_astep; }
uint8_t  as7343_getGain()  { return s_gain;  }

bool as7343_setAtIME(uint8_t atime) {
  if (!writeRegister8(kAs7343Atime, atime)) return false;
  s_atime = atime;
  return true;
}

bool as7343_setAStep(uint16_t astep) {
  if (!writeRegister8(kAs7343AstepL, static_cast<uint8_t>(astep & 0xFF))) return false;
  if (!writeRegister8(kAs7343AstepH, static_cast<uint8_t>(astep >> 8)))   return false;
  s_astep = astep;
  return true;
}

bool as7343_setGain(uint8_t gain) {
  if (!writeRegister8(kAs7343Cfg1, gain)) return false;
  s_gain = gain;
  return true;
}
