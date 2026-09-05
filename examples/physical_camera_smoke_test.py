from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pyneolink import Camera
from pyneolink.config import load_config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run non-destructive physical checks against one camera.",
    )
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--camera", required=True)
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--quality", choices=("high", "low"), default="high")
    parser.add_argument("--duration", type=float, default=20.0)
    parser.add_argument("--list-limit", type=int, default=10)
    parser.add_argument("--output", default=".tmp/physical-camera-smoke")
    parser.add_argument("--state", default=".tmp/physical-camera-smoke-state.json")
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument("--debug", action="store_true")
    return parser.parse_args()


class SmokeReport:
    def __init__(self, path: Path, camera_name: str) -> None:
        self.path = path
        self.data: dict[str, Any] = {
            "camera": camera_name,
            "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "tests": [],
        }
        self.save()

    def run(self, name: str, action: Callable[[], Any]) -> Any:
        print(f"[{_now()}] {name}: running", flush=True)
        try:
            value = action()
        except Exception as exc:
            self.data["tests"].append(
                {
                    "name": name,
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            self.save()
            print(f"[{_now()}] {name}: failed: {type(exc).__name__}: {exc}", flush=True)
            return None
        self.data["tests"].append({"name": name, "status": "passed", "result": value})
        self.save()
        print(f"[{_now()}] {name}: passed", flush=True)
        return value

    def skip(self, name: str, reason: str) -> None:
        self.data["tests"].append({"name": name, "status": "skipped", "reason": reason})
        self.save()
        print(f"[{_now()}] {name}: skipped: {reason}", flush=True)

    def fail(self, name: str, exc: Exception) -> None:
        self.data["tests"].append(
            {
                "name": name,
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        self.save()
        print(f"[{_now()}] {name}: failed: {type(exc).__name__}: {exc}", flush=True)

    def finish(self) -> None:
        self.data["finished_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        failed = sum(item["status"] == "failed" for item in self.data["tests"])
        self.data["result"] = "passed" if failed == 0 else "incomplete"
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.data, indent=2, ensure_ascii=False, default=_json_default) + "\n",
            encoding="utf-8",
        )


def main() -> int:
    args = parse_args()
    camera_config = load_config(args.config).camera(args.camera)
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_dir = Path(args.output) / _safe_name(args.camera) / run_id
    report = SmokeReport(output_dir / "result.json", args.camera)
    listed_files = []

    try:
        with Camera(camera_config, state_path=args.state, debug=args.debug) as camera:
            report.run("camera_info", lambda: _public_camera_info(camera.info()))
            report.run("battery_status", lambda: camera.battery().info(mode="online"))
            report.run("led_status", camera.led)
            report.run("ir_status", camera.settings().ir.status)
            report.run("pir_status", camera.settings().pir.status)

            def list_sd_files() -> dict:
                files = camera.sd_card().files(
                    start=args.date,
                    end=args.date,
                    name=".mp4",
                    sort="asc",
                )
                listed_files.extend(files)
                limit = max(args.list_limit, 0)
                selected = files[-limit:] if limit else []
                return {
                    "date": args.date,
                    "total": len(files),
                    "shown": [item.info() for item in selected],
                }

            report.run("sd_list", list_sd_files)
            report.run(
                "snapshot",
                lambda: _verify_snapshot(
                    camera.snapshot(out=output_dir / "snapshot.jpg"),
                ),
            )
            report.run(
                "record_20_seconds",
                lambda: _verify_recording(
                    camera.record(
                        out=output_dir / f"record-{args.quality}.ts",
                        duration=max(args.duration, 0.0),
                        stream=_stream_type(args.quality),
                    ),
                ),
            )

            if args.skip_download:
                report.skip("download", "disabled with --skip-download")
            elif not listed_files:
                report.skip("download", "no MP4 recordings found for the selected date")
            else:
                report.run(
                    "download",
                    lambda: _verify_download(
                        listed_files[-1].download(
                            output_dir / "download",
                            quality=args.quality,
                            reconnect_retries=3,
                            rewrite_exists=False,
                            progress=True,
                        ),
                    ),
                )
    except Exception as exc:
        report.fail("camera_session", exc)

    report.finish()
    print(f"Report: {report.path}")
    print(f"Result: {report.data['result']}")
    return 0 if report.data["result"] == "passed" else 1


def _public_camera_info(info: dict) -> dict:
    return {
        "name": info.get("name"),
        "device": info.get("device"),
    }


def _verify_snapshot(path: Path) -> dict:
    data = path.read_bytes()
    if not data.startswith(b"\xff\xd8\xff"):
        raise ValueError(f"snapshot is not a JPEG: {path}")
    return {"path": str(path), "bytes": len(data)}


def _verify_recording(path: Path) -> dict:
    size = path.stat().st_size
    with path.open("rb") as stream:
        first_byte = stream.read(1)
    if first_byte != b"\x47":
        raise ValueError(f"recording is empty or not MPEG-TS: {path}")
    return {"path": str(path), "bytes": size}


def _verify_download(path: Path) -> dict:
    size = path.stat().st_size
    if size <= 0:
        raise ValueError(f"downloaded file is empty: {path}")
    with path.open("rb") as stream:
        head = stream.read(4096)
    if path.suffix.lower() == ".mp4" and b"ftyp" not in head:
        raise ValueError(f"downloaded file has no MP4 header: {path}")
    return {"path": str(path), "bytes": size}


def _stream_type(quality: str) -> str:
    return "mainStream" if quality == "high" else "subStream"


def _safe_name(value: str) -> str:
    safe = "".join(char if char.isalnum() or char in ("-", "_") else "_" for char in value)
    return safe.strip("_") or "camera"


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _json_default(value: Any) -> str:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


if __name__ == "__main__":
    raise SystemExit(main())
