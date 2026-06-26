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

AUDIO_FILE = "alert.mp3"
MICROPHONE_SECONDS = 10.0
TONE_SECONDS = 3.0


def open_camera() -> Camera:
    return Camera(**SETTINGS)


def play_file_example(path: str = AUDIO_FILE) -> None:
    with open_camera() as camera:
        camera.voice().play(path)
        print("Voice file sent.")


def microphone_example(seconds: float | None = MICROPHONE_SECONDS) -> None:
    with open_camera() as camera:
        camera.voice().microphone(
            seconds=seconds,
            on_ready=lambda _config: print("Voice connected; speak now."),
        )
        print("Microphone voice sent.")


def tone_example(frequency: float = 1000.0, seconds: float = TONE_SECONDS) -> None:
    with open_camera() as camera:
        camera.voice().tone(frequency=frequency, seconds=seconds, volume=0.4)
        print("Test tone sent.")


def siren_example(*, confirm: bool = False) -> None:
    if not confirm:
        print("Refusing to trigger the siren. Pass confirm=True when you really want this.")
        return
    with open_camera() as camera:
        camera.voice().siren()
        print("Siren command sent.")


if __name__ == "__main__":
    tone_example()
