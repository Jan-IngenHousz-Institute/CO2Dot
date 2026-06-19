#include <ArduinoJson.h>
#include <Wire.h>
#include <bme68xLibrary.h>
#include "app/bme68x_api.h"
#include "app/commands.h"
#include "app/response.h"



static constexpr uint8_t BME68X_ADDR = 0x76; // common: 0x76 or 0x77
Bme68x bme;

// Fills out with the BME reading {T,P,RH,Gas}. Floats use serialized() so the
// emitted precision is fixed (matching the historical 2/3-decimal output).
static void fill_bme_values(JsonObject out, const bme68xData &d)
{
#ifdef BME68X_USE_FPU
  out["T"]   = serialized(String(d.temperature, 2));
  out["P"]   = serialized(String(d.pressure / 100.0f, 2));
  out["RH"]  = serialized(String(d.humidity, 2));
  out["Gas"] = (uint32_t)d.gas_resistance;
#else
  // Bosch fixed-point defaults:
  // temperature: °C * 100, humidity: %RH * 1000, pressure: Pa, gas_resistance: Ω
  out["T"]   = serialized(String(d.temperature / 100.0f, 2));
  out["P"]   = serialized(String(d.pressure / 100.0f, 2));
  out["RH"]  = serialized(String(d.humidity / 1000.0f, 3));
  out["Gas"] = (uint32_t)d.gas_resistance;
#endif
}

bool initBME(void) {
  bme.begin(BME68X_ADDR, Wire);
  if (bme.checkStatus() != 0) {   
    Serial.print("BME68x init failed with status string: ");
    Serial.println(bme.statusString());
    return false;
  }
  // Configure BME68x Forced mode + filter
  bme.setTPH(BME68X_OS_2X, BME68X_OS_4X, BME68X_OS_2X);
  bme.setFilter(BME68X_FILTER_OFF);
  bme.setHeaterProf(320 /*°C*/, 150 /*ms*/);   // Gas heater for forced mode (example profile)
  bme.setOpMode(BME68X_FORCED_MODE); //  = one-shot measurement when you trigger it
  return true;
}


void cmd_bme_read(){
    JsonDocument doc;
    if (!bme_available) {
      doc["error"] = "not_available";
      respond(doc);
      return;
    }
    bme.setOpMode(BME68X_FORCED_MODE);
    uint32_t dur_us = bme.getMeasDur(BME68X_FORCED_MODE);
    delayMicroseconds(dur_us + 150000); // + heater duration (150ms)
    uint8_t n = bme.fetchData();
    if (bme.checkStatus() != 0 || n == 0) {
      doc["error"] = "read_failed";
      respond(doc);
      return;
    }
    bme68xData *all = bme.getAllData();
    fill_bme_values(doc.to<JsonObject>(), all[0]);  // first (most recent) reading
    respond(doc);
}

void fill_bme_status(JsonObject out) {
  out["available"] = bme_available;
}

void cmd_bme_status() {
  JsonDocument doc;
  fill_bme_status(doc.to<JsonObject>());
  respond(doc);
}