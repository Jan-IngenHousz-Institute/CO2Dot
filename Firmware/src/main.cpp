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
  Serial.print(F("]}]}7A1E3AA1"));
  Serial.print('\n');
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
    Serial.print(F("\"}\n"));
    return false;
  }

  serialJsonInit();
  bool firstOut = true;
  bool handledAny = false;
  jsonOutputMode = true;

  auto processProtocolSet = [&](JsonArray proto) {
    for (JsonVariant setV : proto) {
      JsonObject setObj = setV.as<JsonObject>();
      if (setObj.isNull()) continue;

      const char *label = setObj["label"];
      if (!label) continue;

      uint16_t repeats = setObj["protocol_repeats"] | 1;
      if (repeats == 0) repeats = 1;

      if (!firstOut) Serial.print(',');
      firstOut = false;
      // Always wrap in a JSON array so set[N] is consistently a list for macros
      Serial.print('[');
      for (uint16_t i = 0; i < repeats; i++) {
        if (i > 0) Serial.print(',');
        handleCommandText(String(label));
      }
      Serial.print(']');
      handledAny = true;
    }
  };

  if (doc.is<JsonArray>()) {
    JsonArray arr = doc.as<JsonArray>();
    // Try: array of objects each containing a "set" key
    for (JsonVariant v : arr) {
      JsonObject obj = v.as<JsonObject>();
      if (obj.isNull()) continue;
      JsonArray proto = obj["set"].as<JsonArray>();
      if (!proto.isNull()) processProtocolSet(proto);
    }
    // Fallback: treat the array itself as the command list
    if (!handledAny) processProtocolSet(arr);
  } else if (doc.is<JsonObject>()) {
    // Iterate all members; process first array value as the command list
    for (JsonPair kv : doc.as<JsonObject>()) {
      JsonArray proto = kv.value().as<JsonArray>();
      if (!proto.isNull()) {
        processProtocolSet(proto);
        break;
      }
    }
  }

  serialJsonEnd();
  jsonOutputMode = false;
  return true;
}

// ── setup / loop ───────────────────────────────────────────────────────

void setup() {
  Serial.begin(115200);
#if ARDUINO_USB_CDC_ON_BOOT
  delay(2000);  // give USB CDC time to enumerate on host
#endif
  // Serial.println(F("{\"boot\":\"starting\"}"));
  Wire.begin(3, 4);

  bme_available = initBME();
  #if DEBUG
  if (!bme_available)
    Serial.println(F("[init] BME68x not found"));
  else
    Serial.println(F("[init] BME68x OK"));
  #endif

  initSpectrometer();
  // Serial.println(F("{\"boot\":\"ready\"}"));
}

void loop() {
  // Timeout: reset if we're mid‑JSON and no byte arrives within the window.
  if (rxMode == RxMode::JSON && rxLen > 0
      && (millis() - lastCharMs) > kJsonTimeoutMs) {
    Serial.print(F("{\"error\":\"json_timeout\"}\n"));
    resetRx();
  }

  while (Serial.available() > 0) {
    char c = (char)Serial.read();
    if (c == '\r') continue;

    lastCharMs = millis();

    // ── overflow guard (all modes) ──
    if (rxLen >= sizeof(rxBuf) - 1) {
      Serial.print(F("{\"error\":\"rx_overflow\"}\n"));
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
        if (cmd.length() > 0) {
          handleCommandText(cmd);
          Serial.print('\n');  // terminate response so serial parsers can delimit
        }
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





