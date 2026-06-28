from __future__ import annotations

import socket
import struct
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from .crypto import Cipher
from .const import MAGIC, MSG, MSG_CLASS, msg


class ProtocolError(RuntimeError):
    """Raised for Baichuan protocol errors."""

    pass


class InvalidMagicError(ProtocolError):
    """Raised when a Baichuan header has an unexpected magic value."""

    def __init__(self, magic: int, data: bytes) -> None:
        """Create an invalid magic error.

        :param magic: Parsed magic value.
        :param data: Raw bytes used to parse the header.
        """
        super().__init__(msg.Error.InvalidBaichuanMagic.format(magic=magic))
        self.magic = magic
        self.data = data


@dataclass
class Header:
    """Baichuan message header.

    :param msg_id: Baichuan message id.
    :param body_len: Body length in bytes.
    :param channel_id: Camera channel id.
    :param stream_type: Raw stream type code.
    :param msg_num: Message correlation number.
    :param response_code: Camera response/status code.
    :param msg_class: Baichuan message class.
    :param payload_offset: Optional extension length before payload.
    """

    msg_id: int
    body_len: int
    channel_id: int
    stream_type: int
    msg_num: int
    response_code: int
    msg_class: int
    payload_offset: int | None = None

    @property
    def has_payload_offset(self) -> bool:
        return self.msg_class in (MSG_CLASS.MODERN, MSG_CLASS.FILE_DOWNLOAD, MSG_CLASS.MODERN_ZERO) or self.msg_id == MSG.FILE_REPLAY

    @property
    def is_modern(self) -> bool:
        return self.msg_class != MSG_CLASS.LEGACY

    def pack(self) -> bytes:
        header = struct.pack(
            "<III BB HH",
            MAGIC.BAICHUAN,
            self.msg_id,
            self.body_len,
            self.channel_id,
            self.stream_type,
            self.msg_num,
            self.response_code,
        )
        header += struct.pack("<H", self.msg_class)
        if self.has_payload_offset:
            header += struct.pack("<I", self.payload_offset or 0)
        return header

    @classmethod
    def unpack_from(cls, data: bytes) -> "Header":
        """
        Parse a Baichuan header from bytes.

        :param data: Header bytes, optionally followed by payload data.
        """

        if len(data) < 20:
            raise ProtocolError(msg.Error.ShortBaichuanHeader)
        magic, msg_id, body_len, channel_id, stream_type, msg_num, response_code, msg_class = struct.unpack(
            "<III BB HHH", data[:20]
        )
        if magic not in (MAGIC.BAICHUAN, MAGIC.BAICHUAN_REVERSED):
            raise InvalidMagicError(magic, data[:20])
        payload_offset = None
        if msg_class in (MSG_CLASS.MODERN, MSG_CLASS.FILE_DOWNLOAD, MSG_CLASS.MODERN_ZERO) or msg_id == MSG.FILE_REPLAY:
            if len(data) < 24:
                return cls(msg_id, body_len, channel_id, stream_type, msg_num, response_code, msg_class, None)
            payload_offset = struct.unpack("<I", data[20:24])[0]
        return cls(msg_id, body_len, channel_id, stream_type, msg_num, response_code, msg_class, payload_offset)


@dataclass
class Message:
    """Parsed Baichuan message.

    :param header: Parsed message header.
    :param extension: Decrypted extension bytes.
    :param payload: Decrypted or raw payload bytes.
    :param raw_payload_len: Payload length before decryption/splitting.
    :param encrypted_len: Number of encrypted payload bytes when known.
    """

    header: Header
    extension: bytes = b""
    payload: bytes = b""
    raw_payload_len: int = 0
    encrypted_len: int | None = None

    @property
    def xml_text(self) -> str | None:
        if not self.payload:
            return None
        try:
            return self.payload.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return None

    @property
    def xml_root(self) -> ET.Element | None:
        text = self.xml_text
        if not text:
            return None
        return ET.fromstring(text)


def xml_document(inner: str) -> bytes:
    return f'<?xml version="1.0" encoding="UTF-8" ?>\n<body>\n{inner}\n</body>'.encode("utf-8")


def extension_xml(binary: bool = False, channel_id: int | None = None) -> bytes:
    bits = ['<?xml version="1.0" encoding="UTF-8" ?>', '<Extension version="1.1">']
    if channel_id is not None:
        bits.append(f"<channelId>{channel_id}</channelId>")
    if binary:
        bits.append("<binaryData>1</binaryData>")
    bits.append("</Extension>")
    return "\n".join(bits).encode("utf-8")


def encode_modern(
    msg_id: int,
    msg_num: int,
    payload: bytes = b"",
    *,
    extension: bytes = b"",
    channel_id: int = 0,
    stream_type: int = 0,
    response_code: int = 0,
    msg_class: int = MSG_CLASS.MODERN,
    cipher: Cipher | None = None,
) -> bytes:
    cipher = cipher or Cipher("bc")
    wire_cipher = Cipher("bc") if msg_id == MSG.LOGIN and cipher.name == "aes" else cipher
    binary_payload = b"<binaryData>1</binaryData>" in extension
    enc_ext = wire_cipher.encrypt(channel_id, extension) if extension else b""
    enc_payload = payload if binary_payload else wire_cipher.encrypt(channel_id, payload) if payload else b""
    body = enc_ext + enc_payload
    payload_offset = len(enc_ext) if msg_class in (MSG_CLASS.MODERN, MSG_CLASS.MODERN_ZERO) else None
    return Header(msg_id, len(body), channel_id, stream_type, msg_num, response_code, msg_class, payload_offset).pack() + body


def encode_legacy_login(msg_num: int, *, max_encryption: str = "aes", channel_id: int = 0) -> bytes:
    enc = {"none": 0xDC00, "bc": 0xDC01, "aes": 0xDC12}.get(max_encryption, 0xDC12)
    return Header(MSG.LOGIN, 0, channel_id, 0, msg_num, enc, MSG_CLASS.LEGACY).pack()


def recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise EOFError(msg.Error.CameraClosedConnection)
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def recv_message(sock: socket.socket, cipher: Cipher, *, timeout: float | None = None, binary_msg_nums: set[int] | None = None) -> Message:
    if timeout is not None:
        sock.settimeout(timeout)
    first = recv_exact(sock, 20)
    partial = Header.unpack_from(first)
    if partial.has_payload_offset:
        first += recv_exact(sock, 4)
    header = Header.unpack_from(first)
    body = recv_exact(sock, header.body_len) if header.body_len else b""
    ext_len = header.payload_offset or 0
    ext_raw = body[:ext_len]
    payload_raw = body[ext_len:]
    reply_cipher = cipher
    if header.msg_id == MSG.LOGIN and (header.response_code >> 8) == 0xDD:
        reply_cipher = Cipher("none" if (header.response_code & 0xFF) == 0 else "bc")
    elif header.msg_id == MSG.LOGIN and cipher.name == "aes":
        reply_cipher = Cipher("bc")
    extension = reply_cipher.decrypt(header.channel_id, ext_raw) if ext_raw else b""
    in_binary = b"<binaryData>1</binaryData>" in extension
    encrypted_len = _extension_int(extension, "encryptLen") if in_binary else None
    is_binary = in_binary or (binary_msg_nums is not None and header.msg_num in binary_msg_nums)
    if is_binary:
        if reply_cipher.name == "aes" and reply_cipher.full_media and encrypted_len is not None:
            encrypted_part = payload_raw[:encrypted_len]
            raw_tail = payload_raw[encrypted_len:]
            payload = reply_cipher.decrypt(header.channel_id, encrypted_part) + raw_tail
        else:
            payload = payload_raw
    else:
        payload = reply_cipher.decrypt(header.channel_id, payload_raw)
    return Message(header, extension, payload, raw_payload_len=len(payload_raw), encrypted_len=encrypted_len)


def find_text(root: ET.Element | None, tag: str) -> str | None:
    if root is None:
        return None
    found = root.find(f".//{tag}")
    return found.text if found is not None else None


def _extension_int(extension: bytes, tag: str) -> int | None:
    try:
        text = extension.decode("utf-8", errors="ignore")
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    value = find_text(root, tag)
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None
