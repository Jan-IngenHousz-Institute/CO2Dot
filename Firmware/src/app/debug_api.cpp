#include <Wire.h>
#include <Arduino.h>
#include <ArduinoJson.h>
#include <esp_system.h>

#include "app/commands.h"
#include "app/debug_api.h"
#include "app/response.h"

void i2c_scan() {
  JsonDocument doc;
  JsonArray found = doc.to<JsonArray>();
  for (uint8_t addr = 1; addr < 127; addr++) {
    Wire.beginTransmission(addr);
    if (Wire.endTransmission() == 0) {
      char buf[6];
      snprintf(buf, sizeof(buf), "0x%02X", addr);
      found.add(String(buf));
    }
  }
  respond(doc);
}

void cmd_reboot() {
  JsonDocument doc;
  doc["reboot"] = "initiated";
  respond(doc);
  // The LINE rx-loop normally appends the terminating newline after we return,
  // but esp_restart() never returns — emit it (and flush) here.
  Serial.print('\n');
  Serial.flush();
  esp_restart();
}
