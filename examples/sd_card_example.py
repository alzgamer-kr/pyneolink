from __future__ import annotations

import os
from datetime import date

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

from pyneolink import Camera


if load_dotenv:
    load_dotenv()


def open_camera() -> Camera:
    return Camera(
        uuid=os.environ["CAMERA_UID"],
        username=os.environ.get("CAMERA_USERNAME", "admin"),
        password=os.environ["CAMERA_PASSWORD"],
    )


def list_example(target_date: str | None = None) -> list[dict]:
    target_date = target_date or os.environ.get("PYNEOLINK_DATE", date.today().isoformat())
    with open_camera() as camera:
        sd_card = camera.sd_card()
        files = sd_card.list(start=target_date, end=target_date)
        print(f"Found {len(files)} recordings for {target_date}")
        for item in files[-5:]:
            print(f"{item.get('start_time')} - {item.get('end_time')} {item.get('path') or item.get('file_name')}")
        return files


def download_example(target_date: str | None = None, quality: str = "high") -> None:
    target_date = target_date or os.environ.get("PYNEOLINK_DATE", date.today().isoformat())
    quality = os.environ.get("PYNEOLINK_QUALITY", quality)
    output_dir = os.environ.get("PYNEOLINK_DOWNLOAD_DIR", "downloads")
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


def remove_example(target_date: str | None = None, *, confirm: bool = False) -> None:
    if not confirm:
        print("Refusing to remove a recording. Pass confirm=True after choosing the exact file.")
        return
    target_date = target_date or os.environ.get("PYNEOLINK_DATE", date.today().isoformat())
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
