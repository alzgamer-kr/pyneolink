from __future__ import annotations

import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


@dataclass
class MediaPacket:
    kind: str
    codec: str | None
    timestamp_us: int | None
    data: bytes
    width: int | None = None
    height: int | None = None
    fps: int | None = None


class MediaParser:
    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> Iterator[MediaPacket]:
        self._buf.extend(data)
        while True:
            packet = self._try_one()
            if packet is None:
                return
            yield packet

    def _try_one(self) -> MediaPacket | None:
        if len(self._buf) < 8:
            return None
        magic = bytes(self._buf[:4])
        if magic in (b"1001", b"1002"):
            if len(self._buf) < 32:
                return None
            header_size, width, height = struct.unpack("<III", self._buf[4:16])
            if header_size != 32:
                self._resync()
                return None
            fps = self._buf[17]
            del self._buf[:32]
            return MediaPacket("info", None, None, b"", width, height, fps)
        if _is_video_magic(magic):
            if len(self._buf) < 24:
                return None
            codec = bytes(self._buf[4:8]).decode("ascii", errors="replace")
            if codec not in ("H264", "H265"):
                self._resync()
                return None
            size, extra, ts_us, _unknown = struct.unpack("<IIII", self._buf[8:24])
            header_len = 24 + extra
            total = header_len + size + ((8 - size % 8) % 8)
            if len(self._buf) < total:
                return None
            payload = bytes(self._buf[header_len : header_len + size])
            del self._buf[:total]
            return MediaPacket("iframe" if magic[1:2] == b"0" else "pframe", codec, ts_us, payload)
        if magic in (b"05wb", b"01wb"):
            if len(self._buf) < 8:
                return None
            size = struct.unpack("<H", self._buf[4:6])[0]
            total = 8 + size + ((8 - size % 8) % 8)
            if len(self._buf) < total:
                return None
            payload = bytes(self._buf[8 : 8 + size])
            del self._buf[:total]
            return MediaPacket("aac" if magic == b"05wb" else "adpcm", None, None, payload)
        self._resync()
        return None

    def _resync(self) -> None:
        magics = [b"1001", b"1002", b"05wb", b"01wb"]
        for channel in b"0123456789":
            magics.append(bytes([channel]) + b"0dc")
            magics.append(bytes([channel]) + b"1dc")
        indexes = [self._buf.find(magic, 1) for magic in magics]
        indexes = [i for i in indexes if i >= 0]
        del self._buf[: min(indexes) if indexes else len(self._buf)]


def looks_like_bcmedia(path: str | Path) -> bool:
    with Path(path).open("rb") as fh:
        return fh.read(4) in (b"1001", b"1002")


def extract_video_stream(source: str | Path, destination: str | Path) -> tuple[str, int, int]:
    parser = MediaParser()
    codec: str | None = None
    fps = 15
    frames = 0
    with Path(source).open("rb") as src, Path(destination).open("wb") as dst:
        while True:
            chunk = src.read(64 * 1024)
            if not chunk:
                break
            for packet in parser.feed(chunk):
                if packet.kind == "info" and packet.fps:
                    fps = packet.fps
                if packet.kind in ("iframe", "pframe"):
                    codec = packet.codec or codec
                    dst.write(packet.data)
                    frames += 1
    if not codec or not frames:
        raise ValueError("BCMedia stream did not contain readable video frames")
    return codec, fps, frames


def bcmedia_to_mp4(source: str | Path, destination: str | Path) -> None:
    source_path = Path(source)
    destination_path = Path(destination)
    raw_suffix = ".h265" if _contains_codec(source_path, b"H265") else ".h264"
    raw_path = destination_path.with_suffix(destination_path.suffix + raw_suffix)
    try:
        codec, fps, frames = extract_video_stream(source_path, raw_path)
        input_format = "hevc" if codec == "H265" else "h264"
        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            input_format,
            "-r",
            str(fps or 15),
            "-i",
            str(raw_path),
            "-c",
            "copy",
            str(destination_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or f"ffmpeg exited with {result.returncode}"
            raise RuntimeError(detail)
        if not destination_path.exists() or destination_path.stat().st_size == 0:
            raise RuntimeError(f"ffmpeg created no output from {frames} {codec} frames")
    finally:
        raw_path.unlink(missing_ok=True)


def extract_embedded_mp4(source: str | Path, destination: str | Path) -> bool:
    source_path = Path(source)
    destination_path = Path(destination)
    with source_path.open("rb") as fh:
        head = fh.read(4096)
        marker = head.find(b"ftyp")
        if marker < 4:
            return False
        start = marker - 4
        box_size = int.from_bytes(head[start:marker], "big")
        if box_size < 8 or start + box_size > len(head):
            return False
        fh.seek(start)
        with destination_path.open("wb") as dst:
            while True:
                chunk = fh.read(1024 * 1024)
                if not chunk:
                    break
                dst.write(chunk)
    return destination_path.exists() and destination_path.stat().st_size > 0


def _contains_codec(path: Path, codec: bytes) -> bool:
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(256 * 1024)
            if not chunk:
                return False
            if codec in chunk:
                return True


def _is_video_magic(magic: bytes) -> bool:
    return len(magic) == 4 and magic[0:1] in b"0123456789" and magic[1:2] in b"01" and magic[2:] == b"dc"
