# PyNeolink

PyNeolink is a Python client for Reolink/Neolink-style Baichuan cameras. It focuses on UID/P2P access, camera information, SD-card recordings, live viewing, snapshots, local recording, motion events, battery status, voice/talk, and siren control.

Version: `0.3.0` alpha.

This project was developed with OpenAI Codex as an AI-assisted implementation effort. It is a Python port inspired by and based on protocol knowledge from the Rust `neolink` project, especially `QuantumEntangledAndy/neolink` and `surfzoid/neolink`. The reverse-engineering foundation belongs to the Neolink contributors. The goal is not to replace Neolink, but to make a working Python implementation available for people who want to study, adapt, or extend this protocol without working in Rust.

Neolink and Reolink protocol support are reverse engineered. This project is not affiliated with Reolink.

## Status

PyNeolink is experimental alpha software. It works against a limited set of real cameras, but Reolink firmware and model behavior can differ. APIs may change before `1.0.0`.

## What Works

- JSON camera configuration with `address` or `uid`
- Reolink UID/P2P lookup, registration, UDP relay, and local UDP connection
- Baichuan login and command framing
- BC XOR encryption and AES-CFB support through `cryptography`
- Camera information, UID, LED command, and reboot command
- Battery status, including reconnect and online polling modes
- SD-card recording list with pagination and time sorting
- SD-card recording download with high/low quality selection
- Snapshot download to bytes or JPEG file
- Local MPEG-TS recording from the live stream
- Live HTTP MPEG-TS viewing with H264/H265 video and AAC audio
- HLS timeshift viewing with an in-memory sliding buffer
- Motion status and motion event watch mode
- Two-way voice/talk from microphone, audio file, or generated test tone
- Camera siren trigger
- PIR status and PIR on/off settings
- IR light status and IR on/off/auto settings

## Current Limits

- This is reverse engineered and tested against a small number of real cameras, so behavior may differ between models and firmware versions.
- Voice file playback uses `ffmpeg`/`ffprobe` for format validation and conversion. Install FFmpeg and make sure both commands are available in `PATH`.
- Microphone voice input needs the Python `sounddevice` package and a working local input device.
- Local stream recording writes MPEG-TS (`.ts`) files, not MP4.
- SD-card `remove()` and `format()` exist, but are intentionally guarded.
- PTZ, image settings, alarm schedules, floodlight settings, and Web UI are not implemented yet.

## Install

From GitHub, before PyPI publication:

```powershell
python -m pip install git+https://github.com/alzgamer-kr/pyneolink.git
```

From PyPI, after publication:

```powershell
python -m pip install pyneolink
```

For microphone voice input:

```powershell
python -m pip install "pyneolink[voice]"
```

For local development from a checkout:

```powershell
python -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
```

The `cryptography` package is required for AES-encrypted cameras. It is installed automatically when installing the package.

## FFmpeg

FFmpeg is a system dependency for audio-file voice playback and some media conversion paths. PyNeolink expects both `ffmpeg` and `ffprobe` to be available in `PATH`.

Official FFmpeg download page:

- Windows: https://ffmpeg.org/download.html
- Linux: https://ffmpeg.org/download.html
- macOS: https://ffmpeg.org/download.html

On Linux and macOS, using the OS package manager is usually the simplest path when available. On Windows, install one of the builds linked from the official FFmpeg download page and add its `bin` directory to `PATH`.

## Configuration

Create a local `config.json`:

```json
{
  "bind": "0.0.0.0",
  "bind_port": 8554,
  "cameras": [
    {
      "name": "Home-Front",
      "username": "admin",
      "password": "password",
      "uid": "ABCDEF0123456789",
      "discovery": "relay"
    }
  ]
}
```

`config.json` is ignored by Git because it can contain camera credentials.

## CLI

Camera info:

```powershell
python pyneolink/cli.py info --camera "Home-Front" --config config.json
python pyneolink/cli.py --info --camera "Home-Front"
```

Battery:

```powershell
python pyneolink/cli.py battery --camera "Home-Front"
python pyneolink/cli.py battery --camera "Home-Front" --watch --interval 60
python pyneolink/cli.py battery --camera "Home-Front" --watch --interval 60 --mode online
```

Snapshots and local recording:

```powershell
python pyneolink/cli.py snapshot --camera "Home-Front" --out snapshots/
python pyneolink/cli.py record --camera "Home-Front" --out recordings/ --duration 30 --quality high
python pyneolink/cli.py record --camera "Home-Front" --out recordings/live.ts --quality low
```

Motion:

```powershell
python pyneolink/cli.py motion --camera "Home-Front"
python pyneolink/cli.py motion --camera "Home-Front" --watch --duration 30
```

PIR:

```powershell
python pyneolink/cli.py pir --config config.json --camera "Home-Front" status
python pyneolink/cli.py pir --config config.json --camera "Home-Front" on
python pyneolink/cli.py pir --config config.json --camera "Home-Front" off
```

IR light:

```powershell
python pyneolink/cli.py ir --config config.json --camera "Home-Front" status
python pyneolink/cli.py ir --config config.json --camera "Home-Front" on
python pyneolink/cli.py ir --config config.json --camera "Home-Front" off
python pyneolink/cli.py ir --config config.json --camera "Home-Front" auto
python pyneolink/cli.py led --config config.json --camera "Home-Front" auto
```

Voice and siren:

```powershell
python pyneolink/cli.py voice --camera "Home-Front" --file alert.mp3
python pyneolink/cli.py voice --camera "Home-Front" --microphone --seconds 10
python pyneolink/cli.py voice --camera "Home-Front" --tone 1000 --seconds 3
python pyneolink/cli.py voice --camera "Home-Front" --siren
```

Live view server:

```powershell
python pyneolink/cli.py serve --config config.json
```

Open direct MPEG-TS in VLC/ffplay:

```text
http://127.0.0.1:8554/Home-Front/high
http://127.0.0.1:8554/Home-Front/low
```

Open HLS timeshift in VLC/ffplay:

```text
http://127.0.0.1:8554/Home-Front/high/hls.m3u8
http://127.0.0.1:8554/Home-Front/low/hls.m3u8
```

HLS keeps a sliding in-memory buffer. By default it stores up to 100 MB and cuts segments around 2 seconds:

```powershell
python pyneolink/cli.py serve --config config.json --hls-buffer-mb 100 --hls-segment-seconds 2
```

When binding to `0.0.0.0`, do not open `0.0.0.0` in VLC. Use `127.0.0.1` on the same PC or the PC's LAN IP from another device.

## Library Use

Camera information:

```python
from pyneolink import Camera

with Camera(uuid="ABCDEF0123456789", username="admin", password="password") as camera:
    info = camera.info()
    print(info)
```

Battery:

```python
from pyneolink import Camera

with Camera(uuid="ABCDEF0123456789", username="admin", password="password") as camera:
    battery = camera.battery()

    with battery.info() as info:
        print(info["level_percent"], info["is_charging"], info["adapter_status"])

    with battery.info(interval=60, mode="reconnect", count=3) as updates:
        for update in updates:
            print(update["level_percent"], update["adapter_status"])
```

SD-card download:

```python
from pyneolink import Camera

with Camera(uuid="ABCDEF0123456789", username="admin", password="password") as camera:
    sd = camera.sd_card()
    files = sd.list(start="2026-06-03", end="2026-06-03")
    videos = sd.filter(files, name=".mp4")
    if videos:
        sd.download(videos[-1], "downloads", quality="high", progress=True)
```

Motion:

```python
from pyneolink import Camera, EVENTS

with Camera(uuid="ABCDEF0123456789", username="admin", password="password") as camera:
    motion = camera.motion()
    print(motion.status())

    with motion.watch(duration=30) as events:
        for event in events:
            if event == EVENTS.human and event.active:
                print("human detected")
```

Snapshot and local recording:

```python
from pyneolink import Camera

with Camera(uuid="ABCDEF0123456789", username="admin", password="password") as camera:
    camera.snapshot(out="snapshots")
    camera.record(out="recordings", duration=30, stream="mainStream")
```

Voice and siren:

```python
from pyneolink import Camera

with Camera(uuid="ABCDEF0123456789", username="admin", password="password") as camera:
    voice = camera.voice()
    voice.play("alert.mp3")
    voice.siren()
```

Settings and PIR:

```python
from pyneolink import Camera

with Camera(uuid="ABCDEF0123456789", username="admin", password="password") as camera:
    settings = camera.settings()
    print(settings.pir.status())
    settings.pir.on()
    settings.pir.off()
    print(settings.ir.status())
    settings.ir.on()
    settings.ir.off()
    settings.ir.auto()
```

Live stream server from a dict:

```python
from pyneolink import StreamServer

config = {
    "bind": "0.0.0.0",
    "bind_port": 8554,
    "cameras": [
        {
            "name": "Home-Front",
            "username": "admin",
            "password": "password",
            "uid": "ABCDEF0123456789",
            "discovery": "relay",
        }
    ],
}

StreamServer(config, buffer_seconds=1.5, hls_buffer_mb=100, hls_segment_seconds=2).serve_forever()
```

## Examples

See the `examples/` directory:

- `camera_example.py`: info, snapshot, LED, and guarded reboot helpers
- `sd_card_example.py`: list, filter, download, remove, and guarded format calls
- `battery_example.py`: one-shot battery status plus reconnect/online polling
- `motion_example.py`: motion status and watch mode
- `record_example.py`: duration and manual local stream recording
- `voice_example.py`: file, microphone, tone, and siren helpers
- `settings_example.py`: PIR and IR status plus guarded setting helpers
- `stream_example.py`: live MPEG-TS and HLS timeshift server from a dict config

Each example keeps camera settings as a small local dict near the top of the file. Edit those values directly or replace the dict with your own configuration loader.

## Internals

See `docs/` for a sorted internal documentation set that explains the core files, connection flow, login, encryption, Baichuan messages, UDP/P2P transport, SD-card downloads, media streaming, motion, voice, and camera controls.

## Credits

PyNeolink exists because of the reverse-engineering work done by the Neolink community. In particular, the Rust `neolink` implementations and documentation were used as the protocol reference for this Python port.

This Python implementation was developed with OpenAI Codex. Human testing against real cameras guided the implementation and bug fixes.

## Reference

The Rust `neolink/` checkout can be kept locally as a protocol reference, but it is ignored by Git in this workspace. The Python code here is intended to be understandable and hackable for people who want to experiment with Reolink cameras from Python.

## Tested Cameras

- Reolink Argus Eco
