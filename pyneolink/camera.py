from __future__ import annotations

import queue
import socket
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from pathlib import Path

from .config import CameraConfig
from .core.bc import Message, ProtocolError, encode_legacy_login, encode_modern, find_text, recv_message
from .core.const import MSG, MSG_CLASS, msg, payloads
from .battery import Battery
from .core.crypto import Cipher, make_aes_key, md5_hex
from .core.discovery import local_discover, remote_uid_lookup
from .core.state import ConnectionState
from .core.udp_transport import UdpBcConnection, connect_local_direct, connect_relay
from .motion import Motion
from .ptz import Ptz
from .core.xmlutil import xml_to_dict
from .internal.camera import CameraOnlineLease, redact_sensitive, split_address, stream_params
from .internal.snapshot import parse_snapshot_info, snapshot_output_path
from .recorder import StreamRecorder
from .sd_card import SdCard
from .settings import Settings
from .voice import Voice


RECOVERABLE_STREAM_ERRORS = (TimeoutError, EOFError, OSError, ProtocolError)
DEFAULT_STREAM_STALL_TIMEOUT = 15.0


class Camera(AbstractContextManager["Camera"]):
    """High-level Reolink camera client.

    One `Camera` owns one transport session, login state, encryption mode, and
    a dispatcher that routes concurrent replies by message id and message
    number. Helpers such as `battery()`, `motion()`, `sd_card()`, `voice()`, and
    `settings()` all share that managed session.
    """

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
        """Create a camera client.

        :param config: Optional ready `CameraConfig`. When provided, keyword
            camera identity fields are ignored.
        :param uuid: Reolink UID alias. Use this or `uid` for P2P access.
        :param uid: Reolink UID. Use this or `uuid` for P2P access.
        :param username: Camera username.
        :param password: Camera password.
        :param name: Human-readable camera name used in logs and state cache.
        :param address: Direct camera address, optionally with port
            (`host` or `host:port`). Defaults to port 9000.
        :param cached_address: Previously known address to try before UID
            discovery.
        :param discovery: Discovery mode. Common values are `local`, `remote`,
            `map`, `relay`, or `cellular`.
        :param channel_id: Reolink channel id. Battery cameras usually use 0.
        :param stream: Preferred stream selection for config consumers. Use
            method-level `stream`/`quality` parameters for explicit operations.
        :param timeout: Socket/protocol timeout in seconds.
        :param state_path: JSON state cache path, or `None` to disable cache.
        :param debug: Print protocol/debug messages when enabled.
        """
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
        self._send_lock = threading.RLock()
        self._reconnect_lock = threading.RLock()
        self._dispatch_lock = threading.RLock()
        self._dispatch_stop = threading.Event()
        self._dispatch_thread: threading.Thread | None = None
        self._dispatcher_requested = False
        self._dispatch_waiters: dict[tuple[int, int | None], list[_MessageSubscription]] = {}
        self._dispatch_filters: list[_MessageSubscription] = []
        self._dispatch_unmatched: deque = deque()
        self._dispatch_error: BaseException | None = None
        self._playback_resync_requests = 0

    def __enter__(self) -> "Camera":
        self.connect()
        self.login()
        self.start_dispatcher()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def connect(self) -> None:
        """Open a transport connection to the camera."""
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
                self.state.update_address(
                    self.config.name,
                    f"{self.sock.addr[0]}:{self.sock.addr[1]}",
                    uid=self.config.uid,
                    transport="udp-relay",
                )
            return
        host, port = resolved[:2]
        self.sock = socket.create_connection((host, port), timeout=self.timeout)
        self.connected_address = (host, port)
        if self.state:
            self.state.update_address(self.config.name, f"{host}:{port}", uid=self.config.uid, transport="tcp")

    def close(self) -> None:
        """Close the current transport connection and clear login state."""
        with self._reconnect_lock:
            self.stop_dispatcher()
            if self.sock:
                self.sock.close()
                self.sock = None
            self.login_xml = ""

    def reconnect(self) -> None:
        """Close, reconnect, and log in again."""
        with self._reconnect_lock:
            restart_dispatcher = self._dispatcher_requested or self._dispatch_thread is not None
            self.stop_dispatcher(clear_requested=not restart_dispatcher)
            self._dispatcher_requested = restart_dispatcher
            if self.sock:
                self.sock.close()
                self.sock = None
            self.login_xml = ""
            self.connect()
            self.login()
            if restart_dispatcher:
                self.start_dispatcher()

    @property
    def dispatcher_active(self) -> bool:
        """Whether the background message dispatcher is active."""
        return self._dispatch_thread is not None and self._dispatch_thread.is_alive()

    def start_dispatcher(self) -> "Camera":
        """Start the single-session reply dispatcher for concurrent API use."""
        self._dispatcher_requested = True
        if self.dispatcher_active:
            return self
        self._dispatch_stop.clear()
        self._dispatch_error = None
        self._dispatch_thread = threading.Thread(
            target=self._dispatch_loop,
            name=f"pyneolink-dispatch-{self.config.name}",
            daemon=True,
        )
        self._dispatch_thread.start()
        return self

    def stop_dispatcher(self, *, clear_requested: bool = True) -> None:
        """Stop the background message dispatcher if it is running."""
        if clear_requested:
            self._dispatcher_requested = False
        thread = self._dispatch_thread
        if thread is None:
            with self._dispatch_lock:
                self._dispatch_waiters.clear()
                self._dispatch_filters.clear()
                self._dispatch_unmatched.clear()
                self._dispatch_error = None
            return
        self._dispatch_stop.set()
        if thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._dispatch_thread = None
        with self._dispatch_lock:
            subscriptions = [waiter for waiters in self._dispatch_waiters.values() for waiter in waiters]
            subscriptions.extend(self._dispatch_filters)
            self._dispatch_waiters.clear()
            self._dispatch_filters.clear()
            self._dispatch_unmatched.clear()
            self._dispatch_error = None
        for subscription in subscriptions:
            subscription.close()

    @property
    def online_required(self) -> bool:
        return self._online_required > 0

    def require_online(self) -> CameraOnlineLease:
        """Keep this camera's managed session online within a context."""
        return CameraOnlineLease(self)

    def keepalive(self, *, timeout: float = 0.05) -> str:
        """Run one lightweight keepalive/maintenance cycle.

        :param timeout: Maximum time in seconds to wait for a camera packet.
        """
        self.ensure_connected()
        if hasattr(self.sock, "maintain"):
            self.sock.maintain()
        if self._should_use_dispatcher():
            self._ensure_dispatcher_active()
            self.send(MSG.UDP_KEEPALIVE, channel_id=0, msg_num=0)
            return "sent"
        try:
            msg = self._recv(timeout=timeout)
        except TimeoutError:
            return "timeout"
        return f"msg_id={msg.header.msg_id} msg_num={msg.header.msg_num} response={msg.header.response_code}"

    def subscribe_messages(
        self,
        msg_id: int,
        msg_num: int | None = None,
        *,
        maxsize: int = 100,
    ) -> "_MessageSubscription":
        """Subscribe to replies by message id and optional message number.

        Exact `(msg_id, msg_num)` subscriptions separate request/reply traffic.
        A `None` message number subscribes to unsolicited events of one type.
        """
        self._dispatcher_requested = True
        subscription = _MessageSubscription(self, msg_id, msg_num, maxsize=maxsize)
        key = (msg_id, msg_num)
        with self._dispatch_lock:
            self._dispatch_waiters.setdefault(key, []).append(subscription)
            remaining = deque()
            while self._dispatch_unmatched:
                message = self._dispatch_unmatched.popleft()
                if _message_matches(message, msg_id, msg_num):
                    subscription.put(message)
                else:
                    remaining.append(message)
            self._dispatch_unmatched = remaining
        self._ensure_dispatcher_active()
        return subscription

    def subscribe_matching(
        self,
        predicate: Callable[[Message], bool],
        *,
        maxsize: int = 100,
    ) -> "_MessageSubscription":
        """Subscribe to messages accepted by a dispatcher-side predicate.

        Predicate subscriptions are intended for multipart protocol operations
        whose continuation packets may use several message ids or numbers.
        """
        self._dispatcher_requested = True
        subscription = _MessageSubscription(self, predicate=predicate, maxsize=maxsize)
        with self._dispatch_lock:
            self._dispatch_filters.append(subscription)
            remaining = deque()
            while self._dispatch_unmatched:
                message = self._dispatch_unmatched.popleft()
                if subscription.matches(message):
                    subscription.put(message)
                else:
                    remaining.append(message)
            self._dispatch_unmatched = remaining
        self._ensure_dispatcher_active()
        return subscription

    def request_messages(
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
        response_msg_id: int | None = None,
        matcher: Callable[[Message, int], bool] | None = None,
        maxsize: int = 100,
    ) -> "_MessageExchange":
        """Create a dispatcher-managed request and multipart reply context.

        The context registers its routing rule before sending the request, so
        even immediate camera replies cannot fall into the unmatched queue.
        Use `matcher` for protocol exchanges whose continuation packets change
        message id or message number.
        """
        return _MessageExchange(
            self,
            msg_id,
            payload,
            extension=extension,
            binary_reply=binary_reply,
            msg_class=msg_class,
            channel_id=channel_id,
            msg_num=msg_num,
            stream_type=stream_type,
            response_msg_id=response_msg_id,
            matcher=matcher,
            maxsize=maxsize,
        )

    def login(self, max_encryption: str = "aes") -> str:
        """Log in and return the raw login XML.

        :param max_encryption: Highest encryption mode to request from the
            camera. `aes` is the normal/default choice.
        """
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
        """Return normalized camera information.

        :param include_sensitive: Include sensitive fields such as secrets when
            `True`. They are redacted by default.
        """
        self.ensure_connected()
        info = xml_to_dict(self.login_xml)
        if not include_sensitive:
            redact_sensitive(info)
        return {
            "name": self.config.name,
            "uid": self.config.uid or self.get_uid(),
            "connected_address": f"{self.connected_address[0]}:{self.connected_address[1]}"
            if self.connected_address
            else None,
            "device": info,
        }

    def sd_card(self) -> SdCard:
        """Return the SD-card helper for listing and downloading recordings."""
        return SdCard(self)

    def get_uid(self) -> str | None:
        """Read the camera UID if the camera exposes it."""
        self.ensure_connected()
        reply = self.command(MSG.UID)
        return find_text(reply.xml_root, "uid") or find_text(reply.xml_root, "UID")

    def reboot(self) -> None:
        """Send the camera reboot command."""
        self.ensure_connected()
        self.command(MSG.REBOOT)

    def led(self, value: str | None = None) -> dict:
        """Read or set the IR/LED mode.

        :param value: `None` to read status, otherwise `on`, `off`, or `auto`.
        """
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

    def snapshot(
        self,
        *,
        out: str | Path | None = None,
        retry_on_timeout: bool = True,
        reconnect_retries: int = 1,
    ) -> bytes | Path:
        """Capture a JPEG snapshot.

        :param out: Optional file path or directory. When omitted, bytes are
            returned. When a directory is provided, the camera file name is used.
        :param retry_on_timeout: Reconnect and retry the whole snapshot request
            when the current UDP session times out.
        :param reconnect_retries: Number of reconnect attempts for stale snapshot
            sessions. The default retries once.
        """
        attempts = max(0, reconnect_retries if retry_on_timeout else 0) + 1
        last_error: TimeoutError | None = None
        for attempt in range(attempts):
            try:
                return self._snapshot_once(out=out)
            except TimeoutError as exc:
                last_error = exc
                if attempt >= attempts - 1:
                    break
                if self.debug:
                    print(msg.Log.Pyneolink.format(message="snapshot timed out; reconnecting camera session"))
                self.reconnect()
        if last_error is not None:
            raise last_error
        raise TimeoutError(msg.Error.TimedOutResponse.format(msg_id=MSG.SNAP, msg_num="?"))

    def _snapshot_once(
        self,
        *,
        out: str | Path | None,
    ) -> bytes | Path:
        def matches_snapshot(message, sent_msg_num: int) -> bool:
            return message.header.msg_id == MSG.SNAP and (
                message.header.msg_num == sent_msg_num or message.xml_root is None
            )

        with self.request_messages(
            MSG.SNAP,
            payloads.snapshot.format(
                channel_id=self.config.channel_id,
                stream_type="main",
            ),
            extension=payloads.extension.format(channel_id=self.config.channel_id),
            matcher=matches_snapshot,
            maxsize=4096,
        ) as replies:
            info = replies.recv(timeout=self.timeout)
            if info.header.response_code != 200:
                raise ProtocolError(msg.Error.SnapshotInfoFailed.format(response_code=info.header.response_code))

            file_name, expected_size = parse_snapshot_info(info.xml_root)
            data = bytearray()
            while True:
                reply = replies.recv(timeout=self.timeout)
                if reply.payload:
                    data.extend(reply.payload)
                if reply.header.response_code == 201:
                    break
                if reply.header.response_code != 200:
                    raise ProtocolError(msg.Error.SnapshotDataFailed.format(response_code=reply.header.response_code))

        if expected_size is not None and len(data) != expected_size:
            raise ProtocolError(
                msg.Error.SnapshotSizeMismatch.format(actual_size=len(data), expected_size=expected_size)
            )

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
        """Record the live stream locally as MPEG-TS.

        :param out: Output file path or directory.
        :param duration: Seconds to record. When omitted, a running
            `StreamRecorder` is returned and the caller must stop it.
        :param stream: Stream to record, for example `mainStream` or
            `subStream`.
        """
        self.ensure_connected()
        recorder = StreamRecorder(self, out=out, stream=stream, duration=duration).start()
        if duration is not None:
            return recorder.wait()
        return recorder

    def battery(self) -> Battery:
        """Return the battery helper."""
        return Battery(self)

    def motion(self, *, channel_id: int | None = None) -> Motion:
        """Return the motion helper.

        :param channel_id: Optional channel override. Defaults to the camera
            config channel.
        """
        return Motion(self, channel_id=channel_id)

    def motion_status(self, *, timeout: float = 3.0, channel_id: int | None = None) -> dict:
        """Return one motion status snapshot.

        :param timeout: Seconds to wait for a status reply.
        :param channel_id: Optional channel override.
        """
        return self.motion(channel_id=channel_id).status(timeout=timeout)

    def voice(self) -> Voice:
        """Return the voice/talk helper."""
        return Voice(self)

    def ptz(self, *, channel_id: int | None = None) -> Ptz:
        """Return the stored PTZ-preset helper.

        :param channel_id: Optional PTZ channel override. Defaults to the
            camera config channel.
        """
        return Ptz(self, channel_id=channel_id)

    def settings(self) -> Settings:
        """Return the settings helper."""
        return Settings(self)

    def battery_xml(self, *, mode: str = "reconnect") -> str | None:
        """Return raw battery XML.

        :param mode: `reconnect` closes between requests; `online` keeps the
            camera session alive while polling.
        """
        return self.battery().raw(mode=mode)

    def battery_info(self, *, mode: str = "reconnect") -> dict:
        """Return parsed battery information.

        :param mode: `reconnect` closes between requests; `online` keeps the
            camera session alive while polling.
        """
        return self.battery().info(mode=mode)

    def watch_battery(self, interval: float = 60.0, *, count: int | None = None, mode: str = "reconnect"):
        """Yield battery information repeatedly.

        :param interval: Delay between polls in seconds.
        :param count: Optional maximum number of updates.
        :param mode: `reconnect` or `online` polling mode.
        """
        yield from self.battery().watch(interval=interval, count=count, mode=mode)

    def command(
        self,
        msg_id: int,
        payload: bytes = b"",
        *,
        extension: bytes = b"",
        retry_on_timeout: bool = True,
        reconnect_retries: int = 1,
    ) -> Message:
        """Send a command and wait for the matching reply.

        :param msg_id: Baichuan message id.
        :param payload: Optional command payload bytes.
        :param extension: Optional Baichuan extension bytes.
        :param retry_on_timeout: Reconnect and retry after a timeout or closed
            transport before the matching reply arrives.
        :param reconnect_retries: Number of reconnect attempts for a stale or
            closed session. The default retries once.
        """
        attempts = max(0, reconnect_retries if retry_on_timeout else 0) + 1
        last_error: BaseException | None = None
        for attempt in range(attempts):
            try:
                with self.request_messages(
                    msg_id,
                    payload,
                    extension=extension,
                    maxsize=10,
                ) as replies:
                    return replies.recv(timeout=self.timeout)
            except (TimeoutError, EOFError, OSError) as exc:
                last_error = exc
                if attempt >= attempts - 1:
                    break
                if self.debug:
                    print(msg.Log.Pyneolink.format(message=f"command {msg_id} timed out; reconnecting camera session"))
                self.reconnect()
        if last_error is not None:
            raise last_error
        raise TimeoutError(msg.Error.TimedOutResponse.format(msg_id=msg_id, msg_num="?"))

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
        """Send one Baichuan packet and return its message number.

        :param msg_id: Baichuan message id.
        :param payload: Optional payload bytes.
        :param extension: Optional extension bytes.
        :param binary_reply: Mark the reply as binary for payload decoding.
        :param msg_class: Baichuan message class.
        :param channel_id: Optional channel override.
        :param msg_num: Optional explicit message number.
        :param stream_type: Raw Baichuan stream type code.
        """
        self.ensure_connected()
        sent_msg_num = self._next_msg() if msg_num is None else msg_num
        if binary_reply:
            self.binary_msg_nums.add(sent_msg_num)
        self._send_modern(
            msg_id,
            sent_msg_num,
            payload,
            extension=extension,
            msg_class=msg_class,
            channel_id=channel_id,
            stream_type=stream_type,
        )
        return sent_msg_num

    def start_stream(self, stream: str = "mainStream") -> int:
        """Start live stream payload delivery.

        :param stream: Stream alias/name such as `high`, `low`, `mainStream`,
            or `subStream`.
        """
        stream_name, stream_code, handle = stream_params(stream)
        payload = payloads.preview_start.format(
            channel_id=self.config.channel_id, handle=handle, stream_type=stream_name
        )
        with self.request_messages(
            MSG.VIDEO,
            payload,
            stream_type=stream_code,
            maxsize=10,
        ) as replies:
            reply_msg = replies.recv(timeout=self.timeout)
            if reply_msg.header.response_code != 200:
                raise ProtocolError(msg.Error.StreamStartFailed.format(response_code=reply_msg.header.response_code))
            self.binary_msg_nums.add(replies.msg_num)
            return replies.msg_num

    def stop_stream(self, stream: str = "mainStream", msg_num: int | None = None) -> None:
        """Stop live stream payload delivery.

        :param stream: Stream alias/name used to start the stream.
        :param msg_num: Optional stream message number returned by
            `start_stream()`.
        """
        _stream_name, stream_code, handle = stream_params(stream)
        payload = payloads.preview_stop.format(channel_id=self.config.channel_id, handle=handle)
        with self.request_messages(
            MSG.VIDEO_STOP,
            payload,
            msg_num=msg_num,
            stream_type=stream_code,
            maxsize=10,
        ) as replies:
            self.binary_msg_nums.discard(replies.msg_num)
            try:
                reply_msg = replies.recv(timeout=min(self.timeout, 2.0))
            except TimeoutError:
                return
            if reply_msg.header.response_code not in (0, 200) and self.debug:
                print(msg.Log.StreamStopReturned.format(response_code=reply_msg.header.response_code))

    def continue_preview(
        self,
        stream: str = "mainStream",
        *,
        enabled: bool = True,
        retry_on_timeout: bool = True,
        reconnect_retries: int = 1,
    ) -> Message:
        """Request live preview continuation for battery-camera sessions.

        This mirrors the official client's `LongTimePreview` command. It is
        useful for experiments around battery-camera live-view session limits.

        :param stream: Stream alias/name such as `high`, `low`, `mainStream`,
            or `subStream`.
        :param enabled: Send `continuePreview` as 1 when true, otherwise 0.
        :param retry_on_timeout: Reconnect and retry when the command reply
            times out.
        :param reconnect_retries: Number of reconnect attempts after timeout.
        """
        self.ensure_connected()
        stream_name, _stream_code, _handle = stream_params(stream)
        payload = payloads.long_time_preview.format(
            channel_id=self.config.channel_id,
            stream_type=stream_name,
            continue_preview=1 if enabled else 0,
        )
        return self.command(
            MSG.LONG_TIME_PREVIEW,
            payload,
            retry_on_timeout=retry_on_timeout,
            reconnect_retries=reconnect_retries,
        )

    def read_stream_payloads(
        self,
        stream: str = "mainStream",
        *,
        reconnect: bool = True,
        stall_timeout: float = DEFAULT_STREAM_STALL_TIMEOUT,
    ) -> Iterator[bytes]:
        """Yield raw BCMedia payloads from a live stream.

        :param stream: Stream alias/name such as `high`, `low`, `mainStream`,
            or `subStream`.
        :param reconnect: Reconnect and restart the stream after recoverable
            transport errors or a stalled stream.
        :param stall_timeout: Seconds without video payloads before the stream
            is treated as stalled.

        The stream and every concurrent helper share this camera's dispatcher.
        Recoverable failures replace the transport session and re-register the
        stream route without exposing session management to callers.
        """
        with self.require_online():
            while True:
                try:
                    self.ensure_connected()
                    yield from self._read_stream_payloads_dispatched(stream, stall_timeout=stall_timeout)
                    return
                except RECOVERABLE_STREAM_ERRORS as exc:
                    if not reconnect:
                        raise
                    if self.debug:
                        print(msg.Log.Pyneolink.format(message=f"stream stalled; reconnecting after {exc!r}"))
                    self.reconnect()

    def _read_stream_payloads_dispatched(
        self,
        stream: str = "mainStream",
        *,
        stall_timeout: float,
    ) -> Iterator[bytes]:
        stream_name, stream_code, handle = stream_params(stream)
        payload = payloads.preview_start.format(
            channel_id=self.config.channel_id, handle=handle, stream_type=stream_name
        )
        with self.request_messages(
            MSG.VIDEO,
            payload,
            stream_type=stream_code,
            maxsize=200,
        ) as replies:
            msg_num = replies.msg_num
            start_reply = replies.recv(timeout=self.timeout)
            if start_reply.header.response_code != 200:
                raise ProtocolError(msg.Error.StreamStartFailed.format(response_code=start_reply.header.response_code))
            self.binary_msg_nums.add(msg_num)
            next_keepalive_at = time.monotonic() + 0.75
            last_payload_at = time.monotonic()
            try:
                while True:
                    now = time.monotonic()
                    if now >= next_keepalive_at:
                        self.send(MSG.UDP_KEEPALIVE, channel_id=0, msg_num=0)
                        next_keepalive_at = now + 0.75
                    try:
                        message = replies.recv(timeout=1.0)
                    except TimeoutError:
                        self._raise_if_stream_stalled(last_payload_at, stall_timeout)
                        continue
                    if message.payload:
                        last_payload_at = time.monotonic()
                        yield message.payload
                    else:
                        self._raise_if_stream_stalled(last_payload_at, stall_timeout)
            finally:
                try:
                    self.stop_stream(stream, msg_num)
                except Exception as exc:
                    if self.debug:
                        print(msg.Log.StreamStopCloseFailed.format(exc_type=type(exc).__name__, exc=exc))

    def _raise_if_stream_stalled(self, last_payload_at: float, stall_timeout: float) -> None:
        if time.monotonic() - last_payload_at >= max(0.1, stall_timeout):
            raise TimeoutError("stream payload stalled")

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
        """Connect, log in, and start the shared reply dispatcher if needed."""
        with self._reconnect_lock:
            if self.sock is None:
                self.connect()
            if not self.login_xml:
                self.login()
            self.start_dispatcher()

    def _next_msg(self) -> int:
        with self._send_lock:
            self.msg_num = (self.msg_num + 1) & 0xFFFF
            if self.msg_num == 0:
                self.msg_num = 1
            return self.msg_num

    def _send(self, data: bytes) -> None:
        if self.sock is None:
            raise RuntimeError(msg.Error.CameraNotConnected)
        with self._send_lock:
            self.sock.sendall(data)

    def _send_modern(
        self,
        msg_id: int,
        msg_num: int,
        payload: bytes = b"",
        *,
        extension: bytes = b"",
        binary_reply: bool = False,
        msg_class: int = MSG_CLASS.MODERN,
        channel_id: int | None = None,
        stream_type: int = 0,
    ) -> None:
        if binary_reply:
            self.binary_msg_nums.add(msg_num)
        self._send(
            encode_modern(
                msg_id,
                msg_num,
                payload,
                extension=extension,
                channel_id=self.config.channel_id if channel_id is None else channel_id,
                msg_class=msg_class,
                stream_type=stream_type,
                cipher=self.cipher,
            )
        )

    def _recv(
        self,
        timeout: float | None = None,
        *,
        binary_playback_331: bool = False,
    ) -> Message:
        if self._should_use_dispatcher():
            self._ensure_dispatcher_active()
            return self._recv_dispatched(timeout=timeout)
        return self._recv_direct(timeout=timeout, binary_playback_331=binary_playback_331)

    def _should_use_dispatcher(self) -> bool:
        return self._dispatcher_requested and threading.current_thread() is not self._dispatch_thread

    def _ensure_dispatcher_active(self) -> None:
        if self._dispatcher_requested and not self.dispatcher_active:
            with self._reconnect_lock:
                if self._dispatcher_requested and not self.dispatcher_active:
                    self.start_dispatcher()

    def _recv_direct(
        self,
        timeout: float | None = None,
        *,
        binary_playback_331: bool = False,
    ) -> Message:
        if self.sock is None:
            raise RuntimeError(msg.Error.CameraNotConnected)
        message = recv_message(
            self.sock,
            self.cipher,
            timeout=self.timeout if timeout is None else timeout,
            binary_msg_nums=self.binary_msg_nums,
            binary_playback_331=binary_playback_331,
            recover_invalid_magic=self._playback_resync_requests > 0,
        )
        if message.header.msg_id == MSG.UDP_KEEPALIVE:
            self._reply_keepalive(message)
        return message

    def _recv_dispatched(self, timeout: float | None = None) -> Message:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._dispatch_lock:
                if self._dispatch_error is not None:
                    raise self._dispatch_error
                if self._dispatch_unmatched:
                    return self._dispatch_unmatched.popleft()
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(msg.Error.UdpBaichuanTimeout)
                time.sleep(min(0.01, remaining))
            else:
                time.sleep(0.01)

    def _dispatch_loop(self) -> None:
        while not self._dispatch_stop.is_set():
            try:
                message = self._recv_direct(timeout=0.5, binary_playback_331=True)
            except TimeoutError:
                continue
            except BaseException as exc:
                with self._dispatch_lock:
                    self._dispatch_error = exc
                    subscriptions = [waiter for waiters in self._dispatch_waiters.values() for waiter in waiters]
                    subscriptions.extend(self._dispatch_filters)
                    self._dispatch_waiters.clear()
                    self._dispatch_filters.clear()
                for subscription in subscriptions:
                    subscription.fail(exc)
                return
            self._dispatch_message(message)

    def _dispatch_message(self, message: Message) -> None:
        if message.header.msg_id == MSG.UDP_KEEPALIVE:
            return
        with self._dispatch_lock:
            delivered = False
            for key in ((message.header.msg_id, message.header.msg_num), (message.header.msg_id, None)):
                for waiter in list(self._dispatch_waiters.get(key, [])):
                    if waiter.active:
                        waiter.put(message)
                        delivered = True
            for waiter in list(self._dispatch_filters):
                if waiter.active and waiter.matches(message):
                    waiter.put(message)
                    delivered = True
            if delivered:
                return
            self._dispatch_unmatched.append(message)
            while len(self._dispatch_unmatched) > 200:
                self._dispatch_unmatched.popleft()

    def _unsubscribe(self, subscription: "_MessageSubscription") -> None:
        if subscription.predicate is not None:
            with self._dispatch_lock:
                if subscription in self._dispatch_filters:
                    self._dispatch_filters.remove(subscription)
            return
        key = (subscription.msg_id, subscription.msg_num)
        with self._dispatch_lock:
            waiters = self._dispatch_waiters.get(key)
            if not waiters:
                return
            if subscription in waiters:
                waiters.remove(subscription)
            if not waiters:
                self._dispatch_waiters.pop(key, None)

    def _reply_keepalive(self, keepalive_msg: Message) -> None:
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


class _MessageSubscription:
    """Thread-safe queue for one dispatcher routing rule."""

    def __init__(
        self,
        camera: Camera,
        msg_id: int | None = None,
        msg_num: int | None = None,
        *,
        predicate: Callable[[Message], bool] | None = None,
        maxsize: int = 100,
    ) -> None:
        self.camera = camera
        self.msg_id = msg_id
        self.msg_num = msg_num
        self.predicate = predicate
        self.active = True
        self._error: BaseException | None = None
        self._queue: queue.Queue[Message] = queue.Queue(maxsize=max(1, maxsize))

    def __enter__(self) -> "_MessageSubscription":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def put(self, message: Message) -> None:
        if not self.active:
            return
        while True:
            try:
                self._queue.put_nowait(message)
                return
            except queue.Full:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    return

    def matches(self, message: Message) -> bool:
        """Return whether this subscription accepts a message."""
        if self.predicate is not None:
            return self.predicate(message)
        if self.msg_id is None:
            return False
        return _message_matches(message, self.msg_id, self.msg_num)

    def recv(self, *, timeout: float | None = None) -> Message:
        if not self._queue.empty():
            return self._queue.get_nowait()
        if self._error is not None:
            raise self._error
        if not self.active and self._queue.empty():
            raise TimeoutError(msg.Error.EventListenerClosed)
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            if self._error is not None:
                raise self._error
            if not self.active:
                raise TimeoutError(msg.Error.EventListenerClosed) from None
            raise TimeoutError(msg.Error.UdpBaichuanTimeout) from None

    def fail(self, error: BaseException) -> None:
        """Close the subscription and preserve the dispatcher error."""
        self._error = error
        self.active = False

    def close(self) -> None:
        if self.active:
            self.active = False
            self.camera._unsubscribe(self)


class _MessageExchange:
    """Context manager that subscribes before sending one request."""

    def __init__(
        self,
        camera: Camera,
        msg_id: int,
        payload: bytes,
        *,
        extension: bytes,
        binary_reply: bool,
        msg_class: int,
        channel_id: int | None,
        msg_num: int | None,
        stream_type: int,
        response_msg_id: int | None,
        matcher: Callable[[Message, int], bool] | None,
        maxsize: int,
    ) -> None:
        self.camera = camera
        self.msg_id = msg_id
        self.payload = payload
        self.extension = extension
        self.binary_reply = binary_reply
        self.msg_class = msg_class
        self.channel_id = channel_id
        self.msg_num = msg_num
        self.stream_type = stream_type
        self.response_msg_id = msg_id if response_msg_id is None else response_msg_id
        self.matcher = matcher
        self.maxsize = maxsize
        self._subscription: _MessageSubscription | None = None
        self._playback_resync_active = False

    def __enter__(self) -> "_MessageExchange":
        self.camera.ensure_connected()
        if self.msg_id == MSG.FILE_PLAYBACK:
            with self.camera._dispatch_lock:
                self.camera._playback_resync_requests += 1
            self._playback_resync_active = True
        if self.msg_num is None:
            self.msg_num = self.camera._next_msg()
        sent_msg_num = self.msg_num
        if self.matcher is None:
            self._subscription = self.camera.subscribe_messages(
                self.response_msg_id,
                sent_msg_num,
                maxsize=self.maxsize,
            )
        else:
            self._subscription = self.camera.subscribe_matching(
                lambda message: self.matcher(message, sent_msg_num),
                maxsize=self.maxsize,
            )
        try:
            self.camera._send_modern(
                self.msg_id,
                sent_msg_num,
                self.payload,
                extension=self.extension,
                binary_reply=self.binary_reply,
                msg_class=self.msg_class,
                channel_id=self.channel_id,
                stream_type=self.stream_type,
            )
        except BaseException:
            self._subscription.close()
            self._subscription = None
            self._disable_playback_resync()
            raise
        return self

    def __exit__(self, *exc: object) -> None:
        if self._subscription is not None:
            self._subscription.close()
            self._subscription = None
        self._disable_playback_resync()

    def _disable_playback_resync(self) -> None:
        if not self._playback_resync_active:
            return
        with self.camera._dispatch_lock:
            self.camera._playback_resync_requests = max(0, self.camera._playback_resync_requests - 1)
        self._playback_resync_active = False

    def recv(self, *, timeout: float | None = None) -> Message:
        """Receive the next reply routed to this exchange."""
        if self._subscription is None:
            raise RuntimeError("Message exchange is not active")
        return self._subscription.recv(timeout=timeout)


def _message_matches(message: Message, msg_id: int, msg_num: int | None) -> bool:
    return message.header.msg_id == msg_id and (msg_num is None or message.header.msg_num == msg_num)
