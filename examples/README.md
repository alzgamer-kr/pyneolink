# Examples

Small library-use examples for PyNeolink.

- `camera_example.py`: camera info, snapshot, LED read/set, and guarded reboot helper.
- `sd_card_example.py`: list recordings, download a recording, and guarded remove/format helpers.
- `battery_example.py`: one-shot battery info plus reconnect and online polling modes.
- `motion_example.py`: current motion status and event watch mode.
- `record_example.py`: local MPEG-TS recording for a fixed duration or until Ctrl+C.
- `voice_example.py`: play an audio file, use the microphone, send a test tone, and guarded siren helper.
- `stream_example.py`: live MPEG-TS and HLS timeshift HTTP server from a dict config.

Each example keeps camera settings and tuning values as small constants near the top of the file. Edit those values directly or replace them with your own config loader.

Run examples:

```powershell
python examples/camera_example.py
python examples/sd_card_example.py
python examples/battery_example.py
python examples/motion_example.py
python examples/record_example.py
python examples/voice_example.py
python examples/stream_example.py
```

`remove_example()`, `format_example()`, `reboot_example()`, and `siren_example()` are guarded. Keep them that way unless you have selected the exact target and intentionally pass the confirmation arguments.

Voice file playback requires `ffmpeg` and `ffprobe` in `PATH`. Microphone input requires `sounddevice` and a working local input device.
