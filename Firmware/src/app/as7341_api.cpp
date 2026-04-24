#include <Wire.h>
#include <Adafruit_AS7341.h>
#include "app/as7341_api.h"

namespace {

constexpr uint8_t kAs7341I2cAddress  = 0x39;
constexpr uint8_t kAs7341WhoamiReg   = 0x92;
// Chip ID check: (raw & 0xFC) == (0x09 << 2) == 0x24
constexpr uint8_t kAs7341ChipIdMask    = 0xFC;
constexpr uint8_t kAs7341ChipIdMasked  = 0x24;

// Adafruit channel index -> SpectrometerResult.channels[] index mapping.
// Indices 4 and 5 of the Adafruit array are SMUX pass-1 intermediates and must
// be skipped.  The 10 remaining channels map in order:
//   Adafruit[0..3]  -> channels[0..3]  (f1_415..f4_515)
//   Adafruit[6..11] -> channels[4..9]  (f5_555..nir)
constexpr uint8_t kAdafruitLen = 12;
constexpr uint8_t kResultLen   = 10;

} // namespace

static Adafruit_AS7341 as7341;

bool initAS7341() {
  if (!as7341.begin()) {
    return false;
  }
  as7341.setATIME(100);
  as7341.setASTEP(999);
  as7341.setGain(AS7341_GAIN_16X);
  return true;
}

bool as7341_readAndValidateChipId(uint8_t *raw_out) {
  Wire.beginTransmission(kAs7341I2cAddress);
  Wire.write(kAs7341WhoamiReg);
  if (Wire.endTransmission(false) != 0) {
    return false;
  }
  if (Wire.requestFrom(static_cast<int>(kAs7341I2cAddress), 1) != 1) {
    return false;
  }
  const uint8_t raw = Wire.read();
  if (raw_out) {
    *raw_out = raw;
  }
  return (raw & kAs7341ChipIdMask) == kAs7341ChipIdMasked;
}

bool as7341_readInto(SpectrometerResult *out) {
  if (!out) {
    return false;
  }
  uint16_t raw[kAdafruitLen];
  if (!as7341.readAllChannels(raw)) {
    return false;
  }
  out->model         = SpectrometerModel::AS7341;
  out->channel_count = kResultLen;
  out->sat_mask      = 0;
  // First SMUX pass: Adafruit[0..3] -> channels[0..3]
  out->channels[0] = raw[0];
  out->channels[1] = raw[1];
  out->channels[2] = raw[2];
  out->channels[3] = raw[3];
  // raw[4] and raw[5] are SMUX pass-1 clear/NIR intermediates — skipped
  // Second SMUX pass: Adafruit[6..11] -> channels[4..9]
  out->channels[4] = raw[6];
  out->channels[5] = raw[7];
  out->channels[6] = raw[8];
  out->channels[7] = raw[9];
  out->channels[8] = raw[10];
  out->channels[9] = raw[11];
  return true;
}

uint8_t as7341_setAtIME(uint8_t atime_value) {
  return as7341.setATIME(atime_value);
}

uint8_t as7341_getAtIME() {
  return as7341.getATIME();
}

uint16_t as7341_setAStep(uint16_t astep_value) {
  return as7341.setASTEP(astep_value);
}

uint16_t as7341_getAStep() {
  return as7341.getASTEP();
}

bool as7341_setGain(as7341_gain_t gain) {
  if (!as7341.setGain(gain)) {
    return false;
  }
  return as7341.getGain() == gain;
}

uint8_t as7341_getGain() {
  return static_cast<uint8_t>(as7341.getGain());
}

namespace {
// Direct-register helpers for the single-channel fast path.  The Adafruit
// library's SMUX command + SMUX-enable sequence is private, so we drive those
// registers ourselves while reusing its public setup_F{1-4,5-8}_Clear_NIR()
// SMUX-config writers.
constexpr uint8_t kAs7341EnableReg    = 0x80;
constexpr uint8_t kAs7341Cfg6Reg      = 0xAF;
constexpr uint8_t kAs7341Ch0DataLReg  = 0x95;
constexpr uint8_t kAs7341Status2Reg   = 0xA3;
constexpr uint8_t kAs7341SmuxenBit    = 0x10;  // ENABLE bit 4
constexpr uint8_t kAs7341SmuxCmdWrite = 0x10;  // CFG6 bits[4:3] = 0b10
constexpr uint8_t kAs7341AvalidBit    = 0x40;  // STATUS2 bit 6
constexpr uint8_t kAs7341AsatBit      = 0x80;  // STATUS2 bit 7 (ASAT)

bool writeReg8(uint8_t reg, uint8_t value) {
  Wire.beginTransmission(kAs7341I2cAddress);
  Wire.write(reg);
  Wire.write(value);
  return Wire.endTransmission() == 0;
}

bool readReg8(uint8_t reg, uint8_t *value) {
  Wire.beginTransmission(kAs7341I2cAddress);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom(static_cast<int>(kAs7341I2cAddress), 1) != 1) return false;
  *value = Wire.read();
  return true;
}
} // namespace

// Returns the quantized actual LED current in mA, 0 if disabled, 0xFFFF on error.
uint16_t as7341_setLEDCurrent(uint16_t led_current_ma) {
  if (led_current_ma == 0) {
    as7341.enableLED(false);
    return 0;
  }

  if (!as7341.setLEDCurrent(led_current_ma)) {
    return 0xFFFF;
  }

  // Quantize to device resolution: 4 mA minimum, 2 mA steps.
  uint16_t normalized = led_current_ma < 4 ? 4 : led_current_ma;
  normalized = 4 + (((normalized - 4) / 2) * 2);

  if (as7341.getLEDCurrent() != normalized) {
    return 0xFFFF;
  }

  as7341.enableLED(true);
  return normalized;
}

bool as7341_readChannelFast(uint8_t out_index, uint16_t *raw_out,
                            uint16_t *sat_mask_out) {
  if (!raw_out) return false;

  // Map SpectrometerResult channel index to (SMUX pass, ADC index within pass).
  // Pass 1 (setup_F1F4_Clear_NIR): ADC0=F1, 1=F2, 2=F3, 3=F4, 4=Clear, 5=NIR.
  // Pass 2 (setup_F5F8_Clear_NIR): ADC0=F5, 1=F6, 2=F7, 3=F8, 4=Clear, 5=NIR.
  // Clear/NIR exist in both passes; choose pass 2 to match as7341_readInto().
  bool use_pass1;
  uint8_t adc_idx;
  switch (out_index) {
  case 0: use_pass1 = true;  adc_idx = 0; break;  // f1_415
  case 1: use_pass1 = true;  adc_idx = 1; break;  // f2_445
  case 2: use_pass1 = true;  adc_idx = 2; break;  // f3_480
  case 3: use_pass1 = true;  adc_idx = 3; break;  // f4_515
  case 4: use_pass1 = false; adc_idx = 0; break;  // f5_555
  case 5: use_pass1 = false; adc_idx = 1; break;  // f6_590
  case 6: use_pass1 = false; adc_idx = 2; break;  // f7_630
  case 7: use_pass1 = false; adc_idx = 3; break;  // f8_680
  case 8: use_pass1 = false; adc_idx = 4; break;  // clear
  case 9: use_pass1 = false; adc_idx = 5; break;  // nir
  default: return false;
  }

  // 1. Stop any in-flight spectral measurement.
  as7341.enableSpectralMeasurement(false);

  // 2. Set SMUX command = WRITE (CFG6 bits[4:3] = 0b10).
  if (!writeReg8(kAs7341Cfg6Reg, kAs7341SmuxCmdWrite)) return false;

  // 3. Write the SMUX config for the chosen pass.
  if (use_pass1) as7341.setup_F1F4_Clear_NIR();
  else           as7341.setup_F5F8_Clear_NIR();

  // 4. Trigger SMUX load (ENABLE bit 4) and wait for it to self-clear.
  uint8_t enable_val = 0;
  if (!readReg8(kAs7341EnableReg, &enable_val)) return false;
  if (!writeReg8(kAs7341EnableReg, enable_val | kAs7341SmuxenBit)) return false;
  {
    const uint32_t deadline = millis() + 100ul;
    uint8_t e = enable_val | kAs7341SmuxenBit;
    while (millis() < deadline) {
      if (!readReg8(kAs7341EnableReg, &e)) return false;
      if (!(e & kAs7341SmuxenBit)) break;
      delay(1);
    }
    if (e & kAs7341SmuxenBit) return false;
  }

  // 5. Start the single-pass integration.
  if (!as7341.enableSpectralMeasurement(true)) return false;

  // 6. Poll AVALID (STATUS2 bit 6).  Integration = (ATIME+1) * (ASTEP+1) * 2.78 µs.
  {
    const uint32_t deadline = millis() + 1000ul;
    uint8_t status2 = 0;
    bool avalid = false;
    while (millis() < deadline) {
      if (readReg8(kAs7341Status2Reg, &status2) && (status2 & kAs7341AvalidBit)) {
        avalid = true;
        break;
      }
      delay(1);
    }
    if (!avalid) {
      as7341.enableSpectralMeasurement(false);
      return false;
    }
    if (sat_mask_out) {
      *sat_mask_out = (status2 & kAs7341AsatBit) ? 0x1 : 0x0;
    }
  }

  // 7. Read only the two bytes for the target ADC channel.
  const uint8_t reg = kAs7341Ch0DataLReg + adc_idx * 2;
  Wire.beginTransmission(kAs7341I2cAddress);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) {
    as7341.enableSpectralMeasurement(false);
    return false;
  }
  if (Wire.requestFrom(static_cast<int>(kAs7341I2cAddress), 2) != 2) {
    as7341.enableSpectralMeasurement(false);
    return false;
  }
  const uint8_t lo = Wire.read();
  const uint8_t hi = Wire.read();
  *raw_out = static_cast<uint16_t>(lo) | (static_cast<uint16_t>(hi) << 8);

  // 8. Stop the sensor.
  as7341.enableSpectralMeasurement(false);
  return true;
}
