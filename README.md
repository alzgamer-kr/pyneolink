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
python main.py --info --camera "Scherbaka 41 - Front"
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
