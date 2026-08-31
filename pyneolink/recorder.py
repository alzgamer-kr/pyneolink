from __future__ import annotations

from pathlib import Path
import threading
import time

from .core.const import msg
from .core.media import MediaParser, MediaPacket


class StreamRecorder:
    """Record a dispatcher-managed camera stream in a background thread."""

    def __init__(
        self,
        camera,
        *,
        out: str | Path,
        stream: str = "mainStream",
        duration: float | None = None,
    ) -> None:
        """Create a local stream recorder.

        :param camera: Connected `Camera` instance.
        :param out: Output file path or directory. Directories get an automatic
            `.ts` file name.
        :param stream: Stream alias/name, usually `mainStream` or `subStream`.
        :param duration: Optional recording duration in seconds.
        """
        self.camera = camera
        self.path = _record_output_path(out)
        self.stream = stream
        self.duration = duration
        self.bytes_written = 0
        self.packets_written = 0
        self.error: BaseException | None = None
        self.flush_bytes = 1024 * 1024
        self._stop = threading.Event()
        self._done = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "StreamRecorder":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> "StreamRecorder":
        """Start recording in a background thread."""
        if self._thread is not None:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._run, name="pyneolink-recorder", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float | None = 10.0) -> Path:
        """Request stop and return the output path.

        :param timeout: Seconds to wait for the recorder thread, or `None`.
        """
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        if self.error is not None:
            raise self.error
        return self.path

    def wait(self, timeout: float | None = None) -> Path:
        """Wait for recording completion and return the output path.

        :param timeout: Seconds to wait, or `None` for no timeout.
        """
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        if self.running:
            raise TimeoutError(msg.Error.RecordStopTimeout)
        if self.error is not None:
            raise self.error
        return self.path

    def _run(self) -> None:
        try:
            self._record()
        except BaseException as exc:
            self.error = exc
        finally:
            self._done.set()

    def _record(self) -> None:
        parser = MediaParser()
        muxer = None
        bootstrap_packets: list[MediaPacket] = []
        deadline = None if self.duration is None else time.monotonic() + max(self.duration, 0.0)
        next_flush_at = self.flush_bytes
        payloads = self.camera.read_stream_payloads(self.stream)

        try:
            with self.path.open("wb") as fh:
                for payload in payloads:
                    if self._stop.is_set():
                        break
                    if deadline is not None and time.monotonic() >= deadline:
                        break

                    for packet in parser.feed(payload):
                        if muxer is None:
                            if packet.kind == "info":
                                bootstrap_packets = [packet]
                            if packet.kind != "iframe" or packet.codec not in ("H264", "H265"):
                                continue
                            from .stream_server import MpegTsMuxer

                            fps = _fps_from_packets(bootstrap_packets)
                            muxer = MpegTsMuxer(packet.codec, fps=fps)
                            for buffered in [*bootstrap_packets, packet]:
                                self._write_packet(fh, muxer, buffered)
                            bootstrap_packets = []
                            continue
                        self._write_packet(fh, muxer, packet)
                        if self.bytes_written >= next_flush_at:
                            fh.flush()
                            next_flush_at = self.bytes_written + self.flush_bytes
                fh.flush()
        finally:
            payloads.close()

    def _write_packet(self, fh, muxer, packet: MediaPacket) -> None:
        for chunk in muxer.feed(packet):
            fh.write(chunk)
            self.bytes_written += len(chunk)
        if packet.kind in ("iframe", "pframe"):
            self.packets_written += 1


def _record_output_path(out: str | Path) -> Path:
    path = Path(out)
    if path.exists() and path.is_dir():
        return path / _default_record_name()
    if str(out).endswith(("/", "\\")):
        return path / _default_record_name()
    if not path.suffix:
        return path.with_suffix(".ts")
    return path


def _default_record_name() -> str:
    return f"recording-{time.strftime('%Y%m%d-%H%M%S')}.ts"


def _fps_from_packets(packets: list[MediaPacket]) -> int:
    for packet in packets:
        if packet.kind == "info" and packet.fps:
            return packet.fps
    return 15
