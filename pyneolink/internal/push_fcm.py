"""Opt-in, bounded FCM receiver experiment. Never connects to a camera."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import tempfile
from base64 import b64decode, urlsafe_b64encode
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from email.message import Message as HeaderMessage
from pathlib import Path
from uuid import uuid4
from xml.etree import ElementTree as ET

from pyneolink.core.storage import StateDocument, document_exists, read_document
from pyneolink.internal.push_profile import ANDROID_CERT_SHA1
from pyneolink.internal.push_protocol import legacy_payload, normalize_event

ROOT = Path(__file__).resolve().parents[2]
STRINGS = ROOT / ".tmp/research/reolink-android/4.64.3.20261023/resources/res/values/strings.xml"
STATE = ROOT / ".tmp/push/receiver.json"
PURPOSE = "dedicated-test-receiver"
MAX_IDS = 2048
GCM_REGISTER_ATTEMPTS = 5
EVENT_SINK: ContextVar[Callable[[dict], None] | None] = ContextVar("push_event_sink", default=None)
FIREBASE_HOSTS = frozenset({"firebaseinstallations.googleapis.com", "fcmregistrations.googleapis.com"})
SAFE_GCM_ERRORS = frozenset(
    {
        "AUTHENTICATION_FAILED",
        "INVALID_SENDER",
        "INVALID_PARAMETERS",
        "INVALID_TARGET",
        "INVALID_TARGET_VERSION",
        "MISSING_SENDER",
        "PHONE_REGISTRATION_ERROR",
        "SERVICE_NOT_AVAILABLE",
        "INTERNAL_SERVER_ERROR",
        "TOO_MANY_REGISTRATIONS",
        "QUOTA_EXCEEDED",
        "DEPRECATED_ENDPOINT",
    }
)
SAFE_FIREBASE_REASONS = frozenset(
    {
        "API_KEY_ANDROID_APP_BLOCKED",
        "API_KEY_IOS_APP_BLOCKED",
        "API_KEY_HTTP_REFERRER_BLOCKED",
        "API_KEY_IP_ADDRESS_BLOCKED",
        "API_KEY_SERVICE_BLOCKED",
        "API_KEY_INVALID",
        "API_KEY_EXPIRED",
        "API_KEY_NOT_FOUND",
        "SERVICE_DISABLED",
        "CONSUMER_INVALID",
        "CONSUMER_SUSPENDED",
        "BILLING_DISABLED",
        "RATE_LIMIT_EXCEEDED",
        "ACCESS_TOKEN_EXPIRED",
        "ACCESS_TOKEN_SCOPE_INSUFFICIENT",
    }
)
SAFE_RPC_STATUSES = frozenset(
    {
        "PERMISSION_DENIED",
        "UNAUTHENTICATED",
        "INVALID_ARGUMENT",
        "NOT_FOUND",
        "RESOURCE_EXHAUSTED",
        "FAILED_PRECONDITION",
        "INTERNAL",
        "UNAVAILABLE",
    }
)


def firebase_error_details(body: str) -> dict:
    """Allowlist structured error categories, excluding messages and metadata."""
    try:
        document = json.loads(body)
    except (ValueError, TypeError):
        return {}
    error = document.get("error") if isinstance(document, dict) else None
    if not isinstance(error, dict):
        return {}
    result = {}
    code = error.get("code")
    if type(code) is int and 400 <= code <= 599:
        result["google_code"] = code
    status = error.get("status")
    if isinstance(status, str):
        result["google_status"] = status if status in SAFE_RPC_STATUSES else "UNRECOGNIZED_STATUS"
    details = error.get("details", [])
    if isinstance(details, list):
        reasons = set()
        for item in details:
            if not isinstance(item, dict) or item.get("@type") != "type.googleapis.com/google.rpc.ErrorInfo":
                continue
            reason = item.get("reason")
            if isinstance(reason, str):
                reasons.add(reason if reason in SAFE_FIREBASE_REASONS else "UNRECOGNIZED_REASON")
        if reasons:
            result["google_reasons"] = sorted(reasons)
    return result


def emit(status: str, **fields: object) -> None:
    """Emit only explicitly selected, non-credential diagnostic fields."""
    event = {"time": datetime.now(timezone.utc).isoformat(), "status": status, **fields}
    sink = EVENT_SINK.get()
    if sink is not None:
        sink(event)
        return
    print(json.dumps(event), flush=True)


class SafeDependencyLog(logging.Handler):
    """Do not print dependency messages: they may contain tokens or bodies."""

    def emit(self, record: logging.LogRecord) -> None:
        """Report only allowlisted exception metadata, never raw log arguments."""
        details = {}
        # _listen passes the exception as a formatting argument, not exc_info.
        exceptions = (
            [value for value in record.args if isinstance(value, BaseException)]
            if isinstance(record.args, tuple)
            else []
        )
        if record.exc_info and isinstance(record.exc_info, tuple) and isinstance(record.exc_info[1], BaseException):
            exceptions.append(record.exc_info[1])
        for exc in exceptions:
            details["exception_type"] = type(exc).__name__
            traceback = exc.__traceback__
            while traceback is not None:
                details["error_site"] = traceback.tb_frame.f_code.co_name
                details["error_line"] = traceback.tb_lineno
                traceback = traceback.tb_next
            safe_errors = {
                "couldn't find in app_data crypto-key": "missing-crypto-key",
                "couldn't find in app_data encryption": "missing-encryption",
                "couldn't find in app_data subtype": "missing-subtype",
                "Incorrect padding": "invalid-base64-padding",
                "Invalid EC key.": "invalid-ec-key",
            }
            if len(exc.args) == 1 and isinstance(exc.args[0], str) and exc.args[0] in safe_errors:
                details["error_category"] = safe_errors[exc.args[0]]
        if record.funcName in ("fcm_install", "fcm_register", "fcm_refresh_install_token"):
            for value in record.args if isinstance(record.args, tuple) else ():
                if isinstance(value, str):
                    details.update(firebase_error_details(value))
        if record.funcName == "gcm_register" and isinstance(record.args, tuple):
            for value in record.args:
                if isinstance(value, str) and value.strip().startswith("Error="):
                    code = value.strip().removeprefix("Error=")
                    details["google_error"] = code if code in SAFE_GCM_ERRORS else "UNRECOGNIZED_ERROR"
        if record.exc_info and isinstance(record.exc_info, tuple) and record.exc_info[0]:
            details["exception_type"] = record.exc_info[0].__name__
        emit("dependency-warning", stage=record.funcName, level=record.levelname, **details)


def normalize_crypto_header(value: str, parameter: str) -> str:
    """Select a Web Push parameter and restore optional base64url padding."""
    if parameter not in ("dh", "salt") or not isinstance(value, str) or not 0 < len(value) <= 4096:
        raise ValueError("Invalid Web Push crypto header")
    if "\r" in value or "\n" in value:
        raise ValueError("Invalid Web Push crypto header")
    if re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", value):
        encoded = value
    else:
        header = HeaderMessage()
        header["content-type"] = "application/octet-stream; " + value
        matches = [item for name, item in header.get_params()[1:] if name == parameter]
        if len(matches) != 1 or not isinstance(matches[0], str):
            raise ValueError("Missing or ambiguous Web Push crypto parameter")
        encoded = matches[0]
    if not re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", encoded):
        raise ValueError("Invalid Web Push base64url parameter")
    decoded = b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
    expected_size = 65 if parameter == "dh" else 16
    if len(decoded) != expected_size or (parameter == "dh" and decoded[0] != 4):
        raise ValueError("Invalid Web Push crypto parameter size")
    return parameter + "=" + urlsafe_b64encode(decoded).decode("ascii")


class DataMessageDiagnostics:
    """Normalize crypto headers; retain upstream decryption, callbacks and ACKs."""

    def _app_data_by_key(self, message, key, do_not_raise=False):
        """Normalize one unambiguous crypto parameter for upstream decryption."""
        value = super()._app_data_by_key(message, key, do_not_raise=do_not_raise)
        parameter = {"crypto-key": "dh", "encryption": "salt"}.get(key)
        if parameter is None or (do_not_raise and not value):
            return value
        if sum(item.key == key for item in message.app_data) != 1:
            raise ValueError("Ambiguous Web Push crypto header")
        return normalize_crypto_header(value, parameter)

    def _handle_data_message(self, message):
        """Log envelope shape without its contents and delegate normal handling."""
        present = {item.key for item in message.app_data}
        emit(
            "data-message-shape",
            app_data_count=len(message.app_data),
            raw_data_bytes=len(message.raw_data),
            has_crypto_key="crypto-key" in present,
            has_encryption="encryption" in present,
            has_subtype="subtype" in present,
            has_push_version="pushVersion" in present,
            has_message_type="message_type" in present,
        )
        return super()._handle_data_message(message)


class BoundedClose:
    """Bound the dependency's TLS close wait during reset and shutdown."""

    writer_close_timeout = 3.0

    async def _do_writer_close(self):
        """Close TLS within the configured bound, aborting stuck transports."""
        writer, self.writer = self.writer, None
        if writer is None:
            return
        try:
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), self.writer_close_timeout)
        except TimeoutError:
            writer.transport.abort()
            emit("transport-close-forced", reason="tls-close-timeout")
        except asyncio.CancelledError:
            writer.transport.abort()
            raise
        except OSError as exc:
            writer.transport.abort()
            emit("transport-close-forced", reason="close-error", error_type=type(exc).__name__)


def http_trace(android_package: str | None = None, android_cert_sha1: str | None = None):
    """Attach Android identity only to Firebase HTTPS requests; emit safe diagnostics."""
    from aiohttp import TraceConfig

    android_headers = {}
    if android_package is not None or android_cert_sha1 is not None:
        if not android_package or not re.fullmatch(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+", android_package):
            raise ValueError("Invalid Android package")
        if not android_cert_sha1 or not re.fullmatch(r"[A-Fa-f0-9]{40}", android_cert_sha1):
            raise ValueError("Invalid Android signing certificate SHA-1")
        android_headers = {"X-Android-Package": android_package, "X-Android-Cert": android_cert_sha1.upper()}
    trace = TraceConfig()

    async def on_start(session, context, params):
        """Add app identity headers exclusively to approved Firebase HTTPS hosts."""
        url = params.url
        if url.host in FIREBASE_HOSTS and android_headers:
            if url.scheme != "https" or url.port != 443:
                raise ValueError("Firebase requires HTTPS on port 443")
            params.headers.update(android_headers)

    async def on_redirect(session, context, params):
        """Reject unexpected redirects rather than forwarding identity headers."""
        if params.url.host in FIREBASE_HOSTS:
            # These APIs need no redirect; do not forward identity/API headers elsewhere.
            raise RuntimeError("Unexpected Firebase redirect")

    async def on_end(session, context, params):
        """Emit HTTP status and an allowlisted stage without response contents."""
        url = params.url
        stages = {
            ("android.clients.google.com", "/checkin"): "gcm_checkin",
            ("android.clients.google.com", "/c2dm/register3"): "gcm_register",
        }
        stage = stages.get((url.host, url.path))
        if stage is None:
            stage = {
                "firebaseinstallations.googleapis.com": "firebase_installation",
                "fcmregistrations.googleapis.com": "fcm_registration",
            }.get(url.host, "other")
        emit("http-response", stage=stage, http_status=params.response.status)

    trace.on_request_end.append(on_end)
    trace.on_request_start.append(on_start)
    trace.on_request_redirect.append(on_redirect)
    return trace


def firebase_config(path: Path) -> dict[str, str]:
    """Read application identifiers from local JADX resources, without printing keys."""
    values = {node.get("name"): node.text for node in ET.parse(path).getroot().findall("string")}
    fields = {
        "project_id": "project_id",
        "app_id": "google_app_id",
        "api_key": "google_api_key",
        "messaging_sender_id": "gcm_defaultSenderId",
    }
    result = {}
    for field, name in fields.items():
        value = values.get(name)
        if not value or value.startswith("@"):
            raise ValueError(f"Missing decoded resource {name}")
        result[field] = value
    result["bundle_id"] = "com.mcu.reolink"
    return result


def identity(config: dict[str, str]) -> dict[str, str]:
    """Select stable public profile identifiers for receiver-state validation."""
    return {key: config[key] for key in ("project_id", "app_id", "messaging_sender_id", "bundle_id")}


def load_state(path: Path | StateDocument, config: dict[str, str]) -> dict:
    """Refuse invalid/existing unrelated credentials instead of replacing them."""
    if not document_exists(path):
        return {"purpose": PURPOSE, "client_id": uuid4().hex.upper(), "identity": identity(config), "ids": []}
    state = read_document(path)
    if not isinstance(state, dict) or state.get("purpose") != PURPOSE or state.get("identity") != identity(config):
        raise ValueError("State identity mismatch")
    token = state["fcm_credentials"]["fcm"]["registration"]["token"]
    legacy_payload(token, state["client_id"])
    if token != state.get("fcm_token"):
        raise ValueError("Stored token mismatch")
    if not isinstance(state.get("ids"), list) or any(not isinstance(item, str) for item in state["ids"]):
        raise ValueError("Invalid persistent IDs")
    state["ids"] = state["ids"][-MAX_IDS:]
    return state


def save_state(path: Path | StateDocument, state: dict) -> None:
    """Atomically replace credentials. POSIX mode is owner-only; Windows inherits ACLs."""
    if isinstance(path, StateDocument):
        path.store.put(path.key, state)
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".receiver-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def exclusive_run(path: Path | StateDocument):
    """Reject simultaneous listeners; stale crash markers require manual inspection."""
    if isinstance(path, StateDocument):
        with path.store.lease(path.key):
            yield
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker = path.with_suffix(".running")
    fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(str(os.getpid()))
        yield
    finally:
        marker.unlink(missing_ok=True)


def event_server_time(event: dict) -> datetime | None:
    """Decode cloud time only; camera clocks are not reliable queue timestamps."""
    raw = event.get("server_time_raw")
    if not isinstance(raw, (str, int, float)) or isinstance(raw, bool):
        return None
    value = str(raw).strip()
    if not value or len(value) > 128:
        return None
    try:
        if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value):
            timestamp = float(value)
            if timestamp >= 100_000_000_000:
                timestamp /= 1000
            return datetime.fromtimestamp(timestamp, timezone.utc)
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        # The older cloud format omits the UTC suffix (not the camera's local time).
        return (
            timestamp.replace(tzinfo=timezone.utc) if timestamp.tzinfo is None else timestamp.astimezone(timezone.utc)
        )
    except (ValueError, OverflowError, OSError):
        return None


def deliver_event(event: dict, *, skip_previous: bool, started_at: datetime) -> bool:
    """Filter backlog for this watch run without losing events of unknown age."""
    sent_at = event_server_time(event)
    if skip_previous and sent_at is not None and sent_at < started_at:
        emit("previous-event-skipped")
        return False
    fields = {"age_filter": "unknown-server-time"} if skip_previous and sent_at is None else {}
    emit("event", event=event, **fields)
    return True


class GcmRegistrationRetry:
    """Allow a bounded registration window for a freshly checked-in device.

    firebase-messaging 0.4.5 defaults to two attempts one second apart. Pass
    a larger budget to the same call, retaining its Android ID and subtype.
    The outer 60-second timeout still bounds the complete registration.
    """

    async def gcm_register(self, options: dict, retries: int = GCM_REGISTER_ATTEMPTS) -> dict | None:
        """Retry the same registration identity within the configured budget."""
        emit("registration-policy", stage="gcm_register", max_attempts=retries)
        return await super().gcm_register(options, retries=retries)


async def prepare_receiver(
    config: dict[str, str], state_path: Path | StateDocument, state: dict, session: object
) -> None:
    """Register or check in one persisted receiver using the provided HTTP session."""
    from firebase_messaging import FcmRegisterConfig
    from firebase_messaging.fcmregister import FcmRegister

    class Registration(GcmRegistrationRetry, FcmRegister):
        """Upstream registration with a bounded initial retry policy."""

    registration = Registration(FcmRegisterConfig(**config), http_client_session=session)
    emit("checking-in" if "fcm_credentials" in state else "registering")
    if "fcm_credentials" in state:
        gcm = state["fcm_credentials"]["gcm"]
        checked = await asyncio.wait_for(registration.gcm_check_in(gcm["android_id"], gcm["security_token"]), 60)
        if not checked or str(checked.get("androidId")) != str(gcm["android_id"]):
            raise RuntimeError("Checkin failed; refusing automatic identity replacement")
    else:
        state["fcm_credentials"] = await asyncio.wait_for(registration.register(), 60)
    token = state["fcm_credentials"]["fcm"]["registration"]["token"]
    legacy_payload(token, state["client_id"])
    state["fcm_token"] = token
    save_state(state_path, state)
    emit("registered", credentials_saved=True)


async def initialize_receiver(config: dict[str, str], state_path: Path | StateDocument) -> None:
    """Create FCM credentials only, without starting a listener or contacting a camera."""
    state = load_state(state_path, config)
    if "fcm_credentials" in state:
        return
    from aiohttp import ClientSession

    async with ClientSession(trace_configs=[http_trace(config["bundle_id"], ANDROID_CERT_SHA1)]) as session:
        await prepare_receiver(config, state_path, state, session)


@contextmanager
def capture_notifications(
    path: Path | None,
) -> Iterator[Callable[[object, str | None], None] | None]:
    """Append decrypted notification envelopes, never receiver credentials."""
    if path is None:
        yield None
        return
    if path.suffix.lower() != ".jsonl":
        raise ValueError("Raw push output must use a .jsonl file")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8", newline="\n") as output:

        def capture(notification: object, persistent_id: str | None) -> None:
            """Append and flush one unredacted decrypted envelope."""
            record = {
                "received_at": datetime.now(timezone.utc).isoformat(),
                "persistent_id": persistent_id,
                "notification": notification,
            }
            output.write(json.dumps(record, ensure_ascii=True, allow_nan=False) + "\n")
            output.flush()

        yield capture


async def receive(
    config: dict[str, str],
    state_path: Path | StateDocument,
    duration: float | None,
    android_cert_sha1: str = ANDROID_CERT_SHA1,
    *,
    skip_previous: bool = True,
    existing_only: bool = False,
    started_at: datetime | None = None,
    on_notification: Callable[[dict, str | None], None] | None = None,
    on_raw_notification: Callable[[object, str | None], None] | None = None,
) -> int:
    """Receive FCM notifications without a camera connection.

    Invoke raw, normalized-storage, and delivery callbacks synchronously in
    that order. Raw capture precedes deduplication. Keep persistent IDs bounded
    and store them only after callbacks succeed. Callback failures stop the
    receiver; dependency transport failures propagate for the SDK retry policy.
    Duration bounds this attempt, while started_at preserves the watch cutoff.
    """
    started_at = started_at or datetime.now(timezone.utc)
    state = load_state(state_path, config)
    if existing_only and "fcm_credentials" not in state:
        raise ValueError("Watch requires existing receiver credentials; registration is a separate step")
    # Optional imports stay out of the SDK and offline checks.
    from aiohttp import ClientSession
    from firebase_messaging import FcmPushClient, FcmPushClientConfig, FcmRegisterConfig

    class ProbePushClient(BoundedClose, DataMessageDiagnostics, FcmPushClient):
        """Upstream receiver with header compatibility and bounded TLS shutdown."""

    errors = []
    seen = set(state["ids"])
    emit("watch-started", started_at=started_at.isoformat(), skip_previous=skip_previous, camera_connections=0)

    def on_message(notification, persistent_id, context):
        """Capture, persist, and deliver one message before saving its ACK identity."""
        try:
            if on_raw_notification is not None:
                on_raw_notification(notification, persistent_id)
            if persistent_id and persistent_id in seen:
                emit("duplicate")
                return
            data = notification.get("data") if isinstance(notification, dict) else None
            if not isinstance(data, dict):
                emit("unknown-envelope")
            else:
                try:
                    event = normalize_event(data)
                except ValueError:
                    emit("unknown-event-format")
                else:
                    if on_notification is not None:
                        on_notification(event, persistent_id)
                    deliver_event(event, skip_previous=skip_previous, started_at=started_at)
            if persistent_id:
                state["ids"].append(persistent_id)
                state["ids"] = state["ids"][-MAX_IDS:]
                seen.clear()
                seen.update(state["ids"])
                save_state(state_path, state)
        except Exception as exc:
            # The dependency catches callback exceptions; make failures visible to the outer loop.
            errors[:] = [type(exc).__name__]

    async with ClientSession(trace_configs=[http_trace(config["bundle_id"], android_cert_sha1)]) as session:
        await prepare_receiver(config, state_path, state, session)
        client = ProbePushClient(
            on_message,
            FcmRegisterConfig(**config),
            credentials=state["fcm_credentials"],
            received_persistent_ids=list(state["ids"]),
            config=FcmPushClientConfig(connection_retry_count=2, start_seconds_before_retry_connect=10),
            http_client_session=session,
        )
        connected = False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + duration if duration is not None else float("inf")
        login_deadline = loop.time() + 60
        previous = None
        stop_reason = "error-or-cancel"
        try:
            await client.start()
            while loop.time() < deadline:
                if isinstance(state_path, StateDocument):
                    state_path.store.assert_leases()
                status = client.run_state.name
                if status != previous:
                    emit("receiver-state", state=status)
                    previous = status
                connected = connected or client.is_started()
                if errors:
                    raise RuntimeError("Callback failed")
                if not connected and loop.time() > login_deadline:
                    raise TimeoutError("FCM login did not complete")
                if status in ("STOPPING", "STOPPED") or any(task.done() for task in client.tasks):
                    raise ConnectionError("Receiver stopped unexpectedly")
                # Upstream stores acknowledged delivery IDs in a plain growing list.
                if len(client.persistent_ids) > MAX_IDS:
                    del client.persistent_ids[:-MAX_IDS]
                await asyncio.sleep(0.5)
            stop_reason = "duration-elapsed"
        except asyncio.CancelledError:
            stop_reason = "stopped-by-user"
            raise
        finally:
            emit("receiver-stopping", reason=stop_reason)
            await client.stop()
            for task in client.tasks:
                task.cancel()
            await asyncio.wait_for(asyncio.gather(*client.tasks, return_exceptions=True), 10)
        emit("probe-ended", connected=connected, reason=stop_reason, delivery_proven=False)
        return 0 if connected else 1


def main(argv: list[str] | None = None) -> int:
    """Run the retained low-level diagnostic receiver with explicit network opt-in."""
    started_at = datetime.now(timezone.utc)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strings", type=Path, default=STRINGS)
    parser.add_argument("--state", type=Path, default=STATE)
    parser.add_argument("--duration", type=int, default=600)
    parser.add_argument("--allow-network", action="store_true")
    parser.add_argument("--existing-only", action="store_true", help="Never create a new FCM identity")
    parser.add_argument("--skip-previous", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--android-cert-sha1", default=ANDROID_CERT_SHA1, help="APK signer SHA-1, 40 hex characters")
    args = parser.parse_args(argv)
    if not 1 <= args.duration <= 3600:
        parser.error("duration must be between 1 and 3600 seconds")
    # Dependencies sometimes log response bodies even without verbose debugging.
    logging.basicConfig(handlers=[SafeDependencyLog()], level=logging.WARNING, force=True)
    try:
        config = firebase_config(args.strings)
        if not args.allow_network:
            emit("offline-config-ok", network=False, camera_connections=0)
            return 0
        with exclusive_run(args.state):
            return asyncio.run(
                receive(
                    config,
                    args.state,
                    args.duration,
                    args.android_cert_sha1,
                    skip_previous=args.skip_previous,
                    existing_only=args.existing_only,
                    started_at=started_at,
                )
            )
    except KeyboardInterrupt:
        emit("stopped-by-user")
        return 130
    except Exception as exc:
        emit("failed", error_type=type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
