from __future__ import annotations

import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterable
from urllib.parse import quote, unquote, urlparse

from .camera import Camera
from .config import Config, config_from_dict, load_config
from .core.media import MediaParser, MediaPacket


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
    ) -> None:
        self.config = _coerce_config(config)
        self.host = host if host is not None else self.config.bind
        self.port = port if port is not None else self.config.bind_port
        self.state_path = state_path
        self.debug = debug
        self.buffer_seconds = max(buffer_seconds, 0.0)

    def urls(self, *, host: str | None = None) -> list[str]:
        display_host = host or _display_host(self.host)
        urls = []
        for camera in self.config.cameras or []:
            encoded_name = quote(camera.name, safe="")
            urls.append(f"http://{display_host}:{self.port}/{encoded_name}/high")
            urls.append(f"http://{display_host}:{self.port}/{encoded_name}/low")
        return urls

    def serve_forever(self) -> None:
        server = _StreamServer((self.host, self.port), _StreamHandler)
        server.config = self.config
        server.state_path = self.state_path
        server.debug = self.debug
        server.buffer_seconds = self.buffer_seconds
        print(f"Serving camera streams on http://{self.host}:{self.port}/")
        display_host = _display_host(self.host)
        if display_host != self.host:
            print(f"Open locally with http://{display_host}:{self.port}/")
        for url in self.urls(host=display_host):
            print(f"  {url}")
        server.serve_forever()


def serve_streams(
    config_path: str = "config.json",
    *,
    host: str | None = None,
    port: int | None = None,
    state_path: str | None = ".pyneolink_state.json",
    debug: bool = False,
    buffer_seconds: float = 1.0,
) -> None:
    StreamServer(
        config_path,
        host=host,
        port=port,
        state_path=state_path,
        debug=debug,
        buffer_seconds=buffer_seconds,
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


class _StreamHandler(BaseHTTPRequestHandler):
    server: _StreamServer

    def do_GET(self) -> None:
        parts = [unquote(part) for part in urlparse(self.path).path.strip("/").split("/") if part]
        if not parts:
            self._send_index()
            return
        if len(parts) != 2:
            self.send_error(404, "Use /{camera}/{quality}")
            return
        camera_name, quality = parts
        try:
            camera_config = _find_camera(self.server.config, camera_name)
            stream = _quality_to_stream(quality)
        except ValueError as exc:
            self.send_error(404, str(exc))
            return

        camera = Camera(camera_config, state_path=self.server.state_path, debug=self.server.debug)
        parser = MediaParser()
        try:
            camera.__enter__()
            payloads = camera.read_stream_payloads(stream)
            first_packets, codec, fps = _read_until_keyframe(payloads, parser)
            first_packets = _buffer_initial_video(payloads, parser, first_packets, fps, self.server.buffer_seconds)
            if shutil.which("ffmpeg") and codec in ("H264", "H265"):
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
        body = ("\n".join(lines) + "\n").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
        for packet in first_packets:
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
        process = subprocess.Popen(
            _ffmpeg_mpegts_cmd(codec, fps),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        feeder = threading.Thread(
            target=_feed_ffmpeg,
            args=(process, payloads, parser, first_packets),
            daemon=True,
        )
        feeder.start()
        self.send_response(200)
        self.send_header("Content-Type", "video/MP2T")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            while process.stdout is not None:
                read = getattr(process.stdout, "read1", process.stdout.read)
                chunk = read(188 * 16)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        finally:
            _stop_process(process)
            feeder.join(timeout=2.0)


def _quality_to_stream(quality: str) -> str:
    normalized = quality.strip().lower()
    if normalized in ("high", "main", "mainstream", "clear"):
        return "mainStream"
    if normalized in ("low", "sub", "substream", "fluent"):
        return "subStream"
    raise ValueError('quality must be "high" or "low"')


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
    raise ValueError(f"No camera named {name!r}. Available cameras: {available}")


def _normalize_camera_name(name: str) -> str:
    return " ".join(name.casefold().split())


def _is_client_disconnect(exc: OSError) -> bool:
    return getattr(exc, "winerror", None) in (10053, 10054) or getattr(exc, "errno", None) in (32, 104)


def _read_until_keyframe(payloads: Iterable[bytes], parser: MediaParser) -> tuple[list[MediaPacket], str | None, int]:
    fps = 15
    for payload in payloads:
        for packet in parser.feed(payload):
            if packet.kind == "info" and packet.fps:
                fps = packet.fps
            elif packet.kind == "iframe" and packet.codec:
                return [packet], packet.codec, fps
    return [], None, fps


def _buffer_initial_video(
    payloads: Iterable[bytes],
    parser: MediaParser,
    packets: list[MediaPacket],
    fps: int,
    buffer_seconds: float,
) -> list[MediaPacket]:
    target_frames = int(max(fps, 1) * max(buffer_seconds, 0.0))
    if target_frames <= len(packets):
        return packets
    buffered = list(packets)
    for payload in payloads:
        for packet in _video_packets(parser.feed(payload)):
            buffered.append(packet)
            if len(buffered) >= target_frames:
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


def _ffmpeg_mpegts_cmd(codec: str, fps: int = 15) -> list[str]:
    input_format = "hevc" if codec == "H265" else "h264"
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-fflags",
        "+genpts+nobuffer",
        "-flags",
        "low_delay",
        "-r",
        str(max(fps, 1)),
        "-f",
        input_format,
        "-i",
        "pipe:0",
        "-c:v",
        "copy",
        "-an",
        "-muxdelay",
        "0",
        "-muxpreload",
        "0",
        "-mpegts_flags",
        "resend_headers",
        "-flush_packets",
        "1",
        "-f",
        "mpegts",
        "pipe:1",
    ]


def _feed_ffmpeg(
    process: subprocess.Popen,
    payloads: Iterable[bytes],
    parser: MediaParser,
    first_packets: list[MediaPacket],
) -> None:
    try:
        if process.stdin is None:
            return
        for packet in first_packets:
            process.stdin.write(packet.data)
        process.stdin.flush()
        for payload in payloads:
            for packet in _video_packets(parser.feed(payload)):
                process.stdin.write(packet.data)
            process.stdin.flush()
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    finally:
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass


def _stop_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        process.kill()
