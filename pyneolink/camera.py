from __future__ import annotations

import socket
import time
from contextlib import AbstractContextManager
from pathlib import Path

from .config import CameraConfig
from .core.bc import (
    CLASS_MODERN,
    MSG_BATTERY,
    MSG_GET_LED,
    MSG_LOGIN,
    MSG_REBOOT,
    MSG_SET_LED,
    MSG_UID,
    MSG_UDP_KEEPALIVE,
    MSG_VIDEO,
    MSG_VIDEO_STOP,
    ProtocolError,
    encode_legacy_login,
    encode_modern,
    find_text,
    recv_message,
    xml_document,
)
from .battery import Battery
from .core.crypto import Cipher, make_aes_key, md5_hex
from .core.discovery import local_discover, remote_uid_lookup
from .core.state import ConnectionState
from .core.udp_transport import UdpBcConnection, connect_local_direct, connect_relay
from .core.xmlutil import xml_to_dict
from .sd_card import SdCard


class Camera(AbstractContextManager["Camera"]):
    def __init__(
        self,
        config: CameraConfig | None = None,
        *,
        uuid: str | None = None,
        uid: str | None = None,
        username: str = "admin",
        password: str = "123456",
        name: str | None = None,
        address: str | None = None,
        cached_address: str | None = None,
        discovery: str = "relay",
        channel_id: int = 0,
        stream: str = "both",
        timeout: float = 10.0,
        state_path: str | Path | None = ".pyneolink_state.json",
        debug: bool = False,
    ) -> None:
        if config is None:
            camera_uid = uid or uuid
            config = CameraConfig(
                name=name or camera_uid or address or "camera",
                username=username,
                password=password,
                address=address,
                uid=camera_uid,
                discovery=discovery,
                channel_id=channel_id,
                stream=stream,
                cached_address=cached_address,
            )
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
        self._online_required = 0

    def __enter__(self) -> "Camera":
        self.connect()
        self.login()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def connect(self) -> None:
        if (
            self.config.uid
            and not self.config.address
            and not self.config.cached_address
            and self.config.discovery in ("local", "remote", "map", "relay")
        ):
            try:
                probe_timeout = max(self.timeout, 8.0) if self.config.discovery == "local" else min(self.timeout, 2.0)
                self.sock = connect_local_direct(self.config.uid, timeout=probe_timeout, debug=self.debug)
                self.connected_address = self.sock.addr
                if self.state:
                    self.state.update_address(
                        self.config.name,
                        f"{self.sock.addr[0]}:{self.sock.addr[1]}",
                        uid=self.config.uid,
                        transport="udp-local",
                    )
                return
            except Exception as exc:
                if self.debug:
                    print(f"[pyneolink] Local UDP P2P failed: {type(exc).__name__}: {exc}")
                if self.config.discovery == "local":
                    raise
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
        self.login_xml = ""

    def reconnect(self) -> None:
        self.close()
        self.connect()
        self.login()

    @property
    def online_required(self) -> bool:
        return self._online_required > 0

    def require_online(self):
        return _CameraOnlineLease(self)

    def keepalive(self, *, timeout: float = 0.05) -> str:
        self.ensure_connected()
        if hasattr(self.sock, "maintain"):
            self.sock.maintain()
        try:
            msg = self._recv(timeout=timeout)
        except TimeoutError:
            return "timeout"
        return f"msg_id={msg.header.msg_id} msg_num={msg.header.msg_num} response={msg.header.response_code}"

    def login(self, max_encryption: str = "aes") -> str:
        if self.sock is None:
            self.connect()
        if self.login_xml:
            return self.login_xml
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
        self.ensure_connected()
        info = xml_to_dict(self.login_xml)
        if not include_sensitive:
            _redact_sensitive(info)
        return {
            "name": self.config.name,
            "uid": self.config.uid or self.get_uid(),
            "connected_address": f"{self.connected_address[0]}:{self.connected_address[1]}" if self.connected_address else None,
            "device": info,
        }

    def sd_card(self) -> SdCard:
        return SdCard(self)

    def get_uid(self) -> str | None:
        self.ensure_connected()
        reply = self.command(MSG_UID)
        return find_text(reply.xml_root, "uid") or find_text(reply.xml_root, "UID")

    def reboot(self) -> None:
        self.ensure_connected()
        self.command(MSG_REBOOT)

    def led(self, value: str | None = None) -> str | None:
        self.ensure_connected()
        if value is None:
            return self.command(MSG_GET_LED).xml_text
        value_num = 1 if value.lower() in ("1", "on", "true") else 0
        payload = xml_document(f"<LedState version=\"1.1\"><channelId>{self.config.channel_id}</channelId><state>{value_num}</state></LedState>")
        self.command(MSG_SET_LED, payload)
        return None

    def battery(self) -> Battery:
        return Battery(self)

    def battery_xml(self, *, mode: str = "reconnect") -> str | None:
        return self.battery().raw(mode=mode)

    def battery_info(self, *, mode: str = "reconnect") -> dict:
        return self.battery().info(mode=mode)

    def watch_battery(self, interval: float = 60.0, *, count: int | None = None, mode: str = "reconnect"):
        yield from self.battery().watch(interval=interval, count=count, mode=mode)

    def command(self, msg_id: int, payload: bytes = b"", *, extension: bytes = b""):
        self.ensure_connected()
        msg_num = self.send(msg_id, payload, extension=extension)
        deadline = time.monotonic() + self.timeout
        while True:
            msg = self._recv()
            if msg.header.msg_num == msg_num:
                if hasattr(self.sock, "discard_sent"):
                    self.sock.discard_sent()
                return msg
            if self.debug:
                print(
                    f"[pyneolink] Ignoring unmatched message msg_id={msg.header.msg_id} "
                    f"msg_num={msg.header.msg_num}; waiting for msg_num={msg_num}"
                )
            if time.monotonic() > deadline:
                raise TimeoutError(f"Timed out waiting for response to message {msg_id} #{msg_num}")

    def send(
        self,
        msg_id: int,
        payload: bytes = b"",
        *,
        extension: bytes = b"",
        binary_reply: bool = False,
        msg_class: int = CLASS_MODERN,
        channel_id: int | None = None,
        msg_num: int | None = None,
        stream_type: int = 0,
    ) -> int:
        self.ensure_connected()
        sent_msg_num = self._next_msg() if msg_num is None else msg_num
        if binary_reply:
            self.binary_msg_nums.add(sent_msg_num)
        self._send(
            encode_modern(
                msg_id,
                sent_msg_num,
                payload,
                extension=extension,
                channel_id=self.config.channel_id if channel_id is None else channel_id,
                msg_class=msg_class,
                stream_type=stream_type,
                cipher=self.cipher,
            )
        )
        return sent_msg_num

    def start_stream(self, stream: str = "mainStream"):
        self.ensure_connected()
        msg_num = self._next_msg()
        stream_name, stream_code, handle = _stream_params(stream)
        payload = xml_document(
            f"<Preview version=\"1.1\"><channelId>{self.config.channel_id}</channelId><handle>{handle}</handle><streamType>{stream_name}</streamType></Preview>"
        )
        self._send(
            encode_modern(
                MSG_VIDEO,
                msg_num,
                payload,
                channel_id=self.config.channel_id,
                stream_type=stream_code,
                cipher=self.cipher,
            )
        )
        deadline = time.monotonic() + self.timeout
        while True:
            msg = self._recv()
            if msg.header.msg_id == MSG_VIDEO and msg.header.msg_num == msg_num:
                if msg.header.response_code != 200:
                    raise ProtocolError(f"Stream start failed with response {msg.header.response_code}")
                self.binary_msg_nums.add(msg_num)
                return msg_num
            if time.monotonic() > deadline:
                raise TimeoutError(f"Timed out waiting for stream start response #{msg_num}")

    def stop_stream(self, stream: str = "mainStream", msg_num: int | None = None) -> None:
        self.ensure_connected()
        _stream_name, stream_code, handle = _stream_params(stream)
        sent_msg_num = self._next_msg() if msg_num is None else msg_num
        payload = xml_document(
            f"<Preview version=\"1.1\"><channelId>{self.config.channel_id}</channelId><handle>{handle}</handle></Preview>"
        )
        self.binary_msg_nums.discard(sent_msg_num)
        self._send(
            encode_modern(
                MSG_VIDEO_STOP,
                sent_msg_num,
                payload,
                channel_id=self.config.channel_id,
                stream_type=stream_code,
                cipher=self.cipher,
            )
        )
        deadline = time.monotonic() + min(self.timeout, 2.0)
        while time.monotonic() <= deadline:
            try:
                msg = self._recv(timeout=0.5)
            except TimeoutError:
                return
            if msg.header.msg_id == MSG_VIDEO_STOP and msg.header.msg_num == sent_msg_num:
                if msg.header.response_code not in (0, 200):
                    if self.debug:
                        print(f"[pyneolink] Stream stop returned {msg.header.response_code}; ignoring")
                    return
                return

    def read_stream_payloads(self, stream: str = "mainStream"):
        with self.require_online():
            msg_num = self.start_stream(stream)
            next_keepalive_at = time.monotonic() + 0.75
            try:
                while True:
                    now = time.monotonic()
                    if now >= next_keepalive_at:
                        self.send(MSG_UDP_KEEPALIVE, channel_id=0, msg_num=0)
                        next_keepalive_at = now + 0.75
                    try:
                        msg = self._recv(timeout=1.0)
                    except TimeoutError:
                        continue
                    if msg.header.msg_id == MSG_VIDEO and msg.header.msg_num == msg_num and msg.payload:
                        yield msg.payload
            finally:
                try:
                    self.stop_stream(stream, msg_num)
                except Exception as exc:
                    if self.debug:
                        print(f"[pyneolink] Stream stop failed during close: {type(exc).__name__}: {exc}")

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
            if self.config.discovery in ("relay", "cellular"):
                return "", 0, "udp-relay"
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

    def ensure_connected(self) -> None:
        if self.sock is None:
            self.connect()
        if not self.login_xml:
            self.login()

    def _next_msg(self) -> int:
        self.msg_num = (self.msg_num + 1) & 0xFFFF
        return self.msg_num or self._next_msg()

    def _send(self, data: bytes) -> None:
        if self.sock is None:
            raise RuntimeError("Camera is not connected")
        self.sock.sendall(data)

    def _recv(self, timeout: float | None = None):
        if self.sock is None:
            raise RuntimeError("Camera is not connected")
        msg = recv_message(self.sock, self.cipher, timeout=self.timeout if timeout is None else timeout, binary_msg_nums=self.binary_msg_nums)
        if msg.header.msg_id == MSG_UDP_KEEPALIVE:
            self._reply_keepalive(msg)
        return msg

    def _reply_keepalive(self, msg) -> None:
        if self.sock is None:
            return
        try:
            data = encode_modern(
                MSG_UDP_KEEPALIVE,
                msg.header.msg_num,
                channel_id=msg.header.channel_id,
                stream_type=msg.header.stream_type,
                response_code=200,
                cipher=self.cipher,
            )
            if hasattr(self.sock, "send_untracked"):
                self.sock.send_untracked(data)
            else:
                self._send(data)
        except Exception as exc:
            if self.debug:
                print(f"[pyneolink] Failed to reply to stream keepalive: {type(exc).__name__}: {exc}")


def _split_address(address: str) -> tuple[str, int]:
    if ":" in address:
        host, port = address.rsplit(":", 1)
        return host, int(port)
    return address, 9000


class _CameraOnlineLease:
    def __init__(self, camera: Camera) -> None:
        self.camera = camera
        self.active = False

    def __enter__(self) -> Camera:
        if not self.active:
            self.camera._online_required += 1
            self.active = True
        return self.camera

    def __exit__(self, *exc: object) -> None:
        if self.active:
            self.camera._online_required = max(0, self.camera._online_required - 1)
            self.active = False


def _stream_params(stream: str) -> tuple[str, int, int]:
    normalized = stream.strip()
    aliases = {
        "high": "mainStream",
        "main": "mainStream",
        "mainstream": "mainStream",
        "clear": "mainStream",
        "low": "subStream",
        "sub": "subStream",
        "substream": "subStream",
        "fluent": "subStream",
        "extern": "externStream",
        "externstream": "externStream",
    }
    stream_name = aliases.get(normalized.lower(), normalized)
    if stream_name == "mainStream":
        return stream_name, 0, 0
    if stream_name == "subStream":
        return stream_name, 1, 256
    if stream_name == "externStream":
        return stream_name, 0, 1024
    raise ValueError('stream must be "mainStream", "subStream", "externStream", "high", or "low"')


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
