from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import TYPE_CHECKING, Iterable
import time as monotonic_clock
import xml.etree.ElementTree as ET

from .core.bc import (
    CLASS_FILE_DOWNLOAD,
    CLASS_MODERN,
    MSG_DAY_RECORDS,
    MSG_FILE_DOWNLOAD,
    MSG_FILE_DOWNLOAD_VIDEO,
    MSG_FILE_INFO_LIST,
    MSG_FILE_INFO_LIST_ALT,
    MSG_FILE_INFO_LIST_ALT2,
    MSG_FILE_PLAYBACK,
    MSG_FILE_PLAYBACK_STOP,
    MSG_FILE_REPLAY,
    MSG_FILE_REPLAY_STOP,
    MSG_HDD_INFO,
    MSG_HDD_INIT,
    MSG_REPLAY_SEEK,
    InvalidMagicError,
    ProtocolError,
    extension_xml,
    xml_document,
)
from .core.media import bcmedia_to_mp4, looks_like_bcmedia
from .core.xmlutil import xml_to_dict

if TYPE_CHECKING:
    from .camera import Camera


class DangerousSdCardOperation(RuntimeError):
    pass


class DownloadSizeMismatch(RuntimeError):
    pass


@dataclass(frozen=True)
class SdCardFile:
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


class SdCard:
    def __init__(self, camera: Camera) -> None:
        self.camera = camera
        self.last_attempts: list[str] = []
        self.last_download_attempts: list[str] = []
        self.last_xml: str | None = None
        self.last_successes: list[dict] = []
        self._last_download_detail = ""
        self._active_replay_name: str | None = None

    def list(
        self,
        *,
        start: datetime | date | str | None = None,
        end: datetime | date | str | None = None,
        stream_type: str = "mainStream",
        file_type: str = "All",
        channel_id: int | None = None,
        as_dict: bool = True,
    ) -> list[dict] | list[SdCardFile]:
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
        self.last_attempts = attempts
        return [item.to_dict() for item in files] if as_dict else files

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
        for handle_query in _handle_queries(channel, start, end, stream_type):
            reply = _send_query(self.camera, handle_query, attempts)
            if not reply or reply.header.response_code != 200:
                continue
            self.last_xml = reply.xml_text
            self.last_successes.append(_success(handle_query, reply.xml_text))
            for file_info in _file_info_nodes(reply.xml_root):
                handle = _find_child_text(file_info, "handle")
                direct_files = _parse_file_list(file_info)
                files.extend(item for item in direct_files if item.file_name)
                if not handle:
                    continue
                for detail_query in _handle_detail_queries(channel, handle):
                    detail = _send_query(self.camera, detail_query, attempts)
                    if not detail or detail.header.response_code != 200:
                        continue
                    self.last_xml = detail.xml_text
                    self.last_successes.append(_success(detail_query, detail.xml_text))
                    files.extend(_parse_file_list(detail.xml_root))
                    break
            if files:
                break
        return files

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
        items = list(files if files is not None else self.list(start=start, end=end))
        start_dt = _coerce_datetime(start, end_of_day=False) if start is not None else None
        end_dt = _coerce_datetime(end, end_of_day=True) if end is not None else None
        result = []
        for item in items:
            data = item.to_dict() if isinstance(item, SdCardFile) else dict(item)
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

    def download(
        self,
        file: dict | SdCardFile | str,
        output: str | Path,
        *,
        chunk_limit: int = 0,
        progress=False,
        max_attempts: int = 3,
        recv_timeout: float = 2.0,
    ) -> Path:
        item = _file_to_dict(file)
        raw = _download_raw(item)
        file_id = raw.get("Id") or item.get("path") or item.get("file_name") or str(file)
        file_name = Path(str(file_id)).name if raw.get("Id") else item.get("file_name") or Path(str(file_id)).name or str(file)
        output_path = Path(output)
        if output_path.is_dir() or str(output).endswith(("/", "\\")):
            output_path.mkdir(parents=True, exist_ok=True)
            output_path = output_path / Path(file_name).name
        else:
            output_path.parent.mkdir(parents=True, exist_ok=True)

        expected_size = _int_or_none(item.get("size"))
        self.last_download_attempts = []
        last_error = None
        best_mismatch: tuple[str, int] | None = None
        for index, query in enumerate(_download_queries(self.camera.config.channel_id, str(file_id), raw)):
            if max_attempts and index >= max_attempts:
                break
            part_path = output_path.with_name(output_path.name + f".{_safe_label(query.label)}.part")
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
                if expected_size is not None and written != expected_size and not query.label.startswith("playback143/"):
                    _remove_file(part_path)
                    if best_mismatch is None or written > best_mismatch[1]:
                        best_mismatch = (query.label, written)
                    continue
                return _finalize_download(part_path, output_path, None if query.label.startswith("playback143/") else expected_size)
            _remove_empty_file(part_path)
        if best_mismatch and expected_size is not None:
            label, written = best_mismatch
            raise DownloadSizeMismatch(
                f"Best attempt {label} downloaded {written} bytes, expected {expected_size} bytes. "
                f"Tried: {', '.join(self.last_download_attempts)}"
            ) from last_error
        raise ProtocolError(f"SD download failed. Tried: {', '.join(self.last_download_attempts)}") from last_error

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
        msg_class = query.msg_class if query.msg_class is not None else (CLASS_FILE_DOWNLOAD if not replay_mode else CLASS_MODERN)
        msg_num = self.camera.send(query.msg_id, query.payload, msg_class=msg_class)
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
        with output_path.open("wb") as fh:
            while True:
                try:
                    msg = self.camera._recv(timeout=recv_timeout)
                except InvalidMagicError as exc:
                    if exc.data:
                        fh.write(exc.data)
                        written += len(exc.data)
                    self._last_download_detail = f"stopped after invalid Baichuan magic 0x{exc.magic:08x}, chunks={chunks}, msg_nums={len(accepted_msg_nums)}"
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
                if msg.header.msg_num not in accepted_msg_nums and not _is_download_continuation(msg, query.msg_id, written > 0):
                    idle_limit = active_idle_seconds if written else startup_idle_seconds
                    if monotonic_clock.monotonic() - last_progress >= idle_limit:
                        if written:
                            self._last_download_detail = (
                                f"no download progress after unrelated messages, chunks={chunks}, "
                                f"msg_nums={len(accepted_msg_nums)}"
                            )
                            break
                        raise TimeoutError("Timed out waiting for download response")
                    continue
                accepted_msg_nums.add(msg.header.msg_num)
                deadline_misses = 0
                max_raw_payload_len = max(max_raw_payload_len, getattr(msg, "raw_payload_len", 0))
                max_payload_len = max(max_payload_len, len(msg.payload))
                if getattr(msg, "encrypted_len", None) is not None:
                    encrypted_lens.add(msg.encrypted_len)
                replay_payload = replay_mode and msg.header.msg_id == MSG_FILE_REPLAY and bool(msg.payload)
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
                    raise ProtocolError(_response_detail(msg, f"response {msg.header.response_code}"))
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

    def _prepare_replay_download(self, raw: dict) -> None:
        name = str(raw.get("name") or raw.get("fileName") or "")
        start_time = _parse_time(raw, "startTime")
        stream_type = str(raw.get("streamType") or "mainStream")
        if not name or not start_time:
            raise ProtocolError("Replay download needs file name and startTime")
        channel = self.camera.config.channel_id
        seq = int(start_time.timestamp())
        seek_payload = xml_document(
            "<ReplaySeek version=\"1.1\">"
            f"<channelId>{channel}</channelId>"
            f"<seq>{seq}</seq>"
            f"{_time_xml('seekTime', start_time)}"
            "</ReplaySeek>"
        )
        seek_reply = self.camera.command(MSG_REPLAY_SEEK, seek_payload)
        if seek_reply.header.response_code != 200:
            raise ProtocolError(f"ReplaySeek failed with response {seek_reply.header.response_code}")

        detail_payload = xml_document(
            "<FileInfoList version=\"1.1\">"
            "<FileInfo>"
            f"<channelId>{channel}</channelId>"
            f"<name>{_escape(name)}</name>"
            "<supportSub>1</supportSub>"
            "<playSpeed>1</playSpeed>"
            f"<streamType>{_escape(stream_type)}</streamType>"
            "</FileInfo>"
            "</FileInfoList>"
        )
        detail_reply = self.camera.command(MSG_FILE_DOWNLOAD, detail_payload)
        if detail_reply.header.response_code != 200:
            raise ProtocolError(f"Replay file detail failed with response {detail_reply.header.response_code}")
        self._active_replay_name = name

    def _stop_replay_download(self, raw: dict) -> None:
        name = self._active_replay_name or str(raw.get("name") or raw.get("fileName") or "")
        self._active_replay_name = None
        if not name:
            return
        payload = xml_document(
            "<FileInfoList version=\"1.1\">"
            "<FileInfo>"
            f"<channelId>{self.camera.config.channel_id}</channelId>"
            f"<name>{_escape(name)}</name>"
            "</FileInfo>"
            "</FileInfoList>"
        )
        try:
            self.camera.command(MSG_FILE_REPLAY_STOP, payload)
        except Exception:
            pass

    def _stop_playback_download(self) -> None:
        try:
            self.camera.send(MSG_FILE_PLAYBACK_STOP)
        except Exception:
            pass

    def _reconnect_after_download(self) -> None:
        try:
            self.camera.close()
            self.camera.connect()
            self.camera.login()
        except Exception as exc:
            self.last_download_attempts.append(f"reconnect-after-download: {type(exc).__name__}: {exc}")

    def remove(self, file: dict | SdCardFile | str, *, confirm: bool = False) -> None:
        if not confirm:
            raise DangerousSdCardOperation("Refusing to remove an SD-card file without confirm=True")
        raise NotImplementedError("SD-card file removal is not wired yet; list/download are the current safe operations")

    def format(self, *, confirm: bool = False, confirmation_text: str = "", disk_id: int = 0) -> None:
        if not confirm or confirmation_text != "FORMAT SD CARD":
            raise DangerousSdCardOperation('Refusing to format the SD card without confirm=True and confirmation_text="FORMAT SD CARD"')
        payload = xml_document(f"<HddInitList version=\"1.1\"><HddInit><id>{disk_id}</id></HddInit></HddInitList>")
        reply = self.camera.command(MSG_HDD_INIT, payload)
        if reply.header.response_code != 200:
            raise ProtocolError(f"SD-card format failed with response {reply.header.response_code}")

    def disk_info(self) -> dict:
        reply = self.camera.command(MSG_HDD_INFO)
        if reply.header.response_code != 200:
            raise ProtocolError(f"SD-card disk info failed with response {reply.header.response_code}")
        return xml_to_dict(reply.xml_text or "")

    def day_records(self, day: date | str | None = None) -> dict:
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
        raise ProtocolError(f"SD-card day records failed. Tried: {', '.join(attempts)}")


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
        ("replay", MSG_FILE_REPLAY),
        ("info14", MSG_FILE_INFO_LIST),
        ("info15", MSG_FILE_INFO_LIST_ALT),
        ("info16", MSG_FILE_INFO_LIST_ALT2),
    ]
    payloads = [
        *[
            (
                f"compact-{type_value}",
                xml_document(
                    "<FileInfoList version=\"1.1\">"
                    f"<channelId>{channel}</channelId>"
                    f"<type>{type_value}</type>"
                    f"<beginTime>{start:%Y%m%d%H%M%S}</beginTime>"
                    f"<endTime>{end:%Y%m%d%H%M%S}</endTime>"
                    "</FileInfoList>"
                ),
            )
            for type_value in _FILE_TYPE_CANDIDATES
        ],
        *[
            (
                f"compact-{stream_value}",
                xml_document(
                    "<FileInfoList version=\"1.1\">"
                    f"<channelId>{channel}</channelId>"
                    f"<streamType>{stream_value}</streamType>"
                    f"<beginTime>{start:%Y%m%d%H%M%S}</beginTime>"
                    f"<endTime>{end:%Y%m%d%H%M%S}</endTime>"
                    "</FileInfoList>"
                ),
            )
            for stream_value in _STREAM_TYPE_CANDIDATES
        ],
        *[
            (
                f"compact-{stream_value}-{type_value}",
                xml_document(
                    "<FileInfoList version=\"1.1\">"
                    f"<channelId>{channel}</channelId>"
                    f"<streamType>{stream_value}</streamType>"
                    f"<type>{type_value}</type>"
                    f"<beginTime>{start:%Y%m%d%H%M%S}</beginTime>"
                    f"<endTime>{end:%Y%m%d%H%M%S}</endTime>"
                    "</FileInfoList>"
                ),
            )
            for stream_value in _STREAM_TYPE_CANDIDATES
            for type_value in _FILE_TYPE_CANDIDATES
        ],
        (
            "nested-basic",
            xml_document(
                "<FileInfoList version=\"1.1\">"
                f"<channelId>{channel}</channelId>"
                f"<streamType>{stream_type}</streamType>"
                f"{_time_xml('beginTime', start)}"
                f"{_time_xml('endTime', end)}"
                "</FileInfoList>"
            ),
        ),
        (
            "nested-type",
            xml_document(
                "<FileInfoList version=\"1.1\">"
                f"<channelId>{channel}</channelId>"
                f"<streamType>{stream_type}</streamType>"
                f"<type>{file_type}</type>"
                f"{_time_xml('beginTime', start)}"
                f"{_time_xml('endTime', end)}"
                "</FileInfoList>"
            ),
        ),
        (
            "start-end",
            xml_document(
                "<FileInfoList version=\"1.1\">"
                f"<channelId>{channel}</channelId>"
                f"<streamType>{stream_type}</streamType>"
                f"{_time_xml('startTime', start)}"
                f"{_time_xml('endTime', end)}"
                "</FileInfoList>"
            ),
        ),
        (
            "flat",
            xml_document(
                "<FileInfoList version=\"1.1\">"
                f"<channelId>{channel}</channelId>"
                f"<streamType>{stream_type}</streamType>"
                f"{_flat_time_xml('begin', start)}"
                f"{_flat_time_xml('end', end)}"
                "</FileInfoList>"
            ),
        ),
        (
            "compact",
            xml_document(
                "<FileInfoList version=\"1.1\">"
                f"<channelId>{channel}</channelId>"
                f"<streamType>{stream_type}</streamType>"
                f"<beginTime>{start:%Y%m%d%H%M%S}</beginTime>"
                f"<endTime>{end:%Y%m%d%H%M%S}</endTime>"
                "</FileInfoList>"
            ),
        ),
    ]
    queries = []
    ext = extension_xml(channel_id=channel)
    for payload_label, payload in payloads:
        for msg_label, msg_id in msg_ids:
            queries.append(_FileInfoQuery(f"{msg_label}/{payload_label}", msg_id, payload))
            queries.append(_FileInfoQuery(f"{msg_label}/{payload_label}+ext", msg_id, payload, ext))
    return queries


def _day_records_range_query(channel: int, start: datetime, end: datetime) -> _FileInfoQuery:
    return _FileInfoQuery(
        "day-records/range",
        MSG_DAY_RECORDS,
        xml_document(
            "<DayRecords version=\"1.1\">"
            f"{_time_xml('startTime', start)}"
            f"{_time_xml('endTime', end)}"
            "<DayRecordList><DayRecord>"
            "<index>0</index>"
            f"<channelId>{channel}</channelId>"
            "</DayRecord></DayRecordList>"
            "</DayRecords>"
        ),
    )


def _handle_queries(channel: int, start: datetime, end: datetime, stream_type: str) -> list[_FileInfoQuery]:
    record_types = "manual, sched, io, md, people, face, vehicle, dog_cat, visitor"
    streams = [stream_type]
    if stream_type != "subStream":
        streams.append("subStream")
    queries = []
    for stream in streams:
        payload = xml_document(
            "<FileInfoList version=\"1.1\">"
            "<FileInfo>"
            f"<channelId>{channel}</channelId>"
            f"<streamType>{stream}</streamType>"
            f"<recordType>{record_types}</recordType>"
            f"{_time_xml('startTime', start)}"
            f"{_time_xml('endTime', end)}"
            "</FileInfo>"
            "</FileInfoList>"
        )
        queries.append(_FileInfoQuery(f"handle/{stream}", MSG_FILE_INFO_LIST, payload))
        queries.append(_FileInfoQuery(f"handle/{stream}+ext", MSG_FILE_INFO_LIST, payload, extension_xml(channel_id=channel)))
    return queries


def _handle_detail_queries(channel: int, handle: str) -> list[_FileInfoQuery]:
    payload = xml_document(
        "<FileInfoList version=\"1.1\">"
        "<FileInfo>"
        f"<channelId>{channel}</channelId>"
        f"<handle>{handle}</handle>"
        "</FileInfo>"
        "</FileInfoList>"
    )
    return [
        _FileInfoQuery(f"files/handle-{handle}", MSG_FILE_INFO_LIST_ALT, payload),
        _FileInfoQuery(f"files/handle-{handle}+ext", MSG_FILE_INFO_LIST_ALT, payload, extension_xml(channel_id=channel)),
    ]


def _day_record_queries(channel: int, target: date) -> list[_FileInfoQuery]:
    ext = extension_xml(channel_id=channel)
    payloads = [
        (
            "nested",
            xml_document(
                "<DayRecords version=\"1.1\">"
                f"<channelId>{channel}</channelId>"
                f"<year>{target.year}</year><month>{target.month}</month><day>{target.day}</day>"
                "</DayRecords>"
            ),
        ),
        (
            "compact",
            xml_document(
                "<DayRecords version=\"1.1\">"
                f"<channelId>{channel}</channelId>"
                f"<date>{target:%Y%m%d}</date>"
                "</DayRecords>"
            ),
        ),
        (
            "empty",
            b"",
        ),
    ]
    queries = []
    for label, payload in payloads:
        queries.append(_FileInfoQuery(f"day/{target}/{label}", MSG_DAY_RECORDS, payload))
        queries.append(_FileInfoQuery(f"day/{target}/{label}+ext", MSG_DAY_RECORDS, payload, ext))
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


def _time_xml(tag: str, value: datetime) -> str:
    return (
        f"<{tag}>"
        f"<year>{value.year}</year><month>{value.month}</month><day>{value.day}</day>"
        f"<hour>{value.hour}</hour><minute>{value.minute}</minute><second>{value.second}</second>"
        f"</{tag}>"
    )


def _flat_time_xml(prefix: str, value: datetime) -> str:
    return (
        f"<{prefix}Year>{value.year}</{prefix}Year>"
        f"<{prefix}Month>{value.month}</{prefix}Month>"
        f"<{prefix}Day>{value.day}</{prefix}Day>"
        f"<{prefix}Hour>{value.hour}</{prefix}Hour>"
        f"<{prefix}Min>{value.minute}</{prefix}Min>"
        f"<{prefix}Sec>{value.second}</{prefix}Sec>"
    )


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


def _file_to_dict(file: dict | SdCardFile | str) -> dict:
    if isinstance(file, SdCardFile):
        return file.to_dict()
    if isinstance(file, dict):
        return dict(file)
    return {"file_name": file}


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


def _download_queries(channel: int, file_id: str, raw: dict) -> list[_FileInfoQuery]:
    replay_payload = _replay_download_payload(channel, raw)
    playback_payloads = _playback_download_payloads(channel, raw)
    payloads = [
        ("id", _download_payload(channel, file_id, raw, mode="id")),
        ("filename", _download_payload(channel, file_id, raw, mode="fileName")),
        ("name", _download_payload(channel, file_id, raw, mode="name")),
        ("full", _download_payload(channel, file_id, raw, mode="full")),
    ]
    queries = []
    primary_label, primary_payload = payloads[0]
    queries.append(_FileInfoQuery(f"download13/{primary_label}/class6482", MSG_FILE_DOWNLOAD, primary_payload, msg_class=CLASS_FILE_DOWNLOAD))
    for label, payload in playback_payloads:
        queries.append(_FileInfoQuery(f"playback143/{label}/bcmedia", MSG_FILE_PLAYBACK, payload, msg_class=CLASS_MODERN))
    queries.append(_FileInfoQuery(f"download8/{primary_label}/class6482", MSG_FILE_DOWNLOAD_VIDEO, primary_payload, msg_class=CLASS_FILE_DOWNLOAD))
    if replay_payload:
        queries.append(_FileInfoQuery("replay5/start/bcmedia", MSG_FILE_REPLAY, replay_payload, msg_class=CLASS_MODERN))
    for label, payload in payloads:
        if label == primary_label:
            continue
        queries.append(_FileInfoQuery(f"download13/{label}/class6482", MSG_FILE_DOWNLOAD, payload, msg_class=CLASS_FILE_DOWNLOAD))
        queries.append(_FileInfoQuery(f"download8/{label}/class6482", MSG_FILE_DOWNLOAD_VIDEO, payload, msg_class=CLASS_FILE_DOWNLOAD))
    for label, payload in payloads:
        queries.append(_FileInfoQuery(f"download8/{label}/class6414", MSG_FILE_DOWNLOAD_VIDEO, payload, msg_class=CLASS_MODERN))
        queries.append(_FileInfoQuery(f"download13/{label}/class6414", MSG_FILE_DOWNLOAD, payload, msg_class=CLASS_MODERN))
    return queries


def _replay_download_payload(channel: int, raw: dict) -> bytes | None:
    start_time = _parse_time(raw, "startTime")
    stream_type = raw.get("streamType") or "mainStream"
    if not start_time:
        return None
    bits = [
        "<FileInfoList version=\"1.1\">",
        "<FileInfo>",
        f"<channelId>{channel}</channelId>",
        "<supportSub>1</supportSub>",
        f"<streamType>{_escape(str(stream_type))}</streamType>",
        _time_xml("startTime", start_time),
        "<playSpeed>1</playSpeed>",
        "</FileInfo>",
        "</FileInfoList>",
    ]
    return xml_document("".join(bits))


def _playback_download_payloads(channel: int, raw: dict) -> list[tuple[str, bytes]]:
    start_time = _parse_time(raw, "startTime")
    end_time = _parse_time(raw, "endTime")
    stream_type = raw.get("streamType") or "mainStream"
    if not start_time or not end_time:
        return []
    stream_types = [str(stream_type)]
    if "subStream" not in stream_types:
        stream_types.append("subStream")
    return [(f"range-{stream}", _playback_download_payload(channel, start_time, end_time, stream)) for stream in stream_types]


def _playback_download_payload(channel: int, start_time: datetime, end_time: datetime, stream_type: str) -> bytes:
    bits = [
        "<FileInfoList version=\"1.1\">",
        "<FileInfo>",
        "<logicChnBitmap>255</logicChnBitmap>",
        f"<channelId>{channel}</channelId>",
        "<supportSub>1</supportSub>",
        f"<streamType>{_escape(str(stream_type))}</streamType>",
        _time_xml("startTime", start_time),
        _time_xml("endTime", end_time),
        "</FileInfo>",
        "</FileInfoList>",
    ]
    return xml_document("".join(bits))


def _download_payload(channel: int, file_id: str, raw: dict, *, mode: str) -> bytes:
    start_time = _parse_time(raw, "startTime")
    end_time = _parse_time(raw, "endTime")
    bits = [
        "<FileInfoList version=\"1.1\">",
        "<FileInfo>",
        f"<channelId>{channel}</channelId>",
    ]
    if mode in ("id", "full"):
        bits.append(f"<Id>{_escape(file_id)}</Id>")
    if mode in ("fileName", "full"):
        bits.append(f"<fileName>{_escape(file_id)}</fileName>")
    if mode in ("name", "full") and raw.get("name"):
        bits.append(f"<fileName>{_escape(str(raw['name']))}</fileName>")
    if mode == "full" and raw.get("name"):
        bits.append(f"<name>{_escape(str(raw['name']))}</name>")
    handle = raw.get("handle")
    if mode == "full" and handle:
        bits.append(f"<handle>{_escape(str(handle))}</handle>")
    if mode == "full" and raw.get("streamType"):
        bits.append(f"<streamType>{_escape(str(raw['streamType']))}</streamType>")
    if mode == "full" and raw.get("fileType"):
        bits.append(f"<fileType>{_escape(str(raw['fileType']))}</fileType>")
    if mode == "full" and raw.get("recordType"):
        bits.append(f"<recordType>{_escape(str(raw['recordType']))}</recordType>")
    if mode == "full" and start_time:
        bits.append(_time_xml("startTime", start_time))
    if mode == "full" and end_time:
        bits.append(_time_xml("endTime", end_time))
    bits.extend(["</FileInfo>", "</FileInfoList>"])
    return xml_document("".join(bits))


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


def _one_line_preview(value: str | bytes, limit: int = 320) -> str:
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = value
    text = " ".join(text.split())
    return text if len(text) <= limit else f"{text[:limit]}..."


def _is_download_continuation(msg, query_msg_id: int, download_started: bool) -> bool:
    if msg.header.msg_id not in (query_msg_id, MSG_FILE_REPLAY, MSG_FILE_DOWNLOAD_VIDEO, MSG_FILE_DOWNLOAD):
        return False
    if msg.header.response_code not in (0, 200):
        return False
    if msg.header.msg_class == CLASS_FILE_DOWNLOAD:
        return True
    if b"<binaryData>1</binaryData>" in msg.extension:
        return True
    return download_started and bool(msg.payload)


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


def _escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _remove_empty_file(path: Path) -> None:
    try:
        if path.exists() and path.stat().st_size == 0:
            path.unlink()
    except OSError:
        pass


def _finalize_download(part_path: Path, output_path: Path, expected_size: int | None) -> Path:
    actual_size = part_path.stat().st_size
    if expected_size is not None and actual_size != expected_size:
        raise DownloadSizeMismatch(f"Downloaded {actual_size} bytes, expected {expected_size} bytes")
    if output_path.suffix.lower() == ".mp4" and looks_like_bcmedia(part_path):
        if output_path.exists():
            output_path.unlink()
        try:
            bcmedia_to_mp4(part_path, output_path)
        except Exception as exc:
            raw_path = output_path.with_suffix(output_path.suffix + ".bcmedia")
            if raw_path.exists():
                raw_path.unlink()
            part_path.replace(raw_path)
            raise ProtocolError(f"Downloaded BCMedia, but MP4 conversion failed; raw stream saved to {raw_path}: {exc}") from exc
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
        print(f"[pyneolink] {message}")
