#pragma once

#include <Arduino.h>
#include "app/spectrometer_types.h"

bool initAS7343();
bool as7343_readChipId(uint8_t *chip_id);
bool as7343_readInto(SpectrometerResult *out);
// Single-channel fast read: runs the minimum number of AUTO_SMUX cycles needed
// for the target channel (6CH=1 cycle, 12CH=2, 18CH=3 depending on channel).
// out_index is a SpectrometerResult channel index (0..11 for spectral; clear
// is not supported because it requires all cycles).
bool as7343_readChannelFast(uint8_t out_index, uint16_t *raw_out,
                            uint16_t *sat_mask_out);
uint16_t as7343_setLEDCurrent(uint16_t led_current_ma);
uint8_t  as7343_getAtIME();
bool     as7343_setAtIME(uint8_t atime);
uint16_t as7343_getAStep();
bool     as7343_setAStep(uint16_t astep);
uint8_t  as7343_getGain();
bool     as7343_setGain(uint8_t gain);
