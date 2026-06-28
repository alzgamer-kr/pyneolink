from __future__ import annotations

from datetime import datetime
import xml.etree.ElementTree as ET

from pyneolink.core.const import msg


def parse_battery_xml(xml: str | bytes | ET.Element | None) -> dict:
    root = root_element(xml)
    info = first_battery_info(root)
    if info is None:
        return {}

    raw = {child.tag: (child.text or "").strip() for child in info}
    level = int_or_none(raw.get("batteryPercent"))
    charge_status = raw.get("chargeStatus") or ""
    adapter_status = raw.get("adapterStatus") or ""
    charge_type = charge_type_from_adapter(adapter_status)

    return {
        "level_percent": level,
        "is_charging": charge_status.strip().lower() == "charging",
        "charge_type": charge_type,
        "charge_type_label": charge_type_label(charge_type),
        "charge_status": charge_status or None,
        "adapter_status": adapter_status or None,
        "channel_id": int_or_none(raw.get("channelId")),
        "voltage": int_or_none(raw.get("voltage")),
        "current": int_or_none(raw.get("current")),
        "temperature": int_or_none(raw.get("temperature")),
        "low_power": bool_int(raw.get("lowPower")),
        "battery_version": int_or_none(raw.get("batteryVersion")),
        "updated_at": datetime.now().astimezone().isoformat(),
        "raw": raw,
    }


def normalize_mode(mode: str) -> str:
    normalized = mode.strip().lower()
    if normalized not in ("reconnect", "online"):
        raise ValueError(msg.Error.BatteryMode)
    return normalized


def root_element(xml: str | bytes | ET.Element | None) -> ET.Element | None:
    if xml is None:
        return None
    if isinstance(xml, ET.Element):
        return xml
    if isinstance(xml, bytes):
        xml = xml.decode("utf-8", errors="replace")
    if not xml:
        return None
    return ET.fromstring(xml)


def first_battery_info(root: ET.Element | None) -> ET.Element | None:
    if root is None:
        return None
    if root.tag == "BatteryInfo":
        return root
    return root.find(".//BatteryInfo")


def int_or_none(value: str | None) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except ValueError:
        return None


def bool_int(value: str | None) -> bool | None:
    parsed = int_or_none(value)
    if parsed is None:
        return None
    return parsed != 0


def charge_type_from_adapter(adapter_status: str) -> str:
    normalized = adapter_status.strip().lower()
    if not normalized or normalized in ("none", "no", "0", "battery"):
        return "none"
    if "solar" in normalized:
        return "solar_panel"
    if normalized in ("adapter", "poweradapter", "dcpower", "dc", "ac", "mains", "power", "plug", "powercable"):
        return "mains"
    return "unknown"


def charge_type_label(charge_type: str) -> str:
    return {
        "solar_panel": "Solar panel",
        "mains": "Mains",
        "none": "None",
        "unknown": "Unknown",
    }[charge_type]
