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


def open_camera() -> Camera:
    return Camera(**SETTINGS)


def pir_status_example() -> dict:
    with open_camera() as camera:
        status = camera.settings().pir.status()
        print(status)
        return status


def pir_on_example(*, confirm: bool = False) -> dict | None:
    if not confirm:
        print("Refusing to turn PIR on. Pass confirm=True when you really want this.")
        return None
    with open_camera() as camera:
        status = camera.settings().pir.on()
        print(status)
        return status


def pir_off_example(*, confirm: bool = False) -> dict | None:
    if not confirm:
        print("Refusing to turn PIR off. Pass confirm=True when you really want this.")
        return None
    with open_camera() as camera:
        status = camera.settings().pir.off()
        print(status)
        return status


def ir_status_example() -> dict:
    with open_camera() as camera:
        status = camera.settings().ir.status()
        print(status)
        return status


def ir_on_example(*, confirm: bool = False) -> dict | None:
    if not confirm:
        print("Refusing to turn IR on. Pass confirm=True when you really want this.")
        return None
    with open_camera() as camera:
        status = camera.settings().ir.on()
        print(status)
        return status


def ir_off_example(*, confirm: bool = False) -> dict | None:
    if not confirm:
        print("Refusing to turn IR off. Pass confirm=True when you really want this.")
        return None
    with open_camera() as camera:
        status = camera.settings().ir.off()
        print(status)
        return status


def ir_auto_example(*, confirm: bool = False) -> dict | None:
    if not confirm:
        print("Refusing to set IR auto mode. Pass confirm=True when you really want this.")
        return None
    with open_camera() as camera:
        status = camera.settings().ir.auto()
        print(status)
        return status


if __name__ == "__main__":
    pir_status_example()
