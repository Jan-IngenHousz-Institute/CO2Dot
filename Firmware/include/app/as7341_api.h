#pragma once
#include <Adafruit_AS7341.h>
#include "app/spectrometer_types.h"

bool initAS7341();
bool as7341_readAndValidateChipId(uint8_t *raw_out);
bool as7341_readInto(SpectrometerResult *out);
// Single-channel fast read: runs ONE SMUX pass instead of two.
// out_index is a SpectrometerResult channel index (0..9).
// *raw_out receives the ADC count; *sat_mask_out receives 0 or 1 (ASAT).
bool as7341_readChannelFast(uint8_t out_index, uint16_t *raw_out,
                            uint16_t *sat_mask_out);
uint8_t as7341_setAtIME(uint8_t atime_value);
uint8_t as7341_getAtIME();
uint16_t as7341_setAStep(uint16_t astep_value);
uint16_t as7341_getAStep();
bool as7341_setGain(as7341_gain_t gain);
uint8_t as7341_getGain();
uint16_t as7341_setLEDCurrent(uint16_t led_current_ma);
