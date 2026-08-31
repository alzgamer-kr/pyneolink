from __future__ import annotations

import argparse
import csv
import html
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pyneolink import Camera
from pyneolink.config import CameraConfig, load_config
from pyneolink.core.bc import InvalidMagicError, ProtocolError
from pyneolink.core.const import MSG


RECOVERABLE_CONNECTION_ERRORS = (TimeoutError, EOFError, OSError, InvalidMagicError, ProtocolError)
DEFAULT_CONFIG = "config.json"
DEFAULT_MODE = "motion"
DEFAULT_START_PERCENT = 80
DEFAULT_STOP_PERCENT = 20
DEFAULT_WAIT_INTERVAL_SECONDS = 60.0
DEFAULT_SAMPLE_INTERVAL_SECONDS = 300.0
DEFAULT_BATTERY_KEEPALIVE_INTERVAL_SECONDS = 20.0
DEFAULT_RECONNECT_WINDOW_SECONDS = 300.0
DEFAULT_MAX_DURATION_SECONDS = 0.0
DEFAULT_MOTION_POLL_INTERVAL_SECONDS = 1.0
DEFAULT_STREAM_STALL_SECONDS = 15.0
DEFAULT_CONTINUE_PREVIEW_INTERVAL_SECONDS = 0.0
DEFAULT_STREAM = "mainStream"
DEFAULT_OUTPUT_DIR = ".tmp"
CSV_FIELDS = [
    "timestamp",
    "elapsed_seconds",
    "elapsed_hms",
    "level_percent",
    "is_charging",
    "charge_status",
    "adapter_status",
    "charge_type",
    "mode",
    "mode_status",
    "payloads_seen",
    "mode_last_event",
    "motion_none",
    "motion_motion",
    "motion_human",
    "motion_vehicle",
    "motion_unknown",
    "motion_unknown_status",
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
    "udp_unknown",
    "udp_acks_sent",
    "udp_acks_received",
    "udp_heartbeats_sent",
    "udp_resend_packets",
    "udp_seconds_since_data",
    "mode_last_error",
    "note",
]


@dataclass
class ModeStatus:
    state: str = "starting"
    last_error: str | None = None
    last_event: str | None = None
    payloads_seen: int = 0
    event_counts: dict[str, int] = field(default_factory=dict)
    socket_stats: dict[str, Any] = field(default_factory=dict)
    first_failure_at: float | None = None
    failed: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)

    def running(self, detail: str | None = None) -> None:
        with self.lock:
            self.state = "running" if detail is None else detail
            self.last_error = None
            self.first_failure_at = None

    def recovering(self, exc: BaseException) -> None:
        with self.lock:
            self.state = "reconnecting"
            self.last_error = f"{type(exc).__name__}: {exc}"
            if self.first_failure_at is None:
                self.first_failure_at = time.monotonic()

    def mark_failed(self) -> None:
        with self.lock:
            self.failed = True
            self.state = "failed"

    def update_socket_stats(self, camera: Camera) -> None:
        with self.lock:
            self.socket_stats = socket_debug_snapshot(camera)

    def record_motion_status(self, event: dict[str, Any]) -> dict[str, int]:
        event_type = str(event.get("type") or "unknown")
        key = event_type if event.get("known") else "unknown_status"
        with self.lock:
            self.payloads_seen += 1
            self.event_counts[key] = self.event_counts.get(key, 0) + 1
            self.last_event = _motion_event_label(event)
            return dict(self.event_counts)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "state": self.state,
                "last_error": self.last_error,
                "last_event": self.last_event,
                "payloads_seen": self.payloads_seen,
                "event_counts": dict(self.event_counts),
                "socket_stats": self.socket_stats,
                "first_failure_at": self.first_failure_at,
                "failed": self.failed,
            }


@dataclass
class ContinuePreviewTimer:
    interval: float
    next_at: float = field(init=False)

    def __post_init__(self) -> None:
        self.interval = max(0.0, float(self.interval))
        self.next_at = time.monotonic() + self.interval if self.enabled else float("inf")

    @property
    def enabled(self) -> bool:
        return self.interval > 0

    def due(self) -> bool:
        return self.enabled and time.monotonic() >= self.next_at

    def schedule_next(self) -> None:
        self.next_at = time.monotonic() + self.interval if self.enabled else float("inf")


class CsvSampleWriter:
    """Incremental CSV writer that flushes every collected sample."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("w", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._fh, fieldnames=CSV_FIELDS)
        self._writer.writeheader()
        self._fh.flush()
        self.count = 0

    def write(self, row: dict[str, Any]) -> None:
        self._writer.writerow(row)
        self._fh.flush()
        self.count += 1

    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> "CsvSampleWriter":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_config(args.config)
    camera_config = config.camera(args.camera)
    output_dir = Path(args.out)
    output_dir.mkdir(parents=True, exist_ok=True)

    result = run_battery_runtime_test(
        camera_config,
        mode=args.mode,
        output_dir=output_dir,
        state_path=args.state_path,
        debug=args.debug,
        start_percent=args.start_percent,
        stop_percent=args.stop_percent,
        wait_interval=args.wait_interval,
        sample_interval=args.sample_interval,
        battery_keepalive_interval=args.battery_keepalive_interval,
        reconnect_window=args.reconnect_window,
        max_duration=args.max_duration,
        motion_poll_interval=args.motion_poll_interval,
        stream=args.stream,
        continue_preview_interval=args.continue_preview_interval,
        write_html=args.html,
    )
    print(f"CSV: {result['csv_path']}")
    if result.get("html_path"):
        print(f"HTML: {result['html_path']}")
    print(f"Result: {result['result']}")
    return 0 if result["result"] == "completed" else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a long battery runtime test while motion watch or live stream mode is active."
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="JSON or TOML PyNeolink config path")
    parser.add_argument("--camera", help="Camera name from config. Required when config contains multiple cameras")
    parser.add_argument("--mode", choices=["motion", "serve"], default=DEFAULT_MODE)
    parser.add_argument("--out", default=DEFAULT_OUTPUT_DIR, help="Directory for CSV and HTML results")
    parser.add_argument("--state-path", default=".pyneolink_state.json", help="Connection state cache path")
    parser.add_argument("--debug", action="store_true", help="Enable PyNeolink protocol debug logging")
    parser.add_argument("--start-percent", type=int, default=DEFAULT_START_PERCENT)
    parser.add_argument("--stop-percent", type=int, default=DEFAULT_STOP_PERCENT)
    parser.add_argument("--wait-interval", type=float, default=DEFAULT_WAIT_INTERVAL_SECONDS)
    parser.add_argument("--sample-interval", type=float, default=DEFAULT_SAMPLE_INTERVAL_SECONDS)
    parser.add_argument("--battery-keepalive-interval", type=float, default=DEFAULT_BATTERY_KEEPALIVE_INTERVAL_SECONDS)
    parser.add_argument("--reconnect-window", type=float, default=DEFAULT_RECONNECT_WINDOW_SECONDS)
    parser.add_argument(
        "--max-duration",
        type=float,
        default=DEFAULT_MAX_DURATION_SECONDS,
        help="Stop after this many seconds once recording starts. 0 means run until --stop-percent or interruption",
    )
    parser.add_argument(
        "--motion-poll-interval",
        type=float,
        default=DEFAULT_MOTION_POLL_INTERVAL_SECONDS,
        help="Seconds between motion status polls in --mode motion",
    )
    parser.add_argument("--stream", default=DEFAULT_STREAM, help="Stream used by --mode serve")
    parser.add_argument(
        "--continue-preview-interval",
        type=float,
        default=DEFAULT_CONTINUE_PREVIEW_INTERVAL_SECONDS,
        help="Experimental: send LongTimePreview/continuePreview every N seconds during serve mode",
    )
    parser.add_argument("--no-html", dest="html", action="store_false", help="Only write CSV")
    parser.set_defaults(html=True)
    return parser.parse_args(argv)


def run_battery_runtime_test(
    camera_config: CameraConfig,
    *,
    mode: str,
    output_dir: Path,
    state_path: str | Path | None,
    debug: bool,
    start_percent: int,
    stop_percent: int,
    wait_interval: float,
    sample_interval: float,
    battery_keepalive_interval: float,
    reconnect_window: float,
    max_duration: float,
    motion_poll_interval: float,
    stream: str,
    continue_preview_interval: float,
    write_html: bool,
) -> dict[str, str | Path]:
    if stop_percent >= start_percent:
        raise ValueError("--stop-percent must be lower than --start-percent")
    if mode == "motion":
        return run_motion_test(
            camera_config,
            output_dir=output_dir,
            state_path=state_path,
            debug=debug,
            start_percent=start_percent,
            stop_percent=stop_percent,
            wait_interval=wait_interval,
            sample_interval=sample_interval,
            battery_keepalive_interval=battery_keepalive_interval,
            reconnect_window=reconnect_window,
            max_duration=max_duration,
            motion_poll_interval=motion_poll_interval,
            write_html=write_html,
        )
    if mode == "serve":
        return run_stream_test(
            camera_config,
            output_dir=output_dir,
            state_path=state_path,
            debug=debug,
            start_percent=start_percent,
            stop_percent=stop_percent,
            wait_interval=wait_interval,
            sample_interval=sample_interval,
            battery_keepalive_interval=battery_keepalive_interval,
            reconnect_window=reconnect_window,
            max_duration=max_duration,
            stream=stream,
            continue_preview_interval=continue_preview_interval,
            write_html=write_html,
        )


def run_motion_test(
    camera_config: CameraConfig,
    *,
    output_dir: Path,
    state_path: str | Path | None,
    debug: bool,
    start_percent: int,
    stop_percent: int,
    wait_interval: float,
    sample_interval: float,
    battery_keepalive_interval: float,
    reconnect_window: float,
    max_duration: float,
    motion_poll_interval: float,
    write_html: bool,
) -> dict[str, str | Path]:
    status = ModeStatus()
    result = "incomplete"
    note = ""
    csv_path: Path | None = None
    csv_writer: CsvSampleWriter | None = None
    started_at: float | None = None
    poll_interval = max(0.1, motion_poll_interval)
    camera = Camera(camera_config, state_path=state_path, debug=debug)

    try:
        print(f"{now_text()} motion: using one Camera connection for motion and battery")
        with camera:
            with camera.motion().watch(keepalive_interval=min(0.75, poll_interval)) as motion:
                status.running("motion-status-poll")
                status.update_socket_stats(camera)
                print(f"{now_text()} motion: polling status every {poll_interval:g}s")
                print(f"Waiting until battery level is <= {start_percent}%...")
                while True:
                    info = _read_battery_with_recovery(camera, reconnect_window=reconnect_window)
                    level = _battery_level(info)
                    status.update_socket_stats(camera)
                    print_battery("waiting", info)
                    if level is not None and level <= start_percent:
                        started_at = time.monotonic()
                        csv_path = output_dir / result_filename(
                            camera_config,
                            "motion",
                            datetime.now().astimezone(),
                            ".csv",
                        )
                        csv_writer = CsvSampleWriter(csv_path)
                        break
                    _poll_motion_for(
                        motion,
                        seconds=wait_interval,
                        poll_interval=poll_interval,
                        status=status,
                        camera=camera,
                        battery_keepalive_interval=battery_keepalive_interval,
                    )

                print(f"Recording samples every {sample_interval:g}s until battery level is <= {stop_percent}%...")
                while True:
                    info = _read_battery_with_recovery(camera, reconnect_window=reconnect_window)
                    level = _battery_level(info)
                    status.update_socket_stats(camera)
                    if started_at is not None and max_duration > 0 and time.monotonic() - started_at >= max_duration:
                        result = "completed"
                        note = f"Completed requested max duration {max_duration:g}s"
                        break
                    if started_at is None or csv_writer is None:
                        raise RuntimeError("CSV writer was not initialized")
                    row = sample_row(info, mode="motion", mode_status=status.snapshot(), started_at=started_at)
                    csv_writer.write(row)
                    print_battery("sample", info, elapsed=row["elapsed_hms"])
                    if level is not None and level <= stop_percent:
                        result = "completed"
                        break
                    _poll_motion_for(
                        motion,
                        seconds=sample_interval,
                        poll_interval=poll_interval,
                        status=status,
                        camera=camera,
                        battery_keepalive_interval=battery_keepalive_interval,
                    )
    except KeyboardInterrupt:
        note = "Interrupted by user"
        result = "interrupted"
        print("Interrupted; saving collected samples.")
    except Exception as exc:
        note = f"{type(exc).__name__}: {exc}"
        result = "incomplete"
        print(f"Test stopped incomplete: {note}")
    finally:
        if csv_writer is not None:
            csv_writer.close()
        try:
            camera.close()
        except Exception:
            pass

    if csv_path is None:
        csv_path = output_dir / result_filename(camera_config, "motion", datetime.now().astimezone(), ".csv")
        write_csv_header(csv_path)
    if note:
        append_note_row(csv_path, mode="motion", note=note)

    html_path: Path | None = None
    if write_html:
        html_path = csv_path.with_suffix(".html")
        write_html_report_from_csv(
            html_path,
            camera_config=camera_config,
            mode="motion",
            result=result,
            csv_path=csv_path,
            start_percent=start_percent,
            stop_percent=stop_percent,
            sample_interval=sample_interval,
            battery_keepalive_interval=battery_keepalive_interval,
            note=note,
        )
    return {"result": result, "csv_path": csv_path, "html_path": html_path or ""}


def run_stream_test(
    camera_config: CameraConfig,
    *,
    output_dir: Path,
    state_path: str | Path | None,
    debug: bool,
    start_percent: int,
    stop_percent: int,
    wait_interval: float,
    sample_interval: float,
    battery_keepalive_interval: float,
    reconnect_window: float,
    max_duration: float,
    stream: str,
    continue_preview_interval: float,
    write_html: bool,
) -> dict[str, str | Path]:
    status = ModeStatus()
    result = "incomplete"
    note = ""
    csv_path: Path | None = None
    csv_writer: CsvSampleWriter | None = None
    started_at: float | None = None
    camera = Camera(camera_config, state_path=state_path, debug=debug)
    continue_preview = ContinuePreviewTimer(continue_preview_interval)

    try:
        print(f"{now_text()} serve: using one Camera connection for stream and battery")
        stream_msg_num = _open_stream_with_recovery(
            camera,
            stream=stream,
            status=status,
            reconnect_window=reconnect_window,
        )
        status.running(f"stream-{stream}")

        print(f"Waiting until battery level is <= {start_percent}%...")
        while True:
            try:
                connection_id = _connection_identity(camera)
                info = camera.battery().info(mode="online")
                stream_msg_num = _restart_stream_after_implicit_reconnect(
                    camera,
                    previous_connection_id=connection_id,
                    stream=stream,
                    status=status,
                    fallback_msg_num=stream_msg_num,
                )
                _send_continue_preview_if_due(camera, stream, continue_preview)
            except RECOVERABLE_CONNECTION_ERRORS as exc:
                stream_msg_num = _recover_stream(
                    camera,
                    stream=stream,
                    status=status,
                    reconnect_window=reconnect_window,
                    exc=exc,
                )
                continue
            level = _battery_level(info)
            print_battery("waiting", info)
            if level is not None and level <= start_percent:
                started_at = time.monotonic()
                csv_path = output_dir / result_filename(camera_config, "serve", datetime.now().astimezone(), ".csv")
                csv_writer = CsvSampleWriter(csv_path)
                break
            stream_msg_num = _pump_stream_for(
                camera,
                msg_num=stream_msg_num,
                seconds=wait_interval,
                status=status,
                stream=stream,
                reconnect_window=reconnect_window,
                continue_preview=continue_preview,
            )

        print(f"Recording samples every {sample_interval:g}s until battery level is <= {stop_percent}%...")
        while True:
            try:
                connection_id = _connection_identity(camera)
                info = camera.battery().info(mode="online")
                stream_msg_num = _restart_stream_after_implicit_reconnect(
                    camera,
                    previous_connection_id=connection_id,
                    stream=stream,
                    status=status,
                    fallback_msg_num=stream_msg_num,
                )
                _send_continue_preview_if_due(camera, stream, continue_preview)
            except RECOVERABLE_CONNECTION_ERRORS as exc:
                stream_msg_num = _recover_stream(
                    camera,
                    stream=stream,
                    status=status,
                    reconnect_window=reconnect_window,
                    exc=exc,
                )
                continue
            level = _battery_level(info)
            if started_at is not None and max_duration > 0 and time.monotonic() - started_at >= max_duration:
                result = "completed"
                note = f"Completed requested max duration {max_duration:g}s"
                break
            if started_at is None or csv_writer is None:
                raise RuntimeError("CSV writer was not initialized")
            status.update_socket_stats(camera)
            row = sample_row(info, mode="serve", mode_status=status.snapshot(), started_at=started_at, note=note)
            csv_writer.write(row)
            print_battery("sample", info, elapsed=row["elapsed_hms"])
            if level is not None and level <= stop_percent:
                result = "completed"
                break
            stream_msg_num = _pump_stream_for(
                camera,
                msg_num=stream_msg_num,
                seconds=sample_interval,
                status=status,
                stream=stream,
                reconnect_window=reconnect_window,
                continue_preview=continue_preview,
            )
    except KeyboardInterrupt:
        note = "Interrupted by user"
        result = "interrupted"
        print("Interrupted; saving collected samples.")
    except Exception as exc:
        note = f"{type(exc).__name__}: {exc}"
        result = "incomplete"
        print(f"Test stopped incomplete: {note}")
    finally:
        if csv_writer is not None:
            csv_writer.close()
        try:
            camera.close()
        except Exception:
            pass

    if csv_path is None:
        csv_path = output_dir / result_filename(camera_config, "serve", datetime.now().astimezone(), ".csv")
        write_csv_header(csv_path)
    if note:
        append_note_row(csv_path, mode="serve", note=note)

    html_path: Path | None = None
    if write_html:
        html_path = csv_path.with_suffix(".html")
        write_html_report_from_csv(
            html_path,
            camera_config=camera_config,
            mode="serve",
            result=result,
            csv_path=csv_path,
            start_percent=start_percent,
            stop_percent=stop_percent,
            sample_interval=sample_interval,
            battery_keepalive_interval=battery_keepalive_interval,
            note=note,
        )
    return {"result": result, "csv_path": csv_path, "html_path": html_path or ""}


def _open_stream_with_recovery(
    camera: Camera,
    *,
    stream: str,
    status: ModeStatus,
    reconnect_window: float,
) -> int:
    started = time.monotonic()
    attempt = 0
    while True:
        attempt += 1
        try:
            print(f"{now_text()} stream: opening single Camera connection attempt={attempt}")
            camera.__enter__()
            msg_num = camera.start_stream(stream)
            print(f"{now_text()} stream: started {stream}")
            return msg_num
        except RECOVERABLE_CONNECTION_ERRORS as exc:
            status.recovering(exc)
            camera.close()
            if time.monotonic() - started >= reconnect_window:
                status.mark_failed()
                raise
            print(f"{now_text()} stream: open failed with {exc!r}; retrying")
            time.sleep(10.0)


def _pump_stream_for(
    camera: Camera,
    *,
    msg_num: int,
    seconds: float,
    status: ModeStatus,
    stream: str,
    reconnect_window: float,
    continue_preview: ContinuePreviewTimer | None = None,
) -> int:
    deadline = time.monotonic() + max(0.0, seconds)
    next_keepalive_at = time.monotonic() + 0.75
    current_msg_num = msg_num
    last_payload_at = time.monotonic()
    camera.ensure_connected()
    while time.monotonic() < deadline:
        try:
            with camera.subscribe_messages(MSG.VIDEO, current_msg_num, maxsize=200) as replies:
                while time.monotonic() < deadline:
                    if not camera.dispatcher_active:
                        raise TimeoutError("stream dispatcher stopped")
                    now = time.monotonic()
                    _send_continue_preview_if_due(camera, stream, continue_preview)
                    if now >= next_keepalive_at:
                        camera.send(MSG.UDP_KEEPALIVE, channel_id=0, msg_num=0)
                        next_keepalive_at = now + 0.75
                    timeout = min(1.0, max(0.0, deadline - time.monotonic()))
                    if timeout <= 0:
                        return current_msg_num
                    try:
                        message = replies.recv(timeout=timeout)
                    except TimeoutError:
                        _raise_if_stream_stalled(last_payload_at)
                        continue
                    if message.payload:
                        with status.lock:
                            status.payloads_seen += 1
                        last_payload_at = time.monotonic()
                        status.update_socket_stats(camera)
                    else:
                        _raise_if_stream_stalled(last_payload_at)
        except RECOVERABLE_CONNECTION_ERRORS as exc:
            current_msg_num = _recover_stream(
                camera,
                stream=stream,
                status=status,
                reconnect_window=reconnect_window,
                exc=exc,
            )
            next_keepalive_at = time.monotonic() + 0.75
            last_payload_at = time.monotonic()
    return current_msg_num


def _send_continue_preview_if_due(
    camera: Camera,
    stream: str,
    timer: ContinuePreviewTimer | None,
) -> None:
    if timer is None or not timer.due():
        return
    try:
        reply = camera.continue_preview(
            stream,
            retry_on_timeout=False,
        )
    except TimeoutError as exc:
        print(f"{now_text()} preview: continue timed out with {exc!r}")
    except ProtocolError as exc:
        print(f"{now_text()} preview: continue failed with {exc!r}")
    else:
        print(f"{now_text()} preview: continue sent response={reply.header.response_code}")
    finally:
        timer.schedule_next()


def _raise_if_stream_stalled(last_payload_at: float) -> None:
    if time.monotonic() - last_payload_at >= DEFAULT_STREAM_STALL_SECONDS:
        raise TimeoutError("stream payload stalled")


def _recover_stream(
    camera: Camera,
    *,
    stream: str,
    status: ModeStatus,
    reconnect_window: float,
    exc: BaseException,
) -> int:
    status.recovering(exc)
    started = time.monotonic()
    print(f"{now_text()} stream: reconnecting after {exc!r}")
    while True:
        if time.monotonic() - started >= reconnect_window:
            status.mark_failed()
            raise exc
        try:
            camera.reconnect()
            msg_num = camera.start_stream(stream)
            status.running(f"stream-{stream}")
            print(f"{now_text()} stream: reconnected")
            return msg_num
        except RECOVERABLE_CONNECTION_ERRORS as reconnect_exc:
            status.recovering(reconnect_exc)
            print(f"{now_text()} stream: reconnect failed with {reconnect_exc!r}")
            time.sleep(10.0)


def _restart_stream_after_implicit_reconnect(
    camera: Camera,
    *,
    previous_connection_id: int | None,
    stream: str,
    status: ModeStatus,
    fallback_msg_num: int,
) -> int:
    current_connection_id = _connection_identity(camera)
    if current_connection_id == previous_connection_id:
        return fallback_msg_num
    status.recovering(RuntimeError("camera session changed during battery request; restarting stream"))
    msg_num = camera.start_stream(stream)
    status.running(f"stream-{stream}")
    status.update_socket_stats(camera)
    print(f"{now_text()} stream: restarted {stream} after battery reconnect")
    return msg_num


def _connection_identity(camera: Camera) -> int | None:
    return None if camera.sock is None else id(camera.sock)


def _poll_motion_for(
    motion: Any,
    *,
    seconds: float,
    poll_interval: float,
    status: ModeStatus,
    camera: Camera,
    battery_keepalive_interval: float | None = None,
) -> None:
    deadline = time.monotonic() + max(0.0, seconds)
    next_poll_at = time.monotonic()
    next_battery_keepalive_at = (
        time.monotonic() + max(1.0, battery_keepalive_interval) if battery_keepalive_interval else float("inf")
    )
    while time.monotonic() < deadline:
        now = time.monotonic()
        if battery_keepalive_interval and now >= next_battery_keepalive_at:
            try:
                keepalive_result = camera.keepalive(timeout=0.5)
                print(f"{now_text()} battery: keepalive {keepalive_result}")
            except RECOVERABLE_CONNECTION_ERRORS as exc:
                print(f"{now_text()} battery: keepalive failed with {exc!r}")
            next_battery_keepalive_at = time.monotonic() + max(1.0, battery_keepalive_interval)
        if now < next_poll_at:
            sleep_until = min(next_poll_at, next_battery_keepalive_at)
            time.sleep(min(max(0.0, sleep_until - now), max(0.0, deadline - now)))
            continue
        timeout = min(0.5, max(0.0, deadline - time.monotonic()))
        event, known = motion.status(timeout=timeout, close=False)
        event_dict = event.to_dict(known=known)
        counts = status.record_motion_status(event_dict)
        print(
            f"{now_text()} motion: status #{status.snapshot()['payloads_seen']} "
            f"{_motion_event_label(event_dict)} counts={_motion_counts_label(counts)}"
        )
        status.update_socket_stats(camera)
        next_poll_at += poll_interval


def _read_battery_with_recovery(camera: Camera, *, reconnect_window: float) -> dict[str, Any]:
    started = time.monotonic()
    while True:
        try:
            return camera.battery().info(mode="online")
        except RECOVERABLE_CONNECTION_ERRORS as exc:
            print(f"{now_text()} battery: reconnecting after {exc!r}")
            if time.monotonic() - started >= reconnect_window:
                raise
            if camera.sock is not None:
                try:
                    camera.reconnect()
                except RECOVERABLE_CONNECTION_ERRORS as reconnect_exc:
                    print(f"{now_text()} battery: reconnect failed with {reconnect_exc!r}")
            time.sleep(10.0)


def sample_row(
    info: dict[str, Any],
    *,
    mode: str,
    mode_status: dict[str, Any],
    started_at: float,
    note: str = "",
) -> dict[str, Any]:
    elapsed = max(0.0, time.monotonic() - started_at)
    level = _battery_level(info)
    is_charging = info.get("is_charging")
    event_counts = mode_status.get("event_counts") or {}
    return {
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "elapsed_seconds": round(elapsed, 3),
        "elapsed_hms": format_hms(elapsed),
        "level_percent": "" if level is None else level,
        "is_charging": "" if is_charging is None else int(bool(is_charging)),
        "charge_status": info.get("charge_status") or "",
        "adapter_status": info.get("adapter_status") or "",
        "charge_type": info.get("charge_type") or "",
        "mode": mode,
        "mode_status": mode_status["state"],
        "payloads_seen": mode_status["payloads_seen"],
        "mode_last_event": mode_status["last_event"] or "",
        "motion_none": event_counts.get("none", 0),
        "motion_motion": event_counts.get("motion", 0),
        "motion_human": event_counts.get("human", 0),
        "motion_vehicle": event_counts.get("vehicle", 0),
        "motion_unknown": event_counts.get("unknown", 0),
        "motion_unknown_status": event_counts.get("unknown_status", 0),
        **(mode_status.get("socket_stats") or empty_socket_debug_snapshot()),
        "mode_last_error": mode_status["last_error"] or "",
        "note": note,
    }


def write_csv_header(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()


def append_note_row(path: Path, *, mode: str, note: str) -> None:
    row = {
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "elapsed_seconds": "",
        "elapsed_hms": "",
        "level_percent": "",
        "is_charging": "",
        "charge_status": "",
        "adapter_status": "",
        "charge_type": "",
        "mode": mode,
        "mode_status": "stopped",
        "payloads_seen": "",
        "mode_last_event": "",
        "motion_none": "",
        "motion_motion": "",
        "motion_human": "",
        "motion_vehicle": "",
        "motion_unknown": "",
        "motion_unknown_status": "",
        **empty_socket_debug_snapshot(),
        "mode_last_error": "",
        "note": note,
    }
    with path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writerow(row)
        fh.flush()


def write_csv_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    write_csv_header(path)
    with path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writerows(rows)


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def write_html_report_from_csv(
    path: Path,
    *,
    camera_config: CameraConfig,
    mode: str,
    result: str,
    csv_path: Path,
    start_percent: int,
    stop_percent: int,
    sample_interval: float,
    battery_keepalive_interval: float,
    note: str,
) -> None:
    write_html_report(
        path,
        camera_config=camera_config,
        mode=mode,
        result=result,
        rows=read_csv_rows(csv_path),
        start_percent=start_percent,
        stop_percent=stop_percent,
        sample_interval=sample_interval,
        battery_keepalive_interval=battery_keepalive_interval,
        note=note,
    )


def write_html_report(
    path: Path,
    *,
    camera_config: CameraConfig,
    mode: str,
    result: str,
    rows: list[dict[str, Any]],
    start_percent: int,
    stop_percent: int,
    sample_interval: float,
    battery_keepalive_interval: float,
    note: str,
) -> None:
    chart_rows = [
        row
        for row in rows
        if row.get("elapsed_seconds") not in (None, "") and row.get("level_percent") not in (None, "")
    ]
    max_elapsed = max((float(row["elapsed_seconds"]) for row in chart_rows), default=1.0)
    points = "\n".join(_svg_point(row, max_elapsed) for row in chart_rows)
    polyline = " ".join(_svg_xy(row, max_elapsed) for row in chart_rows)
    table_rows = "\n".join(_html_table_row(row) for row in rows)
    camera_name = html.escape(camera_config.name)
    result_label = result_status_label(result)
    result_class = html.escape(result)
    note_html = f"<p><strong>Note:</strong> {html.escape(note)}</p>" if note else ""

    path.write_text(
        f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>PyNeolink Battery Runtime Test</title>
<style>
body {{ font-family: Segoe UI, Arial, sans-serif; margin: 24px; color: #1f2933; }}
.meta {{ display: grid; grid-template-columns: max-content 1fr; gap: 6px 16px; margin-bottom: 20px; }}
.result {{ font-weight: 700; }}
.completed {{ color: #137333; }}
.incomplete, .interrupted {{ color: #b06000; }}
svg {{ width: 100%; max-width: 960px; height: 360px; border: 1px solid #d7dde5; background: #fbfcfe; }}
.axis {{ stroke: #6b7280; stroke-width: 1; }}
.line {{ fill: none; stroke: #374151; stroke-width: 2; }}
.charging {{ fill: #188038; }}
.not-charging {{ fill: #1a73e8; }}
table {{ border-collapse: collapse; margin-top: 20px; font-size: 14px; }}
th, td {{ border: 1px solid #d7dde5; padding: 6px 8px; text-align: left; }}
th {{ background: #eef2f7; }}
</style>
</head>
<body>
<h1>PyNeolink Battery Runtime Test</h1>
<div class="meta">
<strong>Camera</strong><span>{camera_name}</span>
<strong>Mode</strong><span>{html.escape(mode)}</span>
<strong>Result</strong><span class="result {result_class}">{result_label}</span>
<strong>Start threshold</strong><span>{start_percent}%</span>
<strong>Stop threshold</strong><span>{stop_percent}%</span>
<strong>Sample interval</strong><span>{sample_interval:g}s</span>
<strong>Battery keepalive interval</strong><span>{battery_keepalive_interval:g}s</span>
<strong>Samples</strong><span>{len(chart_rows)}</span>
</div>
{note_html}
<svg viewBox="0 0 1000 360" role="img" aria-label="Battery level over time">
<line class="axis" x1="50" y1="20" x2="50" y2="320"></line>
<line class="axis" x1="50" y1="320" x2="970" y2="320"></line>
<text x="8" y="26">100%</text>
<text x="18" y="324">0%</text>
<polyline class="line" points="{polyline}"></polyline>
{points}
</svg>
<p>Green points mean the camera was charging at sample time. Blue points mean it was not charging.</p>
<table>
<thead><tr>{"".join(f"<th>{html.escape(field)}</th>" for field in CSV_FIELDS)}</tr></thead>
<tbody>
{table_rows}
</tbody>
</table>
</body>
</html>
""",
        encoding="utf-8",
    )


def _svg_xy(row: dict[str, Any], max_elapsed: float) -> str:
    elapsed = float(row["elapsed_seconds"])
    level = float(row["level_percent"])
    x = 50.0 + (elapsed / max(max_elapsed, 1.0)) * 920.0
    y = 320.0 - (level / 100.0) * 300.0
    return f"{x:.2f},{y:.2f}"


def _svg_point(row: dict[str, Any], max_elapsed: float) -> str:
    x, y = _svg_xy(row, max_elapsed).split(",", 1)
    point_class = "charging" if str(row["is_charging"]) == "1" else "not-charging"
    title = html.escape(f"{row['elapsed_hms']}: {row['level_percent']}%")
    return f'<circle class="{point_class}" cx="{x}" cy="{y}" r="5"><title>{title}</title></circle>'


def _html_table_row(row: dict[str, Any]) -> str:
    cells = "".join(f"<td>{html.escape(str(row.get(field, '')))}</td>" for field in CSV_FIELDS)
    return f"<tr>{cells}</tr>"


def _motion_event_label(event: dict[str, Any]) -> str:
    known = "known" if event.get("known") else "unknown"
    return (
        f"{event.get('received_at', '')} "
        f"type={event.get('type')} active={event.get('active')} "
        f"status={event.get('status')} ai={event.get('ai_type')} {known}"
    ).strip()


def _motion_counts_label(counts: dict[str, int]) -> str:
    return (
        f"none={counts.get('none', 0)} "
        f"motion={counts.get('motion', 0)} "
        f"human={counts.get('human', 0)} "
        f"vehicle={counts.get('vehicle', 0)} "
        f"unknown={counts.get('unknown', 0)} "
        f"unknown_status={counts.get('unknown_status', 0)}"
    )


def result_filename(camera_config: CameraConfig, mode: str, when: datetime, suffix: str) -> str:
    identity = camera_config.uid or camera_config.name
    timestamp = when.strftime("%Y%m%d-%H%M%S")
    return f"{sanitize_filename(identity)}-{mode}-battery-test-{timestamp}{suffix}"


def sanitize_filename(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    return safe.strip(".-") or "camera"


def format_hms(seconds: float) -> str:
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def result_status_label(result: str) -> str:
    return {
        "completed": "Successful",
        "incomplete": "Incomplete",
        "interrupted": "Interrupted",
    }.get(result, result)


def print_battery(prefix: str, info: dict[str, Any], *, elapsed: str | None = None) -> None:
    elapsed_text = "" if elapsed is None else f" elapsed={elapsed}"
    print(
        f"{now_text()} {prefix}:{elapsed_text} "
        f"level={info.get('level_percent')} "
        f"is_charging={info.get('is_charging')} "
        f"charge_status={info.get('charge_status')} "
        f"adapter_status={info.get('adapter_status')}"
    )


def _battery_level(info: dict[str, Any]) -> int | None:
    level = info.get("level_percent")
    return level if isinstance(level, int) else None


def socket_debug_snapshot(camera: Camera) -> dict[str, Any]:
    sock = getattr(camera, "sock", None)
    if hasattr(sock, "debug_snapshot"):
        return sock.debug_snapshot()
    return empty_socket_debug_snapshot()


def empty_socket_debug_snapshot() -> dict[str, Any]:
    return {
        "udp_next_recv_id": "",
        "udp_last_packet_id": "",
        "udp_max_packet_id": "",
        "udp_pending_chunks": "",
        "udp_pending_gaps": "",
        "udp_buffered_bytes": "",
        "udp_data_packets": "",
        "udp_data_bytes": "",
        "udp_duplicates": "",
        "udp_ignored": "",
        "udp_unknown": "",
        "udp_acks_sent": "",
        "udp_acks_received": "",
        "udp_heartbeats_sent": "",
        "udp_resend_packets": "",
        "udp_seconds_since_data": "",
    }


def now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


if __name__ == "__main__":
    raise SystemExit(main())
