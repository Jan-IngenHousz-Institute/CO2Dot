#include <ArduinoJson.h>
#include <Wire.h>
#include "app/commands.h"
#include "app/response.h"
#include "app/bme68x_api.h"
#include "app/debug_api.h"
#include "app/spectrometer_api.h"

bool jsonOutputMode = false;

// Command catalogue: name -> one-line description (with usage for the ones that
// take args). Built as a command-as-root object so the dot-path layer gives
// `help` (full map), `help.<cmd>` (one description, bare in LINE mode) and
// `help.keys()` (command names) for free. Keep this in sync with the dispatch
// branches below.
static void fill_help(JsonObject h) {
  h["hello"]           = "device identity -> {device,version}";
  h["env"]             = "read BME68x environment (T,P,H,gas)";
  h["i2c_scan"]        = "scan the I2C bus for device addresses";
  h["spec"]            = "read calibrated spectrometer channels";
  h["spec_raw"]        = "read raw spectrometer channel counts";
  h["set_led"]         = "set_led,<mA> - set LED drive current (default 10)";
  h["spec_flash"]      = "spec_flash,<mA>,<repeat> - flash LED then read spectrum";
  h["spec_flash_wave"] = "spec_flash_wave,<mA>,<nm> - flash at wavelength then read";
  h["spec_set_atime"]  = "spec_set_atime,<n> - set integration ATIME";
  h["spec_set_astep"]  = "spec_set_astep,<n> - set integration ASTEP";
  h["spec_set_gain"]   = "spec_set_gain,<n> - set analog gain (AGAIN)";
  h["spec_status"]     = "spectrometer configuration & status";
  h["bme_status"]      = "BME68x configuration & status";
  h["status"]          = "combined spectrometer + bme status";
  h["reboot"]          = "restart the device";
  h["help"]            = "list commands; help.<cmd> for one, help.keys() for names";
}

void handleCommandText(const String &cmd) {
  // Split off an optional dot-path query (LINE mode). Command identifiers use
  // [A-Za-z0-9_], so the first '.' unambiguously begins the path. Action
  // commands carry args after a ',' and contain no '.', so `name` == `cmd`.
  const int dot = cmd.indexOf('.');
  const String name = (dot >= 0) ? cmd.substring(0, dot) : cmd;
  g_requestFull = cmd;
  g_requestPath = (dot >= 0) ? cmd.substring(dot + 1) : String();

  if (name == "hello") {
    JsonDocument doc;
    doc["device"]  = "CO2Dot";
    doc["version"] = "1.0";
    respond(doc);

  } else if (name == "help") {
    JsonDocument doc;
    fill_help(doc.to<JsonObject>());
    respond(doc);

  } else if (name == "env") {
    cmd_bme_read();

  } else if (name == "i2c_scan") {
    i2c_scan();

  } else if (name == "spec_raw") {
    spectrometer_read_raw();

  } else if (name == "spec") {
    spectrometer_read();

  } else if (cmd.startsWith("set_led")) {
    int ledCurrent = 10;
    int comma = cmd.indexOf(',');
    if (comma > 0) {
      String arg = cmd.substring(comma + 1);
      arg.trim();
      ledCurrent = arg.toInt();
    }
    spectrometer_set_led_current(static_cast<uint16_t>(ledCurrent));

  } else if (cmd.startsWith("spec_flash_wave")) {
    int ledCurrent = 10;
    int wavelength = 0;
    int first_comma = cmd.indexOf(',');
    if (first_comma > 0) {
      int second_comma = cmd.indexOf(',', first_comma + 1);
      if (second_comma > 0) {
        String led_arg = cmd.substring(first_comma + 1, second_comma);
        String wv_arg  = cmd.substring(second_comma + 1);
        led_arg.trim();
        wv_arg.trim();
        ledCurrent = led_arg.toInt();
        wavelength = wv_arg.toInt();
      }
    }
    spectrometer_read_flash_wave(static_cast<uint16_t>(ledCurrent),
                                 static_cast<uint16_t>(wavelength));

  } else if (cmd.startsWith("spec_flash")) {
    int ledCurrent = 10;
    int repeat = 1;
    int comma = cmd.indexOf(',');
    if (comma > 0) {
      String rest = cmd.substring(comma + 1);
      rest.trim();
      int comma2 = rest.indexOf(',');
      if (comma2 > 0) {
        ledCurrent = rest.substring(0, comma2).toInt();
        repeat = rest.substring(comma2 + 1).toInt();
        if (repeat < 1) repeat = 1;
      } else {
        ledCurrent = rest.toInt();
      }
    }
    spectrometer_read_flash(static_cast<uint16_t>(ledCurrent), static_cast<uint16_t>(repeat));

  } else if (cmd.startsWith("spec_set_atime")) {
    int comma = cmd.indexOf(',');
    const char *arg = (comma > 0) ? cmd.c_str() + comma + 1 : "";
    cmd_spectrometer_set_atime(comma > 0 ? 1 : 0, &arg);

  } else if (cmd.startsWith("spec_set_astep")) {
    int comma = cmd.indexOf(',');
    const char *arg = (comma > 0) ? cmd.c_str() + comma + 1 : "";
    cmd_spectrometer_set_astep(comma > 0 ? 1 : 0, &arg);

  } else if (cmd.startsWith("spec_set_gain")) {
    int comma = cmd.indexOf(',');
    const char *arg = (comma > 0) ? cmd.c_str() + comma + 1 : "";
    cmd_spectrometer_set_gain(comma > 0 ? 1 : 0, &arg);

  } else if (name == "spec_status") {
    cmd_spectrometer_status();

  } else if (name == "bme_status") {
    cmd_bme_status();

  } else if (name == "status") {
    // Combined command-as-root {"spectrometer":{...},"bme":{...}} — one message
    // per "status" request. Query e.g. with status.bme.T
    JsonDocument doc;
    fill_spectrometer_status(doc["spectrometer"].to<JsonObject>());
    fill_bme_status(doc["bme"].to<JsonObject>());
    respond(doc);

  } else if (name == "reboot") {
    cmd_reboot();

  } else if (cmd.length() > 0) {
    JsonDocument doc;
    doc["error"] = "unknown_command";
    doc["path"]  = cmd;
    respond(doc);
  }
}
