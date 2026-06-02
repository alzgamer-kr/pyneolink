from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


class ConnectionState:
    def __init__(self, path: str | Path = ".pyneolink_state.json") -> None:
        self.path = Path(path)
        self.data = self._load()

    def get_address(self, camera_name: str, *, transport: str | None = None) -> str | None:
        item = self.data.get("cameras", {}).get(camera_name, {})
        if transport is not None and item.get("transport", "tcp") != transport:
            return None
        return item.get("address")

    def update_address(self, camera_name: str, address: str, *, uid: str | None = None, transport: str = "tcp") -> None:
        cameras = self.data.setdefault("cameras", {})
        item = cameras.setdefault(camera_name, {})
        item["address"] = address
        item["transport"] = transport
        if uid:
            item["uid"] = uid
        item["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.save()

    def _load(self) -> dict:
        if not self.path.exists():
            return {"cameras": {}}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"cameras": {}}

    def save(self) -> None:
        self.path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
