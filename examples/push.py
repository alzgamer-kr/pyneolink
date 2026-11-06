"""Push SDK examples; run this file with the same subcommands as ``pyneolink push``."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pyneolink import PushClient, PushEvent, PushResult, load_config
from pyneolink.cli import main as cli_main


def client(camera: str, *, config: str = "config.json") -> PushClient:
    """Build a client without opening a camera or cloud connection."""
    return PushClient(load_config(config).camera(camera))


def initialize(push: PushClient, android_strings: str | Path | None = None) -> PushResult:
    """Use the bundled profile by default; reuse existing receiver credentials."""
    return push.initialize(android_strings=android_strings)


def subscribe(push: PushClient) -> PushResult:
    """Create or verify this PC's subscription, without starting watch."""
    return push.subscribe()


def status(push: PushClient) -> PushResult:
    """Read camera-reported subscription state."""
    return push.status()


def unsubscribe(push: PushClient) -> PushResult:
    """Remove only this receiver's subscription and verify removal."""
    return push.unsubscribe()


def handle_event(event: PushEvent) -> None:
    """Inspect a typed event; replace this function with application actions."""
    print(event.server_time, event.event_type, event.title, event.camera_name)
    if event.low_battery():
        print("Low-battery warning:", event.body)
    elif event.is_people():
        print("Person detected")
    elif event.is_motion():
        print("Motion detected")


def read(push: PushClient, *, raw_output: str | Path | None = None) -> None:
    """Handle a finite unread batch, optionally capturing incoming JSONL data."""
    for event in push.read(raw_output=raw_output):
        handle_event(event)


def watch(
    push: PushClient,
    *,
    duration: float | None = None,
    skip_previous: bool = True,
    raw_output: str | Path | None = None,
) -> None:
    """Receive events without a camera session; replace print with your callback."""
    with push.watch(duration=duration, skip_previous=skip_previous, raw_output=raw_output) as events:
        for event in events:
            handle_event(event)


def main(argv: list[str] | None = None) -> int:
    """Expose init/subscribe/status/unsubscribe/watch/read through the main CLI."""
    return cli_main(["push", *(sys.argv[1:] if argv is None else argv)])


if __name__ == "__main__":
    raise SystemExit(main())
