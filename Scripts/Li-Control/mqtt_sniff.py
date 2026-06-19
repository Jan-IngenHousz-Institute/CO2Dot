"""mqtt_sniff.py — passive discovery of the LI-6800 internal MQTT bus.

Run ON the instrument (paho is already installed there; the firmware uses it):
    python3 mqtt_sniff.py [seconds] [topic_filter]

Connects to the local broker exactly like the firmware does (MQTTv31, localhost),
subscribes to a wildcard, and prints each distinct topic seen plus one sample
payload. Read-only: subscribing never affects publishers. Use it to capture the
real topic strings and payload shapes for this Bluestem version before building
a client against them.
"""
import sys
import time

import paho.mqtt.client as mqtt

DURATION = float(sys.argv[1]) if len(sys.argv) > 1 else 5.0
FILTER = sys.argv[2] if len(sys.argv) > 2 else "licor/#"
HOST = sys.argv[3] if len(sys.argv) > 3 else "localhost"
PORT = int(sys.argv[4]) if len(sys.argv) > 4 else 1883

seen = {}      # topic -> (sample_payload, count)


def on_message(client, userdata, msg):
    try:
        payload = msg.payload.decode("utf-8", "replace")
    except Exception:
        payload = repr(msg.payload)
    sample, count = seen.get(msg.topic, (payload, 0))
    seen[msg.topic] = (sample, count + 1)


try:  # paho-mqtt 2.x requires an explicit callback API version
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1,
                    client_id="li-sniff-discovery", protocol=mqtt.MQTTv31)
except (AttributeError, TypeError):  # paho-mqtt 1.x
    c = mqtt.Client(client_id="li-sniff-discovery", protocol=mqtt.MQTTv31)
c.on_message = on_message
c.connect(HOST, PORT, keepalive=30)
c.subscribe(FILTER)
c.loop_start()
time.sleep(DURATION)
c.loop_stop()

for topic in sorted(seen):
    sample, count = seen[topic]
    one_line = " ".join(sample.split())
    print(f"[{count:>4}x] {topic}")
    print(f"        {one_line[:280]}")
print(f"=== {len(seen)} distinct topics in {DURATION:.0f}s (filter {FILTER}) ===")
