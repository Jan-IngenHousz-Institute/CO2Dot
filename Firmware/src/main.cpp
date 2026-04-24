#include <Arduino.h>
#include <Wire.h>
#include <ArduinoJson.h>
#include "app/commands.h"
#include "app/bme68x_api.h"
#include "app/spectrometer_api.h"

bool bme_available = false;

// ── Receive‐buffer state ───────────────────────────────────────────────
enum class RxMode { UNKNOWN, LINE, JSON };

static char    rxBuf[2048];
static size_t  rxLen       = 0;
static RxMode  rxMode      = RxMode::UNKNOWN;
static int     braceDepth  = 0;
static int     bracketDepth = 0;
static bool    inString    = false;
static char    prevChar    = 0;
static unsigned long lastCharMs = 0;

static constexpr unsigned long kJsonTimeoutMs = 1000; // reset if no char for 1 s

static void resetRx() {
  rxLen        = 0;
  rxMode       = RxMode::UNKNOWN;
  braceDepth   = 0;
  bracketDepth = 0;
  inString     = false;
  prevChar     = 0;
  lastCharMs   = 0;
}

// ── JSON envelope (openJII framing) ────────────────────────────────────

static void serialJsonInit() {
  Serial.print(F("{\"device_name\":\"CO2Dot\",\"device_version\":\"1\","
                  "\"device_battery\":0,\"device_firmware\":1.001,"
                  "\"sample\":[{\"protocol_id\":\"NaN\",\"set\":["));
}

static void serialJsonEnd() {
  Serial.print(F("]}]}"));
  Serial.println();
}

// ── HandleJson ─────────────────────────────────────────────────────────
// Parses a complete JSON document.  Walks the openJII `_protocol_set_`
// array, extracts each `label` string, and dispatches it through the
// normal CLI command handler.

static bool HandleJson(const char *json, size_t len) {
  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, json, len);
  if (err) {
    Serial.print(F("{\"error\":\"json_parse\",\"detail\":\""));
    Serial.print(err.c_str());
    Serial.println(F("\"}"));
    return false;
  }

  serialJsonInit();
  bool firstOut = true;
  jsonOutputMode = true;

  auto processProtocolSet = [&](JsonArray proto) {
    for (JsonVariant setV : proto) {
      JsonObject setObj = setV.as<JsonObject>();
      if (setObj.isNull()) continue;

      const char *label = setObj["label"];
      if (!label) continue;

      uint16_t repeats = setObj["protocol_repeats"] | 1;
      if (repeats == 0) repeats = 1;

      for (uint16_t i = 0; i < repeats; i++) {
        if (!firstOut) Serial.print(',');
        firstOut = false;
        handleCommandText(String(label));
      }
    }
  };

  if (doc.is<JsonArray>()) {
    for (JsonVariant v : doc.as<JsonArray>()) {
      JsonObject obj = v.as<JsonObject>();
      if (obj.isNull()) continue;
      JsonArray proto = obj["_protocol_set_"].as<JsonArray>();
      if (!proto.isNull()) processProtocolSet(proto);
    }
  } else if (doc.is<JsonObject>()) {
    JsonArray proto = doc.as<JsonObject>()["_protocol_set_"].as<JsonArray>();
    if (!proto.isNull()) processProtocolSet(proto);
  }

  serialJsonEnd();
  jsonOutputMode = false;
  return true;
}

// ── setup / loop ───────────────────────────────────────────────────────

void setup() {
  Serial.begin(115200);
  Wire.begin(3, 4);

  bme_available = initBME();
  #if DEBUG
  if (!bme_available)
    Serial.println(F("[init] BME68x not found"));
  else
    Serial.println(F("[init] BME68x OK"));
  #endif

  initSpectrometer();
}

void loop() {
  // Timeout: reset if we're mid‑JSON and no byte arrives within the window.
  if (rxMode == RxMode::JSON && rxLen > 0
      && (millis() - lastCharMs) > kJsonTimeoutMs) {
    Serial.println(F("{\"error\":\"json_timeout\"}"));
    resetRx();
  }

  while (Serial.available() > 0) {
    char c = (char)Serial.read();
    if (c == '\r') continue;

    lastCharMs = millis();

    // ── overflow guard (all modes) ──
    if (rxLen >= sizeof(rxBuf) - 1) {
      Serial.println(F("{\"error\":\"rx_overflow\"}"));
      resetRx();
      continue;
    }
    rxBuf[rxLen++] = c;

    // ── decide mode on first non‑whitespace char ──
    if (rxMode == RxMode::UNKNOWN) {
      size_t i = 0;
      while (i < rxLen && isspace((unsigned char)rxBuf[i])) i++;
      if (i < rxLen) {
        char first = rxBuf[i];
        rxMode = (first == '{' || first == '[') ? RxMode::JSON : RxMode::LINE;
      }
    }

    // ── LINE mode ──
    if (rxMode == RxMode::LINE) {
      if (c == '\n') {
        rxBuf[rxLen] = '\0';
        String cmd(rxBuf);
        cmd.trim();
        if (cmd.length() > 0)
          handleCommandText(cmd);
        resetRx();
      }
      continue;
    }

    // ── JSON mode: track braces / brackets ──
    if (rxMode == RxMode::JSON) {
      if (c == '"' && prevChar != '\\')
        inString = !inString;

      if (!inString) {
        if (c == '{')      braceDepth++;
        else if (c == '}') braceDepth   = (braceDepth   > 0) ? braceDepth   - 1 : 0;
        else if (c == '[') bracketDepth++;
        else if (c == ']') bracketDepth = (bracketDepth > 0) ? bracketDepth - 1 : 0;
      }
      prevChar = c;

      // Complete when top‑level object/array closes.
      if (!inString && braceDepth == 0 && bracketDepth == 0) {
        rxBuf[rxLen] = '\0';
        HandleJson(rxBuf, rxLen);
        resetRx();
      }
      continue;
    }
  }
}





