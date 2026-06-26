from __future__ import annotations

from pyneolink.core.const import msg


def split_address(address: str) -> tuple[str, int]:
    if ":" in address:
        host, port = address.rsplit(":", 1)
        return host, int(port)
    return address, 9000


class CameraOnlineLease:
    def __init__(self, camera) -> None:
        self.camera = camera
        self.active = False

    def __enter__(self):
        if not self.active:
            self.camera._online_required += 1
            self.active = True
        return self.camera

    def __exit__(self, *exc: object) -> None:
        if self.active:
            self.camera._online_required = max(0, self.camera._online_required - 1)
            self.active = False


def stream_params(stream: str) -> tuple[str, int, int]:
    normalized = stream.strip()
    aliases = {
        "high": "mainStream",
        "main": "mainStream",
        "mainstream": "mainStream",
        "clear": "mainStream",
        "low": "subStream",
        "sub": "subStream",
        "substream": "subStream",
        "fluent": "subStream",
        "extern": "externStream",
        "externstream": "externStream",
    }
    stream_name = aliases.get(normalized.lower(), normalized)
    if stream_name == "mainStream":
        return stream_name, 0, 0
    if stream_name == "subStream":
        return stream_name, 1, 256
    if stream_name == "externStream":
        return stream_name, 0, 1024
    raise ValueError(msg.Error.StreamValue)


def redact_sensitive(value: object) -> None:
    if isinstance(value, dict):
        for key in list(value.keys()):
            if key.lower() in {"secretcode", "bootsecret", "password"}:
                value[key] = "***"
            else:
                redact_sensitive(value[key])
    elif isinstance(value, list):
        for item in value:
            redact_sensitive(item)
