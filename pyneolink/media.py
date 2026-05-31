from __future__ import annotations

import struct
from dataclasses import dataclass
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
        if magic[:2] in (b"00", b"01") and magic[2:] == b"dc":
            if len(self._buf) < 20:
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
            return MediaPacket("iframe" if magic[:2] == b"00" else "pframe", codec, ts_us, payload)
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
        magics = [b"1001", b"1002", b"00dc", b"01dc", b"05wb", b"01wb"]
        indexes = [self._buf.find(magic, 1) for magic in magics]
        indexes = [i for i in indexes if i >= 0]
        del self._buf[: min(indexes) if indexes else len(self._buf)]
