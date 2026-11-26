#pragma once

#include <stdint.h>
#include <stdio.h>

#ifndef CO2DOT_FW_VERSION
#define CO2DOT_FW_VERSION "0.0.0-dev"
#endif

// Hardware identity openJII registers a CO2Dot by (Labs decision 8: family plus
// the normalized MAC). ESP.getEfuseMac() stores byte zero in the low eight bits;
// print it first, uppercase and colon-separated, exactly like the Ambit's
// ambit_trace_v3::format_sensor_id, so every JII ESP32 instrument reports its
// MAC in one form.
inline void format_device_mac(uint64_t efuse_mac, char output[18]) {
  snprintf(output, 18, "%02X:%02X:%02X:%02X:%02X:%02X",
           static_cast<unsigned>(efuse_mac & 0xFFU),
           static_cast<unsigned>((efuse_mac >> 8) & 0xFFU),
           static_cast<unsigned>((efuse_mac >> 16) & 0xFFU),
           static_cast<unsigned>((efuse_mac >> 24) & 0xFFU),
           static_cast<unsigned>((efuse_mac >> 32) & 0xFFU),
           static_cast<unsigned>((efuse_mac >> 40) & 0xFFU));
}
