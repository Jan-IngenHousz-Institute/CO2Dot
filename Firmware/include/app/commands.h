#pragma once

#include <Arduino.h>

// When true, command handlers suppress trailing newlines so their output
// can be concatenated inside the openJII JSON envelope.
extern bool jsonOutputMode;

// Prints '\n' only when jsonOutputMode is false.
// Every command handler should call this instead of Serial.println() at the
// end of its output.
inline void cmdEndLine() { if (!jsonOutputMode) Serial.println(); }

void handleCommandText(const String &cmd);
