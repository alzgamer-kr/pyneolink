from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import traceback

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from pyneolink.camera import Camera
    from pyneolink.config import load_config, write_json_config
    from pyneolink.core.discovery import local_discover, remote_uid_lookup
    from pyneolink.core.media import MediaParser
    from pyneolink.stream_server import serve_streams
else:
    from .camera import Camera
    from .config import load_config, write_json_config
    from .core.discovery import local_discover, remote_uid_lookup
    from .core.media import MediaParser
    from .stream_server import serve_streams


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pyneolink")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--state", default=".pyneolink_state.json")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--camera")
    parser.add_argument("--info", action="store_true", help="Connect to the camera and print camera information as JSON")
    sub = parser.add_subparsers(dest="command")

    def add_common_options(p: argparse.ArgumentParser) -> None:
        p.add_argument("--config", default=argparse.SUPPRESS)
        p.add_argument("--state", default=argparse.SUPPRESS)
        p.add_argument("--debug", action="store_true", default=argparse.SUPPRESS)

    for name in ("status", "info", "uid", "reboot"):
        p = sub.add_parser(name)
        add_common_options(p)
        p.add_argument("--camera")

    battery = sub.add_parser("battery")
    add_common_options(battery)
    battery.add_argument("--camera")
    battery.add_argument("--raw", action="store_true", help="Print raw battery XML")
    battery.add_argument("--watch", action="store_true", help="Repeat the battery request")
    battery.add_argument("--interval", type=float, default=60.0, help="Seconds between repeated battery requests")
    battery.add_argument("--count", type=int, help="Number of battery requests before exiting")
    battery.add_argument("--mode", choices=["reconnect", "online"], default="reconnect")

    led = sub.add_parser("led")
    add_common_options(led)
    led.add_argument("--camera")
    led.add_argument("value", nargs="?", choices=["on", "off"])

    disc = sub.add_parser("discover")
    add_common_options(disc)
    disc.add_argument("--uid")
    disc.add_argument("--timeout", type=float, default=5.0)
    disc.add_argument("--remote", action="store_true")

    raw = sub.add_parser("raw-stream")
    add_common_options(raw)
    raw.add_argument("--camera")
    raw.add_argument("--stream", default="mainStream", choices=["mainStream", "subStream"])
    raw.add_argument("--output", required=True)
    raw.add_argument("--packets", type=int, default=0, help="Stop after N video packets; 0 means keep running")

    serve = sub.add_parser("serve")
    add_common_options(serve)
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--buffer-seconds", type=float, default=1.0)
    serve.add_argument("--hls-buffer-mb", type=int, default=100)
    serve.add_argument("--hls-segment-seconds", type=float, default=2.0)

    convert = sub.add_parser("convert-config")
    add_common_options(convert)
    convert.add_argument("--from", dest="source", default="config.toml")
    convert.add_argument("--to", dest="target", default="config.json")

    args = parser.parse_args(argv)
    if args.info:
        args.command = "info"

    if not args.command:
        parser.print_help()
        return 1

    if args.command == "convert-config":
        cfg = load_config(args.source)
        write_json_config(cfg, args.target)
        print(f"wrote {args.target}")
        return 0

    if args.command == "serve":
        try:
            serve_streams(
                args.config,
                host=args.host,
                port=args.port,
                state_path=getattr(args, "state", ".pyneolink_state.json"),
                debug=getattr(args, "debug", False),
                buffer_seconds=args.buffer_seconds,
                hls_buffer_mb=args.hls_buffer_mb,
                hls_segment_seconds=args.hls_segment_seconds,
            )
        except KeyboardInterrupt:
            print("stopped")
        return 0

    if args.command == "discover":
        hits = local_discover(args.uid, timeout=args.timeout)
        if args.remote or not hits:
            hits.extend(remote_uid_lookup(args.uid, timeout=args.timeout))
        if not hits:
            print("No camera addresses found for this UID.")
            return 1
        for hit in hits:
            uid = f" uid={hit.uid}" if hit.uid else ""
            print(f"{hit.address[0]}:{hit.address[1]}{uid} source={hit.source}")
        return 0

    cfg = load_config(args.config)
    cam_cfg = cfg.camera(getattr(args, "camera", None))

    try:
        with Camera(cam_cfg, state_path=args.state, debug=getattr(args, "debug", False)) as cam:
            if args.command == "status":
                print(f"{cam_cfg.name}: connected")
            elif args.command == "info":
                print(json.dumps(cam.info(include_sensitive=getattr(args, "debug", False)), indent=2, ensure_ascii=False))
            elif args.command == "uid":
                print(cam.get_uid() or "")
            elif args.command == "battery":
                if args.raw:
                    print(cam.battery().raw(mode=args.mode) or "")
                elif args.watch:
                    with cam.battery().info(
                        interval=args.interval,
                        count=args.count,
                        mode=args.mode,
                    ) as updates:
                        for item in updates:
                            print(json.dumps(item, indent=2, ensure_ascii=False), flush=True)
                else:
                    print(json.dumps(cam.battery().info(mode=args.mode), indent=2, ensure_ascii=False))
            elif args.command == "reboot":
                cam.reboot()
                print(f"{cam_cfg.name}: reboot command sent")
            elif args.command == "led":
                print(cam.led(args.value) or "")
            elif args.command == "raw-stream":
                parser_ = MediaParser()
                written = 0
                with open(args.output, "wb") as fh:
                    for payload in cam.read_stream_payloads(args.stream):
                        for packet in parser_.feed(payload):
                            if packet.kind in ("iframe", "pframe") and packet.codec == "H264":
                                fh.write(packet.data)
                                written += 1
                                if args.packets and written >= args.packets:
                                    print(f"wrote {written} video packets to {args.output}")
                                    return 0
                return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        if getattr(args, "debug", False):
            traceback.print_exc()
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
