"""Close the PC subscription over HTTP, optionally verify via a brief camera session."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from pyneolink.config import load_config
from pyneolink.core.storage import StateDocument, document_identity, read_document
from pyneolink.internal.push_fcm import PURPOSE, emit, exclusive_run, save_state
from pyneolink.internal.push_protocol import APPROVED_CAMERA, legacy_payload
from pyneolink.internal.push_registration import V2_PURPOSE, listener_payload, request_client_state

ROOT = Path(__file__).resolve().parents[2]
DELETE_URL = "https://pushx.reolink.com/v2/client/listening-devices/deletion"
LIST_URL = "https://pushx.reolink.com/v2/client/listening-devices"
MAX_RESPONSE_BYTES = 65536
DIAGNOSTIC_PATH = ROOT / ".tmp/push/subscription-http-diagnostic.json"
USER_AGENT = "PyNeolinkPushProbe/1.0"


class PreviousRemovalAttempt(ValueError):
    """Prevent accidental repeat mutations after a recorded removal attempt."""


class NoRedirect(HTTPRedirectHandler):
    """Prevent authenticated requests from following unexpected redirects."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """Return no replacement request so urllib rejects the redirect."""
        return None


def post_removal(client_id: str, uid: str) -> dict:
    """One TLS-verified request, no redirects/retries; never print credentials."""
    request = Request(
        DELETE_URL,
        data=json.dumps({"uid": uid, "channelIds": [uid]}).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + client_id,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    opener = build_opener(ProxyHandler({}), NoRedirect())
    try:
        response = opener.open(request, timeout=30)
    except HTTPError as exc:
        response = exc
    with response:
        body = response.read(MAX_RESPONSE_BYTES + 1)
        result = {"http_status": response.code, "body_truncated": len(body) > MAX_RESPONSE_BYTES}
    if not result["body_truncated"]:
        try:
            document = json.loads(body)
        except (ValueError, UnicodeError):
            pass
        else:
            if isinstance(document, dict):
                result.update(describe_json_error(document))
    return result


def describe_json_error(document: dict) -> dict:
    """Report known schema markers, not server messages or challenge secrets."""
    known_fields = {
        "code",
        "error",
        "message",
        "status",
        "statusCode",
        "metadata",
        "challenge",
        "detail",
        "reason",
        "type",
        "title",
        "timestamp",
        "path",
        "url",
        "headers",
        "request",
        "data",
        "success",
    }
    result = {"known_root_fields": sorted(known_fields.intersection(document))}
    if document.get("cloudflare_error") is True and document.get("error_code") == 1010:
        result.update(blocked_by="cloudflare", cloudflare_code=1010)
    if type(document.get("retryable")) is bool:
        result["server_retryable"] = document["retryable"]
    error = document.get("error")
    result["error_object_present"] = isinstance(error, dict)
    for prefix, node in (("response", document), ("error", error)):
        if not isinstance(node, dict):
            continue
        for field in ("code", "status", "statusCode"):
            value = node.get(field)
            if type(value) is int and -1000000 <= value <= 1000000:
                result[f"{prefix}_{field}"] = value
    if not isinstance(error, dict):
        return result
    result["api_challenge_required"] = type(error.get("code")) is int and error["code"] == 8214
    metadata = error.get("metadata")
    challenge = metadata.get("challenge") if isinstance(metadata, dict) else None
    result["api_challenge_present"] = isinstance(challenge, dict)
    if isinstance(challenge, dict):
        challenge_type = challenge.get("type")
        result["api_challenge_type"] = challenge_type if challenge_type in ("captcha/1", "pow/1") else "unknown"
    return result


def get_subscription_status(client_id: str, *, diagnostic_path: Path | None = None) -> dict:
    """Inspect one read-only response without exposing its contents or identity."""
    request = Request(
        LIST_URL,
        headers={"Authorization": "Bearer " + client_id, "Accept": "application/json", "User-Agent": USER_AGENT},
        method="GET",
    )
    opener = build_opener(ProxyHandler({}), NoRedirect())
    try:
        response = opener.open(request, timeout=30)
    except HTTPError as exc:
        response = exc
    with response:
        body = response.read(MAX_RESPONSE_BYTES + 1)
        content_type = response.headers.get_content_type()
        result = {
            "http_status": response.code,
            "content_type": content_type
            if content_type in ("application/json", "text/html", "text/plain")
            else "other",
            "auth_challenge_present": bool(response.headers.get("WWW-Authenticate")),
            "api_challenge_header_present": bool(response.headers.get("X-Api-Challenge")),
            "body_bytes_read": len(body),
            "body_truncated": len(body) > MAX_RESPONSE_BYTES,
            "camera_connections": 0,
            "read_only": True,
            "removal_verified": False,
            # No successful list schema has been captured and verified yet.
            "subscription_status": "unknown",
        }
    if diagnostic_path is not None:
        save_state(
            diagnostic_path,
            {
                "purpose": "private-push-http-diagnostic",
                "http_status": result["http_status"],
                "body_truncated": result["body_truncated"],
                "response_body": body[:MAX_RESPONSE_BYTES]
                .decode("utf-8", errors="replace")
                .replace(client_id, "[REDACTED-CLIENT-ID]"),
            },
        )
        result["diagnostic_saved"] = True
    if not result["body_truncated"]:
        try:
            document = json.loads(body)
        except (ValueError, UnicodeError):
            result["body_format"] = "non-json"
        else:
            result["body_format"] = "json"
            result["json_root"] = type(document).__name__
            if isinstance(document, (dict, list)):
                result["root_items"] = len(document)
            if isinstance(document, dict):
                result.update(describe_json_error(document))
    return result


def unsubscribe(
    config,
    credentials: Path | StateDocument,
    state_path: Path | StateDocument,
    *,
    allow_unsubscribe=False,
    check_subscription=False,
    via_camera=False,
    retry_rejected=False,
    verify_via_camera=False,
    diagnostic_path: Path | None = None,
    send=post_removal,
    inspect=get_subscription_status,
) -> dict:
    """Keep both identities; only address the known PC listener, never global push."""
    if document_identity(credentials) == document_identity(state_path):
        raise ValueError("Separate FCM and Reolink state files required")
    if allow_unsubscribe and check_subscription:
        raise ValueError("Choose either inspection or removal")
    if (retry_rejected or verify_via_camera) and (not allow_unsubscribe or check_subscription):
        raise ValueError("Retry and verification require confirmed unsubscribe")
    if via_camera and (not check_subscription or diagnostic_path is not None):
        raise ValueError("Camera inspection requires status without HTTP diagnostics")
    if diagnostic_path is not None:
        if not check_subscription:
            raise ValueError("Diagnostics require read-only inspection")
        protected = {
            item.store.path.resolve() if isinstance(item, StateDocument) else item.resolve()
            for item in (credentials, state_path)
        }
        if diagnostic_path.resolve() in protected:
            raise ValueError("Diagnostic file must not overwrite credentials")
    # The same marker as the receiver prevents unsubscribe while it is running.
    with exclusive_run(credentials), exclusive_run(state_path):
        receiver = read_document(credentials)
        state = read_document(state_path)
        if not isinstance(receiver, dict) or receiver.get("purpose") != PURPOSE:
            raise ValueError("Dedicated FCM receiver required")
        token = receiver["fcm_token"]
        legacy_payload(token, receiver["client_id"])
        if token != receiver["fcm_credentials"]["fcm"]["registration"]["token"]:
            raise ValueError("FCM token mismatch")
        if not isinstance(state, dict) or state.get("purpose") != V2_PURPOSE:
            raise ValueError("Dedicated Reolink client required")
        if state.get("camera") != config.name or config.channel_id != 0:
            raise ValueError("Push camera identity mismatch or unsupported channel")
        if state.get("camera_uid") != config.uid:
            raise ValueError("Registered camera UID mismatch")
        if state.get("fcm_token_sha256") != hashlib.sha256(token.encode("ascii")).hexdigest():
            raise ValueError("Receiver binding mismatch")
        listener_payload(state["client_id"], config.uid)
        if check_subscription:
            if via_camera:
                return request_client_state(config, state["client_id"])
            if diagnostic_path is not None:
                return inspect(state["client_id"], diagnostic_path=diagnostic_path)
            return inspect(state["client_id"])
        if state.get("listener_status") != "accepted" or state.get("listener_created") is not True:
            raise ValueError("Expected an accepted PC listener")
        previous = state.get("unsubscribe")
        rejected = (
            isinstance(previous, dict)
            and previous.get("status") == "not-accepted"
            and previous.get("http_status") in (400, 401, 403, 404, 405, 422, 429)
        )
        if previous is not None and not (retry_rejected and rejected):
            raise PreviousRemovalAttempt("Unsubscribe already attempted; inspect its result before retrying")
        if not allow_unsubscribe:
            return {"network": False, "camera_connections": 0, "ready": True}
        attempt = previous.get("attempt", 1) + 1 if isinstance(previous, dict) else 1
        state["unsubscribe"] = {"status": "pending-or-unknown", "attempt": attempt}
        save_state(state_path, state)
        try:
            response = send(state["client_id"], config.uid)
        except Exception as exc:
            if not verify_via_camera:
                raise
            response = {"http_status": None, "request_error_type": type(exc).__name__}
        result = {"http_status": response} if isinstance(response, int) else dict(response)
        code = result["http_status"]
        accepted = code is not None and 200 <= code < 300
        state["unsubscribe"].update(
            status="pending-or-unknown" if code is None else ("accepted" if accepted else "not-accepted"),
            http_status=code,
        )
        save_state(state_path, state)
        result.update(removal_accepted=accepted, removal_verified=False, camera_connections=0)
        if verify_via_camera:
            try:
                verification = request_client_state(config, state["client_id"])
            except Exception as exc:
                verification = {"subscription_status": "unknown", "error_type": type(exc).__name__}
            result.update(camera_connections=1, verification=verification)
            result["removal_verified"] = verification.get("subscription_status") == "unsubscribed"
            state["unsubscribe"]["verification"] = verification
            if result["removal_verified"]:
                state["unsubscribe"]["status"] = "verified-removed"
            save_state(state_path, state)
        return result


def main(argv: list[str] | None = None) -> int:
    """Run the retained removal/status probe; mutations require explicit opt-in."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--retry-rejected", action="store_true", help="Permit one retry after a known HTTP rejection")
    parser.add_argument("--verify-via-camera", action="store_true", help="Verify removal using read-only command 526")
    parser.add_argument(
        "--via-camera",
        action="store_true",
        help="Read status via one brief camera session (526), not HTTP",
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=("status", "unsubscribe"),
        help="status: read-only GET; unsubscribe: offline preflight unless --confirm is given",
    )
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("--camera", choices=[APPROVED_CAMERA], default=APPROVED_CAMERA)
    parser.add_argument("--credentials", type=Path, default=ROOT / ".tmp/push/receiver.json")
    parser.add_argument("--state", type=Path, default=ROOT / ".tmp/push/reolink-v2.json")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--allow-unsubscribe", action="store_true")
    action.add_argument("--confirm", action="store_true", help="Permit one unsubscribe POST for the PC identity")
    action.add_argument("--check-subscription", action="store_true", help="Read-only cloud GET; no deletion")
    parser.add_argument(
        "--save-diagnostic",
        action="store_true",
        help="Save private, bounded response body locally; requires status or --check-subscription",
    )
    args = parser.parse_args(argv)
    if args.confirm and args.command != "unsubscribe":
        parser.error("--confirm requires unsubscribe")
    if args.command == "status" and args.allow_unsubscribe:
        parser.error("status cannot unsubscribe")
    if args.command == "unsubscribe" and args.check_subscription:
        parser.error("unsubscribe cannot be combined with --check-subscription")
    check_subscription = args.command == "status" or args.check_subscription
    allow_unsubscribe = args.confirm or args.allow_unsubscribe
    if (args.retry_rejected or args.verify_via_camera) and (not allow_unsubscribe or check_subscription):
        parser.error("--retry-rejected and --verify-via-camera require confirmed unsubscribe")
    if args.save_diagnostic and not check_subscription:
        parser.error("--save-diagnostic requires status or --check-subscription")
    if args.via_camera and (not check_subscription or args.save_diagnostic):
        parser.error("--via-camera requires status without --save-diagnostic")
    try:
        result = unsubscribe(
            load_config(args.config).camera(args.camera),
            args.credentials,
            args.state,
            allow_unsubscribe=allow_unsubscribe,
            check_subscription=check_subscription,
            via_camera=args.via_camera,
            retry_rejected=args.retry_rejected,
            verify_via_camera=args.verify_via_camera,
            diagnostic_path=DIAGNOSTIC_PATH if args.save_diagnostic else None,
        )
        if check_subscription:
            emit("push-status" if args.command else "subscription-check-result", **result)
            if args.via_camera:
                return 0 if result["subscription_status"] != "unknown" else 1
            return 0 if 200 <= result["http_status"] < 300 else 1
        event = "unsubscribe-result" if allow_unsubscribe else "offline-unsubscribe-ok"
        if args.command == "unsubscribe":
            event = "unsubscribe-result" if allow_unsubscribe else "unsubscribe-preflight"
        emit(event, **result)
        if args.verify_via_camera:
            return 0 if result.get("removal_verified") else 1
        return 0 if result.get("ready") or result.get("removal_accepted") else 1
    except PreviousRemovalAttempt:
        emit(
            "unsubscribe-blocked",
            reason="previous-removal-attempt",
            network=False,
            camera_connections=0,
            removal_verified=False,
        )
        return 1
    except Exception as exc:
        emit("unsubscribe-failed", error_type=type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
