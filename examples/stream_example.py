from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pyneolink import StreamServer


STREAM_CONFIG = {
    "bind": "0.0.0.0",
    "bind_port": 8554,
    "cameras": [
        {
            "name": "Home-Front",
            "username": "admin",
            "password": "password",
            "uid": "ABCDEF0123456789",
            "discovery": "relay",
        },
        {
            "name": "Dorway",
            "username": "admin",
            "password": "password",
            "uid": "ZYXWVU9876543210",
            "discovery": "relay",
        },
    ],
}

STATE_PATH = ".pyneolink_state.json"
DEBUG = False
BUFFER_SECONDS = 1.0
HLS_BUFFER_MB = 100
HLS_SEGMENT_SECONDS = 2.0


def stream_example() -> None:
    StreamServer(
        STREAM_CONFIG,
        state_path=STATE_PATH,
        debug=DEBUG,
        buffer_seconds=BUFFER_SECONDS,
        hls_buffer_mb=HLS_BUFFER_MB,
        hls_segment_seconds=HLS_SEGMENT_SECONDS,
    ).serve_forever()


if __name__ == "__main__":
    stream_example()
