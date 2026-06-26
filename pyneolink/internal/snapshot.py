from __future__ import annotations

from pathlib import Path

from pyneolink.core.bc import find_text


def parse_snapshot_info(root) -> tuple[str | None, int | None]:
    file_name = find_text(root, "fileName")
    picture_size = int_or_none(find_text(root, "pictureSize"))
    return file_name, picture_size


def int_or_none(value: str | None) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except ValueError:
        return None


def snapshot_output_path(out: str | Path, file_name: str | None = None) -> Path:
    path = Path(out)
    if path.exists() and path.is_dir():
        path = path / (file_name or "snapshot.jpg")
    elif str(out).endswith(("/", "\\")):
        path = path / (file_name or "snapshot.jpg")
    elif not path.suffix:
        path = path.with_suffix(".jpg")
    return path
