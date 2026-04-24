#include <ArduinoJson.h>
#include <Wire.h>
#include <bme68xLibrary.h>
#include "app/bme68x_api.h"
#include "app/commands.h"



static constexpr uint8_t BME68X_ADDR = 0x76; // common: 0x76 or 0x77
Bme68x bme;

static void printBMEJson(const bme68xData &d)
{
#ifdef BME68X_USE_FPU
  Serial.print(F("{\"T\":"));
  Serial.print(d.temperature, 2);
  Serial.print(F(",\"P\":"));
  Serial.print(d.pressure / 100.0f, 2);
  Serial.print(F(",\"RH\":"));
  Serial.print(d.humidity, 2);
  Serial.print(F(",\"Gas\":"));
  Serial.print(d.gas_resistance, 0);
  Serial.print('}');
#else
  // Bosch fixed-point defaults:
  // temperature: °C * 100, humidity: %RH * 1000, pressure: Pa, gas_resistance: Ω
  Serial.print(F("{\"T\":"));
  Serial.print(d.temperature / 100.0f, 2);
  Serial.print(F(",\"P\":"));
  Serial.print(d.pressure / 100.0f, 2);
  Serial.print(F(",\"RH\":"));
  Serial.print(d.humidity / 1000.0f, 3);
  Serial.print(F(",\"Gas\":"));
  Serial.print(d.gas_resistance);
  Serial.print('}');
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
    if (!bme_available) {
      Serial.print(F("{\"bme_read\":{\"error\":\"not_available\"}}"));
      cmdEndLine();
      return;
    }
    bme.setOpMode(BME68X_FORCED_MODE);
    uint32_t dur_us = bme.getMeasDur(BME68X_FORCED_MODE);
    delayMicroseconds(dur_us + 150000); // + heater duration (150ms)
    uint8_t n = bme.fetchData();
    if (bme.checkStatus() != 0 || n == 0) {
      Serial.print(F("{\"bme_read\":{\"error\":\"read_failed\"}}"));
      cmdEndLine();
      return;
    }
    bme68xData *all = bme.getAllData();
    Serial.print(F("{\"bme_read\":")); 
    printBMEJson(all[0]);  // use first (most recent) reading
    Serial.print('}');
    cmdEndLine();
}

void fill_bme_status(JsonObject out) {
  out["available"] = bme_available;
}

void cmd_bme_status() {
  StaticJsonDocument<64> doc;
  JsonObject obj = doc["bme_status"].to<JsonObject>();
  fill_bme_status(obj);
  serializeJson(doc, Serial);
  cmdEndLine();
}