"""Connection cache backed by per-camera SQLite, with legacy JSON import."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.dialects.sqlite import insert

from .storage import DEFAULT_STORAGE_DIR, STATE, CameraStore, camera_database


class ConnectionState:
    """SQLite connection cache. Legacy .json paths import into a sibling .db."""

    def __init__(self, path: str | Path = DEFAULT_STORAGE_DIR) -> None:
        """Create a cache with serialized access to its in-memory bookkeeping."""
        self.path = Path(path).expanduser()
        self._legacy = self.path if self.path.suffix.lower() == ".json" else Path(".pyneolink_state.json")
        self._stores: dict[str, CameraStore] = {}
        self.data: dict = {"cameras": {}}
        self._dirty: set[str] = set()
        self._lock = threading.RLock()

    def _store(self, name: str) -> CameraStore:
        if name in self._stores:
            return self._stores[name]
        suffix = self.path.suffix.lower()
        explicit = suffix in (".json", ".db", ".sqlite", ".sqlite3")
        database = self.path.with_suffix(".db") if suffix == ".json" else self.path
        if not explicit:
            database = camera_database(name, self.path)
        store = CameraStore(database)
        if not explicit:
            store.bind_camera(name)
        key = f"connection:{name}"
        if self._legacy.is_file() and store.get(key) is None:
            try:
                legacy = json.loads(self._legacy.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                legacy = {}
            cameras = legacy.get("cameras", {}) if isinstance(legacy, dict) else {}
            item = cameras.get(name) if isinstance(cameras, dict) else None
            if isinstance(item, dict):
                with store.transaction() as connection:
                    connection.execute(insert(STATE).values(key=key, value=item).on_conflict_do_nothing())
        self._stores[name] = store
        return store

    def get_address(self, camera_name: str, *, transport: str | None = None) -> str | None:
        """Read the current endpoint, including updates from another process."""
        with self._lock:
            item = self._store(camera_name).get(f"connection:{camera_name}", {})
            self.data["cameras"][camera_name] = item
            if transport is not None and item.get("transport", "tcp") != transport:
                return None
            return item.get("address")

    def update_address(self, camera_name: str, address: str, *, uid: str | None = None, transport: str = "tcp") -> None:
        """Commit one camera's last working endpoint atomically."""
        with self._lock:
            item = self._store(camera_name).get(f"connection:{camera_name}", {})
            item.update(address=address, transport=transport, updated_at=datetime.now(timezone.utc).isoformat())
            if uid:
                item["uid"] = uid
            self.data["cameras"][camera_name] = item
            self._dirty.add(camera_name)
            self.save()

    def save(self) -> None:
        """Save only modified cameras, avoiding stale cross-camera overwrites."""
        with self._lock:
            for name in self._dirty or self.data["cameras"].keys():
                self._store(name).put(f"connection:{name}", self.data["cameras"][name])
            self._dirty.clear()
