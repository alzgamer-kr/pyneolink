from __future__ import annotations

import argparse
import csv
import html
import re
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pyneolink import Camera
from pyneolink.config import CameraConfig, load_config
from pyneolink.core.bc import InvalidMagicError
from pyneolink.core.const import MSG


DEFAULT_CONFIG = "config.json"
DEFAULT_OUTPUT_DIR = ".tmp"
DEFAULT_STATE_PATH = ".pyneolink_state.json"
DEFAULT_STREAM = "mainStream"
DEFAULT_DURATION_SECONDS = 3600.0
DEFAULT_STAGGER_SECONDS = 600.0
DEFAULT_SAMPLE_INTERVAL_SECONDS = 30.0
DEFAULT_KEEPALIVE_INTERVAL_SECONDS = 0.75
DEFAULT_RECONNECT_WINDOW_SECONDS = 300.0
DEFAULT_MAX_RECONNECTS = 0
DEFAULT_STALL_WINDOW_SECONDS = 60.0
DEFAULT_SAME_WALL_WINDOW_SECONDS = 60.0
DEFAULT_SAME_SESSION_WINDOW_SECONDS = 60.0

CSV_FIELDS = [
    "timestamp",
    "camera",
    "uid",
    "event",
    "stream",
    "wall_elapsed_seconds",
    "session_elapsed_seconds",
    "session_elapsed_hms",
    "payloads_seen",
    "payloads_delta",
    "payload_bytes",
    "payload_bytes_delta",
    "timeouts",
    "timeout_streak",
    "keepalives_sent",
    "reconnects",
    "last_error",
    "note",
]


@dataclass
class ProbeEvent:
    camera: str
    event: str
    wall_elapsed_seconds: float
    session_elapsed_seconds: float
    last_error: str


class ProbeStats:
    def __init__(self) -> None:
        self.payloads_seen = 0
        self.payload_bytes = 0
        self.timeouts = 0
        self.timeout_streak = 0
        self.keepalives_sent = 0
        self.reconnects = 0
        self.last_payload_at: float | None = None
        self.last_error = ""

    def mark_payload(self, payload: bytes) -> None:
        self.payloads_seen += 1
        self.payload_bytes += len(payload)
        self.timeout_streak = 0
        self.last_payload_at = time.monotonic()

    def mark_timeout(self) -> None:
        self.timeouts += 1
        self.timeout_streak += 1

    def mark_keepalive(self) -> None:
        self.keepalives_sent += 1

    def mark_reconnect(self) -> None:
        self.reconnects += 1
        self.timeout_streak = 0


class SharedCsvWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("w", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._fh, fieldnames=CSV_FIELDS)
        self._writer.writeheader()
        self._fh.flush()
        self._lock = threading.Lock()

    def write(self, row: dict[str, Any]) -> None:
        with self._lock:
            self._writer.writerow(row)
            self._fh.flush()

    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> "SharedCsvWriter":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if len(args.camera) != 2:
        raise SystemExit("Pass exactly two --camera values.")

    config = load_config(args.config)
    camera_configs = [config.camera(name) for name in args.camera]
    result = run_dual_camera_probe(
        camera_configs,
        output_dir=Path(args.out),
        state_path=args.state_path,
        debug=args.debug,
        stream=args.stream,
        duration=args.duration,
        stagger_seconds=args.stagger_seconds,
        sample_interval=args.sample_interval,
        keepalive_interval=args.keepalive_interval,
        stall_window=args.stall_window,
        reconnect_window=args.reconnect_window,
        max_reconnects=args.max_reconnects,
        same_wall_window=args.same_wall_window,
        same_session_window=args.same_session_window,
        write_html=args.html,
    )
    print(f"CSV: {result['csv_path']}")
    if result.get("html_path"):
        print(f"HTML: {result['html_path']}")
    print(f"Result: {result['result']}")
    print(f"Conclusion: {result['conclusion']}")
    return 0 if result["result"] == "completed" else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Probe two live stream sessions with a staggered start to compare failure timing."
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="JSON or TOML PyNeolink config path")
    parser.add_argument(
        "--camera",
        action="append",
        required=True,
        help="Camera name from config. Pass exactly twice, in the order they should start",
    )
    parser.add_argument("--out", default=DEFAULT_OUTPUT_DIR, help="Directory for CSV and HTML results")
    parser.add_argument("--state-path", default=DEFAULT_STATE_PATH, help="Connection state cache path")
    parser.add_argument("--debug", action="store_true", help="Enable PyNeolink protocol debug logging")
    parser.add_argument("--stream", default=DEFAULT_STREAM, help="Stream alias: high/low/mainStream/subStream")
    parser.add_argument("--duration", type=float, default=DEFAULT_DURATION_SECONDS, help="Run time per camera")
    parser.add_argument(
        "--stagger-seconds",
        type=float,
        default=DEFAULT_STAGGER_SECONDS,
        help="Delay before starting the second camera",
    )
    parser.add_argument("--sample-interval", type=float, default=DEFAULT_SAMPLE_INTERVAL_SECONDS)
    parser.add_argument("--keepalive-interval", type=float, default=DEFAULT_KEEPALIVE_INTERVAL_SECONDS)
    parser.add_argument(
        "--stall-window",
        type=float,
        default=DEFAULT_STALL_WINDOW_SECONDS,
        help="Stop or reconnect a camera when no stream payload is received for this many seconds",
    )
    parser.add_argument("--reconnect-window", type=float, default=DEFAULT_RECONNECT_WINDOW_SECONDS)
    parser.add_argument("--max-reconnects", type=int, default=DEFAULT_MAX_RECONNECTS)
    parser.add_argument("--same-wall-window", type=float, default=DEFAULT_SAME_WALL_WINDOW_SECONDS)
    parser.add_argument("--same-session-window", type=float, default=DEFAULT_SAME_SESSION_WINDOW_SECONDS)
    parser.add_argument("--no-html", dest="html", action="store_false", help="Only write CSV")
    parser.set_defaults(html=True)
    return parser.parse_args(argv)


def run_dual_camera_probe(
    camera_configs: list[CameraConfig],
    *,
    output_dir: Path,
    state_path: str | Path | None,
    debug: bool,
    stream: str,
    duration: float,
    stagger_seconds: float,
    sample_interval: float,
    keepalive_interval: float,
    stall_window: float,
    reconnect_window: float,
    max_reconnects: int,
    same_wall_window: float,
    same_session_window: float,
    write_html: bool,
) -> dict[str, str | Path]:
    if len(camera_configs) != 2:
        raise ValueError("dual camera probe requires exactly two camera configs")

    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / result_filename(camera_configs, stream, datetime.now().astimezone(), ".csv")
    html_path = csv_path.with_suffix(".html") if write_html else None
    wall_started_at = time.monotonic()
    stop_event = threading.Event()
    events: list[ProbeEvent] = []
    events_lock = threading.Lock()

    def record_event(event: ProbeEvent) -> None:
        with events_lock:
            events.append(event)

    with SharedCsvWriter(csv_path) as writer:
        threads = [
            threading.Thread(
                target=_run_camera_probe,
                args=(
                    camera_configs[0],
                    0.0,
                    writer,
                    record_event,
                    stop_event,
                    wall_started_at,
                    state_path,
                    debug,
                    stream,
                    duration,
                    sample_interval,
                    keepalive_interval,
                    stall_window,
                    reconnect_window,
                    max_reconnects,
                ),
                name="pyneolink-dual-probe-1",
                daemon=True,
            ),
            threading.Thread(
                target=_run_camera_probe,
                args=(
                    camera_configs[1],
                    max(0.0, stagger_seconds),
                    writer,
                    record_event,
                    stop_event,
                    wall_started_at,
                    state_path,
                    debug,
                    stream,
                    duration,
                    sample_interval,
                    keepalive_interval,
                    stall_window,
                    reconnect_window,
                    max_reconnects,
                ),
                name="pyneolink-dual-probe-2",
                daemon=True,
            ),
        ]
        try:
            print(
                f"{now_text()} dual-probe: starting {camera_configs[0].name}; "
                f"{camera_configs[1].name} starts after {stagger_seconds:g}s"
            )
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        except KeyboardInterrupt:
            stop_event.set()
            print("Interrupted; saving collected samples.")
            for thread in threads:
                thread.join(timeout=5.0)

    conclusion = analyze_events(
        events,
        same_wall_window=same_wall_window,
        same_session_window=same_session_window,
    )
    rows = read_csv_rows(csv_path)
    result = "completed" if all(_camera_completed(rows, config.name) for config in camera_configs) else "incomplete"

    if html_path is not None:
        write_html_report(
            html_path,
            camera_configs=camera_configs,
            stream=stream,
            result=result,
            conclusion=conclusion,
            rows=rows,
            duration=duration,
            stagger_seconds=stagger_seconds,
            sample_interval=sample_interval,
            keepalive_interval=keepalive_interval,
            stall_window=stall_window,
        )

    return {"result": result, "conclusion": conclusion, "csv_path": csv_path, "html_path": html_path or ""}


def _run_camera_probe(
    camera_config: CameraConfig,
    start_delay: float,
    writer: SharedCsvWriter,
    record_event: Any,
    stop_event: threading.Event,
    wall_started_at: float,
    state_path: str | Path | None,
    debug: bool,
    stream: str,
    duration: float,
    sample_interval: float,
    keepalive_interval: float,
    stall_window: float,
    reconnect_window: float,
    max_reconnects: int,
) -> None:
    if start_delay > 0:
        _sleep_or_stop(start_delay, stop_event)
    if stop_event.is_set():
        return

    camera = Camera(camera_config, state_path=state_path, debug=debug)
    stats = ProbeStats()
    stream_msg_num: int | None = None
    session_started_at = time.monotonic()
    deadline = session_started_at + max(0.0, duration)
    next_keepalive_at = session_started_at
    next_sample_at = session_started_at
    last_payloads_seen = 0
    last_payload_bytes = 0
    result = "incomplete"
    note = ""

    try:
        print(f"{now_text()} {camera_config.name}: opening stream={stream}")
        camera.__enter__()
        stream_msg_num = camera.start_stream(stream)
        configure_socket(camera)
        writer.write(
            event_row(
                camera_config=camera_config,
                event="started",
                stream=stream,
                wall_started_at=wall_started_at,
                session_started_at=session_started_at,
                stats=stats,
                payloads_delta=0,
                payload_bytes_delta=0,
                note="",
            )
        )
        print(f"{now_text()} {camera_config.name}: streaming for {duration:g}s")

        while not stop_event.is_set() and time.monotonic() < deadline:
            now = time.monotonic()
            if now >= next_keepalive_at:
                try:
                    camera.send(MSG.UDP_KEEPALIVE, channel_id=0, msg_num=0)
                except (TimeoutError, EOFError, OSError, InvalidMagicError) as exc:
                    stream_msg_num = _recover_or_stop(
                        camera,
                        camera_config=camera_config,
                        stream=stream,
                        stats=stats,
                        exc=exc,
                        writer=writer,
                        record_event=record_event,
                        wall_started_at=wall_started_at,
                        session_started_at=session_started_at,
                        reconnect_window=reconnect_window,
                        max_reconnects=max_reconnects,
                    )
                    configure_socket(camera)
                    next_keepalive_at = time.monotonic() + max(0.1, keepalive_interval)
                    continue
                else:
                    stats.mark_keepalive()
                    next_keepalive_at = now + max(0.1, keepalive_interval)

            if now >= next_sample_at:
                writer.write(
                    event_row(
                        camera_config=camera_config,
                        event="sample",
                        stream=stream,
                        wall_started_at=wall_started_at,
                        session_started_at=session_started_at,
                        stats=stats,
                        payloads_delta=stats.payloads_seen - last_payloads_seen,
                        payload_bytes_delta=stats.payload_bytes - last_payload_bytes,
                        note="",
                    )
                )
                print(
                    f"{now_text()} {camera_config.name}: "
                    f"session={format_hms(now - session_started_at)} "
                    f"payloads={stats.payloads_seen} "
                    f"delta={stats.payloads_seen - last_payloads_seen} "
                    f"timeouts={stats.timeouts}/{stats.timeout_streak} "
                    f"keepalives={stats.keepalives_sent} reconnects={stats.reconnects}"
                )
                last_payloads_seen = stats.payloads_seen
                last_payload_bytes = stats.payload_bytes
                next_sample_at = now + max(1.0, sample_interval)

            try:
                reply = camera._recv(timeout=0.5)
            except TimeoutError:
                stats.mark_timeout()
                if stream_stalled(session_started_at, stats, stall_window):
                    stream_msg_num = _recover_or_stop(
                        camera,
                        camera_config=camera_config,
                        stream=stream,
                        stats=stats,
                        exc=TimeoutError(f"stream stalled for {stall_window:g}s"),
                        writer=writer,
                        record_event=record_event,
                        wall_started_at=wall_started_at,
                        session_started_at=session_started_at,
                        reconnect_window=reconnect_window,
                        max_reconnects=max_reconnects,
                    )
                    configure_socket(camera)
                    next_keepalive_at = time.monotonic() + max(0.1, keepalive_interval)
                continue
            except (EOFError, OSError, InvalidMagicError) as exc:
                stream_msg_num = _recover_or_stop(
                    camera,
                    camera_config=camera_config,
                    stream=stream,
                    stats=stats,
                    exc=exc,
                    writer=writer,
                    record_event=record_event,
                    wall_started_at=wall_started_at,
                    session_started_at=session_started_at,
                    reconnect_window=reconnect_window,
                    max_reconnects=max_reconnects,
                )
                configure_socket(camera)
                next_keepalive_at = time.monotonic() + max(0.1, keepalive_interval)
                continue

            if reply.header.msg_id == MSG.VIDEO and reply.header.msg_num == stream_msg_num and reply.payload:
                stats.mark_payload(reply.payload)
        result = "completed"
        note = f"Completed requested duration {duration:g}s"
        print(f"{now_text()} {camera_config.name}: completed")
    except Exception as exc:
        result = "incomplete"
        note = _error_text(exc)
        stats.last_error = note
        failure = _probe_event(
            camera_config,
            "failed",
            wall_started_at=wall_started_at,
            session_started_at=session_started_at,
            last_error=note,
        )
        record_event(failure)
        print(f"{now_text()} {camera_config.name}: stopped after {note}")
    finally:
        writer.write(
            event_row(
                camera_config=camera_config,
                event=result,
                stream=stream,
                wall_started_at=wall_started_at,
                session_started_at=session_started_at,
                stats=stats,
                payloads_delta=stats.payloads_seen - last_payloads_seen,
                payload_bytes_delta=stats.payload_bytes - last_payload_bytes,
                note=note,
            )
        )
        try:
            if stream_msg_num is not None:
                camera.stop_stream(stream, stream_msg_num)
        except Exception:
            pass
        camera.close()


def _recover_or_stop(
    camera: Camera,
    *,
    camera_config: CameraConfig,
    stream: str,
    stats: ProbeStats,
    exc: BaseException,
    writer: SharedCsvWriter,
    record_event: Any,
    wall_started_at: float,
    session_started_at: float,
    reconnect_window: float,
    max_reconnects: int,
) -> int:
    stats.last_error = _error_text(exc)
    failure = _probe_event(
        camera_config,
        "failure",
        wall_started_at=wall_started_at,
        session_started_at=session_started_at,
        last_error=stats.last_error,
    )
    record_event(failure)
    writer.write(
        event_row(
            camera_config=camera_config,
            event="failure",
            stream=stream,
            wall_started_at=wall_started_at,
            session_started_at=session_started_at,
            stats=stats,
            payloads_delta=0,
            payload_bytes_delta=0,
            note=stats.last_error,
        )
    )
    if stats.reconnects >= max_reconnects:
        raise exc

    print(f"{now_text()} {camera_config.name}: reconnecting after {stats.last_error}")
    started = time.monotonic()
    while True:
        if time.monotonic() - started >= reconnect_window:
            raise exc
        try:
            camera.reconnect()
            msg_num = camera.start_stream(stream)
            stats.mark_reconnect()
            stats.last_error = ""
            writer.write(
                event_row(
                    camera_config=camera_config,
                    event="reconnected",
                    stream=stream,
                    wall_started_at=wall_started_at,
                    session_started_at=session_started_at,
                    stats=stats,
                    payloads_delta=0,
                    payload_bytes_delta=0,
                    note="",
                )
            )
            print(f"{now_text()} {camera_config.name}: reconnected")
            return msg_num
        except (TimeoutError, EOFError, OSError, InvalidMagicError) as reconnect_exc:
            stats.last_error = _error_text(reconnect_exc)
            writer.write(
                event_row(
                    camera_config=camera_config,
                    event="reconnect_failed",
                    stream=stream,
                    wall_started_at=wall_started_at,
                    session_started_at=session_started_at,
                    stats=stats,
                    payloads_delta=0,
                    payload_bytes_delta=0,
                    note=stats.last_error,
                )
            )
            time.sleep(10.0)


def event_row(
    *,
    camera_config: CameraConfig,
    event: str,
    stream: str,
    wall_started_at: float,
    session_started_at: float,
    stats: ProbeStats,
    payloads_delta: int,
    payload_bytes_delta: int,
    note: str,
) -> dict[str, Any]:
    now = time.monotonic()
    session_elapsed = max(0.0, now - session_started_at)
    return {
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "camera": camera_config.name,
        "uid": camera_config.uid,
        "event": event,
        "stream": stream,
        "wall_elapsed_seconds": f"{max(0.0, now - wall_started_at):.3f}",
        "session_elapsed_seconds": f"{session_elapsed:.3f}",
        "session_elapsed_hms": format_hms(session_elapsed),
        "payloads_seen": stats.payloads_seen,
        "payloads_delta": payloads_delta,
        "payload_bytes": stats.payload_bytes,
        "payload_bytes_delta": payload_bytes_delta,
        "timeouts": stats.timeouts,
        "timeout_streak": stats.timeout_streak,
        "keepalives_sent": stats.keepalives_sent,
        "reconnects": stats.reconnects,
        "last_error": stats.last_error,
        "note": note,
    }


def analyze_events(
    events: list[ProbeEvent],
    *,
    same_wall_window: float,
    same_session_window: float,
) -> str:
    failures = [event for event in events if event.event in ("failure", "failed")]
    first_failures = first_event_per_camera(failures)
    if len(first_failures) < 2:
        return "not enough failures to compare"

    left, right = first_failures[:2]
    wall_delta = abs(left.wall_elapsed_seconds - right.wall_elapsed_seconds)
    session_delta = abs(left.session_elapsed_seconds - right.session_elapsed_seconds)
    if wall_delta <= same_wall_window:
        return (
            "same wall-clock failure window "
            f"({wall_delta:.1f}s apart): network, relay, or shared infrastructure is more likely"
        )
    if session_delta <= same_session_window:
        return (
            "same session-age failure window "
            f"({session_delta:.1f}s apart): camera/session lifetime behavior is more likely"
        )
    return (
        "failures did not match the configured windows "
        f"(wall delta {wall_delta:.1f}s, session delta {session_delta:.1f}s)"
    )


def first_event_per_camera(events: list[ProbeEvent]) -> list[ProbeEvent]:
    by_camera: dict[str, ProbeEvent] = {}
    for event in events:
        by_camera.setdefault(event.camera, event)
    return list(by_camera.values())


def _probe_event(
    camera_config: CameraConfig,
    event: str,
    *,
    wall_started_at: float,
    session_started_at: float,
    last_error: str,
) -> ProbeEvent:
    now = time.monotonic()
    return ProbeEvent(
        camera=camera_config.name,
        event=event,
        wall_elapsed_seconds=max(0.0, now - wall_started_at),
        session_elapsed_seconds=max(0.0, now - session_started_at),
        last_error=last_error,
    )


def configure_socket(camera: Camera) -> None:
    sock = getattr(camera, "sock", None)
    if hasattr(sock, "discard_sent"):
        sock.discard_sent()
    if hasattr(sock, "set_max_pending_chunks"):
        sock.set_max_pending_chunks(512)


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def write_html_report(
    path: Path,
    *,
    camera_configs: list[CameraConfig],
    stream: str,
    result: str,
    conclusion: str,
    rows: list[dict[str, Any]],
    duration: float,
    stagger_seconds: float,
    sample_interval: float,
    keepalive_interval: float,
    stall_window: float,
) -> None:
    table_rows = "\n".join(html_table_row(row) for row in rows)
    cameras = ", ".join(config.name for config in camera_configs)
    path.write_text(
        f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>PyNeolink Dual Camera Session Probe</title>
<style>
body {{ font-family: Segoe UI, Arial, sans-serif; margin: 24px; color: #1f2933; }}
.meta {{ display: grid; grid-template-columns: max-content 1fr; gap: 6px 16px; margin-bottom: 20px; }}
.completed {{ color: #137333; font-weight: 700; }}
.incomplete, .interrupted {{ color: #b06000; font-weight: 700; }}
table {{ border-collapse: collapse; margin-top: 20px; font-size: 14px; }}
th, td {{ border: 1px solid #d7dde5; padding: 6px 8px; text-align: left; }}
th {{ background: #eef2f7; }}
</style>
</head>
<body>
<h1>PyNeolink Dual Camera Session Probe</h1>
<div class="meta">
<strong>Cameras</strong><span>{html.escape(cameras)}</span>
<strong>Stream</strong><span>{html.escape(stream)}</span>
<strong>Result</strong><span class="{html.escape(result)}">{html.escape(result)}</span>
<strong>Conclusion</strong><span>{html.escape(conclusion)}</span>
<strong>Duration per camera</strong><span>{duration:g}s</span>
<strong>Stagger</strong><span>{stagger_seconds:g}s</span>
<strong>Sample interval</strong><span>{sample_interval:g}s</span>
<strong>Keepalive interval</strong><span>{keepalive_interval:g}s</span>
<strong>Stall window</strong><span>{stall_window:g}s</span>
</div>
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


def html_table_row(row: dict[str, Any]) -> str:
    cells = "".join(f"<td>{html.escape(str(row.get(field, '')))}</td>" for field in CSV_FIELDS)
    return f"<tr>{cells}</tr>"


def _camera_completed(rows: list[dict[str, str]], camera: str) -> bool:
    return any(row.get("camera") == camera and row.get("event") == "completed" for row in rows)


def result_filename(camera_configs: list[CameraConfig], stream: str, when: datetime, suffix: str) -> str:
    names = "-".join(sanitize_filename(config.uid or config.name) for config in camera_configs)
    timestamp = when.strftime("%Y%m%d-%H%M%S")
    return f"{names}-dual-session-probe-{sanitize_filename(stream)}-{timestamp}{suffix}"


def stream_stalled(session_started_at: float, stats: ProbeStats, stall_window: float) -> bool:
    if stall_window <= 0:
        return False
    since = session_started_at if stats.last_payload_at is None else stats.last_payload_at
    return time.monotonic() - since >= stall_window


def sanitize_filename(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    return safe.strip(".-") or "camera"


def format_hms(seconds: float) -> str:
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _sleep_or_stop(seconds: float, stop_event: threading.Event) -> None:
    deadline = time.monotonic() + seconds
    while not stop_event.is_set() and time.monotonic() < deadline:
        time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))


def _error_text(exc: BaseException) -> str:
    if isinstance(exc, InvalidMagicError):
        return f"InvalidMagicError: magic=0x{exc.magic:08x} data={exc.data.hex()}"
    return f"{type(exc).__name__}: {exc}"


if __name__ == "__main__":
    raise SystemExit(main())
