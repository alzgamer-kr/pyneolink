"""Separate opt-in v2 push registration steps; no public SDK changes."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import time
from pathlib import Path
from xml.etree import ElementTree as ET

from pyneolink.camera import Camera
from pyneolink.config import CameraConfig, load_config
from pyneolink.core.storage import StateDocument, document_exists, read_document
from pyneolink.internal.push_fcm import PURPOSE, SafeDependencyLog, emit, exclusive_run, save_state
from pyneolink.internal.push_protocol import APPROVED_CAMERA, legacy_payload

ROOT = Path(__file__).resolve().parents[2]
PUSH_CLIENT_ID = 522
PUSH_CREATE_LISTENER = 524
PUSH_CLIENT_STATE = 526
V2_PURPOSE = "dedicated-test-reolink-v2-client"
OUTPUT = ROOT / ".tmp/push/reolink-v2.json"


def client_id_payload(token: str) -> bytes:
    """Encode the SDK's PushClientID request; its token buffer is 256 bytes."""
    legacy_payload(token, "0" * 32)
    if len(token) > 255:
        raise ValueError("Push token exceeds the observed SDK field size")
    body = ET.Element("body")
    request = ET.SubElement(body, "PushClientID", version="1.1")
    ET.SubElement(request, "clientType").text = "reo_fcm"
    ET.SubElement(request, "pushToken").text = token
    return ET.tostring(body, encoding="utf-8", xml_declaration=True)


def parse_client_id(payload: bytes) -> str:
    """Accept one bounded clientID; never include payloads in error messages."""
    if not payload or len(payload) > 65536:
        raise ValueError("Unexpected client-ID response size")
    root = ET.fromstring(payload)
    if root.tag != "body":
        raise ValueError("Unexpected client-ID response root")
    nodes = root.findall("PushClientID/clientID")
    if len(nodes) != 1 or len(list(nodes[0])):
        raise ValueError("Expected one PushClientID/clientID field")
    value = nodes[0].text or ""
    validate_client_id(value)
    return value


def validate_client_id(value: str) -> None:
    """Reject empty, oversized, or non-printable client identifiers."""
    if not isinstance(value, str) or not 1 <= len(value) <= 64 or any(not 33 <= ord(char) <= 126 for char in value):
        raise ValueError("Invalid client-ID field")


def listener_payload(client_id: str, camera_uid: str) -> bytes:
    """Use camera UID as channelId, with an empty tag, as Device.java does."""
    validate_client_id(client_id)
    if not isinstance(camera_uid, str) or not re.fullmatch(r"[A-Za-z0-9]{1,31}", camera_uid):
        raise ValueError("A standalone camera UID is required")
    body = ET.Element("body")
    request = ET.SubElement(body, "PushCreateListener", version="1.1")
    ET.SubElement(request, "clientID").text = client_id
    ET.SubElement(request, "tag").text = ""
    ET.SubElement(request, "channelId").text = camera_uid
    return ET.tostring(body, encoding="utf-8", xml_declaration=True)


def client_state_payload(client_id: str) -> bytes:
    """Request the client's full channel list, matching Android's empty filter."""
    validate_client_id(client_id)
    body = ET.Element("body")
    request = ET.SubElement(body, "PushClientState", version="1.1")
    ET.SubElement(request, "clientID").text = client_id
    ET.SubElement(request, "channelIdList")
    return ET.tostring(body, encoding="utf-8", xml_declaration=True)


def parse_client_state(payload: bytes, client_id: str, camera_uid: str) -> dict:
    """Only a complete, identity-matched list may establish camera-reported state."""
    if not payload or len(payload) > 65536 or b"<!DOCTYPE" in payload.upper():
        raise ValueError("Unexpected client-state response")
    root = ET.fromstring(payload)
    if root.tag != "body" or len(root.findall("PushClientState")) != 1:
        raise ValueError("Expected one client-state object")
    state = root.find("PushClientState")
    identities = state.findall("clientID")
    lists = state.findall("channelIdList")
    if len(identities) != 1 or len(identities[0]) or identities[0].text != client_id or len(lists) != 1:
        raise ValueError("Incomplete or mismatched client-state response")
    channels = list(lists[0])
    if len(channels) > 64 or (lists[0].text or "").strip():
        raise ValueError("Unexpected channel list")
    for channel in channels:
        if channel.tag != "channelId" or len(channel) or not re.fullmatch(r"[A-Za-z0-9]{1,31}", channel.text or ""):
            raise ValueError("Invalid channel entry")
    return {
        "subscription_status": "subscribed" if any(item.text == camera_uid for item in channels) else "unsubscribed",
        "reported_channel_count": len(channels),
        "verified_by": "camera-client-state",
    }


def request_client_state(config: CameraConfig, client_id: str, *, camera_factory=Camera) -> dict:
    """One read-only camera request, with no implicit retries or registration."""
    if config.channel_id != 0:
        raise ValueError("Push currently supports standalone cameras on channel 0")
    payload = client_state_payload(client_id)
    listener_payload(client_id, config.uid)
    camera = camera_factory(config, debug=False, state_path=None)
    try:
        with camera:
            reply = camera.command(PUSH_CLIENT_STATE, payload, retry_on_timeout=False, reconnect_retries=0)
    finally:
        camera.close()
    result = {
        "msg_id": reply.header.msg_id,
        "response_code": reply.header.response_code,
        "camera_connections": 1,
        "read_only": True,
        "subscription_status": "unknown",
    }
    if reply.header.msg_id != PUSH_CLIENT_STATE or reply.header.response_code != 200:
        return result
    try:
        result.update(parse_client_state(reply.payload, client_id, config.uid))
    except (ValueError, ET.ParseError):
        result["parse_error"] = "unexpected-client-state-payload"
    return result


def request_client_id(
    config: CameraConfig,
    token: str,
    output: Path | StateDocument,
    *,
    allow_registration: bool = False,
    camera_factory=Camera,
) -> dict[str, object]:
    """Request an identity once, saving secrets separately from FCM credentials.

    Despite the Get name, this may create a server-side client identity.
    It never sends CreatePushListener (524), enables push, or deletes listeners.
    """
    if not allow_registration or config.channel_id != 0:
        raise ValueError("Explicit registration approval and standalone camera required")
    payload = client_id_payload(token)
    with exclusive_run(output):
        if document_exists(output):
            raise FileExistsError("Existing v2 identity will not be replaced")
        camera = camera_factory(config, debug=False, state_path=None)
        try:
            with camera:
                reply = camera.command(PUSH_CLIENT_ID, payload, retry_on_timeout=False, reconnect_retries=0)
        finally:
            camera.close()
        result = {
            "msg_id": reply.header.msg_id,
            "response_code": reply.header.response_code,
            "client_id_saved": False,
            "listener_created": False,
        }
        if reply.header.msg_id != PUSH_CLIENT_ID or reply.header.response_code != 200:
            return result
        try:
            client_id = parse_client_id(reply.payload)
        except (ValueError, ET.ParseError):
            result["parse_error"] = "unexpected-client-id-payload"
            return result
        save_state(
            output,
            {
                "purpose": V2_PURPOSE,
                "camera": config.name,
                "client_id": client_id,
                "fcm_token_sha256": hashlib.sha256(token.encode("ascii")).hexdigest(),
                "listener_created": False,
            },
        )
        result["client_id_saved"] = True
        return result


def can_resubscribe(state: dict) -> bool:
    """Allow only a camera-verified removal, never an ambiguous HTTP result."""
    removal = state.get("unsubscribe")
    if not isinstance(removal, dict) or removal.get("status") != "verified-removed":
        return False
    verification = removal.get("verification", {})
    return (
        verification.get("subscription_status") == "unsubscribed"
        and verification.get("verified_by") == "camera-client-state"
    )


def load_listener_request(config: CameraConfig, token: str, output: Path | StateDocument) -> tuple[dict, bytes]:
    """Validate local identity binding before opening any camera connection."""
    if config.channel_id != 0:
        raise ValueError("Push currently supports standalone cameras on channel 0")
    client_id_payload(token)
    state = read_document(output)
    if not isinstance(state, dict) or state.get("purpose") != V2_PURPOSE or state.get("camera") != config.name:
        raise ValueError("V2 camera identity mismatch")
    if state.get("fcm_token_sha256") != hashlib.sha256(token.encode("ascii")).hexdigest():
        raise ValueError("V2 identity belongs to another FCM token")
    if can_resubscribe(state):
        if state.get("camera_uid") != config.uid:
            raise ValueError("Removed listener belongs to a different camera")
    elif "unsubscribe" in state or state.get("listener_created") is not False or "listener_status" in state:
        raise ValueError("Listener already attempted; inspect the saved result before any retry")
    return state, listener_payload(state["client_id"], config.uid)


def create_listener(
    config: CameraConfig,
    token: str,
    output: Path | StateDocument,
    *,
    allow_registration: bool = False,
    camera_factory=Camera,
) -> dict[str, object]:
    """Send 524 once. Persist uncertainty before sending; never retry implicitly."""
    if not allow_registration:
        raise ValueError("Explicit registration approval required")
    with exclusive_run(output):
        state, payload = load_listener_request(config, token, output)
        camera = camera_factory(config, debug=False, state_path=None)
        try:
            with camera:
                if can_resubscribe(state):
                    state["last_unsubscribe"] = state.pop("unsubscribe")
                    state.pop("listener_response", None)
                state["listener_created"] = False
                state["subscription_started_at"] = time.time()
                state.update(listener_status="pending-or-unknown", camera_uid=config.uid)
                save_state(output, state)
                reply = camera.command(PUSH_CREATE_LISTENER, payload, retry_on_timeout=False, reconnect_retries=0)
                accepted = reply.header.msg_id == PUSH_CREATE_LISTENER and reply.header.response_code == 200
                state.update(
                    listener_status="accepted" if accepted else "not-accepted",
                    listener_created=accepted,
                    listener_response={"msg_id": reply.header.msg_id, "response_code": reply.header.response_code},
                )
                save_state(output, state)
        finally:
            camera.close()
        return {**state["listener_response"], "listener_created": accepted, "delivery_proven": False}


def main(argv: list[str] | None = None) -> int:
    """Run the retained registration probe with explicit mutation opt-in."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("--camera", choices=[APPROVED_CAMERA], default=APPROVED_CAMERA)
    parser.add_argument("--credentials", type=Path, default=ROOT / ".tmp/push/receiver.json")
    parser.add_argument("--out", type=Path, default=OUTPUT)
    parser.add_argument("--allow-registration", action="store_true")
    parser.add_argument("--action", choices=["client-id", "create-listener"], default="client-id")
    args = parser.parse_args(argv)
    logging.basicConfig(handlers=[SafeDependencyLog()], level=logging.WARNING, force=True)
    try:
        if args.out.resolve() == args.credentials.resolve():
            raise ValueError("FCM credentials cannot be used as the output path")
        state = json.loads(args.credentials.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or state.get("purpose") != PURPOSE:
            raise ValueError("Dedicated receiver credentials required")
        token = state["fcm_token"]
        legacy_payload(token, state["client_id"])
        if token != state["fcm_credentials"]["fcm"]["registration"]["token"]:
            raise ValueError("FCM token mismatch")
        client_id_payload(token)
        if args.action == "create-listener":
            config = load_config(args.config).camera(args.camera)
            if not args.allow_registration:
                load_listener_request(config, token, args.out)
                emit("offline-v2-listener-ok", network=False, camera_connections=0)
                return 0
            result = create_listener(config, token, args.out, allow_registration=True)
            emit("v2-listener-result", **result)
            return 0 if result["listener_created"] else 1
        if not args.allow_registration:
            emit("offline-v2-request-ok", network=False, camera_connections=0)
            return 0
        result = request_client_id(
            load_config(args.config).camera(args.camera), token, args.out, allow_registration=True
        )
        emit("v2-client-id-result", **result)
        return 0 if result["client_id_saved"] else 1
    except Exception as exc:
        emit("failed", error_type=type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
