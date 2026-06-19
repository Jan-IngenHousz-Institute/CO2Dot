#pragma once

#include <Arduino.h>

// When true, command handlers suppress trailing newlines so their output
// can be concatenated inside the openJII JSON envelope.
extern bool jsonOutputMode;

// Terminate the current line of command output.
inline void cmdEndLine() { Serial.print('\n'); }

void handleCommandText(const String &cmd);
