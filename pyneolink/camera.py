from __future__ import annotations

import socket
from contextlib import AbstractContextManager
from pathlib import Path

from .bc import (
    CLASS_MODERN,
    MSG_BATTERY,
    MSG_GET_LED,
    MSG_LOGIN,
    MSG_REBOOT,
    MSG_SET_LED,
    MSG_UID,
    MSG_VIDEO,
    ProtocolError,
    encode_legacy_login,
    encode_modern,
    find_text,
    recv_message,
    xml_document,
)
from .config import CameraConfig
from .crypto import Cipher, make_aes_key, md5_hex
from .discovery import local_discover, remote_uid_lookup
from .state import ConnectionState
from .udp_transport import UdpBcConnection, connect_relay
from .xmlutil import xml_to_dict


class Camera(AbstractContextManager["Camera"]):
    def __init__(self, config: CameraConfig, *, timeout: float = 10.0, state_path: str | Path | None = ".pyneolink_state.json", debug: bool = False) -> None:
        self.config = config
        self.timeout = timeout
        self.sock: socket.socket | UdpBcConnection | None = None
        self.cipher = Cipher("bc")
        self.msg_num = 0
        self.binary_msg_nums: set[int] = set()
        self.state = ConnectionState(state_path) if state_path else None
        self.connected_address: tuple[str, int] | None = None
        self.login_xml = ""
        self.debug = debug

    def __enter__(self) -> "Camera":
        self.connect()
        self.login()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def connect(self) -> None:
        resolved = self._resolve_address()
        if len(resolved) == 3 and resolved[2] == "udp-relay":
            if not self.config.uid:
                raise ValueError("UDP relay requires a camera UID")
            self.sock = connect_relay(self.config.uid, timeout=max(self.timeout, 20.0), debug=self.debug)
            self.connected_address = self.sock.addr
            if self.state:
                self.state.update_address(self.config.name, f"{self.sock.addr[0]}:{self.sock.addr[1]}", uid=self.config.uid, transport="udp-relay")
            return
        host, port = resolved[:2]
        self.sock = socket.create_connection((host, port), timeout=self.timeout)
        self.connected_address = (host, port)
        if self.state:
            self.state.update_address(self.config.name, f"{host}:{port}", uid=self.config.uid, transport="tcp")

    def close(self) -> None:
        if self.sock:
            self.sock.close()
            self.sock = None

    def login(self, max_encryption: str = "aes") -> str:
        msg_num = self._next_msg()
        self._send(encode_legacy_login(msg_num, max_encryption=max_encryption, channel_id=self.config.channel_id))
        reply = self._recv()
        nonce = find_text(reply.xml_root, "nonce")
        if not nonce:
            raise ProtocolError("Camera did not return a login nonce")
        low = reply.header.response_code & 0xFF
        if low == 0:
            self.cipher = Cipher("none")
        elif low == 1:
            self.cipher = Cipher("bc")
        elif low in (2, 3, 0x12):
            self.cipher = Cipher("aes", make_aes_key(nonce, self.config.password), full_media=(low == 0x12))
        username = md5_hex(self.config.username + nonce)
        password = md5_hex((self.config.password or "") + nonce)
        payload = xml_document(
            f"<LoginUser version=\"1.1\"><userName>{username}</userName><password>{password}</password><userVer>1</userVer></LoginUser>"
            "<LoginNet version=\"1.1\"><type>LAN</type><udpPort>0</udpPort></LoginNet>"
        )
        self._send(encode_modern(MSG_LOGIN, msg_num, payload, channel_id=self.config.channel_id, cipher=self.cipher))
        modern = self._recv()
        if modern.header.response_code != 200:
            raise ProtocolError(f"Login failed with response {modern.header.response_code}")
        self.login_xml = modern.xml_text or ""
        return self.login_xml

    def info(self, *, include_sensitive: bool = False) -> dict:
        info = xml_to_dict(self.login_xml)
        if not include_sensitive:
            _redact_sensitive(info)
        return {
            "name": self.config.name,
            "uid": self.config.uid or self.get_uid(),
            "connected_address": f"{self.connected_address[0]}:{self.connected_address[1]}" if self.connected_address else None,
            "device": info,
        }

    def get_uid(self) -> str | None:
        reply = self.command(MSG_UID)
        return find_text(reply.xml_root, "uid") or find_text(reply.xml_root, "UID")

    def reboot(self) -> None:
        self.command(MSG_REBOOT)

    def led(self, value: str | None = None) -> str | None:
        if value is None:
            return self.command(MSG_GET_LED).xml_text
        value_num = 1 if value.lower() in ("1", "on", "true") else 0
        payload = xml_document(f"<LedState version=\"1.1\"><channelId>{self.config.channel_id}</channelId><state>{value_num}</state></LedState>")
        self.command(MSG_SET_LED, payload)
        return None

    def battery(self) -> str | None:
        return self.command(MSG_BATTERY).xml_text

    def command(self, msg_id: int, payload: bytes = b""):
        msg_num = self._next_msg()
        self._send(encode_modern(msg_id, msg_num, payload, channel_id=self.config.channel_id, msg_class=CLASS_MODERN, cipher=self.cipher))
        return self._recv()

    def start_stream(self, stream: str = "mainStream"):
        msg_num = self._next_msg()
        stream_type = 1 if stream == "subStream" else 0
        payload = xml_document(
            f"<Preview version=\"1.1\"><channelId>{self.config.channel_id}</channelId><handle>{stream_type}</handle><streamType>{stream}</streamType></Preview>"
        )
        self.binary_msg_nums.add(msg_num)
        self._send(
            encode_modern(
                MSG_VIDEO,
                msg_num,
                payload,
                channel_id=self.config.channel_id,
                stream_type=stream_type,
                cipher=self.cipher,
            )
        )
        return msg_num

    def read_stream_payloads(self, stream: str = "mainStream"):
        msg_num = self.start_stream(stream)
        while True:
            msg = self._recv()
            if msg.header.msg_id == MSG_VIDEO and msg.header.msg_num == msg_num and msg.payload:
                yield msg.payload

    def _resolve_address(self) -> tuple[str, int] | tuple[str, int, str]:
        if self.config.address:
            return _split_address(self.config.address)
        if self.config.cached_address:
            return _split_address(self.config.cached_address)
        if self.state:
            cached = self.state.get_address(self.config.name, transport="tcp")
            if cached:
                return _split_address(cached)
        if self.config.uid:
            hits = []
            if self.config.discovery in ("local", "remote", "map", "relay"):
                hits.extend(local_discover(self.config.uid, timeout=min(self.timeout, 15.0)))
            if not hits and self.config.discovery in ("remote", "map", "relay", "cellular"):
                hits.extend(remote_uid_lookup(self.config.uid, timeout=min(self.timeout, 15.0)))
            if hits:
                tcp_hits = [hit for hit in hits if hit.transport == "tcp"]
                if tcp_hits:
                    host, port = tcp_hits[0].address
                    return host, port if port else 9000
                return "", 0, "udp-relay"
        raise ValueError("Camera needs address, cached_address, or a UID reachable by discovery")

    def _next_msg(self) -> int:
        self.msg_num = (self.msg_num + 1) & 0xFFFF
        return self.msg_num or self._next_msg()

    def _send(self, data: bytes) -> None:
        if self.sock is None:
            raise RuntimeError("Camera is not connected")
        self.sock.sendall(data)

    def _recv(self):
        if self.sock is None:
            raise RuntimeError("Camera is not connected")
        return recv_message(self.sock, self.cipher, timeout=self.timeout, binary_msg_nums=self.binary_msg_nums)


def _split_address(address: str) -> tuple[str, int]:
    if ":" in address:
        host, port = address.rsplit(":", 1)
        return host, int(port)
    return address, 9000


def _redact_sensitive(value: object) -> None:
    if isinstance(value, dict):
        for key in list(value.keys()):
            if key.lower() in {"secretcode", "bootsecret", "password"}:
                value[key] = "***"
            else:
                _redact_sensitive(value[key])
    elif isinstance(value, list):
        for item in value:
            _redact_sensitive(item)
