from __future__ import annotations

from datetime import datetime
from typing import Any
import time
import xml.etree.ElementTree as ET

from .core.bc import MSG_BATTERY, ProtocolError, extension_xml


class Battery:
    def __init__(self, camera) -> None:
        self.camera = camera

    def raw(self, *, mode: str = "reconnect") -> str | None:
        return self._request(mode=mode).xml_text

    def info(self, *, interval: float | None = None, count: int | None = None, mode: str = "reconnect"):
        if interval is None:
            return self.refresh(mode=mode)
        return BatteryInfoUpdates(self, interval=interval, count=count, mode=mode)

    def refresh(self, *, mode: str = "reconnect") -> dict[str, Any]:
        reply = self._request(mode=mode)
        if reply.header.response_code != 200:
            raise ProtocolError(f"Battery info failed with response {reply.header.response_code}")
        return BatteryInfo(parse_battery_xml(reply.xml_root))

    def watch(self, interval: float = 60.0, *, count: int | None = None, mode: str = "reconnect"):
        with BatteryInfoUpdates(self, interval=interval, count=count, mode=mode) as updates:
            yield from updates

    def keepalive(self) -> str:
        return self.camera.keepalive()

    def _request(self, *, mode: str = "reconnect", retries: int = 1):
        mode = _normalize_mode(mode)
        effective_online = mode == "online" or getattr(self.camera, "online_required", False)
        if not effective_online:
            self.camera.close()
        channel_id = self.camera.config.channel_id
        extension = extension_xml(channel_id=channel_id)
        try:
            for attempt in range(retries + 1):
                try:
                    return self.camera.command(MSG_BATTERY, extension=extension)
                except (TimeoutError, EOFError, OSError):
                    if attempt >= retries:
                        raise
                    self.camera.reconnect()
            raise TimeoutError("Battery request failed")
        finally:
            if not effective_online:
                self.camera.close()


class BatteryInfoUpdates:
    def __init__(
        self,
        battery: Battery,
        *,
        interval: float,
        count: int | None = None,
        mode: str = "reconnect",
        keepalive_interval: float = 1.0,
    ) -> None:
        self.battery = battery
        self.interval = max(interval, 0.0)
        self.mode = _normalize_mode(mode)
        self.keepalive_interval = max(keepalive_interval, 0.1)
        self.count = count
        self.seen = 0
        self.closed = False
        self._online_lease = None

    def __enter__(self) -> "BatteryInfoUpdates":
        self._enter_online_mode()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __iter__(self) -> "BatteryInfoUpdates":
        return self

    def __next__(self) -> dict[str, Any]:
        if self.closed or (self.count is not None and self.seen >= self.count):
            raise StopIteration
        self._enter_online_mode()
        if self.seen:
            self._wait()
        self.seen += 1
        return self.battery.refresh(mode=self.mode)

    def close(self) -> None:
        lease = getattr(self, "_online_lease", None)
        if lease is not None:
            lease.__exit__(None, None, None)
            self._online_lease = None
        self.closed = True

    def _enter_online_mode(self) -> None:
        if self.mode != "online" or getattr(self, "_online_lease", None) is not None:
            return
        require_online = getattr(self.battery.camera, "require_online", None)
        if require_online is None:
            return
        self._online_lease = require_online()
        self._online_lease.__enter__()

    def _wait(self) -> None:
        if self.mode == "reconnect" and not getattr(self.battery.camera, "online_required", False):
            time.sleep(self.interval)
            return
        deadline = time.monotonic() + self.interval
        while not self.closed:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            sleep_for = min(remaining, self.keepalive_interval)
            time.sleep(sleep_for)
            if not self.closed:
                try:
                    self.battery.keepalive()
                except (TimeoutError, EOFError, OSError):
                    self.battery.camera.reconnect()


class BatteryInfo(dict):
    def __enter__(self) -> "BatteryInfo":
        return self

    def __exit__(self, *exc: object) -> None:
        pass


def parse_battery_xml(xml: str | bytes | ET.Element | None) -> dict[str, Any]:
    root = _root(xml)
    info = _first_battery_info(root)
    if info is None:
        return {}

    raw = {child.tag: (child.text or "").strip() for child in info}
    level = _int(raw.get("batteryPercent"))
    charge_status = raw.get("chargeStatus") or ""
    adapter_status = raw.get("adapterStatus") or ""
    charge_type = _charge_type(adapter_status)

    return {
        "level_percent": level,
        "is_charging": charge_status.strip().lower() == "charging",
        "charge_type": charge_type,
        "charge_type_label": _charge_type_label(charge_type),
        "charge_status": charge_status or None,
        "adapter_status": adapter_status or None,
        "channel_id": _int(raw.get("channelId")),
        "voltage": _int(raw.get("voltage")),
        "current": _int(raw.get("current")),
        "temperature": _int(raw.get("temperature")),
        "low_power": _bool_int(raw.get("lowPower")),
        "battery_version": _int(raw.get("batteryVersion")),
        "updated_at": datetime.now().astimezone().isoformat(),
        "raw": raw,
    }


def _root(xml: str | bytes | ET.Element | None) -> ET.Element | None:
    if xml is None:
        return None
    if isinstance(xml, ET.Element):
        return xml
    if isinstance(xml, bytes):
        xml = xml.decode("utf-8", errors="replace")
    if not xml:
        return None
    return ET.fromstring(xml)


def _normalize_mode(mode: str) -> str:
    normalized = mode.strip().lower()
    if normalized not in ("reconnect", "online"):
        raise ValueError('mode must be "reconnect" or "online"')
    return normalized


def _first_battery_info(root: ET.Element | None) -> ET.Element | None:
    if root is None:
        return None
    if root.tag == "BatteryInfo":
        return root
    return root.find(".//BatteryInfo")


def _int(value: str | None) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _bool_int(value: str | None) -> bool | None:
    parsed = _int(value)
    if parsed is None:
        return None
    return parsed != 0


def _charge_type(adapter_status: str) -> str:
    normalized = adapter_status.strip().lower()
    if not normalized or normalized in ("none", "no", "0", "battery"):
        return "none"
    if "solar" in normalized:
        return "solar_panel"
    if normalized in ("adapter", "poweradapter", "dcpower", "dc", "ac", "mains", "power", "plug", "powercable"):
        return "mains"
    return "unknown"


def _charge_type_label(charge_type: str) -> str:
    return {
        "solar_panel": "Сонячна панель",
        "mains": "Мережа",
        "none": "Немає",
        "unknown": "Невідомо",
    }[charge_type]
