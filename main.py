from __future__ import annotations

import os
import sys

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

if load_dotenv:
    load_dotenv()

from pyneolink import Camera
from pyneolink.cli import main as cli_main


def package_example():
    missing = [name for name in ("CAMERA_UID", "CAMERA_PASSWORD") if not os.environ.get(name)]
    if missing:
        print("Set CAMERA_UID and CAMERA_PASSWORD in .env or environment before running the package example.")
        return None
    camera = Camera(
        uuid=os.environ["CAMERA_UID"],
        username=os.environ.get("CAMERA_USERNAME", "admin"),
        password=os.environ["CAMERA_PASSWORD"],
    )
    try:
        sd = camera.sd_card()
        files = sd.list(start="2026-06-02", end="2026-06-02")
        if not files:
            print("SD list returned no files. Attempts:")
            for attempt in sd.last_attempts:
                print(f"  {attempt}")
            if sd.last_successes:
                print("Successful empty responses:")
                for success in sd.last_successes:
                    print(f"  {success['label']}")
                    if success["xml"]:
                        print(success["xml"])
        filtered = sd.filter(files, name=".mp4")
        print(f"Selected files for download: {len(filtered)}")
        if filtered:
            try:
                print(f"Downloading: {filtered[-1].get('path') or filtered[-1].get('file_name')}")
                expected_size = filtered[-1].get("size")
                if expected_size is not None:
                    print(f"Expected size: {expected_size} bytes")
                path = sd.download(filtered[-1], "downloads/", progress=True)
                actual_size = path.stat().st_size
                print(f"Saved: {path}")
                print(f"Actual size: {actual_size} bytes")
                if expected_size is not None:
                    print(f"Size check: {'OK' if actual_size == expected_size else 'MISMATCH'}")
            except Exception:
                print("Download attempts:")
                for attempt in sd.last_download_attempts:
                    print(f"  {attempt}")
                raise
    finally:
        camera.close()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        raise SystemExit(cli_main(sys.argv[1:]))
    package_example()
