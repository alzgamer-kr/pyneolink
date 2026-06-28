from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .core.const import msg


@dataclass
class CameraConfig:
    """Camera configuration.

    :param name: Human-readable camera name.
    :param username: Camera username.
    :param password: Camera password.
    :param address: Direct address, optionally `host:port`.
    :param uid: Reolink UID for P2P access.
    :param discovery: Discovery mode such as `local`, `remote`, `map`, `relay`,
        or `cellular`.
    :param channel_id: Camera channel id.
    :param stream: Preferred stream selection for config consumers.
    :param cached_address: Previously known address.
    """

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
    """Top-level PyNeolink configuration.

    :param bind: HTTP stream server bind host.
    :param bind_port: HTTP stream server bind port.
    :param cameras: Configured cameras.
    """

    bind: str = "0.0.0.0"
    bind_port: int = 8554
    cameras: list[CameraConfig] | None = None

    def camera(self, name: str | None) -> CameraConfig:
        """Return one camera config by name.

        :param name: Camera name. When omitted, exactly one camera must be
            configured.
        """
        cams = self.cameras or []
        if name is None:
            if len(cams) != 1:
                raise ValueError(msg.Error.SelectOneCamera)
            return cams[0]
        for cam in cams:
            if cam.name == name:
                return cam
        raise ValueError(msg.Error.NoCameraNamed.format(name=name))


def load_config(path: str | Path) -> Config:
    """Load JSON or TOML config from disk.

    :param path: Config file path.
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        data = json.loads(text)
    else:
        data = tomllib.loads(text)
    return config_from_dict(data)


def config_from_dict(data: dict) -> Config:
    """Build `Config` from a plain dict.

    :param data: Parsed config dictionary.
    """
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
    """Convert `Config` to a plain serializable dict.

    :param config: Config object to convert.
    """
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
    """Write config as formatted JSON.

    :param config: Config object to write.
    :param path: Destination file path.
    """
    Path(path).write_text(json.dumps(config_to_dict(config), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
