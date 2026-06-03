from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

from pyneolink import Camera


if load_dotenv:
    load_dotenv()


INTERVAL_SECONDS = float(os.environ.get("PYNEOLINK_BATTERY_INTERVAL", "60"))
COUNT = int(os.environ["PYNEOLINK_BATTERY_COUNT"]) if os.environ.get("PYNEOLINK_BATTERY_COUNT") else None


def open_camera() -> Camera:
    return Camera(
        uuid=os.environ["CAMERA_UID"],
        username=os.environ.get("CAMERA_USERNAME", "admin"),
        password=os.environ["CAMERA_PASSWORD"],
    )


def battery_info_example(mode: str = "reconnect") -> dict:
    with open_camera() as camera:
        battery = camera.battery()
        with battery.info(mode=mode) as info:
            print_battery_info(info)
            return info


def reconnect_mode_example(interval: float = INTERVAL_SECONDS, count: int | None = COUNT) -> None:
    watch_example(interval=interval, count=count, mode="reconnect")


def online_mode_example(interval: float = INTERVAL_SECONDS, count: int | None = COUNT) -> None:
    watch_example(interval=interval, count=count, mode="online")


def watch_example(interval: float = INTERVAL_SECONDS, count: int | None = COUNT, mode: str = "reconnect") -> None:
    with open_camera() as camera:
        battery = camera.battery()
        with battery.info(interval=interval, count=count, mode=mode) as updates:
            for info in updates:
                print_battery_info(info)


def print_battery_info(info: dict) -> None:
    print(
        f"{info.get('updated_at')} "
        f"level_percent={info.get('level_percent')} "
        f"is_charging={info.get('is_charging')} "
        f"adapter_status={info.get('adapter_status')} "
        f"charge_type={info.get('charge_type')}"
    )


if __name__ == "__main__":
    reconnect_mode_example()
