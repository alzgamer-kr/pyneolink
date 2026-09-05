from __future__ import annotations

import argparse
import csv
import re
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import MethodType

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pyneolink import Camera, CameraConfig, CameraConnectionError, load_config
from pyneolink.core.const import MSG


DEFAULT_DAYS = 2
DEFAULT_OUTPUT_DIR = Path(".tmp/download-test")


class DownloadMessageTrace:
    """Write every camera send/receive header to a line-buffered CSV file."""

    columns = (
        "datetime",
        "datetime_utc",
        "direction",
        "phase",
        "file_index",
        "file_name",
        "msg_id",
        "message_name",
        "msg_num",
        "response_code",
        "msg_class",
        "channel_id",
        "stream_type",
        "body_len",
        "payload_offset",
        "extension_bytes",
        "payload_bytes",
        "raw_payload_bytes",
        "encrypted_bytes",
        "resync_bytes",
        "xml_or_event",
    )

    def __init__(self, camera: Camera, path: Path) -> None:
        self.camera = camera
        self.path = path
        self.phase = "connect"
        self.file_index = ""
        self.file_name = ""
        self._lock = threading.Lock()
        self._stream = None
        self._writer = None
        self._original_recv = camera._recv_direct
        self._original_send = camera._send_modern
        self._counts: Counter[tuple[str, int, int | str]] = Counter()
        self._message_names = _message_names()
        self._last_timeout_event = 0.0

    def __enter__(self) -> "DownloadMessageTrace":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self.path.open("w", encoding="utf-8", newline="", buffering=1)
        self._writer = csv.DictWriter(self._stream, fieldnames=self.columns)
        self._writer.writeheader()

        def recv_traced(_camera, timeout=None, *, binary_playback_331=False):
            try:
                message = self._original_recv(
                    timeout=timeout,
                    binary_playback_331=binary_playback_331,
                )
            except BaseException as exc:
                if not isinstance(exc, TimeoutError) or time.monotonic() - self._last_timeout_event >= 5.0:
                    self._last_timeout_event = time.monotonic()
                    self._write_event("rx-error", f"{type(exc).__name__}: {exc}")
                raise
            self._write_rx(message)
            return message

        def send_traced(
            _camera,
            msg_id,
            msg_num,
            payload=b"",
            *,
            extension=b"",
            binary_reply=False,
            msg_class=0x6414,
            channel_id=None,
            stream_type=0,
        ):
            self._write_tx(
                msg_id,
                msg_num,
                payload,
                extension=extension,
                msg_class=msg_class,
                channel_id=channel_id,
                stream_type=stream_type,
                binary_reply=binary_reply,
            )
            return self._original_send(
                msg_id,
                msg_num,
                payload,
                extension=extension,
                binary_reply=binary_reply,
                msg_class=msg_class,
                channel_id=channel_id,
                stream_type=stream_type,
            )

        self.camera._recv_direct = MethodType(recv_traced, self.camera)
        self.camera._send_modern = MethodType(send_traced, self.camera)
        return self

    def __exit__(self, *exc: object) -> None:
        self.camera._recv_direct = self._original_recv
        self.camera._send_modern = self._original_send
        if self._stream is not None:
            self._stream.close()
        self._print_summary()

    def select_file(self, index: int, total: int, name: str) -> None:
        """Associate subsequent packets with one selected recording."""
        with self._lock:
            self.phase = "download"
            self.file_index = f"{index}/{total}"
            self.file_name = name

    def set_phase(self, phase: str) -> None:
        """Label subsequent packets with the current test phase."""
        with self._lock:
            self.phase = phase

    def _write_tx(
        self,
        msg_id: int,
        msg_num: int,
        payload: bytes,
        *,
        extension: bytes,
        msg_class: int,
        channel_id: int | None,
        stream_type: int,
        binary_reply: bool,
    ) -> None:
        event = _xml_preview(payload) or ("binary_reply=1" if binary_reply else "")
        self._write(
            direction="tx",
            msg_id=int(msg_id),
            msg_num=msg_num,
            response_code="",
            msg_class=msg_class,
            channel_id=self.camera.config.channel_id if channel_id is None else channel_id,
            stream_type=stream_type,
            body_len=len(extension) + len(payload),
            payload_offset=len(extension),
            extension_bytes=len(extension),
            payload_bytes=len(payload),
            raw_payload_bytes="",
            encrypted_bytes="",
            resync_bytes="",
            xml_or_event=event,
        )

    def _write_rx(self, message) -> None:
        header = message.header
        self._write(
            direction="rx",
            msg_id=int(header.msg_id),
            msg_num=header.msg_num,
            response_code=header.response_code,
            msg_class=header.msg_class,
            channel_id=header.channel_id,
            stream_type=header.stream_type,
            body_len=header.body_len,
            payload_offset=header.payload_offset if header.payload_offset is not None else "",
            extension_bytes=len(message.extension),
            payload_bytes=len(message.payload),
            raw_payload_bytes=message.raw_payload_len,
            encrypted_bytes=message.encrypted_len if message.encrypted_len is not None else "",
            resync_bytes=len(message.resync_data),
            xml_or_event=_xml_preview(message.payload),
        )

    def _write_event(self, direction: str, event: str) -> None:
        self._write(
            direction=direction,
            msg_id="",
            msg_num="",
            response_code="",
            msg_class="",
            channel_id="",
            stream_type="",
            body_len="",
            payload_offset="",
            extension_bytes="",
            payload_bytes="",
            raw_payload_bytes="",
            encrypted_bytes="",
            resync_bytes="",
            xml_or_event=event,
        )

    def _write(self, *, direction: str, msg_id, response_code, msg_class, **values) -> None:
        with self._lock:
            if self._writer is None or self._stream is None:
                return
            msg_id_value = int(msg_id) if msg_id != "" else ""
            response_value = int(response_code) if response_code != "" else ""
            class_value = f"0x{int(msg_class):04x}" if msg_class != "" else ""
            now = datetime.now().astimezone()
            self._writer.writerow(
                {
                    "datetime": now.isoformat(timespec="milliseconds"),
                    "datetime_utc": now.astimezone(timezone.utc).isoformat(timespec="milliseconds"),
                    "direction": direction,
                    "phase": self.phase,
                    "file_index": self.file_index,
                    "file_name": self.file_name,
                    "msg_id": msg_id_value,
                    "message_name": self._message_names.get(msg_id_value, ""),
                    "response_code": response_value,
                    "msg_class": class_value,
                    **values,
                }
            )
            self._stream.flush()
            if msg_id_value != "":
                self._counts[(direction, msg_id_value, response_value)] += 1

    def _print_summary(self) -> None:
        print(f"Message trace: {self.path}", flush=True)
        for (direction, msg_id, response), count in sorted(self._counts.items()):
            name = self._message_names.get(msg_id, "unknown")
            response_text = response if response != "" else "-"
            print(
                f"  {direction} msg_id={msg_id} ({name}) response={response_text} count={count}",
                flush=True,
            )


def _message_names() -> dict[int, str]:
    names = {int(item): item.name for item in MSG}
    catalog = PROJECT_ROOT / "docs" / "10-official-sdk-message-catalog.md"
    if not catalog.exists():
        return names
    for line in catalog.read_text(encoding="utf-8").splitlines():
        match = re.match(r"\| (\d+) \| `([^`]+)` \|", line)
        if match:
            names.setdefault(int(match.group(1)), match.group(2))
    return names


def _xml_preview(payload: bytes, limit: int = 1000) -> str:
    if not payload.lstrip().startswith((b"<", b"<?xml")):
        return ""
    return payload.decode("utf-8", errors="replace").replace("\r", " ").replace("\n", " ")[:limit]


@dataclass
class CameraDownloadResult:
    """Summary of one camera download worker."""

    camera_name: str
    output_dir: Path | None = None
    files_found: int = 0
    files_saved: int = 0
    error: str | None = None


class DownloadTest:
    """Download recordings from multiple cameras using one thread per camera."""

    def __init__(
        self,
        camera_configs: list[CameraConfig],
        *,
        start_date: date,
        end_date: date,
        output_dir: Path = DEFAULT_OUTPUT_DIR,
        quality: str = "high",
        sort: str = "asc",
        rewrite_exists: bool = False,
        max_files: int | None = None,
        debug: bool = False,
        ignore_playback_terminal: bool = False,
        message_trace: Path | None = None,
        recv_timeout: float = 2.0,
    ) -> None:
        self.camera_configs = camera_configs
        self.start_date = start_date
        self.end_date = end_date
        self.output_dir = output_dir
        self.quality = quality
        self.sort = sort
        self.rewrite_exists = rewrite_exists
        self.max_files = max_files
        self.debug = debug
        self.ignore_playback_terminal = ignore_playback_terminal
        self.message_trace = message_trace
        self.recv_timeout = recv_timeout
        self.threads: list[threading.Thread] = []
        self.results: list[CameraDownloadResult] = []
        self._result_lock = threading.Lock()

    def start(self) -> bool:
        """Run all camera workers and return whether every worker succeeded."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._create_threads()
        for thread in self.threads:
            thread.start()
        for thread in self.threads:
            thread.join()

        for result in self.results:
            if result.output_dir is not None:
                self._clean_old_files(result.output_dir)
        self._print_summary()
        return all(result.error is None for result in self.results)

    def _create_threads(self) -> None:
        for camera_config in self.camera_configs:
            thread = threading.Thread(
                target=self._download_camera,
                args=(camera_config,),
                name=f"download-{safe_path_name(camera_config.name)}",
            )
            self.threads.append(thread)

    def _download_camera(self, camera_config: CameraConfig) -> None:
        result = CameraDownloadResult(camera_name=camera_config.name)
        state_path = self.output_dir / ".state" / f"{safe_path_name(camera_config.name)}.json"
        camera = Camera(config=camera_config, state_path=state_path, debug=self.debug)
        trace_path = self._trace_path(camera_config.name)
        trace = DownloadMessageTrace(camera, trace_path) if trace_path is not None else None
        try:
            if trace is not None:
                trace.__enter__()
            with camera:
                if trace is not None:
                    trace.set_phase("camera-info")
                camera_name = str(camera.info().get("name") or camera_config.name)
                result.camera_name = camera_name
                result.output_dir = self.output_dir / safe_path_name(camera_name)
                result.output_dir.mkdir(parents=True, exist_ok=True)

                sd_card = camera.sd_card()
                if trace is not None:
                    trace.set_phase("sd-list")
                files = sd_card.files(
                    start=self.start_date.isoformat(),
                    end=self.end_date.isoformat(),
                    name=".mp4",
                    sort=self.sort,
                )
                if self.max_files is not None:
                    files = files[: self.max_files]
                result.files_found = len(files)
                self._print(camera_name, f"found {len(files)} MP4 recording(s)")

                for index, replay in enumerate(files, start=1):
                    info = replay.info()
                    source_name = info.get("file_name") or info.get("path") or f"recording {index}"
                    if trace is not None:
                        trace.select_file(index, len(files), str(source_name))
                    self._print(camera_name, f"[{index}/{len(files)}] downloading {source_name}")
                    try:
                        replay.download(
                            result.output_dir,
                            quality=self.quality,
                            rewrite_exists=self.rewrite_exists,
                            reconnect_retries=0 if self.ignore_playback_terminal else 3,
                            progress=lambda message, name=camera_name: self._print(name, message),
                            recv_timeout=self.recv_timeout,
                            ignore_playback_terminal=self.ignore_playback_terminal,
                        )
                        result.files_saved += 1
                    except Exception as exc:
                        if not self.ignore_playback_terminal:
                            raise
                        error = f"{type(exc).__name__}: {exc}"
                        result.error = f"{result.error}; {error}" if result.error else error
                        self._print(
                            camera_name,
                            f"[{index}/{len(files)}] probe inconclusive: {error}",
                        )
                        if index < len(files):
                            if trace is not None:
                                trace.set_phase("reconnect")
                            self._print(camera_name, "reconnecting before the next independent probe")
                            time.sleep(5)
                            try:
                                camera.reconnect()
                            except Exception as reconnect_exc:
                                reconnect_error = f"{type(reconnect_exc).__name__}: {reconnect_exc}"
                                result.error = f"{result.error}; reconnect: {reconnect_error}"
                                self._print(camera_name, f"probe reconnect failed: {reconnect_error}")
                                break
        except CameraConnectionError as exc:
            result.error = f"{type(exc).__name__}: {exc}"
            self._print(result.camera_name, f"camera connection error: {exc}")
        except Exception as exc:
            result.error = f"{type(exc).__name__}: {exc}"
            self._print(result.camera_name, f"test failed: {result.error}")
        finally:
            if trace is not None:
                trace.__exit__(None, None, None)
            with self._result_lock:
                self.results.append(result)

    def _trace_path(self, camera_name: str) -> Path | None:
        if self.message_trace is None:
            return None
        if len(self.camera_configs) == 1:
            return self.message_trace
        suffix = self.message_trace.suffix or ".csv"
        return self.message_trace.with_name(f"{self.message_trace.stem}-{safe_path_name(camera_name)}{suffix}")

    def _clean_old_files(self, directory: Path) -> None:
        for path in directory.iterdir():
            if not path.is_file():
                continue
            file_date = date.fromtimestamp(path.stat().st_mtime)
            if file_date < self.start_date:
                path.unlink()
                self._print(directory.name, f"deleted old test file: {path}")

    def _print_summary(self) -> None:
        print("\nDownload test summary:")
        for result in sorted(self.results, key=lambda item: item.camera_name.casefold()):
            status = "failed" if result.error else "completed"
            print(
                f"  {result.camera_name}: {status}; "
                f"found={result.files_found}, saved_or_skipped={result.files_saved}, output={result.output_dir}"
            )
            if result.error:
                print(f"    {result.error}")

    @staticmethod
    def _print(camera_name: str, message: object) -> None:
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        print(f"{timestamp} [{camera_name}] {message}", flush=True)


def safe_path_name(value: str) -> str:
    """Return a readable camera name that is valid as a Windows directory."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", value).strip(" .")
    return cleaned or "camera"


def parse_date(value: str) -> date:
    """Parse an ISO date for argparse."""
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid date {value!r}; expected YYYY-MM-DD") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Download SD-card recordings concurrently from real cameras.")
    parser.add_argument("--config", default="config.json", help="JSON or TOML camera configuration")
    parser.add_argument("--camera", action="append", help="camera name to test; repeat to select multiple cameras")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help="days before today to include")
    parser.add_argument("--start-date", type=parse_date, help="explicit first date (YYYY-MM-DD)")
    parser.add_argument("--end-date", type=parse_date, help="explicit last date (YYYY-MM-DD)")
    parser.add_argument("--quality", choices=("high", "low"), default="high")
    parser.add_argument("--sort", choices=("asc", "desc"), default="asc", help="recording date order")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--rewrite-existing", action="store_true", help="download files that already exist")
    parser.add_argument("--max-files", type=int, help="maximum files to download per camera")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument(
        "--ignore-playback-terminal",
        action="store_true",
        help="diagnostic: observe messages after playback response 300 until the transfer becomes idle",
    )
    parser.add_argument(
        "--message-trace",
        type=Path,
        help="write every received and sent message header to a line-buffered CSV file",
    )
    parser.add_argument(
        "--recv-timeout",
        type=float,
        default=2.0,
        help="per-read timeout; probe idle window is ten times this value, at least 20 seconds",
    )
    return parser


def selected_cameras(config_path: str, names: list[str] | None) -> list[CameraConfig]:
    """Load all configured cameras or the explicitly selected subset."""
    config = load_config(config_path)
    cameras = config.cameras or []
    if not names:
        return cameras
    selected = [camera for camera in cameras if camera.name in names]
    missing = [name for name in names if not any(camera.name == name for camera in selected)]
    if missing:
        raise ValueError(f"camera(s) not found in {config_path}: {', '.join(missing)}")
    return selected


def main() -> int:
    args = build_parser().parse_args()
    if args.days < 0:
        raise SystemExit("--days must be zero or greater")
    if args.max_files is not None and args.max_files < 1:
        raise SystemExit("--max-files must be one or greater")
    if args.recv_timeout <= 0:
        raise SystemExit("--recv-timeout must be greater than zero")
    end_date = args.end_date or date.today()
    start_date = args.start_date or end_date - timedelta(days=args.days)
    if start_date > end_date:
        raise SystemExit("--start-date must not be after --end-date")

    cameras = selected_cameras(args.config, args.camera)
    if not cameras:
        raise SystemExit(f"no cameras found in {args.config}")

    print(
        f"Testing {len(cameras)} camera(s), dates {start_date.isoformat()} through {end_date.isoformat()}, "
        f"quality={args.quality}, sort={args.sort}, output={args.output}"
    )
    test = DownloadTest(
        cameras,
        start_date=start_date,
        end_date=end_date,
        output_dir=args.output,
        quality=args.quality,
        sort=args.sort,
        rewrite_exists=args.rewrite_existing,
        max_files=args.max_files,
        debug=args.debug,
        ignore_playback_terminal=args.ignore_playback_terminal,
        message_trace=args.message_trace,
        recv_timeout=args.recv_timeout,
    )
    return 0 if test.start() else 1


if __name__ == "__main__":
    raise SystemExit(main())
