#pragma once

#include <Arduino.h>
#include <ArduinoJson.h>
#include <stdint.h>
#include "app/spectrometer_types.h"

extern bool spectrometer_available;
extern SpectrometerModel spectrometer_model;

extern float par_coefficients[18];

const char *spectrometerModelName(SpectrometerModel model);
bool initSpectrometer();
// On failure, writes an "error" field into `doc` and returns false.
bool spectrometerPrepareLegacyCommand(JsonDocument &doc);
bool spectrometer_read_raw();
bool spectrometer_read();
bool spectrometer_set_led_current(uint16_t led_current_ma);
bool spectrometer_read_flash(uint16_t led_current_ma, uint16_t repeat = 1);
bool spectrometer_read_flash_wave(uint16_t led_current_ma, uint16_t wavelength_nm);
// Fill an existing JsonObject with the spectrometer status fields (no print).
// Used by cmd_spectrometer_status() and by the combined "status" command.
void fill_spectrometer_status(JsonObject out);
void cmd_spectrometer_status();
void cmd_spectrometer_set_atime(int argc, const char *argv[]);
void cmd_spectrometer_set_astep(int argc, const char *argv[]);
void cmd_spectrometer_set_gain(int argc, const char *argv[]);
