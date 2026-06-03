from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pyneolink import StreamServer

try:
    from dotenv import load_dotenv
except ImportError:
    pass
else:
    load_dotenv()


CONFIG_PATH = Path(os.environ.get("PYNEOLINK_CONFIG", "config.json"))
STATE_PATH = os.environ.get("PYNEOLINK_STATE", ".pyneolink_state.json")
HOST = os.environ.get("PYNEOLINK_HOST")
PORT = int(os.environ["PYNEOLINK_PORT"]) if os.environ.get("PYNEOLINK_PORT") else None
DEBUG = os.environ.get("PYNEOLINK_DEBUG", "").lower() in ("1", "true", "yes", "on")
BUFFER_SECONDS = float(os.environ.get("PYNEOLINK_BUFFER_SECONDS", "1.0"))


def load_stream_config() -> dict[str, Any]:
    config = _load_json_config(CONFIG_PATH)
    _apply_server_env(config)
    env_camera = _camera_from_env()
    if env_camera:
        _upsert_camera(config, env_camera)
    return config


def stream_example() -> None:
    StreamServer(
        load_stream_config(),
        host=HOST,
        port=PORT,
        state_path=STATE_PATH,
        debug=DEBUG,
        buffer_seconds=BUFFER_SECONDS,
    ).serve_forever()


def _load_json_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"cameras": []}
    return json.loads(path.read_text(encoding="utf-8"))


def _apply_server_env(config: dict[str, Any]) -> None:
    if os.environ.get("PYNEOLINK_BIND"):
        config["bind"] = os.environ["PYNEOLINK_BIND"]
    if os.environ.get("PYNEOLINK_BIND_PORT"):
        config["bind_port"] = int(os.environ["PYNEOLINK_BIND_PORT"])


def _camera_from_env() -> dict[str, Any] | None:
    uid = _env("PYNEOLINK_CAMERA_UID", "CAMERA_UID")
    address = _env("PYNEOLINK_CAMERA_ADDRESS", "CAMERA_ADDRESS")
    if not (uid or address):
        return None

    camera = {
        "name": _env("PYNEOLINK_CAMERA_NAME", "CAMERA_NAME") or uid or address,
        "uid": uid,
        "address": address,
    }
    optional = {
        "username": _env("PYNEOLINK_CAMERA_USERNAME", "CAMERA_USERNAME"),
        "password": _env("PYNEOLINK_CAMERA_PASSWORD", "CAMERA_PASSWORD"),
        "discovery": _env("PYNEOLINK_CAMERA_DISCOVERY", "CAMERA_DISCOVERY"),
        "stream": _env("PYNEOLINK_CAMERA_STREAM", "CAMERA_STREAM"),
    }
    camera.update({key: value for key, value in optional.items() if value is not None})
    channel_id = _env("PYNEOLINK_CAMERA_CHANNEL_ID", "CAMERA_CHANNEL_ID")
    if channel_id is not None:
        camera["channel_id"] = int(channel_id)
    return {key: value for key, value in camera.items() if value is not None}


def _upsert_camera(config: dict[str, Any], camera: dict[str, Any]) -> None:
    cameras = list(config.get("cameras") or [])
    name = camera["name"]
    for index, item in enumerate(cameras):
        if item.get("name") == name:
            merged = dict(item)
            merged.update(camera)
            cameras[index] = merged
            break
    else:
        cameras.append(camera)
    config["cameras"] = cameras


def _env(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


if __name__ == "__main__":
    stream_example()
