# Examples

Small library-use examples for PyNeolink.

- [sd_card_example.py](sd_card_example.py) shows SD-card `list`, `download`, `remove`, and `format` calls.
- [battery_example.py](battery_example.py) shows one-shot battery status plus `reconnect` and `online` polling.
- [stream_example.py](stream_example.py) starts a live stream HTTP server from a dict config.

Live stream development:

```powershell
python examples/stream_example.py
```

`stream_example.py` reads `config.json`, builds an in-memory dict, and passes it to `pyneolink.StreamServer`.

Set camera credentials in `.env` or environment variables before running examples:

```powershell
$env:CAMERA_UID="ABCDEF0123456789"
$env:CAMERA_USERNAME="admin"
$env:CAMERA_PASSWORD="password"
```

`remove_example()` and `format_example()` are guarded and should not be enabled until the target file or disk action is explicitly confirmed.
