from __future__ import annotations

import socket
import time
from contextlib import AbstractContextManager
from pathlib import Path

from .config import CameraConfig
from .core.bc import (
    ProtocolError,
    encode_legacy_login,
    encode_modern,
    find_text,
    recv_message,
)
from .core.const import MSG, MSG_CLASS, msg, payloads
from .battery import Battery
from .core.crypto import Cipher, make_aes_key, md5_hex
from .core.discovery import local_discover, remote_uid_lookup
from .core.state import ConnectionState
from .core.udp_transport import UdpBcConnection, connect_local_direct, connect_relay
from .motion import Motion
from .core.xmlutil import xml_to_dict
from .internal.camera import CameraOnlineLease, redact_sensitive, split_address, stream_params
from .internal.snapshot import parse_snapshot_info, snapshot_output_path
from .recorder import StreamRecorder
from .sd_card import SdCard
from .settings import Settings
from .voice import Voice


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
                    print(msg.Log.LocalUdpP2pFailed.format(exc_type=type(exc).__name__, exc=exc))
                if self.config.discovery == "local":
                    raise
        resolved = self._resolve_address()
        if len(resolved) == 3 and resolved[2] == "udp-relay":
            if not self.config.uid:
                raise ValueError(msg.Error.UdpRelayRequiresUid)
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
        return CameraOnlineLease(self)

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
            raise ProtocolError(msg.Error.LoginNonce)
        low = reply.header.response_code & 0xFF
        if low == 0:
            self.cipher = Cipher("none")
        elif low == 1:
            self.cipher = Cipher("bc")
        elif low in (2, 3, 0x12):
            self.cipher = Cipher("aes", make_aes_key(nonce, self.config.password), full_media=(low == 0x12))
        username = md5_hex(self.config.username + nonce)
        password = md5_hex((self.config.password or "") + nonce)
        payload = payloads.login.format(username=username, password=password)
        self._send(encode_modern(MSG.LOGIN, msg_num, payload, channel_id=self.config.channel_id, cipher=self.cipher))
        modern = self._recv()
        if modern.header.response_code != 200:
            raise ProtocolError(msg.Error.LoginFailed.format(response_code=modern.header.response_code))
        self.login_xml = modern.xml_text or ""
        return self.login_xml

    def info(self, *, include_sensitive: bool = False) -> dict:
        self.ensure_connected()
        info = xml_to_dict(self.login_xml)
        if not include_sensitive:
            redact_sensitive(info)
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
        reply = self.command(MSG.UID)
        return find_text(reply.xml_root, "uid") or find_text(reply.xml_root, "UID")

    def reboot(self) -> None:
        self.ensure_connected()
        self.command(MSG.REBOOT)

    def led(self, value: str | None = None) -> dict:
        self.ensure_connected()
        if value is None:
            return self.settings().ir.status()
        normalized = value.lower()
        if normalized in ("1", "on", "true", "open"):
            return self.settings().ir.on()
        if normalized in ("0", "off", "false", "close"):
            return self.settings().ir.off()
        if normalized == "auto":
            return self.settings().ir.auto()
        raise ValueError(msg.Error.IrModeValue)

    def snapshot(self, *, out: str | Path | None = None, stream_type: str = "main") -> bytes | Path:
        self.ensure_connected()
        msg_num = self.send(
            MSG.SNAP,
            payloads.snapshot.format(channel_id=self.config.channel_id, stream_type=stream_type),
            extension=payloads.extension.format(channel_id=self.config.channel_id),
        )

        info = self._recv_matching(MSG.SNAP, msg_num)
        if info.header.response_code != 200:
            raise ProtocolError(msg.Error.SnapshotInfoFailed.format(response_code=info.header.response_code))

        file_name, expected_size = parse_snapshot_info(info.xml_root)
        data = bytearray()
        deadline = time.monotonic() + self.timeout
        while True:
            reply = self._recv()
            if reply.header.msg_id != MSG.SNAP:
                if time.monotonic() > deadline:
                    raise TimeoutError(msg.Error.TimedOutResponse.format(msg_id=MSG.SNAP, msg_num=msg_num))
                continue
            if reply.payload:
                data.extend(reply.payload)
            if reply.header.response_code == 201:
                break
            if reply.header.response_code != 200:
                raise ProtocolError(msg.Error.SnapshotDataFailed.format(response_code=reply.header.response_code))
            deadline = time.monotonic() + self.timeout

        if expected_size is not None and len(data) != expected_size:
            raise ProtocolError(msg.Error.SnapshotSizeMismatch.format(actual_size=len(data), expected_size=expected_size))

        image = bytes(data)
        if out is None:
            return image
        path = snapshot_output_path(out, file_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(image)
        return path

    def record(
        self,
        *,
        out: str | Path,
        duration: float | None = None,
        stream: str = "mainStream",
    ) -> StreamRecorder | Path:
        self.ensure_connected()
        recorder = StreamRecorder(self, out=out, stream=stream, duration=duration).start()
        if duration is not None:
            return recorder.wait()
        return recorder

    def battery(self) -> Battery:
        return Battery(self)

    def motion(self, *, channel_id: int | None = None) -> Motion:
        return Motion(self, channel_id=channel_id)

    def motion_status(self, *, timeout: float = 3.0, channel_id: int | None = None) -> dict:
        return self.motion(channel_id=channel_id).status(timeout=timeout)

    def voice(self) -> Voice:
        return Voice(self)

    def settings(self) -> Settings:
        return Settings(self)

    def battery_xml(self, *, mode: str = "reconnect") -> str | None:
        return self.battery().raw(mode=mode)

    def battery_info(self, *, mode: str = "reconnect") -> dict:
        return self.battery().info(mode=mode)

    def watch_battery(self, interval: float = 60.0, *, count: int | None = None, mode: str = "reconnect"):
        yield from self.battery().watch(interval=interval, count=count, mode=mode)

    def command(self, msg_id: int, payload: bytes = b"", *, extension: bytes = b""):
        self.ensure_connected()
        msg_num = self.send(msg_id, payload, extension=extension)
        return self._recv_matching(msg_id, msg_num)

    def _recv_matching(self, msg_id: int, msg_num: int):
        deadline = time.monotonic() + self.timeout
        while True:
            reply_msg = self._recv()
            if reply_msg.header.msg_num == msg_num:
                if hasattr(self.sock, "discard_sent"):
                    self.sock.discard_sent()
                return reply_msg
            if self.debug:
                print(
                    msg.Log.IgnoringUnmatchedMessage.format(
                        msg_id=reply_msg.header.msg_id,
                        msg_num=reply_msg.header.msg_num,
                        expected_msg_num=msg_num,
                    )
                )
            if time.monotonic() > deadline:
                raise TimeoutError(msg.Error.TimedOutResponse.format(msg_id=msg_id, msg_num=msg_num))

    def send(
        self,
        msg_id: int,
        payload: bytes = b"",
        *,
        extension: bytes = b"",
        binary_reply: bool = False,
        msg_class: int = MSG_CLASS.MODERN,
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
        stream_name, stream_code, handle = stream_params(stream)
        payload = payloads.preview_start.format(channel_id=self.config.channel_id, handle=handle, stream_type=stream_name)
        self._send(
            encode_modern(
                MSG.VIDEO,
                msg_num,
                payload,
                channel_id=self.config.channel_id,
                stream_type=stream_code,
                cipher=self.cipher,
            )
        )
        deadline = time.monotonic() + self.timeout
        while True:
            reply_msg = self._recv()
            if reply_msg.header.msg_id == MSG.VIDEO and reply_msg.header.msg_num == msg_num:
                if reply_msg.header.response_code != 200:
                    raise ProtocolError(msg.Error.StreamStartFailed.format(response_code=reply_msg.header.response_code))
                self.binary_msg_nums.add(msg_num)
                return msg_num
            if time.monotonic() > deadline:
                raise TimeoutError(msg.Error.StreamStartTimeout.format(msg_num=msg_num))

    def stop_stream(self, stream: str = "mainStream", msg_num: int | None = None) -> None:
        self.ensure_connected()
        _stream_name, stream_code, handle = stream_params(stream)
        sent_msg_num = self._next_msg() if msg_num is None else msg_num
        payload = payloads.preview_stop.format(channel_id=self.config.channel_id, handle=handle)
        self.binary_msg_nums.discard(sent_msg_num)
        self._send(
            encode_modern(
                MSG.VIDEO_STOP,
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
                reply_msg = self._recv(timeout=0.5)
            except TimeoutError:
                return
            if reply_msg.header.msg_id == MSG.VIDEO_STOP and reply_msg.header.msg_num == sent_msg_num:
                if reply_msg.header.response_code not in (0, 200):
                    if self.debug:
                        print(msg.Log.StreamStopReturned.format(response_code=reply_msg.header.response_code))
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
                        self.send(MSG.UDP_KEEPALIVE, channel_id=0, msg_num=0)
                        next_keepalive_at = now + 0.75
                    try:
                        msg = self._recv(timeout=1.0)
                    except TimeoutError:
                        continue
                    if msg.header.msg_id == MSG.VIDEO and msg.header.msg_num == msg_num and msg.payload:
                        yield msg.payload
            finally:
                try:
                    self.stop_stream(stream, msg_num)
                except Exception as exc:
                    if self.debug:
                        print(msg.Log.StreamStopCloseFailed.format(exc_type=type(exc).__name__, exc=exc))

    def _resolve_address(self) -> tuple[str, int] | tuple[str, int, str]:
        if self.config.address:
            return split_address(self.config.address)
        if self.config.cached_address:
            return split_address(self.config.cached_address)
        if self.state:
            cached = self.state.get_address(self.config.name, transport="tcp")
            if cached:
                return split_address(cached)
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
        raise ValueError(msg.Error.CameraAddressRequired)

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
            raise RuntimeError(msg.Error.CameraNotConnected)
        self.sock.sendall(data)

    def _recv(self, timeout: float | None = None):
        if self.sock is None:
            raise RuntimeError(msg.Error.CameraNotConnected)
        msg = recv_message(self.sock, self.cipher, timeout=self.timeout if timeout is None else timeout, binary_msg_nums=self.binary_msg_nums)
        if msg.header.msg_id == MSG.UDP_KEEPALIVE:
            self._reply_keepalive(msg)
        return msg

    def _reply_keepalive(self, keepalive_msg) -> None:
        if self.sock is None:
            return
        try:
            data = encode_modern(
                MSG.UDP_KEEPALIVE,
                keepalive_msg.header.msg_num,
                channel_id=keepalive_msg.header.channel_id,
                stream_type=keepalive_msg.header.stream_type,
                response_code=200,
                cipher=self.cipher,
            )
            if hasattr(self.sock, "send_untracked"):
                self.sock.send_untracked(data)
            else:
                self._send(data)
        except Exception as exc:
            if self.debug:
                print(msg.Log.StreamKeepaliveReplyFailed.format(exc_type=type(exc).__name__, exc=exc))
