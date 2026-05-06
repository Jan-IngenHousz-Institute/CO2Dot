#pragma once

#include <Arduino.h>

// When true, command handlers suppress trailing newlines so their output
// can be concatenated inside the openJII JSON envelope.
extern bool jsonOutputMode;

// No-op: JSON responses no longer append trailing newlines.
inline void cmdEndLine() { }

void handleCommandText(const String &cmd);
