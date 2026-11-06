"""Push envelope normalization and retained legacy protocol research utilities."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from xml.etree import ElementTree as ET

from pyneolink.camera import Camera
from pyneolink.config import CameraConfig, load_config

PUSH_INFO_ID = 124  # Neolink's legacy PushInfo, not the unverified v2 commands.
APPROVED_CAMERA = "Home-Back"
MAX_EVENT_LINE = 64 * 1024


def normalize_event(data: Mapping[str, object]) -> dict[str, object]:
    """Extract known fields from an FCM data map without guessing timezones.

    Preserve timestamps as received. Unknown versions are rejected explicitly;
    this probe does not silently label every notification as a motion event.
    No raw payload, image URL, notification body or credentials are returned.
    """
    version = data.get("pushVersion")
    if version not in ("v2", "v2a", "system"):
        raise ValueError("Unsupported or missing pushVersion")
    fields = {
        "camera_uid": "uid",
        "channel_id": "channelId",
        "event_type": "type",
        "device_time_raw": "deviceTime",
        "server_time_raw": "serverPushTime",
        "event_id": "id",
    }
    if version == "v2":
        fields.update(
            camera_uid="UID",
            event_type="ALMTYPE",
            device_time_raw="ALMTIME",
            server_time_raw="SRVTIME",
        )
    event: dict[str, object] = {"push_version": version}
    for target, source in fields.items():
        value = data.get(source)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"Expected a string for {source}")
        event[target] = value
    duplicate = data.get("dup")
    if duplicate not in (None, "0", "1"):
        raise ValueError("Expected dup to be 0 or 1")
    event["duplicate_hint"] = duplicate == "1"
    return event


def legacy_payload(token: str, client_id: str) -> bytes:
    """Encode legacy registration, escaping values and never printing secrets."""
    if not isinstance(token, str) or not token or len(token) > 4096:
        raise ValueError("Invalid receiver token")
    if any(ord(char) < 33 or ord(char) > 126 for char in token):
        raise ValueError("Receiver token must be printable ASCII without spaces")
    if not isinstance(client_id, str) or not re.fullmatch(r"[A-F0-9]{32}", client_id):
        raise ValueError("Expected a 32-character uppercase hex client ID")
    body = ET.Element("body")
    push = ET.SubElement(body, "PushInfo")
    for tag, value in (("token", token), ("phoneType", "reo_fcm"), ("clientID", client_id)):
        ET.SubElement(push, tag).text = value
    return ET.tostring(body, encoding="utf-8", xml_declaration=True)


def register_legacy(
    config: CameraConfig,
    token: str,
    client_id: str,
    *,
    allow_registration: bool = False,
    camera_factory=Camera,
) -> dict[str, int]:
    """Send one legacy request to the approved camera, then close the session.

    This changes the push subscription. A response 200 is NOT proof that an
    event was delivered. No reconnect loop or camera state cache is used.
    """
    if not allow_registration:
        raise ValueError("Registration requires explicit --allow-registration")
    if config.name != APPROVED_CAMERA:
        raise ValueError("This research probe is restricted to Home-Back")
    payload = legacy_payload(token, client_id)
    camera = camera_factory(config, debug=False, state_path=None)
    try:
        with camera:
            reply = camera.command(PUSH_INFO_ID, payload, retry_on_timeout=False, reconnect_retries=0)
            return {"msg_id": reply.header.msg_id, "response_code": reply.header.response_code}
    finally:
        # Also close when __enter__ fails in the baseline SDK.
        camera.close()


def replay(path: Path) -> int:
    """Read sanitized FCM data maps as bounded JSONL; no network access."""
    count = 0
    with path.open(encoding="utf-8") as source:
        while line := source.readline(MAX_EVENT_LINE + 1):
            if len(line) > MAX_EVENT_LINE:
                raise ValueError("Event line exceeds 64 KiB characters")
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                raise ValueError("Invalid JSON event") from None
            if not isinstance(data, dict):
                raise ValueError("Expected an FCM data object per line")
            print(json.dumps(normalize_event(data), ensure_ascii=True))
            count += 1
    print(f"Offline events parsed: {count}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Replay offline fixtures or explicitly register the approved research camera."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    offline = commands.add_parser("replay", help="Offline JSONL parsing only")
    offline.add_argument("--input", type=Path, required=True)
    register = commands.add_parser("register-legacy", help="Mutates one camera's push subscription")
    register.add_argument("--config", type=Path, default=Path("config.json"))
    register.add_argument("--camera", choices=[APPROVED_CAMERA], default=APPROVED_CAMERA)
    register.add_argument("--credentials", type=Path, required=True)
    register.add_argument("--allow-registration", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.action == "replay":
            return replay(args.input)
        if not args.allow_registration:
            parser.error("register-legacy requires --allow-registration; no connection made")
        credentials = json.loads(args.credentials.read_text(encoding="utf-8"))
        if not isinstance(credentials, dict) or credentials.get("purpose") != "dedicated-test-receiver":
            raise ValueError("Use credentials for a dedicated test receiver, never the phone's registration")
        result = register_legacy(
            load_config(args.config).camera(args.camera),
            credentials["fcm_token"],
            credentials["client_id"],
            allow_registration=True,
        )
        print(json.dumps(result))
        print("Camera session closed. This is not a push-delivery test.")
        return 0 if result == {"msg_id": PUSH_INFO_ID, "response_code": 200} else 1
    except Exception as exc:
        # Exceptions may contain network request bodies or credential values.
        print(f"Probe failed ({type(exc).__name__}); details suppressed to protect credentials.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
