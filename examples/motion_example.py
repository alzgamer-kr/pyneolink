from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pyneolink import Camera, EVENTS


SETTINGS = {
    "uuid": "ABCDEF0123456789",
    "username": "admin",
    "password": "password",
}

WATCH_SECONDS = 30.0


def open_camera() -> Camera:
    return Camera(**SETTINGS)


def status_example() -> dict:
    with open_camera() as camera:
        status = camera.motion().status()
        print(status)
        return status


def watch_example(duration: float | None = WATCH_SECONDS) -> None:
    with open_camera() as camera:
        with camera.motion().watch(duration=duration) as events:
            for event in events:
                print(f"{event.received_at:%H:%M:%S}: {event}")
                if event == EVENTS.human and event.active:
                    print("human is active")


if __name__ == "__main__":
    status_example()
