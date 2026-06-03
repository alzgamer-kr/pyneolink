# PyNeolink

Python port of the Neolink/Reolink Baichuan client focused on UID/P2P camera access.

This project follows the behavior of `surfzoid/neolink` / `QuantumEntangledAndy/neolink`:

- JSON camera configuration with `address` or `uid`
- Baichuan TCP packet framing
- Reolink UID/P2P lookup, register, and relay handshake
- legacy-to-modern login flow
- BC XOR encryption and optional AES-CFB when `cryptography` is installed
- local UDP UID discovery
- camera info and read-only status commands
- media packet parsing for H264/H265/AAC/ADPCM payloads

The Rust `neolink/` checkout is kept in this workspace as the reference implementation.

## Quick Start

```powershell
python -m pip install -r requirements.txt
python pyneolink/cli.py --info --camera "Scherbaka 41 - Front"
```

With Docker:

```powershell
docker build -t pyneolink .
docker run --rm --network host `
  -v "${PWD}\config.json:/app/config.json:ro" `
  -v "${PWD}\.pyneolink_state.json:/app/.pyneolink_state.json" `
  pyneolink --info --camera "Scherbaka 41 - Front"
```

For connection diagnostics, add `--debug` to the same command. The normal `--info` output redacts sensitive camera fields.

Library use:

```python
from pyneolink import Camera

camera = Camera(uuid="ABCDEF0123456789", password="password")
info = camera.info()
camera.close()
```

The public API lives at the package root. Low-level protocol, crypto, discovery, relay transport, state, media, and XML helpers live in `pyneolink.core`.

SD-card access:

```python
from examples.sd_card_example import download_example

download_example()
```

See [examples/sd_card_example.py](examples/sd_card_example.py) for:

- `list_example()`
- `download_example()`
- `remove_example()`
- `format_example()`

`list()` sorts recordings by time ascending by default, so `files[-1]` is the newest recording. Use `sort="desc"` for newest first or `sort=None` to keep the camera response order.

When the camera returns a Reolink BCMedia stream for an `.mp4` recording, `download()` converts it to a playable MP4 with `ffmpeg`. If conversion fails, the raw stream is kept as `*.mp4.bcmedia` for debugging.
Use `quality="high"`/`quality="low"` or `stream_type="mainStream"`/`stream_type="subStream"` to choose the recording stream.

`remove()` and `format()` are intentionally guarded. `format()` requires both `confirm=True` and `confirmation_text="FORMAT SD CARD"`.

Battery status:

```python
from pyneolink import Camera

with Camera(uuid="ABCDEF0123456789", password="password") as camera:
    battery = camera.battery()

    with battery.info() as info:
        print(info["level_percent"], info["is_charging"], info["adapter_status"])

    with battery.info(interval=60, count=3, mode="reconnect") as updates:
        for update in updates:
            print(update["level_percent"], update["adapter_status"])

    with battery.info(interval=60, mode="online") as updates:
        for update in updates:
            print(update["level_percent"], update["adapter_status"])
```

From CLI:

```powershell
python pyneolink/cli.py battery --camera "Scherbaka 41 - Front"
python pyneolink/cli.py battery --camera "Scherbaka 41 - Front" --watch --interval 60
python pyneolink/cli.py battery --camera "Scherbaka 41 - Front" --watch --interval 60 --mode online
python pyneolink/cli.py battery --camera "Scherbaka 41 - Front" --raw
```

Battery polling defaults to `mode="reconnect"` to avoid keeping battery cameras awake. Use `mode="online"` only when frequent updates are more important than power saving, or when another component already keeps the camera online.

See [examples/battery_example.py](examples/battery_example.py) for `battery_info_example()`, `reconnect_mode_example()`, and `online_mode_example()`.

Live view:

```powershell
python pyneolink/cli.py serve --config config.json
python examples/stream_example.py
```

The server may bind to `0.0.0.0`, but clients should not open `0.0.0.0` directly. Use the printed URL, `127.0.0.1` on the same PC, or the PC's LAN IP from another device.

Open the stream in VLC/ffplay with a URL shaped like:

```text
http://127.0.0.1:8554/Scherbaka%2041%20-%20Front/high
http://127.0.0.1:8554/Scherbaka%2041%20-%20Front/low
```

The endpoint muxes H264/H265 video and AAC audio into MPEG-TS for VLC/ffplay. ADPCM audio is currently ignored because it needs a transcoding step. Short camera stalls are bridged with MPEG-TS null packets so players are less likely to close the stream while waiting for the camera to resume. This is a lightweight first step toward Neolink-style viewing; RTSP/HLS wrapping can be added on top later.

Library use:

```python
from pyneolink import StreamServer

config = {
    "bind": "0.0.0.0",
    "bind_port": 8554,
    "cameras": [
        {
            "name": "Scherbaka 41 - Front",
            "username": "admin",
            "password": "password",
            "uid": "ABCDEF0123456789",
            "discovery": "relay",
        }
    ],
}

server = StreamServer(config, debug=True, buffer_seconds=1.5)
server.serve_forever()
```

`examples/stream_example.py` is a small development runner for live streams. It parses `config.json` into a dict and passes that dict to `StreamServer`. You can override values with environment variables such as `PYNEOLINK_CONFIG`, `PYNEOLINK_HOST`, `PYNEOLINK_PORT`, `PYNEOLINK_DEBUG`, `PYNEOLINK_BUFFER_SECONDS`, `CAMERA_NAME`, `CAMERA_UID`, `CAMERA_USERNAME`, and `CAMERA_PASSWORD`.

Example `config.json`:

```json
{
  "bind": "0.0.0.0",
  "bind_port": 8554,
  "cameras": [
    {
      "name": "Scherbaka 41 - Front",
      "username": "admin",
      "password": "password",
      "uid": "ABCDEF0123456789",
      "discovery": "relay"
    }
  ]
}
```

## Notes

Neolink is reverse engineered and not affiliated with Reolink. Current SD-card work should stay read-only: listing and downloading files only. Do not format or write to the SD card from this project.
