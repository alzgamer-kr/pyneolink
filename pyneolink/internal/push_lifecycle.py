"""Experimental push subscribe/status/unsubscribe CLI for the dedicated PC identity."""

from __future__ import annotations

import argparse
import hashlib
import logging
from pathlib import Path

from pyneolink.config import CameraConfig, load_config
from pyneolink.core.storage import StateDocument, document_exists, document_identity, read_document
from pyneolink.internal import push_fcm as receiver_probe
from pyneolink.internal import push_registration as registration
from pyneolink.internal import push_subscription as removal
from pyneolink.internal.push_fcm import PURPOSE, SafeDependencyLog, emit, exclusive_run
from pyneolink.internal.push_protocol import APPROVED_CAMERA, legacy_payload

ROOT = Path(__file__).resolve().parents[2]


def verify_subscription(config: CameraConfig, state: dict, state_path: Path | StateDocument) -> dict:
    """Reconcile an authoritative camera response while holding the state lock."""
    try:
        verification = registration.request_client_state(config, state["client_id"])
    except Exception as exc:
        verification = {"subscription_status": "unknown", "error_type": type(exc).__name__}
    subscribed = verification.get("subscription_status") == "subscribed"
    if subscribed:
        if "unsubscribe" in state:
            state["last_unsubscribe"] = state.pop("unsubscribe")
        state.update(
            listener_status="accepted",
            listener_created=True,
            camera_uid=config.uid,
            listener_verification=verification,
        )
        registration.save_state(state_path, state)
    elif (
        verification.get("subscription_status") == "unsubscribed"
        and verification.get("verified_by") == "camera-client-state"
        and isinstance(state.get("unsubscribe"), dict)
    ):
        state["unsubscribe"].update(status="verified-removed", verification=verification)
        registration.save_state(state_path, state)
    return {
        "subscription_status": verification.get("subscription_status", "unknown"),
        "subscription_verified": subscribed,
        "verification": verification,
        "network": True,
        "camera_connections": 1,
    }


def subscribe(
    config: CameraConfig, credentials: Path | StateDocument, state_path: Path | StateDocument, *, confirm: bool = False
) -> dict:
    """Reuse an identity; register once on explicit approval, never reset attempts."""
    if config.channel_id != 0:
        raise ValueError("Push currently supports standalone cameras on channel 0")
    if document_identity(credentials) == document_identity(state_path):
        raise ValueError("Separate FCM and Reolink state files required")
    with exclusive_run(credentials):
        receiver = read_document(credentials)
        if not isinstance(receiver, dict) or receiver.get("purpose") != PURPOSE:
            raise ValueError("Dedicated receiver required")
        token = receiver["fcm_token"]
        legacy_payload(token, receiver["client_id"])
        if token != receiver["fcm_credentials"]["fcm"]["registration"]["token"]:
            raise ValueError("FCM token mismatch")
        registration.client_id_payload(token)
        registration.listener_payload("validation-only", config.uid)
        # Protect inspection against other probes; mutation helpers take their own lock.
        with exclusive_run(state_path):
            has_identity = document_exists(state_path)
            if has_identity:
                state = read_document(state_path)
                if not isinstance(state, dict) or state.get("purpose") != registration.V2_PURPOSE:
                    raise ValueError("Dedicated Reolink identity required")
                if (
                    state.get("camera") != config.name
                    or state.get("fcm_token_sha256") != hashlib.sha256(token.encode("ascii")).hexdigest()
                ):
                    raise ValueError("Identity binding mismatch")
                registration.validate_client_id(state["client_id"])
                if "camera_uid" in state and state["camera_uid"] != config.uid:
                    raise ValueError("Camera UID mismatch")
                if confirm:
                    checked = verify_subscription(config, state, state_path)
                    if checked["subscription_verified"]:
                        return {"outcome": "already-subscribed", "create_request_sent": False, **checked}
                    if checked["subscription_status"] == "unknown":
                        return {"outcome": "verification-unavailable", "create_request_sent": False, **checked}
                resubscribe = registration.can_resubscribe(state)
                if "unsubscribe" in state and not resubscribe:
                    return {
                        "blocked": True,
                        "reason": "previous-removal-attempt",
                        "outcome": "not-subscribed" if confirm else "local-state-unverified",
                        "create_request_sent": False,
                        **(checked if confirm else {"network": False}),
                    }
                if confirm and not resubscribe and "listener_status" in state:
                    return {
                        "outcome": "not-subscribed",
                        "reason": "previous-attempt-needs-review",
                        "create_request_sent": False,
                        **checked,
                    }
                if (
                    not resubscribe
                    and state.get("listener_created") is True
                    and state.get("listener_status") == "accepted"
                ):
                    return {
                        "network": False,
                        "camera_connections": 0,
                        "local_registration": "accepted",
                        "server_status_verified": False,
                        "already_registered_locally": True,
                    }
                registration.load_listener_request(config, token, state_path)
        if not confirm:
            return {
                "ready": True,
                "network": False,
                "camera_connections": 0,
                "planned_msg_ids": [526, 524] if has_identity else [522, 524],
            }
        if not has_identity:
            result = registration.request_client_id(config, token, state_path, allow_registration=True)
            if not result["client_id_saved"]:
                return {"stage": "client-id", **result}
        try:
            result = registration.create_listener(config, token, state_path, allow_registration=True)
        except TimeoutError:
            with exclusive_run(state_path):
                state = read_document(state_path)
                checked = verify_subscription(config, state, state_path)
            return {
                "stage": "listener",
                "creation_error_type": "TimeoutError",
                "outcome": "subscribed-after-timeout" if checked["subscription_verified"] else "creation-unconfirmed",
                **checked,
                "camera_connections": 3,
            }
        return {
            "stage": "listener",
            "outcome": "subscription-accepted" if result["listener_created"] else "subscription-rejected",
            "subscription_verified": False,
            "create_request_sent": True,
            **result,
        }


def main(argv: list[str] | None = None) -> int:
    """Run legacy research subcommands; the public interface is pyneolink push."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    watch = commands.add_parser("watch", aliases=["read"], help="Receive FCM events, without camera registration")
    watch.add_argument("--credentials", type=Path, default=receiver_probe.STATE)
    watch.add_argument("--strings", type=Path, default=receiver_probe.STRINGS)
    watch.add_argument("--duration", type=int, default=600)
    watch.add_argument("--skip-previous", action=argparse.BooleanOptionalAction, default=True)
    for name in ("subscribe", "status", "unsubscribe"):
        command = commands.add_parser(name)
        command.add_argument("--config", type=Path, default=Path("config.json"))
        command.add_argument("--camera", choices=[APPROVED_CAMERA], default=APPROVED_CAMERA)
        command.add_argument("--credentials", type=Path, default=ROOT / ".tmp/push/receiver.json")
        command.add_argument("--state", type=Path, default=registration.OUTPUT)
        if name == "status":
            command.add_argument("--save-diagnostic", action="store_true")
            command.add_argument("--via-camera", action="store_true", help="One brief read-only camera session")
        else:
            command.add_argument("--confirm", action="store_true", help="Permit mutation; default is offline preflight")
        if name == "unsubscribe":
            command.add_argument("--retry-rejected", action="store_true")
            command.add_argument("--verify-via-camera", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(handlers=[SafeDependencyLog()], level=logging.WARNING, force=True)
    if args.command in ("watch", "read"):
        return receiver_probe.main(
            [
                "--allow-network",
                "--existing-only",
                "--state",
                str(args.credentials),
                "--strings",
                str(args.strings),
                "--duration",
                str(args.duration),
                "--skip-previous" if args.skip_previous else "--no-skip-previous",
            ]
        )
    if args.command != "subscribe":
        forwarded = [
            args.command,
            "--config",
            str(args.config),
            "--camera",
            args.camera,
            "--credentials",
            str(args.credentials),
            "--state",
            str(args.state),
        ]
        if getattr(args, "confirm", False):
            forwarded.append("--confirm")
        if getattr(args, "save_diagnostic", False):
            forwarded.append("--save-diagnostic")
        if getattr(args, "via_camera", False):
            forwarded.append("--via-camera")
        if getattr(args, "retry_rejected", False):
            forwarded.append("--retry-rejected")
        if getattr(args, "verify_via_camera", False):
            forwarded.append("--verify-via-camera")
        return removal.main(forwarded)
    try:
        result = subscribe(
            load_config(args.config).camera(args.camera),
            args.credentials,
            args.state,
            confirm=args.confirm,
        )
        emit("subscribe-result", **result)
        return (
            0
            if any(
                result.get(key)
                for key in (
                    "ready",
                    "already_registered_locally",
                    "listener_created",
                    "subscription_verified",
                )
            )
            else 1
        )
    except Exception as exc:
        emit("subscribe-failed", error_type=type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
