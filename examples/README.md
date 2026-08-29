# Examples

Small library-use examples for PyNeolink.

- `camera_example.py`: camera info, snapshot, LED read/set, and guarded reboot helper.
- `sd_card_example.py`: list recordings as `SDFile` objects, download through `file.download()`, serve preview playback, and guarded remove/format helpers.
- `battery_example.py`: one-shot battery info plus reconnect and online polling modes.
- `battery_runtime_test.py`: long motion/serve battery runtime checks with CSV and optional HTML output.
- `stream_session_probe.py`: live stream session diagnostics without battery polling.
- `dual_camera_session_probe.py`: two-camera live stream session diagnostics with a staggered start.
- `motion_example.py`: current motion status and event watch mode.
- `record_example.py`: local MPEG-TS recording for a fixed duration or until Ctrl+C.
- `voice_example.py`: play an audio file, use the microphone, send a test tone, and guarded siren helper.
- `settings_example.py`: PIR and IR status plus guarded setting helpers.
- `ptz_example.py`: list stored PTZ presets and recall one by ID.
- `stream_example.py`: live MPEG-TS and HLS timeshift HTTP server from a dict config.

Each example keeps camera settings and tuning values as small constants near the top of the file. Edit those values directly or replace them with your own config loader.

Run examples:

```powershell
python examples/camera_example.py
python examples/sd_card_example.py
python examples/battery_example.py
python examples/battery_runtime_test.py --config config.json --camera "Home-Front" --mode motion
python examples/battery_runtime_test.py --config config.json --camera "Home-Front" --mode serve --stream high
python examples/battery_runtime_test.py --config config.json --camera "Home-Front" --mode serve --stream high --single-connection-serve
python examples/battery_runtime_test.py --config config.json --camera "Home-Front" --mode serve --stream high --no-serve-internal-client
python examples/stream_session_probe.py --config config.json --camera "Home-Front" --stream high --duration 600 --sample-interval 30
python examples/dual_camera_session_probe.py --config config.json --camera "Home-Front" --camera "Home-Back" --stream high --duration 3600 --stagger-seconds 600
python examples/motion_example.py
python examples/record_example.py
python examples/voice_example.py
python examples/settings_example.py
python examples/ptz_example.py
python examples/stream_example.py
```

`remove_example()`, `format_example()`, `reboot_example()`, `siren_example()`, `pir_on_example()`, `pir_off_example()`, `ir_on_example()`, `ir_off_example()`, and `ir_auto_example()` are guarded. Keep them that way unless you have selected the exact target and intentionally pass the confirmation arguments.

Voice file playback requires `ffmpeg` and `ffprobe` in `PATH`. Microphone input requires `sounddevice` and a working local input device.

`stream_session_probe.py` and `dual_camera_session_probe.py` treat the stream
as stalled when no media payload arrives for `--stall-window` seconds.

`battery_runtime_test.py --mode serve` opens the live stream through a separate
`Camera` connection by default. Use `--single-connection-serve` to validate the
SDK path where one `Camera` session handles both stream payloads and battery
polling through the internal dispatcher. Use `--no-serve-internal-client` when
you want the example to start only the HTTP server and you will open the stream
URL yourself.
