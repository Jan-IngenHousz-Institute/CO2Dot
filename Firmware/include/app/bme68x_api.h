#pragma once

#include <ArduinoJson.h>

extern bool bme_available;

bool initBME(void);
void cmd_bme_read();
// Fill an existing JsonObject with the BME status fields (no print).
// Used by cmd_bme_status() and by the combined "status" command.
void fill_bme_status(JsonObject out);
void cmd_bme_status();

