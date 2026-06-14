"""
Diagnostic: what sample rates does PortAudio/sounddevice report for each device?
Run: python diag_sr.py
"""
import os
os.environ["SD_ENABLE_ASIO"] = "1"

import sounddevice as sd

print(f"sounddevice {sd.__version__}")
print(f"PortAudio {sd.get_portaudio_version()[1]}")
print()

devices = sd.query_devices()
hostapis = sd.query_hostapis()

for i, dev in enumerate(devices):
    if dev["max_input_channels"] == 0:
        continue
    api = hostapis[dev["hostapi"]]["name"]
    default_sr = int(dev["default_samplerate"])

    # Try to open the device at several common rates to see which ones work
    supported = []
    for sr in (44100, 48000, 96000):
        try:
            with sd.InputStream(device=i, channels=1, samplerate=sr,
                                blocksize=512, dtype="float32"):
                supported.append(sr)
        except Exception:
            pass

    print(f"[{i}] {dev['name']}  [{api}]")
    print(f"     default_samplerate : {default_sr}")
    print(f"     actually opens at  : {supported if supported else 'none of 44100/48000/96000'}")
    print()
