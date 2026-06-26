from __future__ import annotations

from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
import time
import xml.etree.ElementTree as ET

from .core.bc import ProtocolError, find_text
from .core.const import EVENTS, MSG, msg


class Motion:
    def __init__(self, camera, *, channel_id: int | None = None) -> None:
        self.camera = camera
        self.channel_id = camera.config.channel_id if channel_id is None else channel_id

    def status(self, *, timeout: float = 3.0) -> dict:
        event, known = self.watch().status(timeout=timeout)
        return event.to_dict(known=known)

    def watch(self, *, duration: float | None = None, keepalive_interval: float = 0.75) -> "CameraEvents":
        return CameraEvents(
            self.camera,
            channel_id=self.channel_id,
            duration=duration,
            keepalive_interval=keepalive_interval,
        )


@dataclass(frozen=True)
class CameraEvent:
    type: EVENTS
    active: bool
    channel_id: int | None = None
    status: str | None = None
    ai_type: str | None = None
    recording: int | None = None
    timestamp: int | None = None
    raw: dict[str, str | None] = field(default_factory=dict)
    received_at: datetime = field(default_factory=datetime.now)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, EVENTS):
            return self.type == other
        return super().__eq__(other)

    def __str__(self) -> str:
        state = "start" if self.active else "stop"
        return f"{self.type.value} {state}"

    def with_type(self, event_type: EVENTS) -> "CameraEvent":
        return CameraEvent(
            type=event_type,
            active=self.active,
            channel_id=self.channel_id,
            status=self.status,
            ai_type=self.ai_type,
            recording=self.recording,
            timestamp=self.timestamp,
            raw=self.raw,
            received_at=self.received_at,
        )

    def to_dict(self, *, known: bool = True) -> dict:
        return {
            "type": self.type.value,
            "active": self.active,
            "known": known,
            "channel_id": self.channel_id,
            "status": self.status,
            "ai_type": self.ai_type,
            "recording": self.recording,
            "timestamp": self.timestamp,
            "received_at": self.received_at.isoformat(),
            "raw": self.raw,
        }


class CameraEvents(Iterator[CameraEvent]):
    def __init__(
        self,
        camera,
        *,
        channel_id: int | None = None,
        duration: float | None = None,
        keepalive_interval: float = 0.75,
    ) -> None:
        self.camera = camera
        self.channel_id = camera.config.channel_id if channel_id is None else channel_id
        self.duration = duration
        self.keepalive_interval = keepalive_interval
        self._lease = None
        self._active = False
        self._pending: deque[CameraEvent] = deque()
        self._next_keepalive_at = 0.0
        self._deadline: float | None = None
        self._last_active_type: EVENTS | None = None

    def __enter__(self) -> "CameraEvents":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __iter__(self) -> "CameraEvents":
        self.start()
        return self

    def __next__(self) -> CameraEvent:
        self.start()
        if self._pending:
            return self._pending.popleft()

        while self._active:
            now = time.monotonic()
            if self._deadline is not None and now >= self._deadline:
                self.close()
                raise StopIteration
            if now >= self._next_keepalive_at:
                self.camera.send(MSG.UDP_KEEPALIVE, channel_id=0, msg_num=0)
                self._next_keepalive_at = now + self.keepalive_interval
            try:
                recv_timeout = 1.0
                if self._deadline is not None:
                    recv_timeout = min(recv_timeout, max(0.0, self._deadline - time.monotonic()))
                reply = self.camera._recv(timeout=recv_timeout)
            except TimeoutError:
                continue
            if reply.header.msg_id != MSG.MOTION:
                continue
            self._pending.extend(self._normalize_events(parse_motion_events(reply.xml_root, channel_id=self.channel_id)))
            if self._pending:
                return self._pending.popleft()
        raise StopIteration

    def start(self) -> "CameraEvents":
        if self._active:
            return self
        self._lease = self.camera.require_online()
        self._lease.__enter__()
        reply = self.camera.command(MSG.MOTION_REQUEST)
        if reply.header.response_code != 200:
            self.close()
            raise ProtocolError(msg.Error.EventStartFailed.format(response_code=reply.header.response_code))
        self._active = True
        self._deadline = None if self.duration is None else time.monotonic() + max(0.0, self.duration)
        self._next_keepalive_at = time.monotonic() + self.keepalive_interval
        return self

    def close(self) -> None:
        self._active = False
        self._pending.clear()
        self._deadline = None
        self._last_active_type = None
        if self._lease is not None:
            self._lease.__exit__(None, None, None)
            self._lease = None

    def status(self, *, timeout: float = 3.0) -> tuple[CameraEvent, bool]:
        self.start()
        deadline = time.monotonic() + max(0.0, timeout)
        try:
            while time.monotonic() <= deadline:
                now = time.monotonic()
                if now >= self._next_keepalive_at:
                    self.camera.send(MSG.UDP_KEEPALIVE, channel_id=0, msg_num=0)
                    self._next_keepalive_at = now + self.keepalive_interval
                try:
                    reply = self.camera._recv(timeout=min(0.5, max(0.0, deadline - time.monotonic())))
                except TimeoutError:
                    continue
                if reply.header.msg_id != MSG.MOTION:
                    continue
                events = self._normalize_events(parse_motion_events(reply.xml_root, channel_id=self.channel_id))
                if events:
                    return events[-1], True
            return CameraEvent(EVENTS.none, active=False, channel_id=self.channel_id), False
        finally:
            self.close()

    def _normalize_events(self, events: list[CameraEvent]) -> list[CameraEvent]:
        normalized: list[CameraEvent] = []
        for event in events:
            if event.active:
                if event.type != EVENTS.none:
                    self._last_active_type = event.type
                normalized.append(event)
            elif event.type == EVENTS.none and self._last_active_type is not None:
                normalized.append(event.with_type(self._last_active_type))
                self._last_active_type = None
            else:
                normalized.append(event)
        return normalized


def parse_motion_events(root: ET.Element | None, *, channel_id: int = 0) -> list[CameraEvent]:
    if root is None:
        return []
    events: list[CameraEvent] = []
    for alarm in root.findall(".//AlarmEvent"):
        event_channel_id = _int_text(alarm, "channelId")
        if event_channel_id is not None and event_channel_id != channel_id:
            continue
        status = find_text(alarm, "status")
        ai_type = find_text(alarm, "AItype") or find_text(alarm, "aiType")
        recording = _int_text(alarm, "recording")
        timestamp = _int_text(alarm, "timeStamp")
        event_type, active = _event_type(status, ai_type)
        events.append(
            CameraEvent(
                type=event_type,
                active=active,
                channel_id=event_channel_id,
                status=status,
                ai_type=ai_type,
                recording=recording,
                timestamp=timestamp,
                raw={
                    "channelId": None if event_channel_id is None else str(event_channel_id),
                    "status": status,
                    "AItype": ai_type,
                    "recording": None if recording is None else str(recording),
                    "timeStamp": None if timestamp is None else str(timestamp),
                },
            )
        )
    return events


def _event_type(status: str | None, ai_type: str | None) -> tuple[EVENTS, bool]:
    ai = (ai_type or "").strip().lower()
    motion_status = (status or "").strip().lower()
    if ai in ("people", "person", "human"):
        return EVENTS.human, True
    if ai in ("vehicle", "car", "auto"):
        return EVENTS.vehicle, True
    if ai and ai != "none":
        return EVENTS.unknown, True
    if motion_status and motion_status not in ("none", "0"):
        return EVENTS.motion, True
    return EVENTS.none, False


def _int_text(root: ET.Element, tag: str) -> int | None:
    value = find_text(root, tag)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None
