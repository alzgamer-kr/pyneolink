from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


class ConnectionState:
    """Small JSON cache for last working camera addresses."""

    def __init__(self, path: str | Path = ".pyneolink_state.json") -> None:
        """Create a JSON connection-state cache.

        :param path: State file path.
        """
        self.path = Path(path)
        self.data = self._load()
        self._dirty: set[str] = set()

    def get_address(self, camera_name: str, *, transport: str | None = None) -> str | None:
        """Return a cached camera address.

        :param camera_name: Camera name used as the cache key.
        :param transport: Optional transport filter, for example `tcp`.
        """
        item = self.data.get("cameras", {}).get(camera_name, {})
        if transport is not None and item.get("transport", "tcp") != transport:
            return None
        return item.get("address")

    def update_address(self, camera_name: str, address: str, *, uid: str | None = None, transport: str = "tcp") -> None:
        """Store the last working camera address.

        :param camera_name: Camera name used as the cache key.
        :param address: Address to store, usually `host:port`.
        :param uid: Optional camera UID.
        :param transport: Transport label such as `tcp`, `udp-local`, or
            `udp-relay`.
        """
        cameras = self.data.setdefault("cameras", {})
        item = cameras.setdefault(camera_name, {})
        item["address"] = address
        item["transport"] = transport
        if uid:
            item["uid"] = uid
        item["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._dirty.add(camera_name)
        self.save()

    def _load(self) -> dict:
        if not self.path.exists():
            return {"cameras": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"cameras": {}}
        if not isinstance(data, dict):
            return {"cameras": {}}
        if not isinstance(data.get("cameras"), dict):
            data["cameras"] = {}
        return data

    def save(self) -> None:
        """Merge changed cameras into the on-disk state and write atomically."""
        current = self._load()
        own = self.data.get("cameras", {})
        if isinstance(own, dict):
            for name in self._dirty or own.keys():
                if name in own:
                    current["cameras"][name] = own[name]
        for key, value in self.data.items():
            if key != "cameras":
                current[key] = value
        self._write_atomic(current)
        self.data = current
        self._dirty = set()

    def _write_atomic(self, data: dict) -> None:
        text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(self.path.parent), prefix=f".{self.path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp_name, self.path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
