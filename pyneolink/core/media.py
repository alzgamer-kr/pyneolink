from __future__ import annotations

import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .const import msg


@dataclass
class MediaPacket:
    """Parsed BCMedia packet.

    :param kind: Packet kind such as `info`, `iframe`, `pframe`, `aac`, or
        `adpcm`.
    :param codec: Video codec when known, usually `H264` or `H265`.
    :param timestamp_us: Packet timestamp in microseconds when present.
    :param data: Raw packet payload.
    :param width: Video width from stream info.
    :param height: Video height from stream info.
    :param fps: Frames per second from stream info.
    """

    kind: str
    codec: str | None
    timestamp_us: int | None
    data: bytes
    width: int | None = None
    height: int | None = None
    fps: int | None = None


class MediaParser:
    """Incremental BCMedia parser."""

    def __init__(self) -> None:
        """Create an empty parser."""
        self._buf = bytearray()

    def feed(self, data: bytes) -> Iterator[MediaPacket]:
        """Feed bytes and yield complete media packets.

        :param data: BCMedia bytes to append to the parser buffer.
        """
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
        head = fh.read(1024 * 1024)
    return _find_media_magic(head) is not None


def _find_media_magic(data: bytes) -> int | None:
    magics = [b"1001", b"1002", b"05wb", b"01wb"]
    for channel in b"0123456789":
        magics.append(bytes([channel]) + b"0dc")
        magics.append(bytes([channel]) + b"1dc")
    positions = [data.find(magic) for magic in magics]
    positions = [position for position in positions if position >= 0]
    return min(positions) if positions else None


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
                    codec = _payload_video_codec(packet.data) or codec or packet.codec
                    dst.write(packet.data)
                    frames += 1
    if not codec or not frames:
        raise ValueError(msg.Error.NoReadableVideoFrames)
    return codec, fps, frames


def bcmedia_to_mp4(source: str | Path, destination: str | Path) -> None:
    source_path = Path(source)
    destination_path = Path(destination)
    raw_path = destination_path.with_suffix(destination_path.suffix + ".video")
    audio_path = destination_path.with_suffix(destination_path.suffix + ".aac")
    try:
        codec, fps, frames, audio_frames = _extract_media_streams(source_path, raw_path, audio_path)
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
        ]
        if audio_frames:
            cmd[cmd.index("-c") : cmd.index("-c")] = ["-f", "aac", "-i", str(audio_path)]
            cmd.extend(["-map", "0:v:0", "-map", "1:a:0"])
        if codec == "H265":
            cmd.extend(["-tag:v", "hvc1"])
        cmd.extend(["-movflags", "+faststart"])
        cmd.append(str(destination_path))
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or f"ffmpeg exited with {result.returncode}"
            destination_path.unlink(missing_ok=True)
            raise RuntimeError(detail)
        if not destination_path.exists() or destination_path.stat().st_size == 0:
            destination_path.unlink(missing_ok=True)
            raise RuntimeError(msg.Error.FfmpegNoOutput.format(frames=frames, codec=codec))
    finally:
        raw_path.unlink(missing_ok=True)
        audio_path.unlink(missing_ok=True)


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


def _extract_media_streams(
    source: Path,
    video_destination: Path,
    audio_destination: Path,
) -> tuple[str, int, int, int]:
    parser = MediaParser()
    codec: str | None = None
    fps = 15
    video_frames = 0
    audio_frames = 0
    with (
        source.open("rb") as src,
        video_destination.open("wb") as video,
        audio_destination.open("wb") as audio,
    ):
        while chunk := src.read(64 * 1024):
            for packet in parser.feed(chunk):
                if packet.kind == "info" and packet.fps:
                    fps = packet.fps
                elif packet.kind in ("iframe", "pframe"):
                    codec = _payload_video_codec(packet.data) or codec or packet.codec
                    video.write(packet.data)
                    video_frames += 1
                elif packet.kind == "aac":
                    audio.write(packet.data)
                    audio_frames += 1
    if not codec or not video_frames:
        raise ValueError(msg.Error.NoReadableVideoFrames)
    return codec, fps, video_frames, audio_frames


def _payload_video_codec(data: bytes) -> str | None:
    for offset in _annex_b_nal_offsets(data):
        nal_header = data[offset]
        h264_type = nal_header & 0x1F
        h265_type = (nal_header >> 1) & 0x3F
        if h265_type in (19, 20, 21, 32, 33, 34):
            return "H265"
        if h264_type in (5, 7, 8):
            return "H264"
    return None


def _annex_b_nal_offsets(data: bytes) -> Iterator[int]:
    index = 0
    while index < len(data) - 3:
        if data[index : index + 4] == b"\0\0\0\1":
            yield index + 4
            index += 4
        elif data[index : index + 3] == b"\0\0\1":
            yield index + 3
            index += 3
        else:
            index += 1


def _is_video_magic(magic: bytes) -> bool:
    return len(magic) == 4 and magic[0:1] in b"0123456789" and magic[1:2] in b"01" and magic[2:] == b"dc"
