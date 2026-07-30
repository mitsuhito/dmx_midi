#!/usr/bin/env python3
"""Minimal, standalone Art-Net receive test: bypasses MidiBridge, the
curses UI, and all logging layers. Just prints DMX channels 1-4 with a
timestamp every time any of them changes. Used to check, with zero extra
layers in the way, whether real-time fader changes actually reach this
process's UDP socket."""
import sys
import time

sys.path.insert(0, ".")
from dmx_midi_bridge import ArtNetReceiver  # noqa: E402

last = [None, None, None, None]


def on_frame(channels):
    values = list(channels[0:4]) if len(channels) >= 4 else list(channels) + [0] * (4 - len(channels))
    if values != last:
        last[:] = values
        ts = time.strftime("%H:%M:%S.") + f"{int(time.time() * 1000) % 1000:03d}"
        print(f"{ts}  ch1={values[0]:3d} ch2={values[1]:3d} ch3={values[2]:3d} ch4={values[3]:3d}", flush=True)


receiver = ArtNetReceiver(on_frame, bind_address="0.0.0.0", port=6454, universe=0)
print("Listening for Art-Net on 0.0.0.0:6454, universe 0. Move the fader now. Ctrl+C to stop.", flush=True)
receiver.start()
try:
    while True:
        time.sleep(0.5)
except KeyboardInterrupt:
    pass
finally:
    receiver.stop()
    receiver.join(timeout=2)
