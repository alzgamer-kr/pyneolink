from __future__ import annotations

import argparse
from collections.abc import Callable
import json
from pathlib import Path
import sys
import time
import traceback

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from pyneolink.camera import Camera
    from pyneolink.config import CameraConfig, load_config, write_json_config
    from pyneolink.core.const import msg
    from pyneolink.core.discovery import local_discover, remote_uid_lookup
    from pyneolink.core.media import MediaParser
    from pyneolink.stream_server import serve_streams
else:
    from .camera import Camera
    from .config import CameraConfig, load_config, write_json_config
    from .core.const import msg
    from .core.discovery import local_discover, remote_uid_lookup
    from .core.media import MediaParser
    from .stream_server import serve_streams


CommandHandler = Callable[[argparse.Namespace], int]
CameraCommandHandler = Callable[[argparse.Namespace, Camera, CameraConfig], int]


class CLI:
    """Command-line interface wrapper around the public SDK."""

    def __init__(self, argv: list[str] | None = None) -> None:
        """
        Create a CLI runner.

        :param argv: Optional argument list. Defaults to ``sys.argv[1:]`` when parsed by argparse.
        """

        self.argv = argv
        self.parser = self.build_parser()
        self.handlers: dict[str, CommandHandler] = {
            "convert-config": self.run_convert_config,
            "serve": self.run_serve,
            "discover": self.run_discover,
            "status": self.run_camera_command,
            "info": self.run_camera_command,
            "uid": self.run_camera_command,
            "battery": self.run_camera_command,
            "reboot": self.run_camera_command,
            "led": self.run_camera_command,
            "snapshot": self.run_camera_command,
            "record": self.run_camera_command,
            "events": self.run_camera_command,
            "motion": self.run_camera_command,
            "voice": self.run_camera_command,
            "pir": self.run_camera_command,
            "ir": self.run_camera_command,
            "raw-stream": self.run_camera_command,
        }
        self.camera_handlers: dict[str, CameraCommandHandler] = {
            "status": self.camera_status,
            "info": self.camera_info,
            "uid": self.camera_uid,
            "battery": self.camera_battery,
            "reboot": self.camera_reboot,
            "led": self.camera_led,
            "snapshot": self.camera_snapshot,
            "record": self.camera_record,
            "events": self.camera_events,
            "motion": self.camera_motion,
            "voice": self.camera_voice,
            "pir": self.camera_pir,
            "ir": self.camera_ir,
            "raw-stream": self.camera_raw_stream,
        }

    def run(self) -> int:
        """Parse arguments and run the selected command."""

        args = self.parse_args(self.argv)
        if not args.command:
            self.parser.print_help()
            return 1

        handler = self.handlers.get(args.command)
        if handler is None:
            self.parser.error(msg.Error.UnknownCommand.format(command=args.command))

        try:
            return handler(args)
        except KeyboardInterrupt:
            print(msg.Log.Stopped)
            return 0
        except Exception as exc:
            print(msg.Log.Error.format(exc=exc), file=sys.stderr)
            if getattr(args, "debug", False):
                traceback.print_exc()
            return 2

    def parse_args(self, argv: list[str] | None = None) -> argparse.Namespace:
        """
        Parse command-line arguments.

        :param argv: Optional argument list to parse.
        """

        args = self.parser.parse_args(argv)
        if args.info:
            args.command = "info"
        return args

    def build_parser(self) -> argparse.ArgumentParser:
        """Build and return the argparse command tree."""

        parser = argparse.ArgumentParser(prog="pyneolink")
        parser.add_argument("--config", default="config.json")
        parser.add_argument("--state", default=".pyneolink_state.json")
        parser.add_argument("--debug", action="store_true")
        parser.add_argument("--camera")
        parser.add_argument("--info", action="store_true", help="Connect to the camera and print camera information as JSON")
        subparsers = parser.add_subparsers(dest="command")

        for name in ("status", "info", "uid", "reboot"):
            command_parser = subparsers.add_parser(name)
            self.add_common_options(command_parser)
            command_parser.add_argument("--camera")

        battery = subparsers.add_parser("battery")
        self.add_common_options(battery)
        battery.add_argument("--camera")
        battery.add_argument("--raw", action="store_true", help="Print raw battery XML")
        battery.add_argument("--watch", action="store_true", help="Repeat the battery request")
        battery.add_argument("--interval", type=float, default=60.0, help="Seconds between repeated battery requests")
        battery.add_argument("--count", type=int, help="Number of battery requests before exiting")
        battery.add_argument("--mode", choices=["reconnect", "online"], default="reconnect")

        led = subparsers.add_parser("led")
        self.add_common_options(led)
        led.add_argument("--camera")
        led.add_argument("value", nargs="?", choices=["on", "off", "auto"])

        snapshot = subparsers.add_parser("snapshot")
        self.add_common_options(snapshot)
        snapshot.add_argument("--camera")
        snapshot.add_argument("-out", "--out", required=True, help="Path or directory for the JPEG snapshot")
        snapshot.add_argument("--stream-type", default="main", choices=["main", "sub"])

        record = subparsers.add_parser("record")
        self.add_common_options(record)
        record.add_argument("--camera")
        record.add_argument("-out", "--out", required=True, help="Path or directory for the MPEG-TS recording")
        record.add_argument("--duration", type=float, help="Seconds to record; omit to record until Ctrl+C")
        record.add_argument("--quality", default="high", choices=["high", "low"])

        events = subparsers.add_parser("events")
        self.add_common_options(events)
        events.add_argument("--camera")
        events.add_argument("--count", type=int, help="Stop after N events; omit to keep listening")

        motion = subparsers.add_parser("motion")
        self.add_common_options(motion)
        motion.add_argument("--camera")
        motion.add_argument("--watch", action="store_true", help="Keep listening for motion events")
        motion.add_argument("--count", type=int, help="With --watch, stop after N events")
        motion.add_argument("--duration", type=float, help="With --watch, stop after this many seconds")
        motion.add_argument("--timeout", type=float, default=3.0, help="Seconds to wait for an immediate motion state event")

        voice = subparsers.add_parser("voice")
        self.add_common_options(voice)
        voice.add_argument("--camera")
        voice.add_argument("--file", help="Audio file to play through the camera speaker")
        voice.add_argument("--microphone", action="store_true", help="Use the local microphone as the voice source")
        voice.add_argument("--tone", type=float, help="Play a generated test tone at this frequency, for example 1000")
        voice.add_argument("--siren", action="store_true", help="Trigger the camera siren")
        voice.add_argument("--seconds", type=float, help="Seconds for microphone or tone modes")
        voice.add_argument("--volume", type=float, default=1.0)
        voice.add_argument("--voice-codec", choices=["python", "ffmpeg"], default="python", help="ADPCM encoder for --file")
        voice.add_argument("--voice-wait-ack", action="store_true", help="Wait for every talk packet acknowledgement")

        pir = subparsers.add_parser("pir")
        self.add_common_options(pir)
        pir.add_argument("--camera")
        pir.add_argument("action", choices=["status", "on", "off"])

        ir = subparsers.add_parser("ir")
        self.add_common_options(ir)
        ir.add_argument("--camera")
        ir.add_argument("action", choices=["status", "on", "off", "auto"])

        discover = subparsers.add_parser("discover")
        self.add_common_options(discover)
        discover.add_argument("--uid")
        discover.add_argument("--timeout", type=float, default=5.0)
        discover.add_argument("--remote", action="store_true")

        raw_stream = subparsers.add_parser("raw-stream")
        self.add_common_options(raw_stream)
        raw_stream.add_argument("--camera")
        raw_stream.add_argument("--stream", default="mainStream", choices=["mainStream", "subStream"])
        raw_stream.add_argument("--output", required=True)
        raw_stream.add_argument("--packets", type=int, default=0, help="Stop after N video packets; 0 means keep running")

        serve = subparsers.add_parser("serve")
        self.add_common_options(serve)
        serve.add_argument("--host")
        serve.add_argument("--port", type=int)
        serve.add_argument("--buffer-seconds", type=float, default=1.0)
        serve.add_argument("--hls-buffer-mb", type=int, default=100)
        serve.add_argument("--hls-segment-seconds", type=float, default=2.0)

        convert = subparsers.add_parser("convert-config")
        self.add_common_options(convert)
        convert.add_argument("--from", dest="source", default="config.toml")
        convert.add_argument("--to", dest="target", default="config.json")

        return parser

    @staticmethod
    def add_common_options(parser: argparse.ArgumentParser) -> None:
        """
        Add global options to a subcommand parser.

        :param parser: Subcommand parser to update.
        """

        parser.add_argument("--config", default=argparse.SUPPRESS)
        parser.add_argument("--state", default=argparse.SUPPRESS)
        parser.add_argument("--debug", action="store_true", default=argparse.SUPPRESS)

    def run_convert_config(self, args: argparse.Namespace) -> int:
        """
        Convert a TOML or JSON config file to JSON.

        :param args: Parsed CLI arguments.
        """

        cfg = load_config(args.source)
        write_json_config(cfg, args.target)
        print(msg.Log.ConfigWritten.format(path=args.target))
        return 0

    def run_serve(self, args: argparse.Namespace) -> int:
        """
        Start the HTTP stream server.

        :param args: Parsed CLI arguments.
        """

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
        return 0

    def run_discover(self, args: argparse.Namespace) -> int:
        """
        Discover camera addresses locally or through UID lookup.

        :param args: Parsed CLI arguments.
        """

        hits = local_discover(args.uid, timeout=args.timeout)
        if args.remote or not hits:
            hits.extend(remote_uid_lookup(args.uid, timeout=args.timeout))
        if not hits:
            print(msg.Log.NoUidAddresses)
            return 1
        for hit in hits:
            uid = f" uid={hit.uid}" if hit.uid else ""
            print(msg.Log.DiscoveryHit.format(host=hit.address[0], port=hit.address[1], uid=uid, source=hit.source))
        return 0

    def run_camera_command(self, args: argparse.Namespace) -> int:
        """
        Load camera configuration, open a camera connection, and dispatch a camera command.

        :param args: Parsed CLI arguments.
        """

        cfg = load_config(args.config)
        cam_cfg = cfg.camera(getattr(args, "camera", None))
        handler = self.camera_handlers[args.command]

        with Camera(cam_cfg, state_path=args.state, debug=getattr(args, "debug", False)) as cam:
            return handler(args, cam, cam_cfg)

    def camera_status(self, args: argparse.Namespace, cam: Camera, cam_cfg: CameraConfig) -> int:
        """
        Print a simple connected status.

        :param args: Parsed CLI arguments.
        :param cam: Connected camera instance.
        :param cam_cfg: Selected camera configuration.
        """

        print(msg.Log.CameraConnected.format(name=cam_cfg.name))
        return 0

    def camera_info(self, args: argparse.Namespace, cam: Camera, cam_cfg: CameraConfig) -> int:
        """
        Print camera information as JSON.

        :param args: Parsed CLI arguments.
        :param cam: Connected camera instance.
        :param cam_cfg: Selected camera configuration.
        """

        print(json.dumps(cam.info(include_sensitive=getattr(args, "debug", False)), indent=2, ensure_ascii=False))
        return 0

    def camera_uid(self, args: argparse.Namespace, cam: Camera, cam_cfg: CameraConfig) -> int:
        """
        Print the camera UID.

        :param args: Parsed CLI arguments.
        :param cam: Connected camera instance.
        :param cam_cfg: Selected camera configuration.
        """

        print(cam.get_uid() or "")
        return 0

    def camera_battery(self, args: argparse.Namespace, cam: Camera, cam_cfg: CameraConfig) -> int:
        """
        Dispatch battery commands.

        :param args: Parsed CLI arguments.
        :param cam: Connected camera instance.
        :param cam_cfg: Selected camera configuration.
        """

        battery_handlers: list[tuple[bool, Callable[[], int]]] = [
            (args.raw, lambda: self.camera_battery_raw(args, cam)),
            (args.watch, lambda: self.camera_battery_watch(args, cam)),
        ]
        for enabled, handler in battery_handlers:
            if enabled:
                return handler()
        return self.camera_battery_once(args, cam)

    def camera_battery_raw(self, args: argparse.Namespace, cam: Camera) -> int:
        """
        Print raw battery XML.

        :param args: Parsed CLI arguments.
        :param cam: Connected camera instance.
        """

        print(cam.battery().raw(mode=args.mode) or "")
        return 0

    def camera_battery_once(self, args: argparse.Namespace, cam: Camera) -> int:
        """
        Print one parsed battery response.

        :param args: Parsed CLI arguments.
        :param cam: Connected camera instance.
        """

        print(json.dumps(cam.battery().info(mode=args.mode), indent=2, ensure_ascii=False))
        return 0

    def camera_battery_watch(self, args: argparse.Namespace, cam: Camera) -> int:
        """
        Print repeated battery responses.

        :param args: Parsed CLI arguments.
        :param cam: Connected camera instance.
        """

        with cam.battery().info(interval=args.interval, count=args.count, mode=args.mode) as updates:
            for item in updates:
                print(json.dumps(item, indent=2, ensure_ascii=False), flush=True)
        return 0

    def camera_reboot(self, args: argparse.Namespace, cam: Camera, cam_cfg: CameraConfig) -> int:
        """
        Send a reboot request.

        :param args: Parsed CLI arguments.
        :param cam: Connected camera instance.
        :param cam_cfg: Selected camera configuration.
        """

        cam.reboot()
        print(msg.Log.RebootSent.format(name=cam_cfg.name))
        return 0

    def camera_led(self, args: argparse.Namespace, cam: Camera, cam_cfg: CameraConfig) -> int:
        """
        Get or set the camera LED state.

        :param args: Parsed CLI arguments.
        :param cam: Connected camera instance.
        :param cam_cfg: Selected camera configuration.
        """

        print(json.dumps(cam.led(args.value), indent=2, ensure_ascii=False))
        return 0

    def camera_snapshot(self, args: argparse.Namespace, cam: Camera, cam_cfg: CameraConfig) -> int:
        """
        Save a JPEG snapshot.

        :param args: Parsed CLI arguments.
        :param cam: Connected camera instance.
        :param cam_cfg: Selected camera configuration.
        """

        path = cam.snapshot(out=args.out, stream_type=args.stream_type)
        print(msg.Log.SnapshotSaved.format(output=path))
        return 0

    def camera_record(self, args: argparse.Namespace, cam: Camera, cam_cfg: CameraConfig) -> int:
        """
        Record a camera stream to a local file.

        :param args: Parsed CLI arguments.
        :param cam: Connected camera instance.
        :param cam_cfg: Selected camera configuration.
        """

        stream = "mainStream" if args.quality == "high" else "subStream"
        if args.duration is not None:
            path = cam.record(out=args.out, duration=args.duration, stream=stream)
            print(msg.Log.RecordingSaved.format(output=path))
            return 0

        recorder = cam.record(out=args.out, stream=stream)
        print(msg.Log.RecordingStarted.format(output=recorder.path), flush=True)
        try:
            while recorder.running:
                time.sleep(0.25)
        finally:
            path = recorder.stop()
            print(msg.Log.RecordingSaved.format(output=path))
        return 0

    def camera_events(self, args: argparse.Namespace, cam: Camera, cam_cfg: CameraConfig) -> int:
        """
        Listen for motion events through the legacy events command.

        :param args: Parsed CLI arguments.
        :param cam: Connected camera instance.
        :param cam_cfg: Selected camera configuration.
        """

        return self.camera_motion_watch(args, cam)

    def camera_motion(self, args: argparse.Namespace, cam: Camera, cam_cfg: CameraConfig) -> int:
        """
        Read current motion status or watch motion events.

        :param args: Parsed CLI arguments.
        :param cam: Connected camera instance.
        :param cam_cfg: Selected camera configuration.
        """

        if args.watch:
            return self.camera_motion_watch(args, cam)
        print(json.dumps(cam.motion().status(timeout=args.timeout), indent=2, ensure_ascii=False))
        return 0

    def camera_motion_watch(self, args: argparse.Namespace, cam: Camera) -> int:
        """
        Print motion events until the watch condition is met.

        :param args: Parsed CLI arguments.
        :param cam: Connected camera instance.
        """

        printed = 0
        with cam.motion().watch(duration=getattr(args, "duration", None)) as events:
            for event in events:
                print(
                    msg.Log.EventReceived.format(
                        time=event.received_at.strftime("%H:%M:%S"),
                        event=event,
                    ),
                    flush=True,
                )
                printed += 1
                if args.count and printed >= args.count:
                    return 0
        return 0

    def camera_voice(self, args: argparse.Namespace, cam: Camera, cam_cfg: CameraConfig) -> int:
        """
        Send voice, tone, microphone audio, or siren command.

        :param args: Parsed CLI arguments.
        :param cam: Connected camera instance.
        :param cam_cfg: Selected camera configuration.
        """

        enabled_sources = sum(1 for enabled in (args.file, args.microphone, args.tone is not None, args.siren) if enabled)
        if enabled_sources > 1:
            self.parser.error("Use only one of --file, --microphone, --tone, or --siren")
        if enabled_sources == 0:
            self.parser.error(msg.Error.VoiceNoInput)

        with cam.voice() as voice:
            if args.siren:
                voice.siren()
                print(msg.Log.SirenSent)
                return 0
            if args.file:
                voice.play(
                    args.file,
                    volume=args.volume,
                    codec=args.voice_codec,
                    wait_ack=args.voice_wait_ack,
                    on_ready=lambda _config: print(msg.Log.VoicePlaying.format(input=args.file), flush=True),
                )
            elif args.microphone:
                voice.microphone(
                    volume=args.volume,
                    seconds=args.seconds,
                    wait_ack=args.voice_wait_ack,
                    on_ready=lambda _config: print(msg.Log.VoiceReady, flush=True),
                )
            else:
                voice.tone(
                    frequency=args.tone,
                    seconds=args.seconds or 3.0,
                    volume=args.volume,
                    wait_ack=args.voice_wait_ack,
                    on_ready=lambda _config: print(msg.Log.VoicePlaying.format(input=f"{args.tone:g} Hz tone"), flush=True),
                )
        print(msg.Log.VoiceSent)
        return 0

    def camera_pir(self, args: argparse.Namespace, cam: Camera, cam_cfg: CameraConfig) -> int:
        """
        Get or set PIR status.

        :param args: Parsed CLI arguments.
        :param cam: Connected camera instance.
        :param cam_cfg: Selected camera configuration.
        """

        pir = cam.settings().pir
        actions: dict[str, Callable[[], dict]] = {
            "status": pir.status,
            "on": pir.on,
            "off": pir.off,
        }
        print(json.dumps(actions[args.action](), indent=2, ensure_ascii=False))
        return 0

    def camera_ir(self, args: argparse.Namespace, cam: Camera, cam_cfg: CameraConfig) -> int:
        """
        Get or set IR light mode.

        :param args: Parsed CLI arguments.
        :param cam: Connected camera instance.
        :param cam_cfg: Selected camera configuration.
        """

        ir = cam.settings().ir
        actions: dict[str, Callable[[], dict]] = {
            "status": ir.status,
            "on": ir.on,
            "off": ir.off,
            "auto": ir.auto,
        }
        print(json.dumps(actions[args.action](), indent=2, ensure_ascii=False))
        return 0

    def camera_raw_stream(self, args: argparse.Namespace, cam: Camera, cam_cfg: CameraConfig) -> int:
        """
        Write raw H.264 video packets from a stream to a file.

        :param args: Parsed CLI arguments.
        :param cam: Connected camera instance.
        :param cam_cfg: Selected camera configuration.
        """

        parser = MediaParser()
        written = 0
        with open(args.output, "wb") as fh:
            for payload in cam.read_stream_payloads(args.stream):
                for packet in parser.feed(payload):
                    if packet.kind in ("iframe", "pframe") and packet.codec == "H264":
                        fh.write(packet.data)
                        written += 1
                        if args.packets and written >= args.packets:
                            print(msg.Log.VideoPacketsWritten.format(count=written, output=args.output))
                            return 0
        return 0


def main(argv: list[str] | None = None) -> int:
    return CLI(argv).run()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
