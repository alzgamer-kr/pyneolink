from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CameraConfig:
    name: str
    username: str = "admin"
    password: str = "123456"
    address: str | None = None
    uid: str | None = None
    discovery: str = "relay"
    channel_id: int = 0
    stream: str = "both"
    cached_address: str | None = None


@dataclass
class Config:
    bind: str = "0.0.0.0"
    bind_port: int = 8554
    cameras: list[CameraConfig] | None = None

    def camera(self, name: str | None) -> CameraConfig:
        cams = self.cameras or []
        if name is None:
            if len(cams) != 1:
                raise ValueError("Specify --camera when the config has zero or multiple cameras")
            return cams[0]
        for cam in cams:
            if cam.name == name:
                return cam
        raise ValueError(f"No camera named {name!r}")


def load_config(path: str | Path) -> Config:
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        data = json.loads(text)
    else:
        data = tomllib.loads(text)
    cameras = [
        CameraConfig(
            name=item["name"],
            username=item.get("username", "admin"),
            password=item.get("password", "123456"),
            address=item.get("address"),
            uid=item.get("uid"),
            discovery=item.get("discovery", "relay"),
            channel_id=int(item.get("channel_id", 0)),
            stream=item.get("stream", "both"),
            cached_address=item.get("cached_address"),
        )
        for item in data.get("cameras", [])
    ]
    return Config(data.get("bind", "0.0.0.0"), int(data.get("bind_port", 8554)), cameras)


def config_to_dict(config: Config) -> dict:
    return {
        "bind": config.bind,
        "bind_port": config.bind_port,
        "cameras": [
            {
                key: value
                for key, value in {
                    "name": camera.name,
                    "username": camera.username,
                    "password": camera.password,
                    "address": camera.address,
                    "uid": camera.uid,
                    "discovery": camera.discovery,
                    "channel_id": camera.channel_id,
                    "stream": camera.stream,
                    "cached_address": camera.cached_address,
                }.items()
                if value is not None
            }
            for camera in (config.cameras or [])
        ],
    }


def write_json_config(config: Config, path: str | Path) -> None:
    Path(path).write_text(json.dumps(config_to_dict(config), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
