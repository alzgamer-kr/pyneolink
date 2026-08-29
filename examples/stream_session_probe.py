from __future__ import annotations

import argparse
import csv
import html
import re
import sys
import time
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
DEFAULT_DURATION_SECONDS = 600.0
DEFAULT_SAMPLE_INTERVAL_SECONDS = 30.0
DEFAULT_KEEPALIVE_INTERVAL_SECONDS = 0.75
DEFAULT_RECONNECT_WINDOW_SECONDS = 60.0
DEFAULT_MAX_RECONNECTS = 0
DEFAULT_STALL_WINDOW_SECONDS = 60.0

CSV_FIELDS = [
    "timestamp",
    "elapsed_seconds",
    "elapsed_hms",
    "stream",
    "state",
    "payloads_seen",
    "payloads_delta",
    "payload_bytes",
    "payload_bytes_delta",
    "last_payload_age_seconds",
    "timeouts",
    "timeout_streak",
    "keepalives_sent",
    "reconnects",
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
    "last_error",
    "note",
]


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


class CsvWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("w", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._fh, fieldnames=CSV_FIELDS)
        self._writer.writeheader()
        self._fh.flush()

    def write(self, row: dict[str, Any]) -> None:
        self._writer.writerow(row)
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> "CsvWriter":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_config(args.config)
    camera_config = config.camera(args.camera)
    result = run_probe(
        camera_config,
        output_dir=Path(args.out),
        state_path=args.state_path,
        debug=args.debug,
        stream=args.stream,
        duration=args.duration,
        sample_interval=args.sample_interval,
        keepalive_interval=args.keepalive_interval,
        stall_window=args.stall_window,
        reconnect_window=args.reconnect_window,
        max_reconnects=args.max_reconnects,
        write_html=args.html,
    )
    print(f"CSV: {result['csv_path']}")
    if result.get("html_path"):
        print(f"HTML: {result['html_path']}")
    print(f"Result: {result['result']}")
    return 0 if result["result"] == "completed" else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Probe live stream session stability without battery polling.")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="JSON or TOML PyNeolink config path")
    parser.add_argument("--camera", help="Camera name from config. Required when config contains multiple cameras")
    parser.add_argument("--out", default=DEFAULT_OUTPUT_DIR, help="Directory for CSV and HTML results")
    parser.add_argument("--state-path", default=DEFAULT_STATE_PATH, help="Connection state cache path")
    parser.add_argument("--debug", action="store_true", help="Enable PyNeolink protocol debug logging")
    parser.add_argument("--stream", default=DEFAULT_STREAM, help="Stream alias: high/low/mainStream/subStream")
    parser.add_argument("--duration", type=float, default=DEFAULT_DURATION_SECONDS)
    parser.add_argument("--sample-interval", type=float, default=DEFAULT_SAMPLE_INTERVAL_SECONDS)
    parser.add_argument("--keepalive-interval", type=float, default=DEFAULT_KEEPALIVE_INTERVAL_SECONDS)
    parser.add_argument(
        "--stall-window",
        type=float,
        default=DEFAULT_STALL_WINDOW_SECONDS,
        help="Stop or reconnect when no stream payload is received for this many seconds",
    )
    parser.add_argument("--reconnect-window", type=float, default=DEFAULT_RECONNECT_WINDOW_SECONDS)
    parser.add_argument(
        "--max-reconnects",
        type=int,
        default=DEFAULT_MAX_RECONNECTS,
        help="Reconnect attempts after stream failure. Defaults to 0 to avoid repeated camera wakeups",
    )
    parser.add_argument("--no-html", dest="html", action="store_false", help="Only write CSV")
    parser.set_defaults(html=True)
    return parser.parse_args(argv)


def run_probe(
    camera_config: CameraConfig,
    *,
    output_dir: Path,
    state_path: str | Path | None,
    debug: bool,
    stream: str,
    duration: float,
    sample_interval: float,
    keepalive_interval: float,
    stall_window: float,
    reconnect_window: float,
    max_reconnects: int,
    write_html: bool,
) -> dict[str, str | Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / result_filename(camera_config, stream, datetime.now().astimezone(), ".csv")
    html_path = csv_path.with_suffix(".html") if write_html else None
    stats = ProbeStats()
    camera = Camera(camera_config, state_path=state_path, debug=debug)
    stream_msg_num: int | None = None
    started_at = time.monotonic()
    deadline = started_at + max(0.0, duration)
    next_keepalive_at = started_at
    next_sample_at = started_at
    last_payloads_seen = 0
    last_payload_bytes = 0
    state = "starting"
    result = "incomplete"
    note = ""

    with CsvWriter(csv_path) as writer:
        try:
            print(f"{now_text()} probe: opening stream={stream}")
            camera.__enter__()
            stream_msg_num = camera.start_stream(stream)
            configure_socket(camera)
            state = "streaming"
            print(f"{now_text()} probe: streaming for {duration:g}s")

            while time.monotonic() < deadline:
                now = time.monotonic()
                if now >= next_keepalive_at:
                    try:
                        camera.send(MSG.UDP_KEEPALIVE, channel_id=0, msg_num=0)
                    except (TimeoutError, EOFError, OSError) as exc:
                        stream_msg_num = recover_or_raise(
                            camera,
                            stream=stream,
                            stats=stats,
                            exc=exc,
                            reconnect_window=reconnect_window,
                            max_reconnects=max_reconnects,
                        )
                        configure_socket(camera)
                        state = "streaming"
                        next_keepalive_at = time.monotonic() + max(0.1, keepalive_interval)
                        continue
                    else:
                        stats.mark_keepalive()
                        next_keepalive_at = now + max(0.1, keepalive_interval)

                if now >= next_sample_at:
                    writer.write(
                        sample_row(
                            started_at=started_at,
                            stream=stream,
                            state=state,
                            stats=stats,
                            payloads_delta=stats.payloads_seen - last_payloads_seen,
                            payload_bytes_delta=stats.payload_bytes - last_payload_bytes,
                            socket_stats=socket_debug_snapshot(camera),
                            note=note,
                        )
                    )
                    print(
                        f"{now_text()} sample: elapsed={format_hms(now - started_at)} "
                        f"payloads={stats.payloads_seen} "
                        f"delta={stats.payloads_seen - last_payloads_seen} "
                        f"bytes={stats.payload_bytes} "
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
                    if stream_stalled(started_at, stats, stall_window):
                        stream_msg_num = recover_or_raise(
                            camera,
                            stream=stream,
                            stats=stats,
                            exc=TimeoutError(f"stream stalled for {stall_window:g}s"),
                            reconnect_window=reconnect_window,
                            max_reconnects=max_reconnects,
                        )
                        configure_socket(camera)
                        state = "streaming"
                        next_keepalive_at = time.monotonic() + max(0.1, keepalive_interval)
                    continue
                except (EOFError, OSError) as exc:
                    stream_msg_num = recover_or_raise(
                        camera,
                        stream=stream,
                        stats=stats,
                        exc=exc,
                        reconnect_window=reconnect_window,
                        max_reconnects=max_reconnects,
                    )
                    configure_socket(camera)
                    state = "streaming"
                    next_keepalive_at = time.monotonic() + max(0.1, keepalive_interval)
                    continue

                if reply.header.msg_id == MSG.VIDEO and reply.header.msg_num == stream_msg_num and reply.payload:
                    stats.mark_payload(reply.payload)
                    continue
        except KeyboardInterrupt:
            result = "interrupted"
            note = "Interrupted by user"
            print("Interrupted; saving collected samples.")
        except InvalidMagicError as exc:
            snapshot = socket_debug_snapshot(camera)
            stats.last_error = f"InvalidMagicError: magic=0x{exc.magic:08x} data={exc.data.hex()}"
            note = stats.last_error
            result = "incomplete"
            print(f"{now_text()} probe: stopped after {stats.last_error}")
            print(f"{now_text()} probe: socket snapshot {snapshot}")
        except (TimeoutError, EOFError, OSError) as exc:
            stats.last_error = f"{type(exc).__name__}: {exc}"
            note = stats.last_error
            result = "incomplete"
            print(f"{now_text()} probe: stopped after {stats.last_error}")
        except Exception as exc:
            note = f"{type(exc).__name__}: {exc}"
            result = "incomplete"
            print(f"{now_text()} probe: stopped after {note}")
        else:
            result = "completed"
            note = f"Completed requested duration {duration:g}s"
            print(f"{now_text()} probe: completed")
        finally:
            writer.write(
                sample_row(
                    started_at=started_at,
                    stream=stream,
                    state=result,
                    stats=stats,
                    payloads_delta=stats.payloads_seen - last_payloads_seen,
                    payload_bytes_delta=stats.payload_bytes - last_payload_bytes,
                    socket_stats=socket_debug_snapshot(camera),
                    note=note,
                )
            )
            try:
                if stream_msg_num is not None:
                    camera.stop_stream(stream, stream_msg_num)
            except Exception:
                pass
            camera.close()

    if html_path is not None:
        write_html_report(
            html_path,
            camera_config=camera_config,
            stream=stream,
            result=result,
            rows=read_csv_rows(csv_path),
            duration=duration,
            sample_interval=sample_interval,
            keepalive_interval=keepalive_interval,
            stall_window=stall_window,
            max_reconnects=max_reconnects,
            note=note,
        )

    return {"result": result, "csv_path": csv_path, "html_path": html_path or ""}


def recover_or_raise(
    camera: Camera,
    *,
    stream: str,
    stats: ProbeStats,
    exc: BaseException,
    reconnect_window: float,
    max_reconnects: int,
) -> int:
    stats.last_error = f"{type(exc).__name__}: {exc}"
    if stats.reconnects >= max_reconnects:
        raise exc
    print(f"{now_text()} probe: reconnecting after {stats.last_error}")
    msg_num = reconnect_stream(camera, stream=stream, reconnect_window=reconnect_window)
    stats.mark_reconnect()
    stats.last_error = ""
    return msg_num


def reconnect_stream(camera: Camera, *, stream: str, reconnect_window: float) -> int:
    started = time.monotonic()
    while True:
        try:
            camera.reconnect()
            return camera.start_stream(stream)
        except (TimeoutError, EOFError, OSError):
            if time.monotonic() - started >= reconnect_window:
                raise
            time.sleep(10.0)


def configure_socket(camera: Camera) -> None:
    sock = getattr(camera, "sock", None)
    if hasattr(sock, "discard_sent"):
        sock.discard_sent()
    if hasattr(sock, "set_max_pending_chunks"):
        sock.set_max_pending_chunks(512)


def sample_row(
    *,
    started_at: float,
    stream: str,
    state: str,
    stats: ProbeStats,
    payloads_delta: int,
    payload_bytes_delta: int,
    socket_stats: dict[str, Any] | None,
    note: str,
) -> dict[str, Any]:
    now = time.monotonic()
    elapsed = max(0.0, now - started_at)
    last_payload_age = "" if stats.last_payload_at is None else f"{max(0.0, now - stats.last_payload_at):.3f}"
    socket_stats = socket_stats or empty_socket_debug_snapshot()
    return {
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "elapsed_seconds": f"{elapsed:.3f}",
        "elapsed_hms": format_hms(elapsed),
        "stream": stream,
        "state": state,
        "payloads_seen": stats.payloads_seen,
        "payloads_delta": payloads_delta,
        "payload_bytes": stats.payload_bytes,
        "payload_bytes_delta": payload_bytes_delta,
        "last_payload_age_seconds": last_payload_age,
        "timeouts": stats.timeouts,
        "timeout_streak": stats.timeout_streak,
        "keepalives_sent": stats.keepalives_sent,
        "reconnects": stats.reconnects,
        **socket_stats,
        "last_error": stats.last_error,
        "note": note,
    }


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


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def write_html_report(
    path: Path,
    *,
    camera_config: CameraConfig,
    stream: str,
    result: str,
    rows: list[dict[str, Any]],
    duration: float,
    sample_interval: float,
    keepalive_interval: float,
    stall_window: float,
    max_reconnects: int,
    note: str,
) -> None:
    chart_rows = [row for row in rows if row.get("elapsed_seconds") not in (None, "")]
    max_elapsed = max((float(row["elapsed_seconds"]) for row in chart_rows), default=1.0)
    points = "\n".join(svg_point(row, max_elapsed) for row in chart_rows)
    table_rows = "\n".join(html_table_row(row) for row in rows)
    note_html = f"<p><strong>Note:</strong> {html.escape(note)}</p>" if note else ""

    path.write_text(
        f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>PyNeolink Stream Session Probe</title>
<style>
body {{ font-family: Segoe UI, Arial, sans-serif; margin: 24px; color: #1f2933; }}
.meta {{ display: grid; grid-template-columns: max-content 1fr; gap: 6px 16px; margin-bottom: 20px; }}
.completed {{ color: #137333; font-weight: 700; }}
.incomplete, .interrupted {{ color: #b06000; font-weight: 700; }}
svg {{ width: 100%; max-width: 960px; height: 260px; border: 1px solid #d7dde5; background: #fbfcfe; }}
.axis {{ stroke: #6b7280; stroke-width: 1; }}
.streaming {{ fill: #188038; }}
.reconnecting {{ fill: #b06000; }}
.failed {{ fill: #b3261e; }}
table {{ border-collapse: collapse; margin-top: 20px; font-size: 14px; }}
th, td {{ border: 1px solid #d7dde5; padding: 6px 8px; text-align: left; }}
th {{ background: #eef2f7; }}
</style>
</head>
<body>
<h1>PyNeolink Stream Session Probe</h1>
<div class="meta">
<strong>Camera</strong><span>{html.escape(camera_config.name)}</span>
<strong>Stream</strong><span>{html.escape(stream)}</span>
<strong>Result</strong><span class="{html.escape(result)}">{html.escape(result)}</span>
<strong>Duration</strong><span>{duration:g}s</span>
<strong>Sample interval</strong><span>{sample_interval:g}s</span>
<strong>Keepalive interval</strong><span>{keepalive_interval:g}s</span>
<strong>Stall window</strong><span>{stall_window:g}s</span>
<strong>Max reconnects</strong><span>{max_reconnects}</span>
<strong>Samples</strong><span>{len(chart_rows)}</span>
</div>
{note_html}
<svg viewBox="0 0 1000 260" role="img" aria-label="Stream payload samples over time">
<line class="axis" x1="50" y1="220" x2="970" y2="220"></line>
{points}
</svg>
<p>
Higher points mean more stream payloads during that sample interval.
Green is streaming; orange/red means recovering or stopped.
</p>
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


def svg_point(row: dict[str, Any], max_elapsed: float) -> str:
    elapsed = float(row["elapsed_seconds"])
    payload_delta = int(float(row.get("payloads_delta") or 0))
    x = 50.0 + (elapsed / max(max_elapsed, 1.0)) * 920.0
    y = 220.0 - min(payload_delta, 500) / 500.0 * 180.0
    state = str(row.get("state") or "failed")
    css_class = state if state in ("streaming", "reconnecting") else "failed"
    title = html.escape(f"{row.get('elapsed_hms')}: {state}, +{payload_delta} payloads")
    return f'<circle class="{css_class}" cx="{x:.2f}" cy="{y:.2f}" r="5"><title>{title}</title></circle>'


def html_table_row(row: dict[str, Any]) -> str:
    cells = "".join(f"<td>{html.escape(str(row.get(field, '')))}</td>" for field in CSV_FIELDS)
    return f"<tr>{cells}</tr>"


def result_filename(camera_config: CameraConfig, stream: str, when: datetime, suffix: str) -> str:
    identity = camera_config.uid or camera_config.name
    timestamp = when.strftime("%Y%m%d-%H%M%S")
    return f"{sanitize_filename(identity)}-stream-session-probe-{sanitize_filename(stream)}-{timestamp}{suffix}"


def stream_stalled(started_at: float, stats: ProbeStats, stall_window: float) -> bool:
    if stall_window <= 0:
        return False
    since = started_at if stats.last_payload_at is None else stats.last_payload_at
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


if __name__ == "__main__":
    raise SystemExit(main())
