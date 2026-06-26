from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pyneolink import Camera


SETTINGS = {
    "uuid": "ABCDEF0123456789",
    "username": "admin",
    "password": "password",
}

SNAPSHOT_DIR = "snapshots"


def open_camera() -> Camera:
    return Camera(**SETTINGS)


def info_example() -> dict:
    with open_camera() as camera:
        info = camera.info()
        print(info)
        return info


def snapshot_example(output: str = SNAPSHOT_DIR) -> Path:
    with open_camera() as camera:
        path = camera.snapshot(out=output)
        print(f"Saved snapshot: {path}")
        return path


def led_example(value: str | None = None) -> str | None:
    with open_camera() as camera:
        result = camera.led(value)
        if result:
            print(result)
        return result


def reboot_example(*, confirm: bool = False) -> None:
    if not confirm:
        print("Refusing to reboot the camera. Pass confirm=True when you really want this.")
        return
    with open_camera() as camera:
        camera.reboot()
        print("Reboot command sent.")


if __name__ == "__main__":
    info_example()
