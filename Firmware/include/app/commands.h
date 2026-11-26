#pragma once

#include <Arduino.h>

// When true, command handlers suppress trailing newlines so their output
// can be concatenated inside the openJII JSON envelope.
extern bool jsonOutputMode;

// Terminate the current line of command output.
inline void cmdEndLine() { Serial.print('\n'); }

void handleCommandText(const String &cmd);

// openJII generic-device identity: device_name, device_type ("co2dot"),
// device_id (eFuse MAC, see identity.h) and firmware_version.
#include <ArduinoJson.h>
void fill_openjii_info(JsonObject data);
