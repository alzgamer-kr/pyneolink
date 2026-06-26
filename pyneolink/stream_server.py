from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterable
from urllib.parse import quote, unquote, urlparse

from .camera import Camera
from .config import Config, config_from_dict, load_config
from .core.const import msg
from .core.media import MediaParser, MediaPacket


_STREAM_END = object()
_STREAM_QUEUE_TIMEOUT = 0.25
_DEFAULT_HLS_BUFFER_MB = 100
_DEFAULT_HLS_SEGMENT_SECONDS = 2.0


class StreamServer:
    def __init__(
        self,
        config: str | dict | Config = "config.json",
        *,
        host: str | None = None,
        port: int | None = None,
        state_path: str | None = ".pyneolink_state.json",
        debug: bool = False,
        buffer_seconds: float = 1.0,
        hls_buffer_mb: int = _DEFAULT_HLS_BUFFER_MB,
        hls_segment_seconds: float = _DEFAULT_HLS_SEGMENT_SECONDS,
    ) -> None:
        self.config = _coerce_config(config)
        self.host = host if host is not None else self.config.bind
        self.port = port if port is not None else self.config.bind_port
        self.state_path = state_path
        self.debug = debug
        self.buffer_seconds = max(buffer_seconds, 0.0)
        self.hls_buffer_bytes = max(int(hls_buffer_mb), 1) * 1024 * 1024
        self.hls_segment_seconds = max(hls_segment_seconds, 0.5)

    def urls(self, *, host: str | None = None) -> list[str]:
        display_host = host or _display_host(self.host)
        urls = []
        for camera in self.config.cameras or []:
            encoded_name = quote(camera.name, safe="")
            urls.append(f"http://{display_host}:{self.port}/{encoded_name}/high")
            urls.append(f"http://{display_host}:{self.port}/{encoded_name}/low")
            urls.append(f"http://{display_host}:{self.port}/{encoded_name}/high/hls.m3u8")
            urls.append(f"http://{display_host}:{self.port}/{encoded_name}/low/hls.m3u8")
        return urls

    def serve_forever(self) -> None:
        server = _StreamServer((self.host, self.port), _StreamHandler)
        server.config = self.config
        server.state_path = self.state_path
        server.debug = self.debug
        server.buffer_seconds = self.buffer_seconds
        server.hls_buffer_bytes = self.hls_buffer_bytes
        server.hls_segment_seconds = self.hls_segment_seconds
        server.hls_sessions = {}
        server.hls_sessions_lock = threading.Lock()
        print(msg.Log.Serving.format(host=self.host, port=self.port))
        display_host = _display_host(self.host)
        if display_host != self.host:
            print(msg.Log.OpenLocal.format(host=display_host, port=self.port))
        for url in self.urls(host=display_host):
            print(msg.Log.Url.format(url=url))
        server.serve_forever()


def serve_streams(
    config_path: str = "config.json",
    *,
    host: str | None = None,
    port: int | None = None,
    state_path: str | None = ".pyneolink_state.json",
    debug: bool = False,
    buffer_seconds: float = 1.0,
    hls_buffer_mb: int = _DEFAULT_HLS_BUFFER_MB,
    hls_segment_seconds: float = _DEFAULT_HLS_SEGMENT_SECONDS,
) -> None:
    StreamServer(
        config_path,
        host=host,
        port=port,
        state_path=state_path,
        debug=debug,
        buffer_seconds=buffer_seconds,
        hls_buffer_mb=hls_buffer_mb,
        hls_segment_seconds=hls_segment_seconds,
    ).serve_forever()


def _coerce_config(config: str | dict | Config) -> Config:
    if isinstance(config, Config):
        return config
    if isinstance(config, str):
        return load_config(config)
    return config_from_dict(config)


class _StreamServer(ThreadingHTTPServer):
    daemon_threads = True
    config: Config
    state_path: str | None
    debug: bool
    buffer_seconds: float
    hls_buffer_bytes: int
    hls_segment_seconds: float
    hls_sessions: dict[tuple[str, str], "HlsSession"]
    hls_sessions_lock: threading.Lock


class _StreamHandler(BaseHTTPRequestHandler):
    server: _StreamServer

    def do_GET(self) -> None:
        parts = [unquote(part) for part in urlparse(self.path).path.strip("/").split("/") if part]
        if not parts:
            self._send_index()
            return
        if len(parts) not in (2, 3, 4):
            self.send_error(404, msg.Error.StreamRoute)
            return
        camera_name, quality = parts[0], parts[1]
        try:
            camera_config = _find_camera(self.server.config, camera_name)
            stream = _quality_to_stream(quality)
        except ValueError as exc:
            self.send_error(404, str(exc))
            return

        if len(parts) == 3:
            if parts[2] not in ("hls.m3u8", "playlist.m3u8"):
                self.send_error(404, msg.Error.HlsRoute)
                return
            self._serve_hls_playlist(camera_config, stream)
            return
        if len(parts) == 4:
            if parts[2] != "segments" or not parts[3].endswith(".ts"):
                self.send_error(404, msg.Error.HlsSegmentRoute)
                return
            try:
                sequence = int(parts[3][:-3])
            except ValueError:
                self.send_error(404, msg.Error.InvalidHlsSegmentSequence)
                return
            self._serve_hls_segment(camera_config, stream, sequence)
            return

        camera = Camera(camera_config, state_path=self.server.state_path, debug=self.server.debug)
        parser = MediaParser()
        try:
            camera.__enter__()
            payloads = camera.read_stream_payloads(stream)
            first_packets, codec, fps = _read_until_keyframe(payloads, parser)
            first_packets = _buffer_initial_video(payloads, parser, first_packets, fps, self.server.buffer_seconds)
            if codec in ("H264", "H265"):
                self._serve_mpegts(payloads, parser, first_packets, codec, fps)
            else:
                self._serve_raw(payloads, parser, first_packets, codec)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except OSError as exc:
            if not _is_client_disconnect(exc) and not self.wfile.closed:
                try:
                    self.send_error(502, str(exc))
                except Exception:
                    pass
        except Exception as exc:
            if not self.wfile.closed:
                try:
                    self.send_error(502, str(exc))
                except Exception:
                    pass
        finally:
            camera.close()

    def log_message(self, format: str, *args) -> None:
        if self.server.debug:
            super().log_message(format, *args)

    def _send_index(self) -> None:
        lines = ["PyNeolink live streams", ""]
        for camera in self.server.config.cameras or []:
            encoded_name = quote(camera.name, safe="")
            lines.append(f"/{encoded_name}/high")
            lines.append(f"/{encoded_name}/low")
            lines.append(f"/{encoded_name}/high/hls.m3u8")
            lines.append(f"/{encoded_name}/low/hls.m3u8")
        body = ("\n".join(lines) + "\n").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_hls_playlist(self, camera_config, stream: str) -> None:
        session = self._hls_session(camera_config, stream)
        try:
            playlist = session.playlist()
        except Exception as exc:
            self.send_error(502, str(exc))
            return
        body = playlist.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.apple.mpegurl")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_hls_segment(self, camera_config, stream: str, sequence: int) -> None:
        session = self._hls_session(camera_config, stream)
        segment = session.segment(sequence)
        if segment is None:
            self.send_error(404, msg.Error.HlsSegmentExpired)
            return
        self.send_response(200)
        self.send_header("Content-Type", "video/MP2T")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(segment.data)))
        self.end_headers()
        self.wfile.write(segment.data)

    def _hls_session(self, camera_config, stream: str) -> "HlsSession":
        key = (camera_config.name, stream)
        with self.server.hls_sessions_lock:
            session = self.server.hls_sessions.get(key)
            if session is None:
                session = HlsSession(
                    camera_config,
                    stream,
                    state_path=self.server.state_path,
                    debug=self.server.debug,
                    buffer_bytes=self.server.hls_buffer_bytes,
                    segment_seconds=self.server.hls_segment_seconds,
                )
                self.server.hls_sessions[key] = session
            session.start()
            return session

    def _serve_raw(
        self,
        payloads: Iterable[bytes],
        parser: MediaParser,
        first_packets: list[MediaPacket],
        codec: str | None,
    ) -> None:
        self.send_response(200)
        self.send_header("Content-Type", _raw_content_type(codec))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        for packet in _video_packets(first_packets):
            self.wfile.write(packet.data)
        self.wfile.flush()
        for payload in payloads:
            for packet in _video_packets(parser.feed(payload)):
                self.wfile.write(packet.data)
            self.wfile.flush()

    def _serve_mpegts(
        self,
        payloads: Iterable[bytes],
        parser: MediaParser,
        first_packets: list[MediaPacket],
        codec: str,
        fps: int,
    ) -> None:
        chunks: queue.Queue[object] = queue.Queue(maxsize=_stream_queue_size(fps, self.server.buffer_seconds))
        stop_event = threading.Event()
        producer = threading.Thread(
            target=_produce_mpegts_chunks,
            args=(chunks, stop_event, codec, fps, payloads, parser, first_packets),
            daemon=True,
        )
        producer.start()
        self.send_response(200)
        self.send_header("Content-Type", "video/MP2T")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        started = False
        try:
            while True:
                try:
                    item = chunks.get(timeout=_STREAM_QUEUE_TIMEOUT)
                except queue.Empty:
                    if started:
                        self.wfile.write(_mpegts_null_packet())
                        self.wfile.flush()
                    continue
                if item is _STREAM_END:
                    return
                if isinstance(item, BaseException):
                    raise item
                started = True
                self.wfile.write(item)
                while True:
                    try:
                        item = chunks.get_nowait()
                    except queue.Empty:
                        break
                    if item is _STREAM_END:
                        self.wfile.flush()
                        return
                    if isinstance(item, BaseException):
                        raise item
                    self.wfile.write(item)
                self.wfile.flush()
        finally:
            stop_event.set()
            producer.join(timeout=2.0)


class MpegTsMuxer:
    PAT_PID = 0x0000
    PMT_PID = 0x0100
    VIDEO_PID = 0x0101
    AUDIO_PID = 0x0102

    def __init__(self, codec: str, *, fps: int = 15) -> None:
        self.codec = codec
        self.fps = max(fps, 1)
        self.continuity: dict[int, int] = {}
        self.tables_written = False
        self.video_pts = 0
        self.audio_pts = 0

    def feed(self, packet: MediaPacket) -> Iterable[bytes]:
        if not self.tables_written:
            yield from self.table_packets()

        if packet.kind in ("iframe", "pframe"):
            if packet.timestamp_us is not None:
                self.video_pts = int(packet.timestamp_us * 90_000 / 1_000_000)
            else:
                self.video_pts += 90_000 // self.fps
            pes = _pes_packet(0xE0, self.video_pts, packet.data, unbounded=True)
            yield from self._packetize(self.VIDEO_PID, pes, start=True, pcr=self.video_pts)
        elif packet.kind == "aac" and _looks_like_adts(packet.data):
            self.audio_pts = max(self.audio_pts, self.video_pts)
            pes = _pes_packet(0xC0, self.audio_pts, packet.data)
            self.audio_pts += _aac_duration_90k(packet.data)
            yield from self._packetize(self.AUDIO_PID, pes, start=True)

    def table_packets(self) -> list[bytes]:
        packets: list[bytes] = []
        packets.extend(self._packetize(self.PAT_PID, b"\x00" + _pat_section(self.PMT_PID), start=True))
        packets.extend(self._packetize(self.PMT_PID, b"\x00" + _pmt_section(self.codec, self.VIDEO_PID, self.AUDIO_PID), start=True))
        self.tables_written = True
        return packets

    def _packetize(self, pid: int, payload: bytes, *, start: bool = False, pcr: int | None = None) -> Iterable[bytes]:
        offset = 0
        first = True
        while offset < len(payload):
            include_pcr = pcr is not None and first
            max_payload = 176 if include_pcr else 184
            chunk = payload[offset : offset + min(len(payload) - offset, max_payload)]
            offset += len(chunk)

            adaptation = b""
            if include_pcr or len(chunk) < 184:
                total_adaptation = 184 - len(chunk)
                if total_adaptation > 0:
                    flags = 0x10 if include_pcr else 0x00
                    body = bytes([flags])
                    if include_pcr:
                        body += _encode_pcr(pcr or 0)
                    stuffing = total_adaptation - 1 - len(body)
                    adaptation = bytes([len(body) + max(stuffing, 0)]) + body + (b"\xff" * max(stuffing, 0))

            adaptation_control = 0x30 if adaptation else 0x10
            continuity = self.continuity.get(pid, 0) & 0x0F
            self.continuity[pid] = (continuity + 1) & 0x0F
            header = bytes(
                [
                    0x47,
                    (0x40 if first and start else 0x00) | ((pid >> 8) & 0x1F),
                    pid & 0xFF,
                    adaptation_control | continuity,
                ]
            )
            packet = header + adaptation + chunk
            yield packet + (b"\xff" * (188 - len(packet)))
            first = False


@dataclass
class HlsSegment:
    sequence: int
    duration: float
    data: bytes
    created_at: float


class HlsSession:
    def __init__(
        self,
        camera_config,
        stream: str,
        *,
        state_path: str | None,
        debug: bool,
        buffer_bytes: int,
        segment_seconds: float,
    ) -> None:
        self.camera_config = camera_config
        self.stream = stream
        self.state_path = state_path
        self.debug = debug
        self.buffer_bytes = buffer_bytes
        self.segment_seconds = segment_seconds
        self.lock = threading.Lock()
        self.condition = threading.Condition(self.lock)
        self.segments: list[HlsSegment] = []
        self.total_bytes = 0
        self.next_sequence = 0
        self.started = False
        self.error: BaseException | None = None

    def start(self) -> None:
        with self.lock:
            if self.started:
                return
            self.started = True
            thread = threading.Thread(target=self._run, daemon=True)
            thread.start()

    def playlist(self, *, timeout: float = 15.0) -> str:
        self.start()
        deadline = time.monotonic() + timeout
        with self.condition:
            while not self.segments and self.error is None and time.monotonic() < deadline:
                self.condition.wait(timeout=0.25)
            if self.error and not self.segments:
                raise self.error
            segments = list(self.segments)
        return _hls_playlist(segments, self.segment_seconds)

    def segment(self, sequence: int) -> HlsSegment | None:
        self.start()
        with self.lock:
            for segment in self.segments:
                if segment.sequence == sequence:
                    return segment
        return None

    def _run(self) -> None:
        camera = Camera(self.camera_config, state_path=self.state_path, debug=self.debug)
        parser = MediaParser()
        try:
            camera.__enter__()
            payloads = camera.read_stream_payloads(self.stream)
            first_packets, codec, fps = _read_until_keyframe(payloads, parser)
            if codec not in ("H264", "H265"):
                raise RuntimeError(msg.Error.HlsRequiresH264OrH265)
            first_packets = _packets_from_first_keyframe(first_packets)
            muxer = MpegTsMuxer(codec, fps=fps)
            current = bytearray()
            started_at = time.monotonic()
            saw_video = False

            def add_packet(packet: MediaPacket) -> None:
                nonlocal current, started_at, saw_video
                now = time.monotonic()
                if (
                    packet.kind == "iframe"
                    and saw_video
                    and current
                    and now - started_at >= self.segment_seconds
                ):
                    self._append_segment(bytes(current), now - started_at)
                    current = bytearray()
                    started_at = now
                if not current:
                    for table_chunk in muxer.table_packets():
                        current.extend(table_chunk)
                for chunk in muxer.feed(packet):
                    current.extend(chunk)
                if packet.kind in ("iframe", "pframe"):
                    saw_video = True

            for packet in first_packets:
                add_packet(packet)
            for payload in payloads:
                for packet in parser.feed(payload):
                    add_packet(packet)
        except BaseException as exc:
            with self.condition:
                self.error = exc
                self.condition.notify_all()
        finally:
            camera.close()

    def _append_segment(self, data: bytes, duration: float) -> None:
        if not data:
            return
        with self.condition:
            segment = HlsSegment(self.next_sequence, max(duration, 0.001), data, time.monotonic())
            self.next_sequence += 1
            self.segments.append(segment)
            self.total_bytes += len(data)
            while self.segments and self.total_bytes > self.buffer_bytes:
                removed = self.segments.pop(0)
                self.total_bytes -= len(removed.data)
            self.condition.notify_all()


def _packets_from_first_keyframe(packets: list[MediaPacket]) -> list[MediaPacket]:
    for index, packet in enumerate(packets):
        if packet.kind == "iframe":
            return packets[index:]
    return packets


def _hls_playlist(segments: list[HlsSegment], segment_seconds: float) -> str:
    target = max(1, int(max([segment.duration for segment in segments], default=segment_seconds) + 0.999))
    media_sequence = segments[0].sequence if segments else 0
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{target}",
        f"#EXT-X-MEDIA-SEQUENCE:{media_sequence}",
        "#EXT-X-INDEPENDENT-SEGMENTS",
    ]
    for segment in segments:
        lines.append(f"#EXTINF:{segment.duration:.3f},")
        lines.append(f"segments/{segment.sequence}.ts")
    return "\n".join(lines) + "\n"


def _produce_mpegts_chunks(
    chunks: queue.Queue[object],
    stop_event: threading.Event,
    codec: str,
    fps: int,
    payloads: Iterable[bytes],
    parser: MediaParser,
    first_packets: list[MediaPacket],
) -> None:
    muxer = MpegTsMuxer(codec, fps=fps)
    try:
        for packet in first_packets:
            for chunk in muxer.feed(packet):
                if not _put_stream_item(chunks, stop_event, chunk):
                    return
        for payload in payloads:
            if stop_event.is_set():
                return
            for packet in parser.feed(payload):
                for chunk in muxer.feed(packet):
                    if not _put_stream_item(chunks, stop_event, chunk):
                        return
    except BaseException as exc:
        _put_stream_item(chunks, stop_event, exc)
    finally:
        _put_stream_item(chunks, stop_event, _STREAM_END)


def _put_stream_item(chunks: queue.Queue[object], stop_event: threading.Event, item: object) -> bool:
    while not stop_event.is_set():
        try:
            chunks.put(item, timeout=_STREAM_QUEUE_TIMEOUT)
            return True
        except queue.Full:
            continue
    return False


def _stream_queue_size(fps: int, buffer_seconds: float) -> int:
    return max(512, int(max(fps, 1) * max(buffer_seconds, 1.0) * 64))


def _mpegts_null_packet() -> bytes:
    return b"\x47\x1f\xff\x10" + (b"\xff" * 184)


def _quality_to_stream(quality: str) -> str:
    normalized = quality.strip().lower()
    if normalized in ("high", "main", "mainstream", "clear"):
        return "mainStream"
    if normalized in ("low", "sub", "substream", "fluent"):
        return "subStream"
    raise ValueError(msg.Error.InvalidQuality)


def _display_host(bind_host: str) -> str:
    return "127.0.0.1" if bind_host in ("0.0.0.0", "::") else bind_host


def _find_camera(config: Config, name: str):
    try:
        return config.camera(name)
    except ValueError:
        pass
    wanted = _normalize_camera_name(name)
    for camera in config.cameras or []:
        if _normalize_camera_name(camera.name) == wanted:
            return camera
    available = ", ".join(camera.name for camera in config.cameras or []) or "none"
    raise ValueError(msg.Error.NoCameraNamedWithAvailable.format(name=name, available=available))


def _normalize_camera_name(name: str) -> str:
    return " ".join(name.casefold().split())


def _is_client_disconnect(exc: OSError) -> bool:
    return getattr(exc, "winerror", None) in (10053, 10054) or getattr(exc, "errno", None) in (32, 104)


def _read_until_keyframe(payloads: Iterable[bytes], parser: MediaParser) -> tuple[list[MediaPacket], str | None, int]:
    fps = 15
    packets: list[MediaPacket] = []
    for payload in payloads:
        for packet in parser.feed(payload):
            if packet.kind == "info" and packet.fps:
                fps = packet.fps
                packets.append(packet)
            elif packet.kind in ("aac", "adpcm"):
                packets.append(packet)
            elif packet.kind == "iframe" and packet.codec:
                packets.append(packet)
                return packets, packet.codec, fps
    return [], None, fps


def _buffer_initial_video(
    payloads: Iterable[bytes],
    parser: MediaParser,
    packets: list[MediaPacket],
    fps: int,
    buffer_seconds: float,
) -> list[MediaPacket]:
    target_frames = int(max(fps, 1) * max(buffer_seconds, 0.0))
    video_count = sum(1 for packet in packets if packet.kind in ("iframe", "pframe"))
    if target_frames <= video_count:
        return packets
    buffered = list(packets)
    for payload in payloads:
        for packet in parser.feed(payload):
            buffered.append(packet)
            if packet.kind in ("iframe", "pframe"):
                video_count += 1
            if video_count >= target_frames:
                return buffered
    return buffered


def _video_packets(packets: Iterable[MediaPacket]) -> Iterable[MediaPacket]:
    for packet in packets:
        if packet.kind in ("iframe", "pframe"):
            yield packet


def _raw_content_type(codec: str | None) -> str:
    if codec == "H265":
        return "video/h265"
    if codec == "H264":
        return "video/h264"
    return "application/octet-stream"


def _pat_section(pmt_pid: int) -> bytes:
    section = bytearray()
    section.extend(b"\x00\xb0\x0d")
    section.extend(b"\x00\x01\xc1\x00\x00")
    section.extend(b"\x00\x01")
    section.extend(bytes([0xE0 | ((pmt_pid >> 8) & 0x1F), pmt_pid & 0xFF]))
    section.extend(_mpeg_crc32(section).to_bytes(4, "big"))
    return bytes(section)


def _pmt_section(codec: str, video_pid: int, audio_pid: int) -> bytes:
    video_type = 0x24 if codec == "H265" else 0x1B
    streams = [
        (video_type, video_pid),
        (0x0F, audio_pid),
    ]
    section_length = 9 + (5 * len(streams)) + 4
    section = bytearray()
    section.extend(bytes([0x02, 0xB0 | ((section_length >> 8) & 0x0F), section_length & 0xFF]))
    section.extend(b"\x00\x01\xc1\x00\x00")
    section.extend(bytes([0xE0 | ((video_pid >> 8) & 0x1F), video_pid & 0xFF]))
    section.extend(b"\xf0\x00")
    for stream_type, pid in streams:
        section.append(stream_type)
        section.extend(bytes([0xE0 | ((pid >> 8) & 0x1F), pid & 0xFF, 0xF0, 0x00]))
    section.extend(_mpeg_crc32(section).to_bytes(4, "big"))
    return bytes(section)


def _pes_packet(stream_id: int, pts_90k: int, payload: bytes, *, unbounded: bool = False) -> bytes:
    header = b"\x80\x80\x05" + _encode_pts(pts_90k)
    pes_length = 0 if unbounded or len(header) + len(payload) > 0xFFFF else len(header) + len(payload)
    return b"\x00\x00\x01" + bytes([stream_id]) + pes_length.to_bytes(2, "big") + header + payload


def _encode_pts(value: int) -> bytes:
    pts = value & ((1 << 33) - 1)
    return bytes(
        [
            0x20 | (((pts >> 30) & 0x07) << 1) | 1,
            (pts >> 22) & 0xFF,
            (((pts >> 15) & 0x7F) << 1) | 1,
            (pts >> 7) & 0xFF,
            ((pts & 0x7F) << 1) | 1,
        ]
    )


def _encode_pcr(value: int) -> bytes:
    base = value & ((1 << 33) - 1)
    return bytes(
        [
            (base >> 25) & 0xFF,
            (base >> 17) & 0xFF,
            (base >> 9) & 0xFF,
            (base >> 1) & 0xFF,
            ((base & 1) << 7) | 0x7E,
            0x00,
        ]
    )


def _mpeg_crc32(data: bytes | bytearray) -> int:
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if crc & 0x80000000 else (crc << 1) & 0xFFFFFFFF
    return crc


def _looks_like_adts(data: bytes) -> bool:
    return len(data) >= 7 and data[0] == 0xFF and (data[1] & 0xF0) == 0xF0


def _aac_duration_90k(data: bytes) -> int:
    if not _looks_like_adts(data):
        return 90_000 // 50
    sample_rate_index = (data[2] >> 2) & 0x0F
    sample_rate = {
        0: 96000,
        1: 88200,
        2: 64000,
        3: 48000,
        4: 44100,
        5: 32000,
        6: 24000,
        7: 22050,
        8: 16000,
        9: 12000,
        10: 11025,
        11: 8000,
        12: 7350,
    }.get(sample_rate_index, 8000)
    return max(1, int(1024 * 90_000 / sample_rate))
