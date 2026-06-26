from __future__ import annotations

import time
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

OUTPUT_DIR = "recordings"
STREAM = "mainStream"
SHORT_RECORD_SECONDS = 30.0


def open_camera() -> Camera:
    return Camera(**SETTINGS)


def duration_record_example(seconds: float = SHORT_RECORD_SECONDS, output: str = OUTPUT_DIR) -> Path:
    with open_camera() as camera:
        path = camera.record(out=output, duration=seconds, stream=STREAM)
        print(f"Saved recording: {path}")
        return path


def manual_record_example(output: str = OUTPUT_DIR) -> Path:
    with open_camera() as camera:
        recorder = camera.record(out=output, stream=STREAM)
        print(f"Recording to {recorder.path}. Press Ctrl+C to stop.")
        try:
            while recorder.running:
                time.sleep(0.25)
        finally:
            path = recorder.stop()
            print(f"Saved recording: {path}")
        return path


if __name__ == "__main__":
    duration_record_example()
