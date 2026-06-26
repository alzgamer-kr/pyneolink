from __future__ import annotations

import sys
from datetime import date
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

TARGET_DATE = date.today().isoformat()
DOWNLOAD_DIR = "downloads"
DOWNLOAD_QUALITY = "high"


def open_camera() -> Camera:
    return Camera(**SETTINGS)


def list_example(target_date: str = TARGET_DATE) -> list[dict]:
    with open_camera() as camera:
        sd_card = camera.sd_card()
        files = sd_card.list(start=target_date, end=target_date)
        print(f"Found {len(files)} recordings for {target_date}")
        for item in files[-5:]:
            print(f"{item.get('start_time')} - {item.get('end_time')} {item.get('path') or item.get('file_name')}")
        return files


def download_example(target_date: str = TARGET_DATE, quality: str = DOWNLOAD_QUALITY, output_dir: str = DOWNLOAD_DIR) -> None:
    with open_camera() as camera:
        sd_card = camera.sd_card()
        files = sd_card.list(start=target_date, end=target_date)
        videos = sd_card.filter(files, name=".mp4")
        if not videos:
            print(f"No MP4 recordings found for {target_date}")
            return
        selected = videos[-1]
        print(f"Downloading: {selected.get('path') or selected.get('file_name')}")
        saved_path = sd_card.download(selected, output_dir, quality=quality, progress=True)
        print(f"Saved: {saved_path}")


def remove_example(target_date: str = TARGET_DATE, *, confirm: bool = False) -> None:
    if not confirm:
        print("Refusing to remove a recording. Pass confirm=True after choosing the exact file.")
        return
    with open_camera() as camera:
        sd_card = camera.sd_card()
        files = sd_card.list(start=target_date, end=target_date)
        videos = sd_card.filter(files, name=".mp4")
        if not videos:
            print(f"No MP4 recordings found for {target_date}")
            return
        selected = videos[-1]
        print(f"Removing: {selected.get('path') or selected.get('file_name')}")
        sd_card.remove(selected, confirm=True)


def format_example(*, confirm: bool = False, confirmation_text: str = "") -> None:
    if not confirm:
        print('Refusing to format the SD card. Pass confirm=True and confirmation_text="FORMAT SD CARD".')
        return
    with open_camera() as camera:
        sd_card = camera.sd_card()
        sd_card.format(confirm=True, confirmation_text=confirmation_text)
        print("SD card format command sent.")


if __name__ == "__main__":
    download_example()
