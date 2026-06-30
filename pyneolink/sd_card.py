from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Iterable
import threading
import time as monotonic_clock
import random
import xml.etree.ElementTree as ET

from .core.bc import (
    InvalidMagicError,
    ProtocolError,
)
from .core.const import MSG, MSG_CLASS, msg as const_msg, payloads
from .core.media import MediaParser, bcmedia_to_mp4, extract_embedded_mp4, looks_like_bcmedia
from .core.xmlutil import xml_to_dict
from .errors import CameraConnectionError

if TYPE_CHECKING:
    from .camera import Camera


class DangerousSdCardOperation(RuntimeError):
    """Raised when a destructive SD-card action is missing explicit confirmation."""

    pass


class DownloadSizeMismatch(RuntimeError):
    """Raised when downloaded bytes do not match the expected camera file size."""

    pass


@dataclass(frozen=True)
class SdCardFile:
    """Normalized SD-card recording/file metadata."""

    file_name: str | None = None
    path: str | None = None
    size: int | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
    stream_type: str | None = None
    file_type: str | None = None
    channel_id: int | None = None
    raw: dict | None = None

    def to_dict(self) -> dict:
        data = asdict(self)
        if self.start_time:
            data["start_time"] = self.start_time.isoformat()
        if self.end_time:
            data["end_time"] = self.end_time.isoformat()
        return data


class SDFile:
    """Action wrapper around one SD-card file.

    :param sd_card: Parent SD-card helper.
    :param file: File metadata returned by `SdCard.list()`.
    """

    def __init__(self, sd_card: SdCard, file: dict | SdCardFile | str) -> None:
        self.sd_card = sd_card
        self._file = file

    def info(self) -> dict:
        """Return this file metadata as a plain dictionary."""
        return _file_to_dict(self._file)

    def download(
        self,
        output: str | Path,
        *,
        stream_type: str | None = None,
        quality: str | None = None,
        chunk_limit: int = 0,
        progress=False,
        max_attempts: int = 3,
        reconnect_retries: int = 3,
        rewrite_exists: bool = True,
        recv_timeout: float = 2.0,
    ) -> Path:
        """Download this SD-card file.

        :param output: Output directory or complete output file path.
        :param stream_type: Explicit stream type, for example `mainStream` or
            `subStream`. Mutually exclusive with `quality`.
        :param quality: Quality alias such as `high`/`main` or `low`/`sub`.
            Mutually exclusive with `stream_type`.
        :param chunk_limit: Optional low-level chunk limit for diagnostics.
        :param progress: `True` to print progress, or a callable accepting a
            progress string.
        :param max_attempts: Maximum number of protocol download strategies to
            try for one connection.
        :param reconnect_retries: Number of reconnect attempts after an
            interrupted download before raising `CameraConnectionError`.
        :param rewrite_exists: When `False`, skip an already finalized local
            file. Non-empty `.mp4` files are treated as complete.
        :param recv_timeout: Per-read timeout while waiting for download data.
        """

        return self.sd_card._download_file(
            self._file,
            output,
            stream_type=stream_type,
            quality=quality,
            chunk_limit=chunk_limit,
            progress=progress,
            max_attempts=max_attempts,
            reconnect_retries=reconnect_retries,
            rewrite_exists=rewrite_exists,
            recv_timeout=recv_timeout,
        )

    def preview(
        self,
        *,
        cache: str | Path | None = None,
        stream_type: str = "mainStream",
        channel_id: int | None = None,
        max_bytes: int | None = 100 * 1024 * 1024,
        progress=False,
        recv_timeout: float = 2.0,
        idle_timeouts: int = 10,
        cleanup: bool = True,
    ) -> SDFilePreview:
        """Open a cached preview stream context for this SD-card file.

        :param cache: Cache file or directory. A temporary file is used when
            omitted.
        :param stream_type: Reolink stream type, usually `mainStream` or
            `subStream`.
        :param channel_id: Optional channel override.
        :param max_bytes: Maximum raw preview bytes to cache. Defaults to
            100 MiB as a safety limit.
        :param progress: `True` to print progress, or a callable accepting a
            progress string.
        :param recv_timeout: Per-read timeout while waiting for preview data.
        :param idle_timeouts: Number of idle read timeouts before stopping.
        :param cleanup: Remove temporary cache file when the context exits.
        """

        return SDFilePreview(
            self.sd_card,
            self._file,
            cache=cache,
            stream_type=stream_type,
            channel_id=channel_id,
            max_bytes=max_bytes,
            progress=progress,
            recv_timeout=recv_timeout,
            idle_timeouts=idle_timeouts,
            cleanup=cleanup,
        )


class SDFilePreview:
    """Context manager for a cached SD-card preview playback stream."""

    def __init__(
        self,
        sd_card: SdCard,
        file: dict | SdCardFile | str,
        *,
        cache: str | Path | None,
        stream_type: str,
        channel_id: int | None,
        max_bytes: int | None,
        progress,
        recv_timeout: float,
        idle_timeouts: int,
        cleanup: bool,
    ) -> None:
        self.sd_card = sd_card
        self.file = file
        self.stream_type = stream_type
        self.channel_id = channel_id
        self.max_bytes = max_bytes
        self.progress = progress
        self.recv_timeout = recv_timeout
        self.idle_timeouts = idle_timeouts
        self.cleanup = cleanup
        self.path = _preview_cache_path(cache, _file_to_dict(file))
        self.error: BaseException | None = None
        self.done = threading.Event()
        self.ready = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> SDFilePreview:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._run, name="pyneolink-sd-preview", daemon=True)
        self._thread.start()
        self.ready.wait(timeout=max(self.recv_timeout * 2, 5.0))
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @property
    def size(self) -> int:
        """Current cache file size in bytes."""
        return self.path.stat().st_size if self.path.exists() else 0

    def wait_ready(self, timeout: float | None = None) -> bool:
        """Wait until the cache file has enough data to open."""
        return self.ready.wait(timeout=timeout)

    def wait_done(self, timeout: float | None = None) -> bool:
        """Wait until preview caching finishes."""
        return self.done.wait(timeout=timeout)

    def serve(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 8560,
        path: str = "/preview.mp4",
        cleanup_on_disconnect: bool = True,
    ) -> SDFilePreviewServer:
        """Serve this preview cache as an HTTP stream.

        :param host: Bind host.
        :param port: Bind port. Use `0` to let the OS choose a free port.
        :param path: HTTP path for the preview stream.
        :param cleanup_on_disconnect: Stop caching and remove the cache file
            after the last connected player disconnects.
        """

        return SDFilePreviewServer(
            self,
            host=host,
            port=port,
            path=path,
            cleanup_on_disconnect=cleanup_on_disconnect,
        )

    def close(self) -> None:
        """Stop caching and optionally remove the temporary cache file."""
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=max(self.recv_timeout * 2, 5.0))
        if self.cleanup and self.path.exists():
            _remove_file(self.path)

    def _run(self) -> None:
        try:
            self.sd_card._cache_preview(
                self.file,
                self.path,
                stream_type=self.stream_type,
                channel_id=self.channel_id,
                max_bytes=self.max_bytes,
                progress=self.progress,
                recv_timeout=self.recv_timeout,
                idle_timeouts=self.idle_timeouts,
                ready=self.ready,
                stop=self._stop,
            )
        except BaseException as exc:
            self.error = exc
            self.ready.set()
        finally:
            self.done.set()


class SDFilePreviewServer:
    """Context manager serving an `SDFilePreview` cache over HTTP."""

    def __init__(
        self,
        preview: SDFilePreview,
        *,
        host: str,
        port: int,
        path: str,
        cleanup_on_disconnect: bool,
    ) -> None:
        self.preview = preview
        self.host = host
        self.port = port
        self.path = "/" + path.strip("/")
        self.cleanup_on_disconnect = cleanup_on_disconnect
        self.url = ""
        self._server: _PreviewHttpServer | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> SDFilePreviewServer:
        server = _PreviewHttpServer((self.host, self.port), _PreviewHttpHandler)
        server.preview = self.preview
        server.route_path = self.path
        server.cleanup_on_disconnect = self.cleanup_on_disconnect
        server.active_clients = 0
        server.seen_client = False
        server.lock = threading.Lock()
        self._server = server
        bound_host, bound_port = server.server_address
        display_host = "127.0.0.1" if bound_host in ("", "0.0.0.0") else bound_host
        self.url = f"http://{display_host}:{bound_port}{self.path}"
        self._thread = threading.Thread(target=server.serve_forever, name="pyneolink-sd-preview-http", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        """Stop the HTTP preview server."""
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)


class _PreviewHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    preview: SDFilePreview
    route_path: str
    cleanup_on_disconnect: bool
    active_clients: int
    seen_client: bool
    lock: threading.Lock


class _PreviewHttpHandler(BaseHTTPRequestHandler):
    server: _PreviewHttpServer

    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] != self.server.route_path:
            self.send_error(404, "Preview stream not found")
            return
        self.server.preview.wait_ready(timeout=30.0)
        if self.server.preview.error:
            self.send_error(502, str(self.server.preview.error))
            return
        with self.server.lock:
            self.server.active_clients += 1
            self.server.seen_client = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self._copy_cache_from_start()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        finally:
            should_cleanup = False
            with self.server.lock:
                self.server.active_clients = max(self.server.active_clients - 1, 0)
                should_cleanup = (
                    self.server.cleanup_on_disconnect
                    and self.server.seen_client
                    and self.server.active_clients == 0
                )
            if should_cleanup:
                self.server.preview.close()

    def log_message(self, format: str, *args) -> None:
        return

    def _copy_cache_from_start(self) -> None:
        pos = 0
        idle_sleep = 0.1
        while True:
            if self.server.preview.path.exists():
                with self.server.preview.path.open("rb") as fh:
                    fh.seek(pos)
                    while True:
                        chunk = fh.read(64 * 1024)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                        pos += len(chunk)
                        idle_sleep = 0.1
            if self.server.preview.done.is_set():
                if not self.server.preview.path.exists() or pos >= self.server.preview.size:
                    break
            monotonic_clock.sleep(idle_sleep)
            idle_sleep = min(idle_sleep * 1.5, 1.0)


class SdCard:
    """SD-card helper for listing, filtering, and downloading recordings."""

    def __init__(self, camera: Camera) -> None:
        """Create an SD-card helper.

        :param camera: Connected or connectable `Camera` instance.
        """
        self.camera = camera
        self.last_attempts: list[str] = []
        self.last_download_attempts: list[str] = []
        self.last_xml: str | None = None
        self.last_successes: list[dict] = []
        self._last_download_detail = ""
        self._active_replay_name: str | None = None
        self._playback_channel_id = 0

    def list(
        self,
        *,
        start: datetime | date | str | None = None,
        end: datetime | date | str | None = None,
        stream_type: str = "mainStream",
        file_type: str = "All",
        channel_id: int | None = None,
        as_dict: bool = True,
        sort: str | None = "asc",
    ) -> list[dict] | list[SdCardFile]:
        """List SD-card recordings.

        :param start: Start date/time. A `YYYY-MM-DD` string selects the start
            of that day.
        :param end: End date/time. A `YYYY-MM-DD` string selects the end of
            that day.
        :param stream_type: Reolink stream type to query, usually
            `mainStream` or `subStream`.
        :param file_type: Camera file type filter sent to the camera.
        :param channel_id: Optional channel override.
        :param as_dict: Return plain dicts when `True`, otherwise `SdCardFile`
            objects.
        :param sort: `asc`, `desc`, or `None`.
        """
        start_dt, end_dt = _date_range(start, end)
        channel = self.camera.config.channel_id if channel_id is None else channel_id
        attempts = []
        self.last_successes = []
        files = []
        days = self._recorded_days(start_dt, end_dt, channel, attempts)
        if not days:
            days = list(_days_between(start_dt.date(), end_dt.date()))
        for target_day in days:
            day_start = max(start_dt, datetime.combine(target_day, time.min))
            day_end = min(end_dt, datetime.combine(target_day, time.max))
            files.extend(self._list_day_files(channel, day_start, day_end, stream_type, attempts))
        _sort_recordings(files, sort)
        self.last_attempts = attempts
        return [item.to_dict() for item in files] if as_dict else files

    def files(
        self,
        *,
        start: datetime | date | str | None = None,
        end: datetime | date | str | None = None,
        name: str | None = None,
        stream_type: str = "mainStream",
        file_type: str = "All",
        channel_id: int | None = None,
        sort: str | None = "asc",
    ) -> list[SDFile]:
        """List SD-card recordings as `SDFile` action objects.

        Each returned item has `info()`, `download()`, and `preview()` methods.

        :param start: Start date/time.
        :param end: End date/time.
        :param name: Optional substring to keep, for example `.mp4`.
        :param stream_type: Reolink stream type to query.
        :param file_type: Camera file type filter sent to the camera.
        :param channel_id: Optional channel override.
        :param sort: `asc`, `desc`, or `None`.
        """

        items = self.list(
            start=start,
            end=end,
            stream_type=stream_type,
            file_type=file_type,
            channel_id=channel_id,
            as_dict=False,
            sort=sort,
        )
        result = []
        for item in items:
            data = item.to_dict()
            if name and name.lower() not in _searchable_file_text(data).lower():
                continue
            result.append(SDFile(self, item))
        return result

    def file(self, file: dict | SdCardFile | str) -> SDFile:
        """Wrap existing file metadata as an `SDFile` action object."""
        return SDFile(self, file)

    def _recorded_days(self, start: datetime, end: datetime, channel: int, attempts: list[str]) -> list[date]:
        query = _day_records_range_query(channel, start, end)
        reply = _send_query(self.camera, query, attempts)
        if not reply or reply.header.response_code != 200:
            return []
        self.last_xml = reply.xml_text
        self.last_successes.append(_success(query, reply.xml_text))
        days = []
        for node in reply.xml_root.findall(".//dayType") if reply.xml_root is not None else []:
            index = _int_or_none(_find_child_text(node, "index"))
            if index is not None:
                days.append(start.date().fromordinal(start.date().toordinal() + index))
        return [day for day in days if start.date() <= day <= end.date()]

    def _list_day_files(self, channel: int, start: datetime, end: datetime, stream_type: str, attempts: list[str]) -> list[SdCardFile]:
        files = []
        seen = set()
        for handle_query in _handle_queries(channel, start, end, stream_type):
            reply = _send_query(self.camera, handle_query, attempts)
            if not reply or reply.header.response_code != 200:
                continue
            self.last_xml = reply.xml_text
            self.last_successes.append(_success(handle_query, reply.xml_text))
            for file_info in _file_info_nodes(reply.xml_root):
                handle = _find_child_text(file_info, "handle")
                direct_files = _parse_file_list(file_info)
                _append_unique_files(files, (item for item in direct_files if item.file_name), seen)
                if not handle:
                    continue
                if self._list_handle_files(channel, handle, files, seen, attempts):
                    break
            if files:
                break
        return files

    def _list_handle_files(
        self,
        channel: int,
        handle: str,
        files: list[SdCardFile],
        seen: set[tuple],
        attempts: list[str],
        max_pages: int = 64,
    ) -> bool:
        for detail_query in _handle_detail_queries(channel, handle):
            got_response = False
            for page in range(1, max_pages + 1):
                detail = _send_query(self.camera, detail_query, attempts)
                if not detail or detail.header.response_code != 200:
                    if page == 1:
                        break
                    return got_response
                got_response = True
                self.last_xml = detail.xml_text
                success = _success(detail_query, detail.xml_text)
                if page > 1:
                    success["label"] = f"{detail_query.label}/page-{page}"
                self.last_successes.append(success)
                page_files = [item for item in _parse_file_list(detail.xml_root) if item.file_name]
                added = _append_unique_files(files, page_files, seen)
                if not page_files or added == 0:
                    return True
            if got_response:
                return True
        return False

    def filter(
        self,
        files: Iterable[dict | SdCardFile] | None = None,
        *,
        start: datetime | date | str | None = None,
        end: datetime | date | str | None = None,
        name: str | None = None,
        file_type: str | None = None,
        stream_type: str | None = None,
    ) -> list[dict]:
        """Filter an SD-card file list in memory.

        :param files: Existing files from `list()` or `files()`. When omitted,
            `list()` is called with `start`/`end`.
        :param start: Optional minimum recording time.
        :param end: Optional maximum recording time.
        :param name: Substring to search in path/name/type fields.
        :param file_type: Exact file type to keep.
        :param stream_type: Exact stream type to keep.
        """
        items = list(files if files is not None else self.list(start=start, end=end))
        start_dt = _coerce_datetime(start, end_of_day=False) if start is not None else None
        end_dt = _coerce_datetime(end, end_of_day=True) if end is not None else None
        result = []
        for item in items:
            data = item.info() if isinstance(item, SDFile) else item.to_dict() if isinstance(item, SdCardFile) else dict(item)
            item_start = _coerce_datetime(data.get("start_time"), end_of_day=False) if data.get("start_time") else None
            item_end = _coerce_datetime(data.get("end_time"), end_of_day=True) if data.get("end_time") else None
            if name and name.lower() not in _searchable_file_text(data).lower():
                continue
            if file_type and (data.get("file_type") or "").lower() != file_type.lower():
                continue
            if stream_type and (data.get("stream_type") or "").lower() != stream_type.lower():
                continue
            if start_dt and item_end and item_end < start_dt:
                continue
            if end_dt and item_start and item_start > end_dt:
                continue
            result.append(data)
        return result

    def _download_file(
        self,
        file: dict | SdCardFile | str,
        output: str | Path,
        *,
        stream_type: str | None = None,
        quality: str | None = None,
        chunk_limit: int = 0,
        progress=False,
        max_attempts: int = 3,
        reconnect_retries: int = 3,
        rewrite_exists: bool = True,
        recv_timeout: float = 2.0,
    ) -> Path:
        """Download one SD-card recording.

        :param file: File dict, `SdCardFile`, or path/name string.
        :param output: Output directory or complete output file path.
        :param stream_type: Explicit stream type, for example `mainStream` or
            `subStream`. Mutually exclusive with `quality`.
        :param quality: Quality alias such as `high`/`main` or `low`/`sub`.
            Mutually exclusive with `stream_type`.
        :param chunk_limit: Optional low-level chunk limit for diagnostics.
        :param progress: `True` to print progress, or a callable accepting a
            progress string.
        :param max_attempts: Maximum number of protocol download strategies to
            try for one connection.
        :param reconnect_retries: Number of reconnect attempts after an
            interrupted download before raising `CameraConnectionError`.
        :param rewrite_exists: When `False`, skip an already finalized local
            file. Non-empty `.mp4` files are treated as complete.
        :param recv_timeout: Per-read timeout while waiting for download data.
        """
        item = _file_to_dict(file)
        raw = _download_raw(item)
        requested_stream = _normalize_download_stream_type(stream_type=stream_type, quality=quality)
        if requested_stream:
            raw["streamType"] = requested_stream
            raw["_streamTypeForced"] = True
        file_id = raw.get("Id") or item.get("path") or item.get("file_name") or str(file)
        file_name = _download_output_file_name(item, raw, file_id, str(file))
        output_path = Path(output)
        if output_path.is_dir() or str(output).endswith(("/", "\\")):
            output_path.mkdir(parents=True, exist_ok=True)
            output_path = output_path / Path(file_name).name
        else:
            output_path.parent.mkdir(parents=True, exist_ok=True)

        expected_size = _int_or_none(item.get("size")) or _file_size(raw)
        if not rewrite_exists and _existing_download_matches(output_path, expected_size):
            _remove_stale_part_files(output_path)
            _emit_progress_message(progress, f"  skipped existing file: {output_path}")
            return output_path
        if not rewrite_exists and output_path.exists():
            _emit_progress_message(progress, _existing_download_mismatch_message(output_path, expected_size))

        self.last_download_attempts = []
        while True:
            try:
                return self._download_once(
                    item,
                    dict(raw),
                    str(file_id),
                    output_path,
                    expected_size=expected_size,
                    chunk_limit=chunk_limit,
                    progress=progress,
                    max_attempts=max_attempts,
                    recv_timeout=recv_timeout,
                )
            except DownloadSizeMismatch as exc:
                _emit_progress_message(progress, f"  download incomplete: {exc}")
                self._reconnect_for_download(file_name, reconnect_retries, progress, exc)
            except TimeoutError as exc:
                _emit_progress_message(progress, f"  download failed: {type(exc).__name__}: {exc}")
                self._reconnect_for_download(file_name, reconnect_retries, progress, exc)

    def _reconnect_for_download(
        self,
        file_name: str,
        reconnect_retries: int,
        progress,
        cause: BaseException,
    ) -> None:
        attempts = max(reconnect_retries, 0)
        if attempts <= 0:
            raise CameraConnectionError(
                f"Camera connection is unavailable while downloading {file_name}: {type(cause).__name__}: {cause}"
            ) from None
        last_error: BaseException = cause
        for attempt in range(1, attempts + 1):
            _emit_progress_message(progress, f"  reconnect attempt {attempt}/{attempts} after 5s: {file_name}")
            monotonic_clock.sleep(5)
            try:
                self.camera.reconnect()
                _emit_progress_message(progress, f"  reconnect ok: {file_name}")
                return
            except Exception as exc:
                last_error = exc
                message = (
                    f"SD download reconnect failed for {file_name}: "
                    f"{type(exc).__name__}: {exc}"
                )
                self.last_download_attempts.append(message)
                _emit_progress_message(progress, f"  {message}")
        raise CameraConnectionError(
            f"Camera connection is unavailable after {attempts} reconnect attempt(s) while downloading "
            f"{file_name}: {type(last_error).__name__}: {last_error}"
        ) from None

    def _download_once(
        self,
        item: dict,
        raw: dict,
        file_id: str,
        output_path: Path,
        *,
        expected_size: int | None,
        chunk_limit: int,
        progress,
        max_attempts: int,
        recv_timeout: float,
    ) -> Path:
        forced_high = _is_forced_high_quality(raw)
        last_error = None
        best_mismatch: tuple[str, int] | None = None
        self._playback_channel_id = random.randint(16, 63)
        raw["_playbackChannelId"] = self._playback_channel_id
        effective_max_attempts = max(max_attempts, 5) if forced_high else max_attempts
        for index, query in enumerate(_download_queries(self.camera.config.channel_id, str(file_id), raw)):
            if effective_max_attempts and index >= effective_max_attempts:
                break
            part_path = output_path.with_name(output_path.name + f".{_safe_label(query.label)}.part")
            _remove_file(part_path)
            try:
                if query.label.startswith("replay5/"):
                    self._prepare_replay_download(raw)
                written = self._download_with_query(
                    query,
                    part_path,
                    expected_size=expected_size,
                    chunk_limit=chunk_limit,
                    idle_timeouts=10,
                    progress=progress,
                    recv_timeout=recv_timeout,
                )
                if query.label.startswith("replay5/"):
                    self._stop_replay_download(raw)
                if query.label.startswith("playback143/"):
                    self._stop_playback_download()
            except TimeoutError as exc:
                if query.label.startswith("replay5/"):
                    self._stop_replay_download(raw)
                if query.label.startswith("playback143/"):
                    self._stop_playback_download()
                self.last_download_attempts.append(f"{query.label}: timeout{_transport_snapshot_text(self.camera.sock)}")
                _remove_empty_file(part_path)
                last_error = exc
                continue
            except ProtocolError as exc:
                if query.label.startswith("replay5/"):
                    self._stop_replay_download(raw)
                if query.label.startswith("playback143/"):
                    self._stop_playback_download()
                self.last_download_attempts.append(f"{query.label}: {exc}{_transport_snapshot_text(self.camera.sock)}")
                _remove_empty_file(part_path)
                last_error = exc
                continue
            except Exception as exc:
                if query.label.startswith("replay5/"):
                    self._stop_replay_download(raw)
                    self.last_download_attempts.append(f"{query.label}: {type(exc).__name__}: {exc}{_transport_snapshot_text(self.camera.sock)}")
                    _remove_empty_file(part_path)
                    last_error = exc
                    continue
                if query.label.startswith("playback143/"):
                    self._stop_playback_download()
                raise
            detail = f", {self._last_download_detail}" if self._last_download_detail else ""
            self.last_download_attempts.append(f"{query.label}: wrote {written} bytes{detail}")
            if written:
                if expected_size is not None and written != expected_size and (forced_high or not query.label.startswith("playback143/")):
                    _remove_file(part_path)
                    if best_mismatch is None or written > best_mismatch[1]:
                        best_mismatch = (query.label, written)
                    self._reconnect_after_download()
                    continue
                return _finalize_download(part_path, output_path, expected_size if forced_high else (None if query.label.startswith("playback143/") else expected_size))
            _remove_empty_file(part_path)
        if best_mismatch and expected_size is not None:
            label, written = best_mismatch
            raise DownloadSizeMismatch(
                const_msg.Error.SdBestAttemptMismatch.format(
                    label=label,
                    written=written,
                    expected_size=expected_size,
                    attempts=", ".join(self.last_download_attempts),
                )
            ) from last_error
        raise ProtocolError(const_msg.Error.SdDownloadFailed.format(attempts=", ".join(self.last_download_attempts))) from last_error

    def _download_with_query(
        self,
        query: _FileInfoQuery,
        output_path: Path,
        *,
        expected_size: int | None,
        chunk_limit: int,
        idle_timeouts: int,
        progress,
        recv_timeout: float,
    ) -> int:
        replay_mode = query.label.startswith("replay5/")
        playback_mode = query.label.startswith("playback143/")
        msg_class = query.msg_class if query.msg_class is not None else (MSG_CLASS.FILE_DOWNLOAD if not replay_mode else MSG_CLASS.MODERN)
        msg_num = self.camera.send(query.msg_id, query.payload, msg_class=msg_class, channel_id=query.channel_id, msg_num=query.msg_num)
        accepted_msg_nums = {msg_num}
        chunks = 0
        written = 0
        effective_expected_size = expected_size
        deadline_misses = 0
        self._last_download_detail = ""
        max_raw_payload_len = 0
        max_payload_len = 0
        encrypted_lens: set[int] = set()
        startup_idle_seconds = max(recv_timeout * 2, 2.0)
        active_idle_seconds = max(recv_timeout * idle_timeouts, 20.0)
        last_progress = monotonic_clock.monotonic()
        next_progress_at = 0
        progress_step = 512 * 1024
        next_keepalive_at = monotonic_clock.monotonic()
        with output_path.open("wb") as fh:
            while True:
                next_keepalive_at = self._send_download_keepalive(next_keepalive_at)
                try:
                    msg = self.camera._recv(timeout=recv_timeout)
                except InvalidMagicError as exc:
                    if exc.data:
                        payload = _clip_payload(exc.data, written, effective_expected_size)
                        fh.write(payload)
                        written += len(payload)
                    raw_written = self._copy_raw_download_tail(
                        fh,
                        None if effective_expected_size is None else max(effective_expected_size - written, 0),
                        progress=progress,
                        written=written,
                        expected_size=effective_expected_size,
                        chunks=chunks,
                        recv_timeout=recv_timeout,
                        idle_timeouts=idle_timeouts,
                    )
                    written += raw_written
                    if effective_expected_size is not None and written >= effective_expected_size:
                        self._last_download_detail = (
                            f"completed with raw tail after invalid Baichuan magic 0x{exc.magic:08x}, "
                            f"raw_tail={raw_written}, chunks={chunks}, msg_nums={len(accepted_msg_nums)}"
                        )
                    else:
                        self._last_download_detail = (
                            f"stopped after invalid Baichuan magic 0x{exc.magic:08x}, "
                            f"raw_tail={raw_written}, chunks={chunks}, msg_nums={len(accepted_msg_nums)}"
                        )
                    break
                except TimeoutError:
                    deadline_misses += 1
                    if written and effective_expected_size is not None and written >= effective_expected_size:
                        self._last_download_detail = f"complete after timeout, chunks={chunks}, msg_nums={len(accepted_msg_nums)}"
                        break
                    if written and monotonic_clock.monotonic() - last_progress < active_idle_seconds:
                        continue
                    if written:
                        self._last_download_detail = f"idle timeout after {deadline_misses} recv timeouts, chunks={chunks}, msg_nums={len(accepted_msg_nums)}"
                        break
                    raise
                if not _is_download_message(msg, query.msg_id, accepted_msg_nums, written > 0):
                    idle_limit = active_idle_seconds if written else startup_idle_seconds
                    if monotonic_clock.monotonic() - last_progress >= idle_limit:
                        if written:
                            self._last_download_detail = (
                                f"no download progress after unrelated messages, chunks={chunks}, "
                                f"msg_nums={len(accepted_msg_nums)}"
                            )
                            break
                        raise TimeoutError(const_msg.Error.SdDownloadTimeout)
                    continue
                accepted_msg_nums.add(msg.header.msg_num)
                deadline_misses = 0
                max_raw_payload_len = max(max_raw_payload_len, getattr(msg, "raw_payload_len", 0))
                max_payload_len = max(max_payload_len, len(msg.payload))
                if getattr(msg, "encrypted_len", None) is not None:
                    encrypted_lens.add(msg.encrypted_len)
                replay_payload = replay_mode and msg.header.msg_id == MSG.FILE_REPLAY and bool(msg.payload)
                if replay_mode and msg.header.response_code == 201:
                    if msg.payload:
                        payload = _clip_payload(msg.payload, written, effective_expected_size)
                        fh.write(payload)
                        written += len(payload)
                    self._last_download_detail = f"replay finished response=201, chunks={chunks}, msg_nums={len(accepted_msg_nums)}"
                    break
                if playback_mode and msg.header.response_code == 300:
                    self._last_download_detail = f"playback finished response=300, chunks={chunks}, msg_nums={len(accepted_msg_nums)}"
                    break
                if msg.header.response_code not in (0, 200) and not replay_payload:
                    if written:
                        self._last_download_detail = (
                            f"stopped after response {msg.header.response_code}, chunks={chunks}, "
                            f"msg_nums={len(accepted_msg_nums)}"
                        )
                        break
                    raise ProtocolError(_response_detail(msg, const_msg.Error.Response.format(response_code=msg.header.response_code)))
                if b"<binaryData>1</binaryData>" in msg.extension:
                    self.camera.binary_msg_nums.add(msg_num)
                    self.camera.binary_msg_nums.add(msg.header.msg_num)
                xml_text = msg.xml_text
                if xml_text and _looks_like_xml(xml_text):
                    self.last_xml = xml_text
                    if playback_mode:
                        playback_size = _xml_file_size(xml_text)
                        if playback_size:
                            effective_expected_size = playback_size
                            self.camera.binary_msg_nums.add(msg_num)
                            self.camera.binary_msg_nums.add(msg.header.msg_num)
                            continue
                    if _download_xml_done_text(xml_text):
                        self._last_download_detail = (
                            f"done xml response={msg.header.response_code}, chunks={chunks}, "
                            f"msg_nums={len(accepted_msg_nums)}, xml={_one_line_preview(xml_text)}"
                        )
                        break
                    self.camera.binary_msg_nums.add(msg_num)
                    self.camera.binary_msg_nums.add(msg.header.msg_num)
                    continue
                if msg.payload:
                    payload = _clip_payload(msg.payload, written, effective_expected_size)
                    fh.write(payload)
                    written += len(payload)
                    last_progress = monotonic_clock.monotonic()
                    chunks += 1
                    if progress and written >= next_progress_at:
                        _emit_progress(progress, written, effective_expected_size, chunks, self.camera.sock)
                        next_progress_at = written + progress_step
                    if effective_expected_size is not None and written >= effective_expected_size:
                        break
                    if chunk_limit and chunks >= chunk_limit:
                        break
                    continue
                if not msg.payload:
                    if effective_expected_size is not None and written < effective_expected_size and monotonic_clock.monotonic() - last_progress < active_idle_seconds:
                        continue
                    if written:
                        self._last_download_detail = f"empty payload response={msg.header.response_code}, chunks={chunks}, msg_nums={len(accepted_msg_nums)}"
                        break
                    self._last_download_detail = f"empty payload before data response={msg.header.response_code}, msg_nums={len(accepted_msg_nums)}"
                    break
                if deadline_misses > 1:
                    self._last_download_detail = f"deadline misses={deadline_misses}, chunks={chunks}, msg_nums={len(accepted_msg_nums)}"
                    break
        if written and not self._last_download_detail:
            self._last_download_detail = f"loop ended, chunks={chunks}, msg_nums={len(accepted_msg_nums)}"
        if written:
            if max_raw_payload_len:
                payload_detail = f"max_payload={max_payload_len}, max_raw_payload={max_raw_payload_len}"
                if encrypted_lens:
                    lens = ",".join(str(item) for item in sorted(encrypted_lens))
                    payload_detail = f"{payload_detail}, encryptLen={lens}"
                if effective_expected_size is not None and effective_expected_size != expected_size:
                    payload_detail = f"{payload_detail}, effective_expected={effective_expected_size}"
                self._last_download_detail = (
                    f"{self._last_download_detail}, {payload_detail}"
                    if self._last_download_detail
                    else payload_detail
                )
            snapshot = _transport_snapshot_text(self.camera.sock)
            if snapshot:
                self._last_download_detail = f"{self._last_download_detail}{snapshot}"
        return written

    def _copy_raw_download_tail(
        self,
        fh,
        remaining: int | None,
        *,
        progress,
        written: int,
        expected_size: int | None,
        chunks: int,
        recv_timeout: float,
        idle_timeouts: int,
    ) -> int:
        sock = self.camera.sock
        if sock is None or not hasattr(sock, "recv_some"):
            return 0
        copied = 0
        deadline_misses = 0
        next_progress_at = written + 512 * 1024
        while remaining is None or copied < remaining:
            try:
                limit = 64 * 1024 if remaining is None else min(remaining - copied, 64 * 1024)
                if limit <= 0:
                    break
                chunk = sock.recv_some(limit)
            except TimeoutError:
                deadline_misses += 1
                if deadline_misses >= idle_timeouts:
                    break
                continue
            if not chunk:
                break
            deadline_misses = 0
            fh.write(chunk)
            copied += len(chunk)
            current = written + copied
            if progress and current >= next_progress_at:
                _emit_progress(progress, current, expected_size, chunks, self.camera.sock)
                next_progress_at = current + 512 * 1024
        return copied

    def _prepare_replay_download(self, raw: dict) -> None:
        name = str(raw.get("name") or raw.get("fileName") or "")
        start_time = _parse_time(raw, "startTime")
        stream_type = str(raw.get("streamType") or "mainStream")
        if not name or not start_time:
            raise ProtocolError(const_msg.Error.SdReplayNeedsNameAndStart)
        channel = self.camera.config.channel_id
        seq = int(start_time.timestamp())
        seek_payload = payloads.replay_seek.format(channel_id=channel, seq=seq, seek_time=_time_fragment("seekTime", start_time))
        seek_reply = self.camera.command(MSG.REPLAY_SEEK, seek_payload)
        if seek_reply.header.response_code != 200:
            raise ProtocolError(const_msg.Error.ReplaySeekFailed.format(response_code=seek_reply.header.response_code))

        detail_payload = payloads.replay_file_detail.format(channel_id=channel, name=name, stream_type=stream_type)
        detail_reply = self.camera.command(MSG.FILE_DOWNLOAD, detail_payload)
        if detail_reply.header.response_code != 200:
            raise ProtocolError(const_msg.Error.ReplayFileDetailFailed.format(response_code=detail_reply.header.response_code))
        self._active_replay_name = name

    def _stop_replay_download(self, raw: dict) -> None:
        name = self._active_replay_name or str(raw.get("name") or raw.get("fileName") or "")
        self._active_replay_name = None
        if not name:
            return
        payload = payloads.replay_stop.format(channel_id=self.camera.config.channel_id, name=name)
        try:
            self.camera.command(MSG.FILE_REPLAY_STOP, payload)
        except Exception:
            pass

    def _stop_playback_download(self) -> None:
        try:
            self.camera.send(MSG.FILE_PLAYBACK_STOP, channel_id=random.randint(64, 255), msg_num=0)
        except Exception:
            pass

    def _send_download_keepalive(self, next_at: float) -> float:
        now = monotonic_clock.monotonic()
        if now < next_at:
            return next_at
        try:
            self.camera.send(MSG.UDP_KEEPALIVE, channel_id=0, msg_num=0)
        except Exception:
            return now + 0.75
        return now + 0.75

    def _reconnect_after_download(self) -> None:
        try:
            self.camera.close()
            self.camera.connect()
            self.camera.login()
        except Exception as exc:
            self.last_download_attempts.append(f"reconnect-after-download: {type(exc).__name__}: {exc}")

    def remove(self, file: dict | SdCardFile | str, *, confirm: bool = False) -> None:
        """Remove an SD-card file.

        :param file: File dict, `SdCardFile`, or path/name string.
        :param confirm: Must be `True` to allow the operation.
        """
        if not confirm:
            raise DangerousSdCardOperation(const_msg.Error.SdRemoveNeedsConfirm)
        raise NotImplementedError(const_msg.Error.SdRemoveNotImplemented)

    def format(self, *, confirm: bool = False, confirmation_text: str = "", disk_id: int = 0) -> None:
        """Format the camera SD card.

        :param confirm: Must be `True` to allow formatting.
        :param confirmation_text: Must be exactly `FORMAT SD CARD`.
        :param disk_id: Camera disk id, usually 0.
        """
        if not confirm or confirmation_text != "FORMAT SD CARD":
            raise DangerousSdCardOperation(const_msg.Error.SdFormatNeedsConfirm)
        payload = payloads.hdd_init.format(disk_id=disk_id)
        reply = self.camera.command(MSG.HDD_INIT, payload)
        if reply.header.response_code != 200:
            raise ProtocolError(const_msg.Error.SdFormatFailed.format(response_code=reply.header.response_code))

    def disk_info(self) -> dict:
        """Return parsed SD-card disk information."""
        reply = self.camera.command(MSG.HDD_INFO)
        if reply.header.response_code != 200:
            raise ProtocolError(const_msg.Error.SdDiskInfoFailed.format(response_code=reply.header.response_code))
        return xml_to_dict(reply.xml_text or "")

    def day_records(self, day: date | str | None = None) -> dict:
        """Return raw day-record information for one day.

        :param day: Target date. Defaults to today.
        """
        target = _coerce_date(day or date.today())
        attempts = []
        for query in _day_record_queries(self.camera.config.channel_id, target):
            reply = self.camera.command(query.msg_id, query.payload, extension=query.extension)
            attempts.append(f"{query.label}: {reply.header.response_code}")
            if reply.header.response_code == 200:
                self.last_attempts = attempts
                self.last_xml = reply.xml_text
                return xml_to_dict(reply.xml_text or "")
        self.last_attempts = attempts
        raise ProtocolError(const_msg.Error.SdDayRecordsFailed.format(attempts=", ".join(attempts)))

    def preview(
        self,
        file: dict | SdCardFile | str,
        *,
        debug: bool = False,
        stream_type: str = "mainStream",
        channel_id: int | None = None,
        max_attempts: int = 0,
        binary_probe_bytes: int = 256 * 1024,
        binary_probe_idle: float = 0.5,
    ) -> bytes | list[dict]:
        """Try experimental SD-card preview/thumbnail requests.

        :param file: File dict, `SdCardFile`, or path/name string.
        :param debug: Return all response diagnostics instead of only JPEG bytes.
        :param stream_type: Stream type to request in preview payloads.
        :param channel_id: Optional channel override.
        :param max_attempts: Maximum preview strategies to try. `0` tries all.
        :param binary_probe_bytes: Maximum additional binary bytes to collect
            for debug responses that look like BCMedia.
        :param binary_probe_idle: Idle timeout while collecting debug binary
            continuation payloads.
        """

        item = _file_to_dict(file)
        raw = _download_raw(item)
        raw.setdefault("streamType", stream_type)
        channel = self.camera.config.channel_id if channel_id is None else channel_id
        file_id = raw.get("Id") or item.get("path") or item.get("file_name") or str(file)
        attempts = []
        queries = list(_preview_queries(channel, str(file_id), raw))
        seen_handle_queries: set[tuple[str, str]] = set()
        index = 0
        while index < len(queries):
            if max_attempts and index >= max_attempts:
                break
            query = queries[index]
            index += 1
            try:
                msg_num = self.camera.send(
                    query.msg_id,
                    query.payload,
                    extension=query.extension,
                    msg_class=query.msg_class if query.msg_class is not None else MSG_CLASS.MODERN,
                    channel_id=query.channel_id,
                    msg_num=query.msg_num,
                )
                reply = self.camera._recv_matching(query.msg_id, msg_num)
                detail = _preview_response(query.label, reply)
                if debug and _preview_should_collect_binary(detail):
                    _merge_preview_binary_probe(
                        detail,
                        self._collect_preview_binary(
                            msg_num,
                            query.msg_id,
                            first_payload=reply.payload,
                            max_bytes=binary_probe_bytes,
                            idle_timeout=binary_probe_idle,
                        ),
                    )
                attempts.append(detail)
                for handle in _preview_handles(reply.xml_text) if "/handle-" not in query.label else []:
                    key = (query.label, handle)
                    if key in seen_handle_queries:
                        continue
                    seen_handle_queries.add(key)
                    queries.extend(_preview_handle_queries(channel, handle, query.label))
                if detail["jpeg"] and not debug:
                    return reply.payload
            except Exception as exc:
                attempts.append({"label": query.label, "error": f"{type(exc).__name__}: {exc}"})
        self.last_attempts = [_preview_attempt_text(item) for item in attempts]
        if debug:
            return attempts
        raise ProtocolError(const_msg.Error.SdPreviewFailed.format(attempts=", ".join(self.last_attempts)))

    def _collect_preview_binary(
        self,
        msg_num: int,
        query_msg_id: int,
        *,
        first_payload: bytes,
        max_bytes: int,
        idle_timeout: float,
    ) -> bytes:
        data = bytearray(first_payload or b"")
        deadline = monotonic_clock.monotonic() + idle_timeout
        while len(data) < max_bytes:
            try:
                msg = self.camera._recv(timeout=min(idle_timeout, 0.5))
            except TimeoutError:
                break
            if msg.header.msg_num != msg_num and not _is_download_continuation(msg, query_msg_id, bool(data)):
                if monotonic_clock.monotonic() >= deadline:
                    break
                continue
            if msg.header.response_code not in (0, 200):
                break
            if not msg.payload:
                if monotonic_clock.monotonic() >= deadline:
                    break
                continue
            data.extend(msg.payload[: max(max_bytes - len(data), 0)])
            deadline = monotonic_clock.monotonic() + idle_timeout
            if _preview_binary_has_video_frame(data):
                break
        return bytes(data)

    def preview_dump(
        self,
        file: dict | SdCardFile | str,
        output: str | Path,
        *,
        stream_type: str = "mainStream",
        channel_id: int | None = None,
        raw: bool = False,
        progress=False,
        recv_timeout: float = 2.0,
        idle_timeouts: int = 10,
        max_bytes: int | None = None,
    ) -> Path:
        """Dump the experimental `preview8/thumbnail/class6482` binary response.

        :param file: File dict, `SdCardFile`, or path/name string.
        :param output: Output path or directory. MP4 output is used by default.
        :param stream_type: Stream type to request in preview payloads.
        :param channel_id: Optional channel override.
        :param raw: Save raw camera bytes including the leading `1002` header.
            When `False`, save the embedded MP4 starting at `ftyp`.
        :param progress: `True` to print progress, or a callable accepting a
            progress string.
        :param recv_timeout: Per-read timeout while waiting for preview data.
        :param idle_timeouts: Number of idle read timeouts before stopping.
        :param max_bytes: Optional maximum raw bytes to collect.
        """

        item = _file_to_dict(file)
        raw_item = _download_raw(item)
        raw_item.setdefault("streamType", stream_type)
        channel = self.camera.config.channel_id if channel_id is None else channel_id
        file_id = raw_item.get("Id") or item.get("path") or item.get("file_name") or str(file)
        file_name = item.get("file_name") or Path(str(file_id)).name or "preview.mp4"
        output_path = _preview_output_path(output, file_name, raw=raw)
        query = _preview_dump_query(channel, str(file_id), raw_item)
        payload = self._read_preview_dump(
            query,
            progress=progress,
            recv_timeout=recv_timeout,
            idle_timeouts=idle_timeouts,
            max_bytes=max_bytes,
        )
        data = payload if raw else _extract_embedded_mp4_bytes(payload)
        if not data:
            raise ProtocolError(const_msg.Error.SdPreviewFailed.format(attempts="preview8/thumbnail/class6482 returned no MP4 data"))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(data)
        _emit_progress_message(progress, f"  saved preview dump: {output_path} ({len(data)} bytes)")
        return output_path

    def _cache_preview(
        self,
        file: dict | SdCardFile | str,
        output_path: Path,
        *,
        stream_type: str,
        channel_id: int | None,
        max_bytes: int | None,
        progress,
        recv_timeout: float,
        idle_timeouts: int,
        ready: threading.Event,
        stop: threading.Event,
    ) -> Path:
        item = _file_to_dict(file)
        raw_item = _download_raw(item)
        raw_item.setdefault("streamType", stream_type)
        channel = self.camera.config.channel_id if channel_id is None else channel_id
        file_id = raw_item.get("Id") or item.get("path") or item.get("file_name") or str(file)
        query = _preview_dump_query(channel, str(file_id), raw_item)
        msg_num = self.camera.send(
            query.msg_id,
            query.payload,
            extension=query.extension,
            msg_class=query.msg_class if query.msg_class is not None else MSG_CLASS.MODERN,
            channel_id=query.channel_id,
            msg_num=query.msg_num,
        )
        reply = self.camera._recv_matching(query.msg_id, msg_num)
        if reply.header.response_code not in (0, 200):
            raise ProtocolError(_response_detail(reply, const_msg.Error.Response.format(response_code=reply.header.response_code)))

        raw_seen = 0
        mp4_offset: int | None = None
        mp4_started = False
        head = bytearray()
        expected_total = 0
        deadline_misses = 0
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("wb") as fh:
            raw_seen, mp4_offset, mp4_started, expected_total = _write_preview_cache_payload(
                fh,
                reply.payload or b"",
                head=head,
                raw_seen=raw_seen,
                mp4_offset=mp4_offset,
                mp4_started=mp4_started,
                expected_total=expected_total,
                ready=ready,
            )
            while not stop.is_set():
                if expected_total and raw_seen >= expected_total:
                    break
                if max_bytes is not None and raw_seen >= max_bytes:
                    break
                try:
                    msg = self.camera._recv(timeout=recv_timeout)
                except TimeoutError:
                    deadline_misses += 1
                    if deadline_misses >= idle_timeouts:
                        break
                    continue
                if msg.header.msg_num != msg_num and not _is_download_continuation(msg, query.msg_id, mp4_started or bool(head)):
                    continue
                if msg.header.response_code not in (0, 200):
                    break
                if not msg.payload:
                    continue
                deadline_misses = 0
                remaining = None if max_bytes is None else max(max_bytes - raw_seen, 0)
                payload = msg.payload if remaining is None else msg.payload[:remaining]
                raw_seen, mp4_offset, mp4_started, expected_total = _write_preview_cache_payload(
                    fh,
                    payload,
                    head=head,
                    raw_seen=raw_seen,
                    mp4_offset=mp4_offset,
                    mp4_started=mp4_started,
                    expected_total=expected_total,
                    ready=ready,
                )
                if progress and raw_seen % (512 * 1024) < len(payload):
                    total_text = f"/{expected_total}" if expected_total else ""
                    _emit_progress_message(progress, f"  preview cache bytes: {raw_seen}{total_text}")
        ready.set()
        return output_path

    def _read_preview_dump(
        self,
        query: _FileInfoQuery,
        *,
        progress,
        recv_timeout: float,
        idle_timeouts: int,
        max_bytes: int | None,
    ) -> bytes:
        msg_num = self.camera.send(
            query.msg_id,
            query.payload,
            extension=query.extension,
            msg_class=query.msg_class if query.msg_class is not None else MSG_CLASS.MODERN,
            channel_id=query.channel_id,
            msg_num=query.msg_num,
        )
        reply = self.camera._recv_matching(query.msg_id, msg_num)
        if reply.header.response_code not in (0, 200):
            raise ProtocolError(_response_detail(reply, const_msg.Error.Response.format(response_code=reply.header.response_code)))
        data = bytearray(reply.payload or b"")
        expected_total = _embedded_mp4_total_size(data)
        deadline_misses = 0
        while True:
            if expected_total and len(data) >= expected_total:
                break
            if max_bytes is not None and len(data) >= max_bytes:
                break
            try:
                msg = self.camera._recv(timeout=recv_timeout)
            except TimeoutError:
                deadline_misses += 1
                if deadline_misses >= idle_timeouts:
                    break
                continue
            if msg.header.msg_num != msg_num and not _is_download_continuation(msg, query.msg_id, bool(data)):
                continue
            if msg.header.response_code not in (0, 200):
                break
            if not msg.payload:
                continue
            remaining = None if max_bytes is None else max(max_bytes - len(data), 0)
            chunk = msg.payload if remaining is None else msg.payload[:remaining]
            data.extend(chunk)
            deadline_misses = 0
            expected_total = expected_total or _embedded_mp4_total_size(data)
            if progress and len(data) % (512 * 1024) < len(chunk):
                total_text = f"/{expected_total}" if expected_total else ""
                _emit_progress_message(progress, f"  preview dump bytes: {len(data)}{total_text}")
        return bytes(data)


def _parse_file_list(root: ET.Element | None) -> list[SdCardFile]:
    if root is None:
        return []
    if root.tag == "FileInfo":
        return [_parse_file_node(root)]
    nodes = root.findall(".//FileInfo")
    if not nodes:
        nodes = root.findall(".//file")
    if not nodes:
        nodes = [
            node
            for node in root.iter()
            if node is not root
            and node.tag.lower() in {"fileinfo", "file", "record", "dayrecord", "recordtime", "timeblock"}
            and len(node) > 0
        ]
    if not nodes:
        nodes = [
            node
            for node in root.iter()
            if node is not root
            and len(node) > 0
            and any(child.tag in _TIME_OR_FILE_KEYS for child in node)
        ]
    return [_parse_file_node(node) for node in nodes]


def _parse_file_node(node: ET.Element) -> SdCardFile:
    raw = _element_to_dict(node)
    file_name = _first(raw, "fileName", "name", "file")
    return SdCardFile(
        file_name=file_name,
        path=_first(raw, "path", "filePath", "Id", "dir"),
        size=_file_size(raw),
        start_time=_parse_time(raw, "beginTime", "startTime", "start", "begin", "time"),
        end_time=_parse_time(raw, "endTime", "stopTime", "end", "stop"),
        stream_type=_first(raw, "streamType"),
        file_type=_first(raw, "fileType", "recordType", "type"),
        channel_id=_int_or_none(_first(raw, "channelId")),
        raw=raw,
    )


def _date_range(start: datetime | date | str | None, end: datetime | date | str | None) -> tuple[datetime, datetime]:
    if start is None and end is None:
        today = date.today()
        return datetime.combine(today, time.min), datetime.combine(today, time.max)
    start_dt = _coerce_datetime(start or end, end_of_day=False)
    end_dt = _coerce_datetime(end or start, end_of_day=True)
    return start_dt, end_dt


@dataclass(frozen=True)
class _FileInfoQuery:
    label: str
    msg_id: int
    payload: bytes
    extension: bytes = b""
    msg_class: int | None = None
    channel_id: int | None = None
    msg_num: int | None = None


_TIME_OR_FILE_KEYS = {
    "fileName",
    "name",
    "file",
    "path",
    "filePath",
    "beginTime",
    "startTime",
    "endTime",
    "stopTime",
    "beginHour",
    "endHour",
    "time",
}

_STREAM_TYPE_CANDIDATES = ("mainStream", "subStream", "clear", "fluent")
_FILE_TYPE_CANDIDATES = ("All", "all", "Rec", "rec", "record", "Record", "MD", "motion", "alarm", "human", "vehicle", "visitor")


def _file_info_queries(channel: int, start: datetime, end: datetime, stream_type: str, file_type: str) -> list[_FileInfoQuery]:
    msg_ids = [
        ("replay", MSG.FILE_REPLAY),
        ("info14", MSG.FILE_INFO_LIST),
        ("info15", MSG.FILE_INFO_LIST_ALT),
        ("info16", MSG.FILE_INFO_LIST_ALT2),
    ]
    variants = [
        *[
            (
                f"compact-{type_value}",
                payloads.file_info_compact_type.format(channel_id=channel, start=start, end=end, type_value=type_value),
            )
            for type_value in _FILE_TYPE_CANDIDATES
        ],
        *[
            (
                f"compact-{stream_value}",
                payloads.file_info_compact_stream.format(channel_id=channel, start=start, end=end, stream_type=stream_value),
            )
            for stream_value in _STREAM_TYPE_CANDIDATES
        ],
        *[
            (
                f"compact-{stream_value}-{type_value}",
                payloads.file_info_compact_stream_type.format(channel_id=channel, start=start, end=end, stream_type=stream_value, type_value=type_value),
            )
            for stream_value in _STREAM_TYPE_CANDIDATES
            for type_value in _FILE_TYPE_CANDIDATES
        ],
        (
            "nested-basic",
            payloads.file_info_nested.format(
                channel_id=channel,
                stream_type=stream_type,
                start_time=_time_fragment("beginTime", start),
                end_time=_time_fragment("endTime", end),
            ),
        ),
        (
            "nested-type",
            payloads.file_info_nested_type.format(
                channel_id=channel,
                stream_type=stream_type,
                file_type=file_type,
                start_time=_time_fragment("beginTime", start),
                end_time=_time_fragment("endTime", end),
            ),
        ),
        (
            "start-end",
            payloads.file_info_nested.format(
                channel_id=channel,
                stream_type=stream_type,
                start_time=_time_fragment("startTime", start),
                end_time=_time_fragment("endTime", end),
            ),
        ),
        (
            "flat",
            payloads.file_info_flat.format(
                channel_id=channel,
                stream_type=stream_type,
                begin_time=_flat_time_fragment("begin", start),
                end_time=_flat_time_fragment("end", end),
            ),
        ),
        (
            "compact",
            payloads.file_info_compact_stream.format(channel_id=channel, start=start, end=end, stream_type=stream_type),
        ),
    ]
    queries = []
    ext = payloads.extension.format(channel_id=channel)
    for payload_label, payload in variants:
        for msg_label, msg_id in msg_ids:
            queries.append(_FileInfoQuery(f"{msg_label}/{payload_label}", msg_id, payload))
            queries.append(_FileInfoQuery(f"{msg_label}/{payload_label}+ext", msg_id, payload, ext))
    return queries


def _day_records_range_query(channel: int, start: datetime, end: datetime) -> _FileInfoQuery:
    return _FileInfoQuery(
        "day-records/range",
        MSG.DAY_RECORDS,
        payloads.day_records_range.format(
            channel_id=channel,
            start_time=_time_fragment("startTime", start),
            end_time=_time_fragment("endTime", end),
        ),
    )


def _handle_queries(channel: int, start: datetime, end: datetime, stream_type: str) -> list[_FileInfoQuery]:
    record_types = "manual, sched, io, md, people, face, vehicle, dog_cat, visitor"
    streams = [stream_type]
    if stream_type != "subStream":
        streams.append("subStream")
    queries = []
    for stream in streams:
        payload = payloads.file_handle_request.format(
            channel_id=channel,
            stream_type=stream,
            record_types=record_types,
            start_time=_time_fragment("startTime", start),
            end_time=_time_fragment("endTime", end),
        )
        queries.append(_FileInfoQuery(f"handle/{stream}", MSG.FILE_INFO_LIST, payload))
        queries.append(_FileInfoQuery(f"handle/{stream}+ext", MSG.FILE_INFO_LIST, payload, payloads.extension.format(channel_id=channel)))
    return queries


def _handle_detail_queries(channel: int, handle: str) -> list[_FileInfoQuery]:
    payload = payloads.files_for_handle.format(channel_id=channel, handle=handle)
    return [
        _FileInfoQuery(f"files/handle-{handle}", MSG.FILE_INFO_LIST_ALT, payload),
        _FileInfoQuery(f"files/handle-{handle}+ext", MSG.FILE_INFO_LIST_ALT, payload, payloads.extension.format(channel_id=channel)),
    ]


def _day_record_queries(channel: int, target: date) -> list[_FileInfoQuery]:
    ext = payloads.extension.format(channel_id=channel)
    variants = [
        (
            "nested",
            payloads.day_record_nested.format(channel_id=channel, target=target),
        ),
        (
            "compact",
            payloads.day_record_compact.format(channel_id=channel, target=target),
        ),
        (
            "empty",
            b"",
        ),
    ]
    queries = []
    for label, payload in variants:
        queries.append(_FileInfoQuery(f"day/{target}/{label}", MSG.DAY_RECORDS, payload))
        queries.append(_FileInfoQuery(f"day/{target}/{label}+ext", MSG.DAY_RECORDS, payload, ext))
    return queries


def _days_between(start: date, end: date):
    current = start
    while current <= end:
        yield current
        current = date.fromordinal(current.toordinal() + 1)


def _send_query(camera: Camera, query: _FileInfoQuery, attempts: list[str]):
    try:
        reply = camera.command(query.msg_id, query.payload, extension=query.extension)
    except TimeoutError:
        attempts.append(f"{query.label}: timeout")
        _debug(camera, f"SD query {query.label} -> timeout")
        return None
    attempts.append(f"{query.label}: {reply.header.response_code}")
    _debug(camera, f"SD query {query.label} -> {reply.header.response_code}")
    return reply


def _file_info_nodes(root: ET.Element | None) -> list[ET.Element]:
    if root is None:
        return []
    if root.tag == "FileInfo":
        return [root]
    return root.findall(".//FileInfo")


def _find_child_text(node: ET.Element, tag: str) -> str | None:
    found = node.find(tag)
    return found.text if found is not None else None


def _coerce_date(value: date | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.fromisoformat(value).date()


def _coerce_datetime(value: datetime | date | str | None, *, end_of_day: bool) -> datetime:
    if value is None:
        return datetime.combine(date.today(), time.max if end_of_day else time.min)
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time.max if end_of_day else time.min)
    text = str(value)
    if len(text) == 10:
        return datetime.combine(date.fromisoformat(text), time.max if end_of_day else time.min)
    return datetime.fromisoformat(text)


def _time_fragment(tag: str, value: datetime) -> payloads.Raw:
    return payloads.Raw(payloads.time_node.format(tag=tag, value=value))


def _flat_time_fragment(prefix: str, value: datetime) -> payloads.Raw:
    return payloads.Raw(payloads.flat_time.format(prefix=prefix, value=value))


def _parse_time(raw: dict, *keys: str) -> datetime | None:
    value = _first(raw, *keys)
    if isinstance(value, dict):
        try:
            return datetime(
                int(value.get("year", 1970)),
                int(value.get("month", 1)),
                int(value.get("day", 1)),
                int(value.get("hour", 0)),
                int(value.get("minute", 0)),
                int(value.get("second", 0)),
            )
        except (TypeError, ValueError):
            return None
    if isinstance(value, str):
        try:
            if value.isdigit() and len(value) == 14:
                return datetime.strptime(value, "%Y%m%d%H%M%S")
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _element_to_dict(node: ET.Element) -> dict:
    if len(node) == 0:
        return node.text or ""
    result: dict = {}
    for child in node:
        value = _element_to_dict(child)
        if child.tag in result:
            if not isinstance(result[child.tag], list):
                result[child.tag] = [result[child.tag]]
            result[child.tag].append(value)
        else:
            result[child.tag] = value
    return result


def _first(raw: dict, *keys: str):
    for key in keys:
        value = raw.get(key)
        if value not in (None, ""):
            return value
    return None


def _int_or_none(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _file_size(raw: dict) -> int | None:
    size = _int_or_none(_first(raw, "size", "fileSize", "length"))
    if size is not None:
        return size
    low = _int_or_none(raw.get("sizeL"))
    high = _int_or_none(raw.get("sizeH"))
    if low is None and high is None:
        return None
    return (low or 0) + ((high or 0) << 32)


def _searchable_file_text(data: dict) -> str:
    raw = data.get("raw") if isinstance(data.get("raw"), dict) else {}
    parts = [
        data.get("file_name"),
        data.get("path"),
        data.get("file_type"),
        raw.get("Id"),
        raw.get("name"),
        raw.get("fileName"),
        raw.get("fileType"),
        raw.get("recordType"),
    ]
    return " ".join(str(part) for part in parts if part)


def _file_to_dict(file: dict | SdCardFile | SDFile | str) -> dict:
    if isinstance(file, SDFile):
        return file.info()
    if isinstance(file, SdCardFile):
        return file.to_dict()
    if isinstance(file, dict):
        return dict(file)
    return {"file_name": file}


def _append_unique_files(files: list[SdCardFile], items: Iterable[SdCardFile], seen: set[tuple]) -> int:
    added = 0
    for item in items:
        identity = _file_identity(item)
        if identity in seen:
            continue
        seen.add(identity)
        files.append(item)
        added += 1
    return added


def _file_identity(file: SdCardFile) -> tuple:
    return (
        file.path or "",
        file.file_name or "",
        file.start_time,
        file.end_time,
        file.size,
    )


def _sort_recordings(files: list[SdCardFile], sort: str | None) -> None:
    if sort is None:
        return
    normalized = sort.lower()
    if normalized not in ("asc", "desc"):
        raise ValueError(const_msg.Error.SortValue)
    files.sort(key=_recording_sort_key, reverse=normalized == "desc")


def _recording_sort_key(file: SdCardFile) -> tuple[datetime, str]:
    timestamp = file.end_time or file.start_time or datetime.min
    name = file.file_name or file.path or ""
    return timestamp, name


def _download_raw(item: dict) -> dict:
    raw = dict(item.get("raw")) if isinstance(item.get("raw"), dict) else {}
    if item.get("file_name"):
        raw.setdefault("name", item["file_name"])
        raw.setdefault("fileName", item["file_name"])
    if item.get("path"):
        raw.setdefault("Id", item["path"])
    if item.get("start_time"):
        raw.setdefault("startTime", item["start_time"])
    if item.get("end_time"):
        raw.setdefault("endTime", item["end_time"])
    if item.get("stream_type"):
        raw.setdefault("streamType", item["stream_type"])
    if item.get("file_type"):
        raw.setdefault("fileType", item["file_type"])
    if item.get("channel_id") is not None:
        raw.setdefault("channelId", item["channel_id"])
    return raw


def _download_output_file_name(item: dict, raw: dict, file_id: str, fallback: str) -> str:
    base = str(item.get("file_name") or raw.get("fileName") or raw.get("name") or "").strip()
    if not base:
        base = Path(str(file_id)).name or Path(fallback).name or "download"
    suffix = Path(base).suffix
    if not suffix:
        suffix = Path(str(item.get("path") or raw.get("Id") or "")).suffix
    if not suffix:
        file_type = str(item.get("file_type") or raw.get("fileType") or "").strip().lstrip(".")
        suffix = f".{file_type}" if file_type else ""
    return f"{Path(base).stem}{suffix}" if suffix else Path(base).name


def _normalize_download_stream_type(*, stream_type: str | None, quality: str | None) -> str | None:
    if stream_type and quality:
        raise ValueError(const_msg.Error.StreamTypeOrQuality)
    value = stream_type or quality
    if value is None:
        return None
    normalized = str(value).strip()
    aliases = {
        "high": "mainStream",
        "main": "mainStream",
        "clear": "mainStream",
        "mainstream": "mainStream",
        "low": "subStream",
        "sub": "subStream",
        "fluent": "subStream",
        "substream": "subStream",
    }
    return aliases.get(normalized.lower(), normalized)


def _download_queries(channel: int, file_id: str, raw: dict) -> list[_FileInfoQuery]:
    replay_payload = _replay_download_payload(channel, raw)
    playback_payloads = _playback_download_payloads(channel, raw)
    playback_channel_id = _int_or_none(raw.get("_playbackChannelId")) or 29
    download_payloads = [
        ("id", _download_payload(channel, file_id, raw, mode="id")),
        ("filename", _download_payload(channel, file_id, raw, mode="fileName")),
        ("name", _download_payload(channel, file_id, raw, mode="name")),
        ("full", _download_payload(channel, file_id, raw, mode="full")),
    ]
    queries = []
    primary_label, primary_payload = download_payloads[0]
    full_payload = download_payloads[-1][1]
    if _is_forced_high_quality(raw):
        queries.append(_FileInfoQuery("download13/full-high/class6482", MSG.FILE_DOWNLOAD, full_payload, msg_class=MSG_CLASS.FILE_DOWNLOAD))
        queries.append(_FileInfoQuery("download8/full-high/class6482", MSG.FILE_DOWNLOAD_VIDEO, full_payload, msg_class=MSG_CLASS.FILE_DOWNLOAD))
        return queries
    queries.append(_FileInfoQuery(f"download13/{primary_label}/class6482", MSG.FILE_DOWNLOAD, primary_payload, msg_class=MSG_CLASS.FILE_DOWNLOAD))
    for label, payload in playback_payloads:
        queries.append(_FileInfoQuery(f"playback143/{label}/bcmedia", MSG.FILE_PLAYBACK, payload, msg_class=MSG_CLASS.MODERN, channel_id=playback_channel_id, msg_num=0))
    queries.append(_FileInfoQuery(f"download8/{primary_label}/class6482", MSG.FILE_DOWNLOAD_VIDEO, primary_payload, msg_class=MSG_CLASS.FILE_DOWNLOAD))
    if replay_payload:
        queries.append(_FileInfoQuery("replay5/start/bcmedia", MSG.FILE_REPLAY, replay_payload, msg_class=MSG_CLASS.MODERN))
    for label, payload in download_payloads:
        if label == primary_label:
            continue
        queries.append(_FileInfoQuery(f"download13/{label}/class6482", MSG.FILE_DOWNLOAD, payload, msg_class=MSG_CLASS.FILE_DOWNLOAD))
        queries.append(_FileInfoQuery(f"download8/{label}/class6482", MSG.FILE_DOWNLOAD_VIDEO, payload, msg_class=MSG_CLASS.FILE_DOWNLOAD))
    for label, payload in download_payloads:
        queries.append(_FileInfoQuery(f"download8/{label}/class6414", MSG.FILE_DOWNLOAD_VIDEO, payload, msg_class=MSG_CLASS.MODERN))
        queries.append(_FileInfoQuery(f"download13/{label}/class6414", MSG.FILE_DOWNLOAD, payload, msg_class=MSG_CLASS.MODERN))
    return queries


def _preview_queries(channel: int, file_id: str, raw: dict) -> list[_FileInfoQuery]:
    stream_type = str(raw.get("streamType") or "mainStream")
    start_time = _parse_time(raw, "startTime")
    end_time = _parse_time(raw, "endTime")
    variants = [
        ("thumbnail", "<thumbnail>1</thumbnail>"),
        ("thumbnail-filetype", "<thumbnail>1</thumbnail><fileType>jpg</fileType>"),
        ("snap", "<snap>1</snap>"),
        ("picture", "<picture>1</picture>"),
        ("preview", "<preview>1</preview>"),
    ]
    queries: list[_FileInfoQuery] = []
    for label, extra in variants:
        payload = _preview_payload(channel, file_id, raw, stream_type=stream_type, extra=extra, mode="full")
        queries.append(_FileInfoQuery(f"preview15/{label}/full", MSG.FILE_INFO_LIST_ALT, payload))
        queries.append(_FileInfoQuery(f"preview14/{label}/full", MSG.FILE_INFO_LIST, payload))
        queries.append(_FileInfoQuery(f"preview16/{label}/full", MSG.FILE_INFO_LIST_ALT2, payload))
    if start_time and end_time:
        for label, extra in variants:
            payload = payloads.playback_download.format(
                channel_id=channel,
                stream_type=stream_type,
                support_sub=1,
                start_time=_time_fragment("startTime", start_time),
                end_time=_time_fragment("endTime", end_time),
            )
            text = payload.decode("utf-8").replace("</FileInfo>", f"{extra}</FileInfo>")
            queries.append(_FileInfoQuery(f"preview143/{label}/range", MSG.FILE_PLAYBACK, text.encode("utf-8")))
    for label, extra in variants[:2]:
        payload = _preview_payload(channel, file_id, raw, stream_type=stream_type, extra=extra, mode="full")
        queries.append(_FileInfoQuery(f"preview13/{label}/class6482", MSG.FILE_DOWNLOAD, payload, msg_class=MSG_CLASS.FILE_DOWNLOAD))
        queries.append(_FileInfoQuery(f"preview8/{label}/class6482", MSG.FILE_DOWNLOAD_VIDEO, payload, msg_class=MSG_CLASS.FILE_DOWNLOAD))
    return queries


def _preview_dump_query(channel: int, file_id: str, raw: dict) -> _FileInfoQuery:
    payload = _preview_payload(channel, file_id, raw, stream_type=str(raw.get("streamType") or "mainStream"), extra="<thumbnail>1</thumbnail>", mode="full")
    return _FileInfoQuery("preview8/thumbnail/class6482", MSG.FILE_DOWNLOAD_VIDEO, payload, msg_class=MSG_CLASS.FILE_DOWNLOAD)


def _preview_handle_queries(channel: int, handle: str, source_label: str) -> list[_FileInfoQuery]:
    variants = [
        ("thumbnail", "<thumbnail>1</thumbnail>"),
        ("snap", "<snap>1</snap>"),
        ("picture", "<picture>1</picture>"),
        ("preview", "<preview>1</preview>"),
    ]
    queries = []
    for label, extra in variants:
        payload = _preview_handle_payload(channel, handle, extra=extra)
        queries.append(_FileInfoQuery(f"{source_label}/handle-{handle}/msg15/{label}", MSG.FILE_INFO_LIST_ALT, payload))
        queries.append(_FileInfoQuery(f"{source_label}/handle-{handle}/msg14/{label}", MSG.FILE_INFO_LIST, payload))
        queries.append(_FileInfoQuery(f"{source_label}/handle-{handle}/msg16/{label}", MSG.FILE_INFO_LIST_ALT2, payload))
    queries.append(_FileInfoQuery(f"{source_label}/handle-{handle}/plain15", MSG.FILE_INFO_LIST_ALT, payloads.files_for_handle.format(channel_id=channel, handle=handle)))
    return queries


def _preview_payload(channel: int, file_id: str, raw: dict, *, stream_type: str, extra: str, mode: str) -> bytes:
    fields = _download_fields(file_id, raw, mode=mode)
    if raw.get("name") and "<name>" not in fields:
        fields += payloads.download_name_field.format(name=raw["name"])
    if "<streamType>" not in fields:
        fields += payloads.download_stream_type_field.format(stream_type=stream_type)
    fields += extra
    return payloads.download_file.format(channel_id=channel, fields=payloads.Raw(fields))


def _preview_handle_payload(channel: int, handle: str, *, extra: str) -> bytes:
    fields = payloads.download_handle_field.format(handle=handle) + extra
    return payloads.download_file.format(channel_id=channel, fields=payloads.Raw(fields))


def _is_forced_high_quality(raw: dict) -> bool:
    return bool(raw.get("_streamTypeForced")) and str(raw.get("streamType") or "").lower() in ("mainstream", "clear")


def _replay_download_payload(channel: int, raw: dict) -> bytes | None:
    start_time = _parse_time(raw, "startTime")
    stream_type = raw.get("streamType") or "mainStream"
    if not start_time:
        return None
    return payloads.replay_download.format(
        channel_id=channel,
        stream_type=str(stream_type),
        start_time=_time_fragment("startTime", start_time),
    )


def _playback_download_payloads(channel: int, raw: dict) -> list[tuple[str, bytes]]:
    start_time = _parse_time(raw, "startTime")
    end_time = _parse_time(raw, "endTime")
    stream_type = raw.get("streamType") or "mainStream"
    if not start_time or not end_time:
        return []
    if _is_forced_high_quality(raw):
        return [
            ("range-mainStream-nosub", _playback_download_payload(channel, start_time, end_time, "mainStream", support_sub=0)),
            ("range-mainStream-nosupport", _playback_download_payload(channel, start_time, end_time, "mainStream", support_sub=None)),
            ("range-mainStream", _playback_download_payload(channel, start_time, end_time, "mainStream", support_sub=1)),
        ]
    stream_types = [str(stream_type)]
    if not raw.get("_streamTypeForced") and "subStream" not in stream_types:
        stream_types.append("subStream")
    return [(f"range-{stream}", _playback_download_payload(channel, start_time, end_time, stream)) for stream in stream_types]


def _playback_download_payload(channel: int, start_time: datetime, end_time: datetime, stream_type: str, *, support_sub: int | None = 1) -> bytes:
    template = payloads.playback_download_no_support if support_sub is None else payloads.playback_download
    return template.format(
        channel_id=channel,
        stream_type=stream_type,
        support_sub=support_sub,
        start_time=_time_fragment("startTime", start_time),
        end_time=_time_fragment("endTime", end_time),
    )


def _download_payload(channel: int, file_id: str, raw: dict, *, mode: str) -> bytes:
    fields = _download_fields(file_id, raw, mode=mode)
    return payloads.download_file.format(channel_id=channel, fields=payloads.Raw(fields))


def _download_fields(file_id: str, raw: dict, *, mode: str) -> str:
    start_time = _parse_time(raw, "startTime")
    end_time = _parse_time(raw, "endTime")
    fields = []
    if mode in ("id", "full"):
        fields.append(payloads.download_id_field.format(file_id=file_id))
    if mode in ("fileName", "full"):
        fields.append(payloads.download_file_name_field.format(file_id=file_id))
    if mode in ("name", "full") and raw.get("name"):
        fields.append(payloads.download_name_as_file_name_field.format(name=raw["name"]))
    if mode == "full" and raw.get("name"):
        fields.append(payloads.download_name_field.format(name=raw["name"]))
    handle = raw.get("handle")
    if mode == "full" and handle:
        fields.append(payloads.download_handle_field.format(handle=handle))
    if mode == "full" and raw.get("streamType"):
        fields.append(payloads.download_stream_type_field.format(stream_type=raw["streamType"]))
    if mode == "full" and raw.get("fileType"):
        fields.append(payloads.download_file_type_field.format(file_type=raw["fileType"]))
    if mode == "full" and raw.get("recordType"):
        fields.append(payloads.download_record_type_field.format(record_type=raw["recordType"]))
    if mode == "full" and start_time:
        fields.append(_time_fragment("startTime", start_time).value)
    if mode == "full" and end_time:
        fields.append(_time_fragment("endTime", end_time).value)
    return "".join(fields)


def _download_xml_done_text(text: str) -> bool:
    return any(token in text for token in ("</FileInfoList>", "<rsp>0</rsp>", "<result>0</result>"))


def _xml_file_size(text: str) -> int | None:
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    best_size = None
    for node in _file_info_nodes(root):
        size = _file_size(_element_to_dict(node))
        if size:
            best_size = max(best_size or 0, size)
    return best_size


def _response_detail(msg, prefix: str) -> str:
    detail = (
        f"{prefix} msg_id={msg.header.msg_id} msg_num={msg.header.msg_num} "
        f"class=0x{msg.header.msg_class:04x} payload_len={len(msg.payload)}"
    )
    if msg.extension:
        detail += f" ext={_one_line_preview(msg.extension)}"
    xml_text = msg.xml_text
    if xml_text and _looks_like_xml(xml_text):
        detail += f" xml={_one_line_preview(xml_text)}"
    elif msg.payload:
        detail += f" payload_hex={msg.payload[:64].hex()}"
    return detail


def _preview_response(label: str, msg) -> dict:
    xml_text = msg.xml_text
    payload = msg.payload or b""
    result = {
        "label": label,
        "msg_id": int(msg.header.msg_id),
        "msg_num": int(msg.header.msg_num),
        "response_code": int(msg.header.response_code),
        "class": f"0x{msg.header.msg_class:04x}",
        "extension_len": len(msg.extension or b""),
        "payload_len": len(payload),
        "jpeg": _looks_like_jpeg(payload),
    }
    if msg.extension:
        result["extension"] = _one_line_preview(msg.extension)
    if xml_text and _looks_like_xml(xml_text):
        result["xml"] = _one_line_preview(xml_text, limit=1200)
    elif payload:
        result["payload_hex"] = payload[:128].hex()
    return result


def _preview_should_collect_binary(detail: dict) -> bool:
    if detail.get("jpeg"):
        return False
    payload_hex = str(detail.get("payload_hex") or "")
    extension = str(detail.get("extension") or "")
    return payload_hex.startswith(("31303031", "31303032")) or "<binaryData>1</binaryData>" in extension


def _merge_preview_binary_probe(detail: dict, payload: bytes) -> None:
    packets = list(MediaParser().feed(payload))
    mp4_info = _embedded_mp4_info(payload)
    if payload:
        detail["binary_probe_len"] = len(payload)
        detail["binary_probe_hex"] = payload[:128].hex()
    if mp4_info:
        detail["embedded_mp4"] = mp4_info
    if packets:
        detail["media_packets"] = [
            {
                "kind": packet.kind,
                "codec": packet.codec,
                "width": packet.width,
                "height": packet.height,
                "fps": packet.fps,
                "payload_len": len(packet.data),
            }
            for packet in packets[:8]
        ]
    detail["has_video_frame"] = any(packet.kind in ("iframe", "pframe") for packet in packets) or bool(mp4_info.get("has_mdat"))


def _preview_binary_has_video_frame(payload: bytes | bytearray) -> bool:
    data = bytes(payload)
    return any(packet.kind in ("iframe", "pframe") for packet in MediaParser().feed(data)) or bool(_embedded_mp4_info(data).get("has_mdat"))


def _preview_output_path(output: str | Path, file_name: str, *, raw: bool) -> Path:
    output_path = Path(output)
    suffix = ".raw" if raw else ".mp4"
    if output_path.is_dir() or str(output).endswith(("/", "\\")):
        stem = Path(file_name).stem or "preview"
        return output_path / f"{stem}.preview{suffix}"
    return output_path


def _preview_cache_path(cache: str | Path | None, item: dict) -> Path:
    file_name = item.get("file_name") or Path(str(item.get("path") or "preview.mp4")).name
    if cache is None:
        return Path(".tmp") / "pyneolink-preview-cache" / f"{Path(file_name).stem or 'preview'}.preview.mp4"
    cache_path = Path(cache)
    if cache_path.is_dir() or str(cache).endswith(("/", "\\")):
        return cache_path / f"{Path(file_name).stem or 'preview'}.preview.mp4"
    return cache_path


def _write_preview_cache_payload(
    fh,
    payload: bytes,
    *,
    head: bytearray,
    raw_seen: int,
    mp4_offset: int | None,
    mp4_started: bool,
    expected_total: int | None,
    ready: threading.Event,
) -> tuple[int, int | None, bool, int | None]:
    before = raw_seen
    raw_seen += len(payload)
    if payload and len(head) < 2 * 1024 * 1024:
        head.extend(payload[: max(2 * 1024 * 1024 - len(head), 0)])
    if mp4_offset is None:
        info = _embedded_mp4_info(bytes(head))
        mp4_offset = _int_or_none(info.get("offset")) if info else None
    expected_total = expected_total or _embedded_mp4_total_size(head)
    if mp4_offset is None:
        return raw_seen, mp4_offset, mp4_started, expected_total
    if not mp4_started and mp4_offset < before:
        fh.write(bytes(head[mp4_offset:]))
        fh.flush()
        mp4_started = True
        ready.set()
        return raw_seen, mp4_offset, mp4_started, expected_total
    start_in_payload = max(mp4_offset - before, 0)
    if start_in_payload < len(payload):
        fh.write(payload[start_in_payload:])
        fh.flush()
        mp4_started = True
        ready.set()
    return raw_seen, mp4_offset, mp4_started, expected_total


def _extract_embedded_mp4_bytes(payload: bytes) -> bytes:
    info = _embedded_mp4_info(payload)
    offset = _int_or_none(info.get("offset")) if info else None
    if offset is None:
        return b""
    total = _embedded_mp4_total_size(payload)
    return payload[offset:total] if total else payload[offset:]


def _embedded_mp4_total_size(payload: bytes | bytearray) -> int | None:
    info = _embedded_mp4_info(bytes(payload))
    if not info:
        return None
    boxes = info.get("boxes") or []
    if not boxes:
        return None
    last = boxes[-1]
    if last.get("type") != "mdat":
        return None
    total = int(last["offset"]) + int(last["size"])
    return total if total > 0 else None


def _embedded_mp4_info(payload: bytes) -> dict:
    marker = payload.find(b"ftyp")
    if marker < 4:
        return {}
    start = marker - 4
    boxes = []
    pos = start
    while pos + 8 <= len(payload) and len(boxes) < 12:
        size = int.from_bytes(payload[pos : pos + 4], "big")
        box_type = payload[pos + 4 : pos + 8].decode("ascii", errors="replace")
        if size == 0:
            size = len(payload) - pos
        if size == 1:
            if pos + 16 > len(payload):
                break
            size = int.from_bytes(payload[pos + 8 : pos + 16], "big")
        if size < 8:
            break
        boxes.append({"type": box_type, "size": size, "offset": pos})
        pos += size
    brands = []
    if payload[start + 8 : start + 12]:
        brands.append(payload[start + 8 : start + 12].decode("ascii", errors="replace"))
    if payload[start + 12 : start + 16]:
        brands.append(payload[start + 12 : start + 16].decode("ascii", errors="replace"))
    return {
        "offset": start,
        "brands": brands,
        "boxes": boxes,
        "has_moov": any(box["type"] == "moov" for box in boxes),
        "has_mdat": any(box["type"] == "mdat" for box in boxes),
    }


def _preview_handles(xml_text: str | None) -> list[str]:
    if not xml_text or not _looks_like_xml(xml_text):
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    handles = []
    for node in root.findall(".//handle"):
        if node.text and node.text.strip():
            handles.append(node.text.strip())
    return handles


def _preview_attempt_text(item: dict) -> str:
    if "error" in item:
        return f"{item.get('label')}: {item['error']}"
    return (
        f"{item.get('label')}: response={item.get('response_code')} "
        f"msg_id={item.get('msg_id')} payload_len={item.get('payload_len')} jpeg={item.get('jpeg')}"
    )


def _looks_like_jpeg(payload: bytes) -> bool:
    return payload.startswith(b"\xff\xd8\xff")


def _one_line_preview(value: str | bytes, limit: int = 320) -> str:
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = value
    text = " ".join(text.split())
    return text if len(text) <= limit else f"{text[:limit]}..."


def _is_download_continuation(msg, query_msg_id: int, download_started: bool) -> bool:
    if msg.header.msg_id not in (query_msg_id, MSG.FILE_REPLAY, MSG.FILE_DOWNLOAD_VIDEO, MSG.FILE_DOWNLOAD):
        return False
    if msg.header.response_code not in (0, 200):
        return False
    if msg.header.msg_class == MSG_CLASS.FILE_DOWNLOAD:
        return True
    if b"<binaryData>1</binaryData>" in msg.extension:
        return True
    return download_started and bool(msg.payload)


def _is_download_message(msg, query_msg_id: int, accepted_msg_nums: set[int], download_started: bool) -> bool:
    if msg.header.msg_num in accepted_msg_nums:
        if msg.header.msg_num != 0:
            return True
        return msg.header.msg_id == query_msg_id or _is_download_continuation(msg, query_msg_id, download_started)
    return _is_download_continuation(msg, query_msg_id, download_started)


def _emit_progress(progress, written: int, expected_size: int | None, chunks: int, sock) -> None:
    snapshot = _transport_snapshot(sock)
    if expected_size:
        percent = written * 100 / expected_size
        message = f"  downloaded {written}/{expected_size} bytes ({percent:.1f}%), chunks={chunks}"
    else:
        message = f"  downloaded {written} bytes, chunks={chunks}"
    if snapshot:
        message += (
            f", udp_next={snapshot.get('udp_next_recv_id')}"
            f", udp_max={snapshot.get('udp_max_packet_id')}"
            f", gaps={snapshot.get('udp_pending_gaps')}"
        )
    if callable(progress):
        progress(message)
    else:
        print(message)


def _emit_progress_message(progress, message: str) -> None:
    if not progress:
        return
    if callable(progress):
        progress(message)
    else:
        print(message)


def _existing_download_matches(path: Path, expected_size: int | None) -> bool:
    if not path.exists() or not path.is_file():
        return False
    actual_size = path.stat().st_size
    if path.suffix.lower() == ".mp4":
        return actual_size > 0
    if expected_size is None:
        return actual_size > 0
    return actual_size == expected_size


def _remove_stale_part_files(output_path: Path) -> None:
    prefix = f"{output_path.name}."
    for candidate in output_path.parent.glob("*.part"):
        if candidate.name.startswith(prefix):
            _remove_file(candidate)


def _existing_download_mismatch_message(path: Path, expected_size: int | None) -> str:
    try:
        actual_size = path.stat().st_size
    except OSError as exc:
        return f"  existing file cannot be checked: {path}: {type(exc).__name__}: {exc}"
    if expected_size is None:
        return f"  existing file found without camera size; downloading again: {path} local={actual_size} bytes"
    return (
        f"  existing file size differs; downloading again: {path} "
        f"local={actual_size} bytes camera={expected_size} bytes"
    )


def _clip_payload(payload: bytes, written: int, expected_size: int | None) -> bytes:
    if expected_size is not None and written + len(payload) > expected_size:
        return payload[: max(expected_size - written, 0)]
    return payload


def _transport_snapshot(sock) -> dict:
    snapshot = getattr(sock, "debug_snapshot", None)
    if not callable(snapshot):
        return {}
    try:
        return snapshot()
    except Exception:
        return {}


def _transport_snapshot_text(sock) -> str:
    snapshot = _transport_snapshot(sock)
    if not snapshot:
        return ""
    keys = (
        "udp_next_recv_id",
        "udp_last_packet_id",
        "udp_max_packet_id",
        "udp_pending_chunks",
        "udp_pending_gaps",
        "udp_buffered_bytes",
        "udp_data_packets",
        "udp_data_bytes",
        "udp_duplicates",
        "udp_ignored",
        "udp_acks_sent",
        "udp_acks_received",
        "udp_heartbeats_sent",
        "udp_resend_packets",
        "udp_seconds_since_data",
    )
    bits = [f"{key}={snapshot.get(key)}" for key in keys if snapshot.get(key) is not None]
    return "; " + ", ".join(bits)


def _looks_like_xml(text: str) -> bool:
    return text.lstrip().startswith("<")


def _success(query: _FileInfoQuery, xml_text: str | None) -> dict:
    return {
        "label": query.label,
        "xml": xml_text or "",
        "payload": query.payload.decode("utf-8", errors="replace"),
    }


def _remove_empty_file(path: Path) -> None:
    try:
        if path.exists() and path.stat().st_size == 0:
            path.unlink()
    except OSError:
        pass


def _finalize_download(part_path: Path, output_path: Path, expected_size: int | None) -> Path:
    actual_size = part_path.stat().st_size
    if expected_size is not None and actual_size != expected_size:
        raise DownloadSizeMismatch(const_msg.Error.DownloadSizeMismatch.format(actual_size=actual_size, expected_size=expected_size))
    if output_path.suffix.lower() == ".mp4" and looks_like_bcmedia(part_path):
        if output_path.exists():
            output_path.unlink()
        try:
            bcmedia_to_mp4(part_path, output_path)
        except Exception as exc:
            if extract_embedded_mp4(part_path, output_path):
                part_path.unlink(missing_ok=True)
                return output_path
            raw_path = output_path.with_suffix(output_path.suffix + ".bcmedia")
            if raw_path.exists():
                raw_path.unlink()
            part_path.replace(raw_path)
            raise ProtocolError(const_msg.Error.Mp4ConversionFailed.format(raw_path=raw_path, exc=exc)) from exc
        part_path.unlink(missing_ok=True)
        return output_path
    if output_path.exists():
        output_path.unlink()
    part_path.replace(output_path)
    return output_path


def _remove_file(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


def _safe_label(label: str) -> str:
    return "".join(char if char.isalnum() else "_" for char in label)


def _debug(camera: Camera, message: str) -> None:
    if getattr(camera, "debug", False):
        print(const_msg.Log.Pyneolink.format(message=message))
