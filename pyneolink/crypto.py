from __future__ import annotations

import binascii
import hashlib
from dataclasses import dataclass

BC_XML_KEY = bytes([0x1F, 0x2D, 0x3C, 0x4B, 0x5A, 0x69, 0x78, 0xFF])
UDP_XML_KEY = [
    0x1F2D3C4B,
    0x5A6C7F8D,
    0x38172E4B,
    0x8271635A,
    0x863F1A2B,
    0xA5C6F7D8,
    0x8371E1B4,
    0x17F2D3A5,
]
AES_IV = b"0123456789abcdef"


def md5_hex(value: str, *, truncate: bool = True) -> str:
    text = hashlib.md5(value.encode("utf-8")).hexdigest().upper()
    return text[:31] if truncate else text


def make_aes_key(nonce: str, password: str) -> bytes:
    phrase = f"{nonce}-{password}"
    return (hashlib.md5(phrase.encode("utf-8")).hexdigest().upper() + "\0").encode("ascii")[:16]


def bc_xor(offset: int, data: bytes) -> bytes:
    skip = offset % len(BC_XML_KEY)
    key = BC_XML_KEY[skip:] + BC_XML_KEY[:skip]
    return bytes(byte ^ key[i % len(key)] ^ (offset & 0xFF) for i, byte in enumerate(data))


def udp_xor(offset: int, data: bytes) -> bytes:
    stream = b"".join(((key + offset) & 0xFFFFFFFF).to_bytes(4, "little") for key in UDP_XML_KEY)
    return bytes(byte ^ stream[i % len(stream)] for i, byte in enumerate(data))


def neolink_crc32(data: bytes) -> int:
    return binascii.crc32(data, 0xFFFFFFFF) ^ 0xFFFFFFFF


@dataclass
class Cipher:
    name: str = "bc"
    key: bytes | None = None
    full_media: bool = False

    def encrypt(self, offset: int, data: bytes, *, media: bool = False) -> bytes:
        if self.name == "none":
            return data
        if self.name == "bc" or (media and not self.full_media):
            return bc_xor(offset, data)
        return self._aes(data, True)

    def decrypt(self, offset: int, data: bytes, *, media: bool = False) -> bytes:
        if self.name == "none":
            return data
        if self.name == "bc" or (media and not self.full_media):
            return bc_xor(offset, data)
        return self._aes(data, False)

    def _aes(self, data: bytes, encrypt: bool) -> bytes:
        if not self.key:
            raise RuntimeError("AES cipher selected without a key")
        try:
            from cryptography.hazmat.primitives.ciphers import Cipher as AesCipher
            from cryptography.hazmat.primitives.ciphers import algorithms, modes
        except ImportError as exc:
            raise RuntimeError("Install pyneolink[aes] to use AES encrypted cameras") from exc
        cipher = AesCipher(algorithms.AES(self.key), modes.CFB(AES_IV))
        ctx = cipher.encryptor() if encrypt else cipher.decryptor()
        return ctx.update(data) + ctx.finalize()
