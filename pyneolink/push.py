"""Optional cloud push subscriptions and camera-independent event reception."""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import math
import queue
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import CameraConfig
from .core.storage import (
    DEFAULT_STORAGE_DIR,
    CameraStore,
    StateDocument,
    camera_database,
    document_exists,
    read_document,
)
from .internal import push_fcm as fcm
from .internal import push_lifecycle as lifecycle
from .internal import push_subscription as subscription

DEFAULT_PUSH_STATE_DIR = DEFAULT_STORAGE_DIR
LOGGER = logging.getLogger(__name__)


class PushError(RuntimeError):
    """Push operation failed without exposing authentication material."""


class PushDependencyError(PushError):
    """The optional push receiver dependencies are missing."""


@dataclass(frozen=True)
class PushEvent:
    """One cloud notification with typed helpers and the original JSON envelope.

    Summary fields preserve the original event codes and timestamp strings.
    ``response`` contains every decrypted field supplied by the FCM dependency,
    not transport frames or receiver credentials. It is absent for historical
    rows saved before full-response support. Treat it as read-only; use
    ``get_response()`` for an independent copy. Missing optional values return
    None, and unfamiliar event codes remain accessible through ``event_type``.

    Titles, bodies, names and URLs may contain private information. Neither
    event classification nor a low-battery warning implies a charge percentage.
    """

    event_type: str | None
    camera_uid: str | None
    channel_id: str | None
    push_version: str
    device_time_raw: str | None = None
    server_time_raw: str | None = None
    event_id: str | None = None
    duplicate_hint: bool = False
    received_at: str | None = None
    age_filter: str | None = None
    response: dict[str, Any] | None = field(default=None, repr=False, compare=False)
    title: str | None = None

    def __post_init__(self) -> None:
        """Recover the title from new or previously stored full responses."""
        if self.title is not None or not isinstance(self.response, dict):
            return
        for section in ("data", "notification"):
            payload = self.response.get(section)
            title = payload.get("title") if isinstance(payload, dict) else None
            if isinstance(title, str) and title.strip():
                object.__setattr__(self, "title", title)
                break

    def info(self, *, full_response: bool = False) -> dict[str, Any]:
        """Return summary fields, optionally including the decrypted response.

        The summary includes the notification title. Full responses may contain
        additional private notification text and URLs. Older
        stored events have response=None because their original data was not
        retained. The returned full response is a defensive copy.

        :param full_response: Include the original envelope under ``response``.
        :return: A JSON-compatible summary, with title included by default.
        """
        result = {key: value for key, value in vars(self).items() if key != "response"}
        if full_response:
            result["response"] = self.get_response()
        return result

    def get_response(self) -> dict[str, Any] | None:
        """Return a deep copy of the complete envelope, or None for older rows."""
        return deepcopy(self.response)

    def is_people(self) -> bool:
        """Return True for the observed PEOPLE person-detection event code."""
        return self.event_type == "PEOPLE"

    def is_motion(self) -> bool:
        """Return True for observed motion codes MD, PEOPLE, or VEHICLE.

        Use ``is_general_motion()`` to match MD alone. Unknown future codes are
        not guessed to be motion; their original values remain available.
        """
        return self.event_type in ("MD", "PEOPLE", "VEHICLE")

    def is_general_motion(self) -> bool:
        """Return True for MD, which does not identify a specific object type."""
        return self.event_type == "MD"

    def is_vehicle(self) -> bool:
        """Return True for the observed VEHICLE detection event code."""
        return self.event_type == "VEHICLE"

    def is_test(self) -> bool:
        """Return True for TEST notifications, not real detection events."""
        return self.event_type == "TEST"

    def low_battery(self) -> bool:
        """Identify LPC (physically verified) or LP (official app LowBatteryTip).

        This is a warning flag, not a battery measurement or a shutdown event.
        The exact threshold and support on other camera models are unknown.
        """
        return self.event_type in ("LPC", "LP")

    def _text(self, key: str, *, section: str = "data") -> str | None:
        """Read a nonempty string without coercing malformed payload values."""
        source = self.response.get(section) if isinstance(self.response, dict) else None
        value = source.get(key) if isinstance(source, dict) else None
        return value if isinstance(value, str) and value.strip() else None

    @property
    def body(self) -> str | None:
        """Notification text from data.alert, falling back to notification.body."""
        return self._text("alert") or self._text("body", section="notification")

    @property
    def language(self) -> str | None:
        """Language reported by the server, without translating the message."""
        return self._text("language")

    @property
    def camera_name(self) -> str | None:
        """Device name reported in DEVNAME, not the local configuration alias."""
        return self._text("DEVNAME")

    @property
    def channel_name(self) -> str | None:
        """Channel display name reported in CHNAME, if present."""
        return self._text("CHNAME")

    @property
    def device_model(self) -> str | None:
        """Camera model reported in DEVMOD, if present."""
        return self._text("DEVMOD")

    @property
    def server_time(self) -> datetime | None:
        """Timezone-aware UTC server timestamp, or None if absent/invalid.

        The unmodified representation remains available in server_time_raw.
        This is server notification time, not necessarily detection time.
        """
        return fcm.event_server_time(self.info())

    def _object(self, key: str) -> dict[str, Any] | None:
        """Decode an optional JSON object without losing its original payload."""
        source = self.response.get("data") if isinstance(self.response, dict) else None
        value = source.get(key) if isinstance(source, dict) else None
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (ValueError, RecursionError):
                return None
        return deepcopy(value) if isinstance(value, dict) else None

    @property
    def options(self) -> dict[str, Any] | None:
        """Decoded options object, or None when missing or malformed."""
        return self._object("options")

    @property
    def custom_config(self) -> dict[str, Any] | None:
        """Decoded customConfig object; preserve unknown fields without guessing."""
        return self._object("customConfig")


@dataclass(frozen=True)
class PushResult:
    """Operation outcome, with optional sanitized diagnostics in ``info()``."""

    action: str
    status: str
    ok: bool
    details: dict[str, Any] = field(default_factory=dict, repr=False)

    def info(self) -> dict[str, Any]:
        """Return the result and safe protocol diagnostics."""
        return {**self.details, "action": self.action, "status": self.status, "ok": self.ok}


def _safe_error(exc: Exception, diagnostics: Mapping[str, Any] | None = None) -> PushError:
    """Expose allowlisted diagnostic codes without exception text or credentials."""
    if isinstance(exc, ModuleNotFoundError):
        return PushDependencyError('Install receiver dependencies with: python -m pip install "pyneolink[push]"')
    details = diagnostics or {}
    fields = [type(exc).__name__]
    stage = details.get("stage")
    if stage in _REGISTRATION_STAGES.values():
        fields.append(f"stage={stage}")
    status = details.get("http_status")
    if type(status) is int and 100 <= status <= 599:
        fields.append(f"http_status={status}")
    code = details.get("google_error")
    if isinstance(code, str) and code in fcm.SAFE_GCM_ERRORS:
        fields.append(f"google_error={code}")
    reasons = details.get("google_reasons")
    if isinstance(reasons, list):
        fields.extend(
            f"google_reason={reason}"
            for reason in reasons
            if isinstance(reason, str) and reason in fcm.SAFE_FIREBASE_REASONS
        )
    return PushError(f"Push operation failed ({'; '.join(fields)}); existing receiver credentials were not replaced")


_REGISTRATION_STAGES = {
    "gcm_checkin": "gcm_checkin",
    "gcm_check_in": "gcm_checkin",
    "gcm_register": "gcm_register",
    "firebase_installation": "firebase_installation",
    "fcm_install": "firebase_installation",
    "fcm_registration": "fcm_registration",
    "fcm_register": "fcm_registration",
}


def _transient_network_error(exc: Exception) -> bool:
    """Retry transport failures, never local credential/disk or consumer errors."""
    if isinstance(exc, (PermissionError, FileNotFoundError, FileExistsError)):
        return False
    if isinstance(exc, OSError) and exc.errno in (errno.ENOSPC, errno.EIO, errno.EROFS):
        return False
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return True
    try:
        from aiohttp import ClientConnectionError, ClientPayloadError
    except ImportError:
        return False
    return isinstance(exc, (ClientConnectionError, ClientPayloadError))


@contextmanager
def _diagnostics(callback: Callable[[dict[str, Any]], None] | None) -> Iterator[dict[str, Any]]:
    """Route receiver diagnostics to this operation and restore the prior sink."""
    summary: dict[str, Any] = {}

    def output(record: dict[str, Any]) -> None:
        """Collect allowlisted failure context and forward sanitized diagnostics."""
        stage = _REGISTRATION_STAGES.get(record.get("stage"))
        if stage is not None:
            if summary.get("stage") != stage or record.get("status") == "http-response":
                summary.clear()
            summary["stage"] = stage
            for key in ("http_status", "google_error", "google_reasons"):
                if key in record:
                    summary[key] = record[key]
        if callback is not None:
            callback(record)
        else:
            LOGGER.debug("%s", json.dumps(record))

    token = fcm.EVENT_SINK.set(output)
    # Only this optional dependency's logger is isolated, never the root logger.
    dependency_logger = logging.getLogger("firebase_messaging")
    if not any(isinstance(handler, fcm.SafeDependencyLog) for handler in dependency_logger.handlers):
        dependency_logger.addHandler(fcm.SafeDependencyLog())
    dependency_logger.propagate = False
    try:
        yield summary
    finally:
        fcm.EVENT_SINK.reset(token)


class PushClient:
    """Own one PC receiver identity and one standalone camera subscription.

    Each camera has its own SQLite database. ``subscribe`` and
    camera ``status`` use short managed sessions. ``watch`` never connects to a
    camera, even when a camera configuration was supplied. Construction has no
    network side effects. Existing research receiver/state files are supported.

    Language research: the official Android app 4.64.3 includes localizations
    for ar, bg, cs, da, de, el, en, es, et, fi, fr, he, hr, hu, id, it, ja, ko,
    lt, lv, ms, nb, nl, pl, pt, ro, ru, sl, sr, sv, th, tr, uk, vi, zh-CN,
    zh-HK, zh-MO, and zh-TW. This list is not exhaustive; the full supported
    language list may be larger. App localizations do not establish push-server
    language support. The app sends the system language to the client-config
    endpoint, using zhHans or zhHant for Chinese. Language configuration is not
    yet exposed by this SDK.
    """

    def __init__(
        self,
        config: CameraConfig | None = None,
        *,
        state_dir: str | Path | None = None,
        camera_name: str | None = None,
        storage_dir: str | Path = DEFAULT_STORAGE_DIR,
        database: str | Path | None = None,
        legacy_state_dir: str | Path | None = None,
        diagnostic_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        """Open local storage and optionally import legacy state, without network I/O.

        :param config: Camera credentials for subscribe/status/unsubscribe only.
        :param state_dir: Legacy alias for storage location and import source.
        :param camera_name: Local camera identity when config is omitted.
        :param storage_dir: Directory for per-camera databases; defaults to
            ~/pyneolink. Accepts absolute paths or relative paths such as
            ./storage, resolved from the working directory at construction.
            Missing directories are created. An explicit database overrides
            the generated database path; legacy state_dir takes precedence
            over storage_dir. Changing directories does not move existing state.
        :param database: Explicit SQLite path instead of the generated name.
        :param legacy_state_dir: Optional directory of legacy research JSON files.
        :param diagnostic_callback: Optional callback receiving sanitized records.
        :raises ValueError: Identity is missing or imported state belongs elsewhere.
        :raises FileExistsError: A legacy receiver is still marked as active.
        """
        self.config = config
        legacy = Path(legacy_state_dir or state_dir).expanduser() if (legacy_state_dir or state_dir) else None
        name = config.name if config is not None else camera_name
        if name is None and legacy is not None and (legacy / "reolink-v2.json").is_file():
            name = json.loads((legacy / "reolink-v2.json").read_text(encoding="utf-8")).get("camera")
        if name is None and database is not None:
            identity = CameraStore(database).get("camera.identity", {})
            name = identity.get("name") or Path(database).stem
        if name is None:
            raise ValueError("Specify camera config or camera_name for the per-camera database")
        self.state_dir = Path(state_dir or storage_dir).expanduser()
        self.database_path = Path(database).expanduser() if database else camera_database(name, self.state_dir)
        self.store = CameraStore(self.database_path)
        self.store.bind_camera(name, config.uid if config else None)
        candidates = [legacy] if legacy else [Path(".tmp/push"), Path.home() / ".pyneolink/push"]
        for source in candidates:
            if all(self.store.get(key) is not None for key in ("push.receiver", "push.subscription", "push.firebase")):
                break
            identity_file = source / "reolink-v2.json"
            if not identity_file.is_file():
                continue
            old = (
                self.store.get("push.subscription")
                if self.store.has_imported("push.subscription", identity_file)
                else json.loads(identity_file.read_text(encoding="utf-8"))
            )
            mismatch = old.get("camera") != name or (
                config is not None
                and config.uid
                and old.get("camera_uid") is not None
                and old["camera_uid"] != config.uid
            )
            if mismatch:
                if legacy is not None:
                    raise ValueError("Legacy push state belongs to a different camera")
                continue
            files = {
                "push.receiver": source / "receiver.json",
                "push.subscription": identity_file,
                "push.firebase": source / "firebase-config.json",
            }
            if any(path.with_suffix(".running").exists() for path in files.values()):
                raise FileExistsError("Stop the old receiver before importing its state")
            self.store.import_documents(files)
        self.credentials_path = StateDocument(self.store, "push.receiver")
        self.subscription_path = StateDocument(self.store, "push.subscription")
        self.firebase_path = StateDocument(self.store, "push.firebase")
        self.diagnostic_callback = diagnostic_callback

    def _camera_config(self) -> CameraConfig:
        """Require camera credentials only for operations that use the camera."""
        if self.config is None:
            raise ValueError("A camera configuration is required for subscription operations")
        return self.config

    def _firebase_config(self) -> dict[str, str]:
        """Load the initialized profile without registering a new identity."""
        if not document_exists(self.firebase_path):
            raise ValueError("Initialize push first to store the Firebase application configuration")
        return self._validate_firebase(read_document(self.firebase_path))

    @staticmethod
    def _validate_firebase(value: Mapping[str, str]) -> dict[str, str]:
        """Validate and retain only fields accepted by the FCM dependency."""
        keys = ("project_id", "app_id", "api_key", "messaging_sender_id", "bundle_id")
        if not isinstance(value, Mapping) or any(not isinstance(value.get(key), str) or not value[key] for key in keys):
            raise ValueError("Firebase config requires project_id, app_id, api_key, messaging_sender_id, bundle_id")
        return {key: value[key] for key in keys}

    def initialize(
        self,
        firebase_config: Mapping[str, str] | None = None,
        *,
        android_strings: str | Path | None = None,
    ) -> PushResult:
        """Persist application config and initialize FCM only, never a camera.

        Use the saved Firebase configuration or the bundled application profile.
        Explicit Firebase config / Android strings override either default.
        An existing receiver is validated and reused without re-registration.
        Repeated calls return receiver-ready with reused=True, without network
        access; initialize before watch, which exclusively leases the receiver.
        Application IDs must match the Reolink sender; an unrelated Firebase
        project cannot receive its notifications. Keep the directory private.

        :param firebase_config: Optional explicit application profile.
        :param android_strings: Alternative strings.xml source for that profile.
        :return: Receiver-ready result with a reused flag in its details.
        :raises PushError: Registration fails; existing credentials are retained.
        """
        if firebase_config is not None and android_strings is not None:
            raise ValueError("Choose Firebase config or Android strings, not both")
        values = firebase_config
        if android_strings is not None:
            values = fcm.firebase_config(Path(android_strings))
        if values is None:
            from pyneolink.internal.push_profile import default_firebase_config

            values = (
                read_document(self.firebase_path) if document_exists(self.firebase_path) else default_firebase_config()
            )
        config = self._validate_firebase(values)
        with fcm.exclusive_run(self.credentials_path), _diagnostics(self.diagnostic_callback) as diagnostics:
            existing = document_exists(self.credentials_path)
            fcm.load_state(self.credentials_path, config)
            fcm.save_state(self.firebase_path, config)
            try:
                asyncio.run(fcm.initialize_receiver(config, self.credentials_path))
            except Exception as exc:
                raise _safe_error(exc, diagnostics) from None
        return PushResult("init", "receiver-ready", True, {"reused": existing, "camera_connections": 0})

    def subscribe(self) -> PushResult:
        """Ensure the PC listener exists, reconciling timeouts without blind retries.

        Requires initialized receiver credentials and camera configuration.
        Opens a short camera session; does not start a watcher. A successful
        creation response does not itself prove notification delivery.

        :return: Accepted, verified, or unconfirmed subscription outcome.
        :raises PushError: The operation cannot be completed safely.
        """
        try:
            details = lifecycle.subscribe(
                self._camera_config(),
                self.credentials_path,
                self.subscription_path,
                confirm=True,
            )
        except Exception as exc:
            raise _safe_error(exc) from None
        ok = bool(details.get("subscription_verified") or details.get("listener_created"))
        return PushResult("subscribe", details.get("outcome", "subscription-unconfirmed"), ok, details)

    def status(self) -> PushResult:
        """Read subscription state with command 526, then close the camera session.

        :return: Subscribed/unsubscribed, or unknown with ok=False. This checks
            the listener, not the health of an active FCM connection.
        :raises PushError: The query cannot be completed.
        """
        try:
            details = subscription.unsubscribe(
                self._camera_config(),
                self.credentials_path,
                self.subscription_path,
                check_subscription=True,
                via_camera=True,
            )
        except Exception as exc:
            raise _safe_error(exc) from None
        status = details.get("subscription_status", "unknown")
        return PushResult("status", status, status != "unknown", details)

    def unsubscribe(self, *, verify: bool = True, retry_rejected: bool = False) -> PushResult:
        """Remove only this PC listener; optionally verify with a brief camera query.

        Does not disable global push or remove a phone's credentials. A known
        rejected HTTP attempt can be retried explicitly, never automatically.

        :param verify: Confirm removal with a short camera session.
        :param retry_rejected: Allow another previously rejected removal request.
        :return: Removal outcome distinguishing accepted from verified.
        """
        try:
            details = subscription.unsubscribe(
                self._camera_config(),
                self.credentials_path,
                self.subscription_path,
                allow_unsubscribe=True,
                verify_via_camera=verify,
                retry_rejected=retry_rejected,
            )
        except Exception as exc:
            raise _safe_error(exc) from None
        if details.get("removal_verified"):
            return PushResult("unsubscribe", "unsubscribed", True, details)
        accepted = bool(details.get("removal_accepted"))
        return PushResult(
            "unsubscribe", "removal-accepted" if accepted else "removal-unconfirmed", accepted and not verify, details
        )

    def watch(
        self,
        *,
        skip_previous: bool = True,
        duration: float | None = None,
        reconnect_delay: float = 30,
        raw_output: str | Path | None = None,
    ) -> PushWatch:
        """Return a bounded, context-managed event iterator; no camera connection.

        Events older than watch start are skipped by cloud time by default.
        Unknown timestamps are delivered with an age-filter marker. Duration is
        seconds or None (until close); no background work starts until iteration
        or context entry. Transient network failures retry with exponential delay
        starting at ``reconnect_delay`` seconds, capped at 300 seconds.
        ``raw_output`` optionally appends full decrypted notifications to a
        private .jsonl diagnostic file, including old/duplicate notifications.
        It may contain sensitive text and URLs; it is disabled by default.

        :param skip_previous: Skip deliveries older than the initial watch start.
        :param duration: Total reception seconds, including retries, or None.
        :param reconnect_delay: Initial retry delay from 1 to 300 seconds.
        :param raw_output: Optional append-only .jsonl capture path.
        :return: Lazy PushWatch yielding PushEvent objects with full responses.
        :raises ValueError: A duration, retry delay, or capture suffix is invalid.
        """
        return PushWatch(
            self,
            skip_previous=skip_previous,
            duration=duration,
            reconnect_delay=reconnect_delay,
            raw_output=raw_output,
        )

    def read(
        self,
        *,
        sync: bool = True,
        sync_timeout: float = 10,
        idle_timeout: float = 2,
        limit: int = 1000,
        raw_output: str | Path | None = None,
    ) -> list[PushEvent]:
        """Synchronize briefly, return unread events since subscribe, then finish.

        FCM has no queue-end marker: stop after idle_timeout seconds idle or
        sync_timeout total. Events newer than invocation remain unread. Use
        sync=False for SQLite-only reads. At most limit (1..10000) are returned.
        raw_output captures full incoming notifications during synchronization
        in an append-only JSONL file; it requires sync=True. It includes messages
        received during catch-up even if they are not returned by this read.

        :param sync: Contact FCM first; False only reads the local inbox.
        :param sync_timeout: Maximum catch-up seconds, excluding bounded cleanup.
        :param idle_timeout: Finish after this many idle seconds once connected.
        :param limit: Maximum number of returned events, from 1 to 10000.
        :param raw_output: Optional full incoming JSONL capture; requires sync.
        :return: Oldest unread events, marked read atomically before returning.
        :raises PushError: Synchronization fails; stored unread events are retained.
        :raises ValueError: Limits, timeouts, or capture options are invalid.
        """
        if not 1 <= limit <= 10000:
            raise ValueError("Read limit must be between 1 and 10000")
        if raw_output is not None and not sync:
            raise ValueError("Raw push capture requires synchronization; omit --no-sync")
        if not math.isfinite(sync_timeout) or not math.isfinite(idle_timeout) or not 0 < idle_timeout <= sync_timeout:
            raise ValueError("Read timeouts must be finite and 0 < idle_timeout <= sync_timeout")
        cutoff = time.time()
        if sync:
            watcher = self.watch(skip_previous=False, duration=sync_timeout, raw_output=raw_output)
            watcher._collect_only = True
            deadline = time.monotonic() + sync_timeout
            with watcher:
                while time.monotonic() < deadline:
                    if watcher._done.is_set():
                        break
                    if watcher._ready.is_set() and time.monotonic() - watcher._last_received >= idle_timeout:
                        break
                    time.sleep(0.05)
                if watcher._error is not None:
                    raise watcher._error
                if not watcher._ready.is_set():
                    raise PushError("Read could not connect within sync_timeout; unread events were retained")
            if watcher._error is not None:
                raise watcher._error
        state = self.store.get("push.subscription", {})
        since = state.get("subscription_started_at", 0)
        return [PushEvent(**event) for event in self.store.read_events(cutoff=cutoff, since=since, limit=limit)]


class PushWatch(Iterator[PushEvent]):
    """Closeable receiver iterator with a bounded 128-event queue.

    Use ``with`` when breaking early. A slow consumer that exhausts the queue
    receives an error instead of unbounded RAM growth or silent data loss.
    Closing the iterator keeps the cloud subscription and saved credentials.
    """

    def __init__(
        self,
        client: PushClient,
        *,
        skip_previous: bool = True,
        duration: float | None = None,
        reconnect_delay: float = 30,
        raw_output: str | Path | None = None,
    ) -> None:
        """Configure lazy reception; use PushClient.watch for parameter details."""
        if duration is not None and (not math.isfinite(duration) or duration <= 0):
            raise ValueError("Push watch duration must be a positive finite number")
        if not math.isfinite(reconnect_delay) or not 1 <= reconnect_delay <= 300:
            raise ValueError("Reconnect delay must be between 1 and 300 seconds")
        self.client = client
        self.skip_previous = skip_previous
        self.duration = duration
        self.reconnect_delay = reconnect_delay
        self.raw_output = Path(raw_output).expanduser() if raw_output is not None else None
        if self.raw_output is not None and self.raw_output.suffix.lower() != ".jsonl":
            raise ValueError("Raw push output must use a .jsonl file")
        self._raw_capture: Callable[[object, str | None], None] | None = None
        self._response: dict[str, Any] | None = None
        self._events: queue.Queue[tuple[int, PushEvent]] = queue.Queue(maxsize=128)
        self._done = threading.Event()
        self._closed = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._error: PushError | None = None
        self._started_at: datetime | None = None
        self._collect_only = False
        self._ready = threading.Event()
        self._last_received = time.monotonic()
        self._last_event_id: int | None = None

    def __enter__(self) -> PushWatch:
        """Start reception and return this iterator; no camera session is opened."""
        self._start()
        return self

    def __exit__(self, *args: object) -> None:
        """Stop reception and release the receiver lease without unsubscribing."""
        self.close()

    def __iter__(self) -> PushWatch:
        """Return this single-use iterator without starting network activity."""
        return self

    def _start(self) -> None:
        """Start one worker, requiring an already initialized receiver identity."""
        if self._closed.is_set() or self._thread is not None:
            return
        config = self.client._firebase_config()
        if not document_exists(self.client.credentials_path):
            raise ValueError("Initialize the receiver before watch; watch never registers it")
        self._started_at = datetime.now(timezone.utc)
        self._thread = threading.Thread(target=self._run, args=(config,), name="pyneolink-push", daemon=True)
        try:
            self._thread.start()
        except KeyboardInterrupt:
            # __exit__ is not called when __enter__ is interrupted.
            self.close()
            raise

    def _on_record(self, record: dict[str, Any]) -> None:
        """Track readiness and enqueue normalized deliveries without blocking FCM."""
        if record["status"] == "receiver-state" and record.get("state") == "STARTED":
            self._last_received = time.monotonic()
            self._ready.set()
        if record["status"] == "event":
            event = PushEvent(
                **record["event"],
                received_at=record["time"],
                age_filter=record.get("age_filter"),
                response=self._response,
            )
            event_id = self._last_event_id or self._store_event(record["event"], None)
            self._last_event_id = None
            try:
                if not self._collect_only:
                    self._events.put_nowait((event_id, event))
            except queue.Full:
                self._error = PushError("Push event queue is full; consume events faster")
                raise self._error from None
        if self.client.diagnostic_callback is not None:
            self.client.diagnostic_callback(record)

    def _on_raw_notification(self, notification: object, persistent_id: str | None) -> None:
        """Retain only the current envelope and optionally append diagnostic data."""
        # The transport invokes raw, normalized, and delivery callbacks serially.
        self._response = notification if isinstance(notification, dict) else None
        if self._raw_capture is not None:
            self._raw_capture(notification, persistent_id)

    def _store_event(self, event: dict, persistent_id: str | None) -> int:
        """Persist the current notification before acknowledging its delivery."""
        self._last_received = time.monotonic()
        timestamp = fcm.event_server_time(event)
        saved = PushEvent(
            **event,
            received_at=datetime.now(timezone.utc).isoformat(),
            age_filter=None if timestamp else "unknown-server-time",
            response=self._response,
        ).info(full_response=True)
        self._last_event_id = self.client.store.record_event(
            saved,
            persistent_id=persistent_id,
            sent_at=timestamp.timestamp() if timestamp else None,
        )
        return self._last_event_id

    async def _receive(self, config: dict[str, str]) -> None:
        """Retry transient FCM failures within the original watch deadline."""
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.current_task()
        if self._closed.is_set():
            return
        deadline = self._loop.time() + self.duration if self.duration is not None else math.inf
        delay = self.reconnect_delay
        while not self._closed.is_set():
            remaining = deadline - self._loop.time()
            if remaining <= 0:
                return
            attempt_started = self._loop.time()
            try:
                operation = fcm.receive(
                    config,
                    self.client.credentials_path,
                    remaining if self.duration is not None else None,
                    skip_previous=self.skip_previous,
                    existing_only=True,
                    started_at=self._started_at,
                    on_notification=self._store_event,
                    on_raw_notification=self._on_raw_notification,
                )
                result = await operation if self.duration is None else await asyncio.wait_for(operation, remaining)
                if result != 0:
                    raise ConnectionError("Push receiver did not connect")
                return
            except Exception as exc:
                if not _transient_network_error(exc):
                    raise
                if self._loop.time() >= deadline:
                    return
                if self._loop.time() - attempt_started >= 300:
                    delay = self.reconnect_delay
                wait = min(delay, max(0, deadline - self._loop.time()))
                fcm.emit("reconnecting", delay=wait, error_type=type(exc).__name__, camera_connections=0)
                await asyncio.sleep(wait)
                delay = min(300, delay * 2)

    def _run(self, config: dict[str, str]) -> None:
        """Own the receiver lease and asyncio loop until bounded cleanup finishes."""
        try:
            with (
                fcm.exclusive_run(self.client.credentials_path),
                _diagnostics(self._on_record),
                fcm.capture_notifications(self.raw_output) as capture,
            ):
                self._raw_capture = capture
                asyncio.run(self._receive(config))
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._error = self._error or _safe_error(exc)
        finally:
            self._raw_capture = None
            self._response = None
            self._done.set()

    def __next__(self) -> PushEvent:
        """Wait for and atomically claim one unread event, propagating worker errors."""
        self._start()
        while not self._closed.is_set():
            try:
                event_id, event = self._events.get(timeout=0.2)
                if self.client.store.mark_read(event_id):
                    return event
            except queue.Empty:
                if self._done.is_set():
                    if self._error is not None:
                        raise self._error
                    raise StopIteration
        raise StopIteration

    def close(self) -> None:
        """Cancel reception, wait for bounded cleanup, and retain the subscription."""
        first_close = not self._closed.is_set()
        self._closed.set()
        if first_close and self._loop is not None and self._task is not None and not self._done.is_set():
            try:
                self._loop.call_soon_threadsafe(self._task.cancel)
            except RuntimeError:
                pass
        if self._thread is not None and self._thread is not threading.current_thread():
            deadline = time.monotonic() + 20
            interrupted = False
            # _done is set after releasing the SQLite lease. A thread join alone
            # is not a cleanup guarantee when interrupted by another Ctrl+C.
            while True:
                try:
                    remaining = max(0, deadline - time.monotonic())
                    if not self._done.wait(timeout=remaining):
                        raise PushError("Push receiver cleanup did not finish within 20 seconds")
                    self._thread.join(timeout=max(0, deadline - time.monotonic()))
                    break
                except KeyboardInterrupt:
                    interrupted = True
                    if time.monotonic() >= deadline:
                        raise PushError("Push receiver cleanup did not finish within 20 seconds") from None
            if interrupted:
                raise KeyboardInterrupt
