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


def main() -> None:
    missing = [name for name in ("CAMERA_UID", "CAMERA_PASSWORD") if not os.environ.get(name)]
    if missing:
        print("Set CAMERA_UID and CAMERA_PASSWORD in .env or environment before running download.")
        return

    target_date = os.environ.get("PYNEOLINK_DATE", date.today().isoformat())
    quality = os.environ.get("PYNEOLINK_QUALITY", "high")
    output_dir = os.environ.get("PYNEOLINK_DOWNLOAD_DIR", "downloads")

    camera = Camera(
        uuid=os.environ["CAMERA_UID"],
        username=os.environ.get("CAMERA_USERNAME", "admin"),
        password=os.environ["CAMERA_PASSWORD"],
    )
    sd = None
    try:
        sd = camera.sd_card()
        files = sd.list(start=target_date, end=target_date, sort="asc")
        videos = sd.filter(files, name=".mp4")
        if not videos:
            print(f"No MP4 recordings found for {target_date}.")
            return

        selected = videos[-1]
        expected_size = selected.get("size")
        print(f"Downloading: {selected.get('path') or selected.get('file_name')}")
        print(f"Recording time: {selected.get('start_time')} - {selected.get('end_time')}")
        print(f"Quality: {quality}")
        if expected_size is not None:
            print(f"Expected size: {expected_size} bytes")

        path = sd.download(selected, output_dir, quality=quality, progress=True)
        actual_size = path.stat().st_size
        print(f"Saved: {path}")
        print(f"Actual size: {actual_size} bytes")
        if expected_size is not None:
            print(f"Size check: {'OK' if actual_size == expected_size else 'MISMATCH'}")
    except Exception:
        print("Download attempts:")
        for attempt in getattr(sd, "last_download_attempts", []) if sd is not None else []:
            print(f"  {attempt}")
        raise
    finally:
        camera.close()


if __name__ == "__main__":
    main()
