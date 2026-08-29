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

from pyneolink import Camera, StreamServer
from pyneolink.config import CameraConfig, Config, load_config
from pyneolink.core.const import MSG


DEFAULT_CONFIG = "config.json"
DEFAULT_MODE = "motion"
DEFAULT_START_PERCENT = 80
DEFAULT_STOP_PERCENT = 20
DEFAULT_WAIT_INTERVAL_SECONDS = 60.0
DEFAULT_SAMPLE_INTERVAL_SECONDS = 300.0
DEFAULT_BATTERY_KEEPALIVE_INTERVAL_SECONDS = 20.0
DEFAULT_RECONNECT_WINDOW_SECONDS = 300.0
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
    "mode_last_error",
    "note",
]


@dataclass
class ModeStatus:
    state: str = "starting"
    last_error: str | None = None
    last_event: str | None = None
    payloads_seen: int = 0
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

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "state": self.state,
                "last_error": self.last_error,
                "last_event": self.last_event,
                "payloads_seen": self.payloads_seen,
                "first_failure_at": self.first_failure_at,
                "failed": self.failed,
            }


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
        stream=args.stream,
        serve_host=args.serve_host if args.serve_host is not None else config.bind,
        serve_port=args.serve_port if args.serve_port is not None else config.bind_port,
        serve_internal_client=not args.no_serve_internal_client,
        single_connection_serve=args.single_connection_serve,
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
    parser.add_argument("--stream", default=DEFAULT_STREAM, help="Stream used by --mode serve")
    parser.add_argument("--serve-host", help="Bind host used by --mode serve. Defaults to config bind")
    parser.add_argument("--serve-port", type=int, help="Bind port used by --mode serve. Defaults to config bind_port")
    parser.add_argument(
        "--no-serve-internal-client",
        action="store_true",
        help="Run only the HTTP server during --mode serve. Open a stream URL externally to create camera load",
    )
    parser.add_argument(
        "--single-connection-serve",
        action="store_true",
        help="Diagnostic mode: read stream and battery through one Camera connection",
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
    stream: str,
    serve_host: str,
    serve_port: int,
    serve_internal_client: bool,
    single_connection_serve: bool,
    write_html: bool,
) -> dict[str, str | Path]:
    if stop_percent >= start_percent:
        raise ValueError("--stop-percent must be lower than --start-percent")
    if mode == "serve" and single_connection_serve:
        return run_single_connection_stream_test(
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
            stream=stream,
            write_html=write_html,
        )

    stop_event = threading.Event()
    mode_status = ModeStatus()
    worker = threading.Thread(
        target=_run_mode_worker,
        args=(
            camera_config,
            mode,
            stream,
            serve_host,
            serve_port,
            serve_internal_client,
            state_path,
            debug,
            stop_event,
            mode_status,
            reconnect_window,
        ),
        name=f"pyneolink-{mode}-battery-runtime",
        daemon=True,
    )
    worker.start()

    battery_camera = Camera(camera_config, state_path=state_path, debug=debug)
    started_at: float | None = None
    start_wall: datetime | None = None
    csv_path: Path | None = None
    csv_writer: CsvSampleWriter | None = None
    result = "incomplete"
    note = ""

    try:
        battery_camera.__enter__()
        print(f"Waiting until battery level is <= {start_percent}%...")
        while True:
            _raise_if_mode_failed(mode_status)
            info = _read_battery_with_recovery(battery_camera, reconnect_window=reconnect_window)
            level = _battery_level(info)
            print_battery("waiting", info)
            if level is not None and level <= start_percent:
                started_at = time.monotonic()
                start_wall = datetime.now().astimezone()
                csv_path = output_dir / result_filename(camera_config, mode, start_wall, ".csv")
                csv_writer = CsvSampleWriter(csv_path)
                break
            _sleep_with_battery_keepalive(
                wait_interval,
                battery_camera,
                stop_event,
                mode_status,
                keepalive_interval=battery_keepalive_interval,
            )

        print(f"Recording samples every {sample_interval:g}s until battery level is <= {stop_percent}%...")
        while True:
            _raise_if_mode_failed(mode_status)
            info = _read_battery_with_recovery(battery_camera, reconnect_window=reconnect_window)
            level = _battery_level(info)
            row = sample_row(
                info,
                mode=mode,
                mode_status=mode_status.snapshot(),
                started_at=started_at,
                note=note,
            )
            if csv_writer is None:
                raise RuntimeError("CSV writer was not initialized")
            csv_writer.write(row)
            print_battery("sample", info, elapsed=row["elapsed_hms"])
            if level is not None and level <= stop_percent:
                result = "completed"
                break
            _sleep_with_battery_keepalive(
                sample_interval,
                battery_camera,
                stop_event,
                mode_status,
                keepalive_interval=battery_keepalive_interval,
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
        stop_event.set()
        try:
            battery_camera.close()
        finally:
            worker.join(timeout=5.0)
        if csv_writer is not None:
            csv_writer.close()

    if csv_path is None:
        start_wall = datetime.now().astimezone()
        csv_path = output_dir / result_filename(camera_config, mode, start_wall, ".csv")
        write_csv_header(csv_path)
    if note:
        append_note_row(csv_path, mode=mode, note=note)

    html_path: Path | None = None
    if write_html:
        html_path = csv_path.with_suffix(".html")
        write_html_report_from_csv(
            html_path,
            camera_config=camera_config,
            mode=mode,
            result=result,
            csv_path=csv_path,
            start_percent=start_percent,
            stop_percent=stop_percent,
            sample_interval=sample_interval,
            battery_keepalive_interval=battery_keepalive_interval,
            note=note,
        )

    return {"result": result, "csv_path": csv_path, "html_path": html_path or ""}


def run_single_connection_stream_test(
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
    stream: str,
    write_html: bool,
) -> dict[str, str | Path]:
    status = ModeStatus()
    result = "incomplete"
    note = ""
    csv_path: Path | None = None
    csv_writer: CsvSampleWriter | None = None
    started_at: float | None = None
    camera = Camera(camera_config, state_path=state_path, debug=debug)

    try:
        print(f"{now_text()} serve: using one Camera connection for stream and battery")
        stream_msg_num = _open_single_stream_with_recovery(
            camera,
            stream=stream,
            status=status,
            reconnect_window=reconnect_window,
        )
        status.running(f"stream-single-{stream}")

        print(f"Waiting until battery level is <= {start_percent}%...")
        while True:
            try:
                info = camera.battery().info(mode="online")
            except (TimeoutError, EOFError, OSError) as exc:
                stream_msg_num = _recover_single_stream(
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
            )

        print(f"Recording samples every {sample_interval:g}s until battery level is <= {stop_percent}%...")
        while True:
            try:
                info = camera.battery().info(mode="online")
            except (TimeoutError, EOFError, OSError) as exc:
                stream_msg_num = _recover_single_stream(
                    camera,
                    stream=stream,
                    status=status,
                    reconnect_window=reconnect_window,
                    exc=exc,
                )
                continue
            level = _battery_level(info)
            if started_at is None or csv_writer is None:
                raise RuntimeError("CSV writer was not initialized")
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


def _open_single_stream_with_recovery(
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
        except (TimeoutError, EOFError, OSError) as exc:
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
) -> int:
    deadline = time.monotonic() + max(0.0, seconds)
    next_keepalive_at = time.monotonic() + 0.75
    current_msg_num = msg_num
    while time.monotonic() < deadline:
        try:
            now = time.monotonic()
            if now >= next_keepalive_at:
                camera.send(MSG.UDP_KEEPALIVE, channel_id=0, msg_num=0)
                next_keepalive_at = now + 0.75
            timeout = min(1.0, max(0.0, deadline - time.monotonic()))
            if timeout <= 0:
                return current_msg_num
            reply = camera._recv(timeout=timeout)
            if reply.header.msg_id == MSG.VIDEO and reply.header.msg_num == current_msg_num and reply.payload:
                with status.lock:
                    status.payloads_seen += 1
        except TimeoutError:
            continue
        except (EOFError, OSError) as exc:
            current_msg_num = _recover_single_stream(
                camera,
                stream=stream,
                status=status,
                reconnect_window=reconnect_window,
                exc=exc,
            )
            next_keepalive_at = time.monotonic() + 0.75
    return current_msg_num


def _recover_single_stream(
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
            status.running(f"stream-single-{stream}")
            print(f"{now_text()} stream: reconnected")
            return msg_num
        except (TimeoutError, EOFError, OSError) as reconnect_exc:
            status.recovering(reconnect_exc)
            print(f"{now_text()} stream: reconnect failed with {reconnect_exc!r}")
            time.sleep(10.0)


def _run_mode_worker(
    camera_config: CameraConfig,
    mode: str,
    stream: str,
    serve_host: str,
    serve_port: int,
    serve_internal_client: bool,
    state_path: str | Path | None,
    debug: bool,
    stop_event: threading.Event,
    status: ModeStatus,
    reconnect_window: float,
) -> None:
    while not stop_event.is_set():
        try:
            if mode == "motion":
                _run_motion_mode(camera_config, state_path, debug, stop_event, status)
            elif serve_internal_client:
                _run_stream_client_mode(camera_config, stream, state_path, debug, stop_event, status)
            else:
                _run_http_serve_mode(
                    camera_config,
                    serve_host,
                    serve_port,
                    state_path,
                    debug,
                    stop_event,
                    status,
                )
        except Exception as exc:
            status.recovering(exc)
            failure_started = status.snapshot().get("first_failure_at")
            if failure_started is not None and time.monotonic() - float(failure_started) >= reconnect_window:
                status.mark_failed()
                stop_event.set()
                return
            time.sleep(min(10.0, max(1.0, reconnect_window)))


def _run_motion_mode(
    camera_config: CameraConfig,
    state_path: str | Path | None,
    debug: bool,
    stop_event: threading.Event,
    status: ModeStatus,
) -> None:
    with Camera(camera_config, state_path=state_path, debug=debug) as camera:
        with camera.motion().watch() as events:
            status.running("motion-watch")
            for event in events:
                with status.lock:
                    status.last_event = f"{event.received_at.isoformat()} {event}"
                if stop_event.is_set():
                    return


def _run_http_serve_mode(
    camera_config: CameraConfig,
    serve_host: str,
    serve_port: int,
    state_path: str | Path | None,
    debug: bool,
    stop_event: threading.Event,
    status: ModeStatus,
) -> None:
    server_config = Config(bind=serve_host, bind_port=serve_port, cameras=[camera_config])
    with StreamServer(server_config, state_path=None if state_path is None else str(state_path), debug=debug):
        status.running("serve-listening")
        print(f"{now_text()} serve: waiting for an external stream client")
        while not stop_event.is_set():
            time.sleep(1.0)


def _run_stream_client_mode(
    camera_config: CameraConfig,
    stream: str,
    state_path: str | Path | None,
    debug: bool,
    stop_event: threading.Event,
    status: ModeStatus,
) -> None:
    with Camera(camera_config, state_path=state_path, debug=debug) as camera:
        status.running(f"stream-client-{stream}")
        print(f"{now_text()} serve: reading {stream} through an isolated Camera connection")
        for payload in camera.read_stream_payloads(stream):
            if payload:
                with status.lock:
                    status.payloads_seen += 1
            if stop_event.is_set():
                return


def _read_battery_with_recovery(camera: Camera, *, reconnect_window: float) -> dict[str, Any]:
    started = time.monotonic()
    while True:
        try:
            return camera.battery().info(mode="online")
        except (TimeoutError, EOFError, OSError) as exc:
            print(f"{now_text()} battery: reconnecting after {exc!r}")
            if time.monotonic() - started >= reconnect_window:
                raise
            try:
                camera.reconnect()
            except (TimeoutError, EOFError, OSError) as reconnect_exc:
                print(f"{now_text()} battery: reconnect failed with {reconnect_exc!r}")
                pass
            time.sleep(10.0)


def _sleep_with_status(seconds: float, stop_event: threading.Event, status: ModeStatus) -> None:
    deadline = time.monotonic() + max(0.0, seconds)
    while not stop_event.is_set():
        _raise_if_mode_failed(status)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(1.0, remaining))


def _sleep_with_battery_keepalive(
    seconds: float,
    camera: Camera,
    stop_event: threading.Event,
    status: ModeStatus,
    *,
    keepalive_interval: float,
) -> None:
    deadline = time.monotonic() + max(0.0, seconds)
    next_keepalive_at = time.monotonic() + max(1.0, keepalive_interval)
    while not stop_event.is_set():
        _raise_if_mode_failed(status)
        now = time.monotonic()
        remaining = deadline - now
        if remaining <= 0:
            return
        if now >= next_keepalive_at:
            try:
                keepalive_result = camera.keepalive(timeout=0.5)
                print(f"{now_text()} battery: keepalive {keepalive_result}")
            except (TimeoutError, EOFError, OSError) as exc:
                print(f"{now_text()} battery: keepalive failed with {exc!r}")
            next_keepalive_at = time.monotonic() + max(1.0, keepalive_interval)
        time.sleep(min(1.0, remaining))


def _raise_if_mode_failed(status: ModeStatus) -> None:
    snapshot = status.snapshot()
    if snapshot["failed"]:
        detail = snapshot["last_error"] or "mode connection did not recover"
        raise RuntimeError(detail)


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


def now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


if __name__ == "__main__":
    raise SystemExit(main())
