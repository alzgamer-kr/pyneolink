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
PREVIEW_CACHE_DIR = ".tmp/pyneolink-preview-cache"


def open_camera() -> Camera:
    """Create a camera instance for the examples."""
    return Camera(**SETTINGS)


def list_example(target_date: str = TARGET_DATE) -> list:
    """List SD-card recordings as `SDFile` objects."""
    with open_camera() as camera:
        sd_card = camera.sd_card()
        files = sd_card.files(start=target_date, end=target_date)
        print(f"Found {len(files)} recordings for {target_date}")
        for file in files[-5:]:
            info = file.info()
            print(f"{info.get('start_time')} - {info.get('end_time')} {info.get('path') or info.get('file_name')}")
        return files


def files_iterator_example(target_date: str = TARGET_DATE) -> None:
    """Iterate over MP4 recordings and use `SDFile.info()` for each item."""
    with open_camera() as camera:
        sd_card = camera.sd_card()
        for video in sd_card.files(start=target_date, end=target_date, name=".mp4"):
            info = video.info()
            print(f"{info.get('start_time')} {info.get('file_name')} {info.get('path')}")


def download_example(target_date: str = TARGET_DATE, quality: str = DOWNLOAD_QUALITY, output_dir: str = DOWNLOAD_DIR) -> None:
    """Download the newest MP4 recording through the `SDFile` API."""
    with open_camera() as camera:
        sd_card = camera.sd_card()
        videos = sd_card.files(start=target_date, end=target_date, name=".mp4")
        if not videos:
            print(f"No MP4 recordings found for {target_date}")
            return
        selected = videos[-1]
        selected_info = selected.info()
        print(f"Downloading: {selected_info.get('path') or selected_info.get('file_name')}")
        saved_path = selected.download(
            output_dir,
            quality=quality,
            reconnect_retries=3,
            rewrite_exists=False,
            progress=True,
        )
        print(f"Saved: {saved_path}")


def preview_example(target_date: str = TARGET_DATE, quality: str = DOWNLOAD_QUALITY) -> None:
    """Serve the newest MP4 recording preview for a player such as VLC."""
    stream_type = "mainStream" if quality == "high" else "subStream"
    with open_camera() as camera:
        sd_card = camera.sd_card()
        videos = sd_card.files(start=target_date, end=target_date, name=".mp4")
        if not videos:
            print(f"No MP4 recordings found for {target_date}")
            return
        selected = videos[-1]
        selected_info = selected.info()
        print(f"Previewing: {selected_info.get('path') or selected_info.get('file_name')}")
        with selected.preview(cache=PREVIEW_CACHE_DIR, stream_type=stream_type, cleanup=True, progress=True) as preview:
            with preview.serve(port=8560) as server:
                print(f"Open in VLC: {server.url}")
                input("Press Enter to stop preview...")


def remove_example(target_date: str = TARGET_DATE, *, confirm: bool = False) -> None:
    """Show how removal should be guarded before destructive use."""
    if not confirm:
        print("Refusing to remove a recording. Pass confirm=True after choosing the exact file.")
        return
    with open_camera() as camera:
        sd_card = camera.sd_card()
        videos = sd_card.files(start=target_date, end=target_date, name=".mp4")
        if not videos:
            print(f"No MP4 recordings found for {target_date}")
            return
        selected = videos[-1]
        selected_info = selected.info()
        print(f"Removing: {selected_info.get('path') or selected_info.get('file_name')}")
        sd_card.remove(selected_info, confirm=True)


def format_example(*, confirm: bool = False, confirmation_text: str = "") -> None:
    """Show the explicit confirmation required for SD-card formatting."""
    if not confirm:
        print('Refusing to format the SD card. Pass confirm=True and confirmation_text="FORMAT SD CARD".')
        return
    with open_camera() as camera:
        sd_card = camera.sd_card()
        sd_card.format(confirm=True, confirmation_text=confirmation_text)
        print("SD card format command sent.")


if __name__ == "__main__":
    download_example()
