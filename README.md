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
from pyneolink import Camera

camera = Camera(uuid="ABCDEF0123456789", password="password")
sd_card = camera.sd_card()

files = sd_card.list(start="2026-06-01", end="2026-06-01")
motion_files = sd_card.filter(files, name=".mp4")
saved_path = sd_card.download(motion_files[-1], "downloads/", quality="high")
camera.close()
```

`list()` sorts recordings by time ascending by default, so `files[-1]` is the newest recording. Use `sort="desc"` for newest first or `sort=None` to keep the camera response order.

When the camera returns a Reolink BCMedia stream for an `.mp4` recording, `download()` converts it to a playable MP4 with `ffmpeg`. If conversion fails, the raw stream is kept as `*.mp4.bcmedia` for debugging.
Use `quality="high"`/`quality="low"` or `stream_type="mainStream"`/`stream_type="subStream"` to choose the recording stream.

`remove()` and `format()` are intentionally guarded. `format()` requires both `confirm=True` and `confirmation_text="FORMAT SD CARD"`.

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
