from __future__ import annotations

import argparse
import re
import sys
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pyneolink import Camera, CameraConfig, CameraConnectionError, load_config


DEFAULT_DAYS = 2
DEFAULT_OUTPUT_DIR = Path(".tmp/download-test")


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
        try:
            with Camera(config=camera_config, state_path=state_path, debug=self.debug) as camera:
                camera_name = str(camera.info().get("name") or camera_config.name)
                result.camera_name = camera_name
                result.output_dir = self.output_dir / safe_path_name(camera_name)
                result.output_dir.mkdir(parents=True, exist_ok=True)

                sd_card = camera.sd_card()
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
                    self._print(camera_name, f"[{index}/{len(files)}] downloading {source_name}")
                    replay.download(
                        result.output_dir,
                        quality=self.quality,
                        rewrite_exists=self.rewrite_exists,
                        reconnect_retries=0 if self.ignore_playback_terminal else 3,
                        progress=lambda message, name=camera_name: self._print(name, message),
                        ignore_playback_terminal=self.ignore_playback_terminal,
                    )
                    result.files_saved += 1
        except CameraConnectionError as exc:
            result.error = f"{type(exc).__name__}: {exc}"
            self._print(result.camera_name, f"camera connection error: {exc}")
        except Exception as exc:
            result.error = f"{type(exc).__name__}: {exc}"
            self._print(result.camera_name, f"test failed: {result.error}")
        finally:
            with self._result_lock:
                self.results.append(result)

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
        help="diagnostic: ignore playback response 300 and read until interrupted or failed",
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
    )
    return 0 if test.start() else 1


if __name__ == "__main__":
    raise SystemExit(main())
