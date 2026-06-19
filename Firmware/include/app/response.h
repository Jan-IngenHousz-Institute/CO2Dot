#pragma once

#include <Arduino.h>
#include <ArduinoJson.h>

// ── openJII serial response layer ──────────────────────────────────────────
// See /CommunicationProtocolOpenJIISerial.md for the full protocol spec.
//
// Every command builds a command-as-root JsonDocument and hands it to respond().
// respond() renders it according to the active mode:
//   - envelope mode (jsonOutputMode == true): strict JSON, no newline
//   - LINE mode, no path: full payload as JSON, no newline
//   - LINE mode, with a dot-path: the projected sub-node — a scalar leaf is
//     printed bare (unquoted), a container is printed as JSON — or an error
// respond() never appends a newline; the LINE rx-loop / envelope writer own
// message termination.

// Dot-path query state for LINE mode, set by handleCommandText before dispatch.
extern String g_requestPath;   // path after the command, "" when absent
extern String g_requestFull;   // full request incl. command, for error "path"

void respond(JsonDocument &doc);

// Convenience: emit {"error":code} (command-as-root error object).
void respondError(const char *code);
