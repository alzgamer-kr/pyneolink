"""Offline tests for the experimental FCM wrapper; Google is never contacted."""

import asyncio
import json
import logging
import sys
from base64 import b64decode, urlsafe_b64encode
from datetime import datetime, timezone
from types import ModuleType, SimpleNamespace

import pytest

from pyneolink.internal import push_fcm as probe


@pytest.mark.parametrize(
    "value",
    [
        "2026-11-05T08:00:00Z",
        "2026-11-05T10:00:00+02:00",
        "2026-11-05T08:00:00",
        "1793865600",
        "1793865600000",
        1793865600,
    ],
)
def test_server_time_formats(value):
    expected = datetime(2026, 11, 5, 8, tzinfo=timezone.utc)
    assert probe.event_server_time({"server_time_raw": value}) == expected


@pytest.mark.parametrize("value", [None, "", "invalid", True, [], {}, "9" * 129, "NaN"])
def test_unknown_server_time_is_not_invented(value):
    assert probe.event_server_time({"server_time_raw": value}) is None


@pytest.mark.parametrize("skip", [True, False])
@pytest.mark.parametrize(
    "sent,expected",
    [
        ("2026-11-05T07:59:59Z", False),
        ("2026-11-05T08:00:00Z", True),
        ("2026-11-05T08:00:01Z", True),
        (None, True),
    ],
)
def test_backlog_filter_uses_run_start_not_arrival_time(capsys, skip, sent, expected):
    start = datetime(2026, 11, 5, 8, tzinfo=timezone.utc)
    result = probe.deliver_event({"server_time_raw": sent}, skip_previous=skip, started_at=start)
    assert result is (expected or not skip)
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == ("event" if result else "previous-event-skipped")
    if skip and sent is None:
        assert output["age_filter"] == "unknown-server-time"


def test_watch_missing_credentials_fails_without_registration(tmp_path, config):
    path = tmp_path / "missing.json"
    with pytest.raises(ValueError, match="existing receiver"):
        asyncio.run(probe.receive(config, path, 1, existing_only=True))
    assert not path.exists()


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import socket

    original_connect = socket.socket.connect

    def loopback_only(sock, address):
        if isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1"):
            return original_connect(sock, address)
        raise AssertionError("No external network in FCM wrapper tests")

    def forbidden(*args, **kwargs):
        raise AssertionError("No network in FCM wrapper tests")

    # Leave socketpair available for asyncio's local wakeup mechanism.
    monkeypatch.setattr(socket.socket, "connect", loopback_only)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket.socket, "sendto", forbidden)


@pytest.fixture
def config():
    return dict(
        project_id="test", app_id="test-app", api_key="SECRET", messaging_sender_id="123", bundle_id="org.example.test"
    )


def fake_trace():
    return SimpleNamespace(on_request_start=[], on_request_end=[], on_request_redirect=[])


def test_config_is_read_from_xml_without_printing_key(tmp_path, capsys):
    path = tmp_path / "strings.xml"
    path.write_text(
        '<resources><string name="project_id">test</string>'
        '<string name="google_app_id">test-app</string>'
        '<string name="google_api_key">SECRET</string>'
        '<string name="gcm_defaultSenderId">123</string></resources>',
        encoding="utf-8",
    )
    assert probe.firebase_config(path)["api_key"] == "SECRET"
    assert probe.main(["--strings", str(path)]) == 0
    assert "SECRET" not in capsys.readouterr().out


def test_invalid_existing_state_is_not_replaced(tmp_path, config):
    path = tmp_path / "receiver.json"
    path.write_text("broken", encoding="utf-8")
    with pytest.raises(ValueError):
        probe.load_state(path, config)
    assert path.read_text(encoding="utf-8") == "broken"


def test_gcm_retry_budget_uses_one_upstream_call_and_same_options(capsys):
    calls = []
    options = {"androidId": "123", "securityToken": "SECRET"}

    class Base:
        async def gcm_register(self, given, retries=2):
            calls.append((given, retries))
            return {"token": "TOKEN"}

    class Registration(probe.GcmRegistrationRetry, Base):
        pass

    assert asyncio.run(Registration().gcm_register(options)) == {"token": "TOKEN"}
    assert len(calls) == 1
    assert calls[0][0] is options
    assert calls[0][1] == 5
    output = capsys.readouterr().out
    assert '"max_attempts": 5' in output
    assert "SECRET" not in output and "TOKEN" not in output


def test_gcm_retry_failure_does_not_start_another_upstream_registration():
    calls = []

    class Base:
        async def gcm_register(self, options, retries=2):
            calls.append(retries)
            return None

    class Registration(probe.GcmRegistrationRetry, Base):
        pass

    assert asyncio.run(Registration().gcm_register({})) is None
    assert calls == [5]


def test_atomic_state_and_single_listener(tmp_path, config):
    path = tmp_path / "receiver.json"
    state = probe.load_state(path, config)
    probe.save_state(path, state)
    assert json.loads(path.read_text()) == state
    with probe.exclusive_run(path):
        with pytest.raises(FileExistsError):
            with probe.exclusive_run(path):
                pytest.fail("Second listener admitted")
    assert not path.with_suffix(".running").exists()


def test_dependency_logs_are_sanitized(capsys):
    handler = probe.SafeDependencyLog()
    record = logging.LogRecord("test", logging.ERROR, "file", 1, "token=%s", ("SECRET",), None, "register")
    handler.emit(record)
    output = capsys.readouterr().out
    assert "SECRET" not in output
    assert "register" in output


@pytest.mark.parametrize(
    "message,category",
    [
        ("couldn't find in app_data crypto-key", "missing-crypto-key"),
        ("Incorrect padding", "invalid-base64-padding"),
        ("Invalid EC key.", "invalid-ec-key"),
        ("token=SECRET", None),
    ],
)
def test_listen_exception_argument_is_sanitized(message, category, capsys):
    try:
        raise RuntimeError(message)
    except RuntimeError as exc:
        record = logging.LogRecord(
            "test",
            logging.ERROR,
            "private/file",
            1,
            "Unknown error: %s\n%s",
            (exc, "SECRET traceback"),
            None,
            "_listen",
        )
        probe.SafeDependencyLog().emit(record)
    output = capsys.readouterr().out
    assert "SECRET" not in output and "private/file" not in output
    result = json.loads(output)
    assert result["exception_type"] == "RuntimeError"
    assert result.get("error_category") == category
    assert result["error_site"] == "test_listen_exception_argument_is_sanitized"
    assert type(result["error_line"]) is int


@pytest.mark.parametrize("encrypted", [False, True])
def test_data_shape_never_prints_payload_or_changes_dispatch(encrypted, capsys):
    received = []

    class Base:
        def _handle_data_message(self, message):
            received.append(message)
            return 17

    class Client(probe.DataMessageDiagnostics, Base):
        pass

    keys = ["crypto-key", "encryption", "subtype"] if encrypted else ["pushVersion", "SECRET"]
    message = SimpleNamespace(
        app_data=[SimpleNamespace(key=key, value="SECRET") for key in keys], raw_data=b"SECRET" if encrypted else b""
    )
    assert Client()._handle_data_message(message) == 17
    assert received == [message]
    output = capsys.readouterr().out
    assert "SECRET" not in output
    result = json.loads(output)
    assert result["has_crypto_key"] is encrypted
    assert result["has_push_version"] is not encrypted
    assert result["raw_data_bytes"] == (6 if encrypted else 0)


def test_structured_firebase_error_keeps_reason_but_not_secrets(capsys):
    body = json.dumps(
        {
            "error": {
                "code": 403,
                "status": "PERMISSION_DENIED",
                "message": "private SECRET",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                        "reason": "API_KEY_ANDROID_APP_BLOCKED",
                        "metadata": {"apiKey": "SECRET"},
                    }
                ],
            }
        }
    )
    record = logging.LogRecord("test", logging.ERROR, "file", 1, "%s", (body,), None, "fcm_install")
    probe.SafeDependencyLog().emit(record)
    output = capsys.readouterr().out
    assert "SECRET" not in output
    parsed = json.loads(output)
    assert parsed["google_reasons"] == ["API_KEY_ANDROID_APP_BLOCKED"]
    assert parsed["google_code"] == 403
    assert parsed["google_status"] == "PERMISSION_DENIED"


@pytest.mark.parametrize("body", ["SECRET", "[]", '{"error": null}', '{"error": "SECRET"}'])
def test_invalid_firebase_error_is_ignored(body):
    assert probe.firebase_error_details(body) == {}


def test_unknown_firebase_error_fields_are_not_printed():
    body = json.dumps(
        {
            "error": {
                "code": "SECRET",
                "status": "SECRET",
                "details": [
                    {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "SECRET"},
                    {"@type": "other", "message": "SECRET"},
                    None,
                ],
            }
        }
    )
    assert "SECRET" not in json.dumps(probe.firebase_error_details(body))


@pytest.mark.parametrize(
    "body,expected",
    [
        ("Error=AUTHENTICATION_FAILED\n", "AUTHENTICATION_FAILED"),
        ("Error=PHONE_REGISTRATION_ERROR", "PHONE_REGISTRATION_ERROR"),
        ("Error=SECRET_TOKEN", "UNRECOGNIZED_ERROR"),
        ("Error=AUTHENTICATION_FAILED\ntoken=SECRET_TOKEN", "UNRECOGNIZED_ERROR"),
    ],
)
def test_google_error_allowlist(body, expected, capsys):
    record = logging.LogRecord("test", logging.WARNING, "file", 1, "%s %s %s", (1, 2, body), None, "gcm_register")
    probe.SafeDependencyLog().emit(record)
    output = capsys.readouterr().out
    assert json.loads(output)["google_error"] == expected
    assert "SECRET_TOKEN" not in output


def test_http_trace_does_not_log_url_or_headers(monkeypatch, capsys):
    module = ModuleType("aiohttp")
    module.TraceConfig = fake_trace
    monkeypatch.setitem(sys.modules, "aiohttp", module)
    callback = probe.http_trace().on_request_end[0]
    params = SimpleNamespace(
        url=SimpleNamespace(host="android.clients.google.com", path="/c2dm/register3", query="SECRET"),
        response=SimpleNamespace(status=401, headers={"Authorization": "SECRET"}),
    )
    asyncio.run(callback(None, None, params))
    output = capsys.readouterr().out
    assert "SECRET" not in output
    assert json.loads(output)["http_status"] == 401


@pytest.mark.parametrize(
    "host,expected",
    [
        ("firebaseinstallations.googleapis.com", True),
        ("fcmregistrations.googleapis.com", True),
        ("android.clients.google.com", False),
        ("firebaseinstallations.googleapis.com.example.org", False),
    ],
)
def test_android_headers_are_scoped_to_firebase(monkeypatch, host, expected, capsys):
    module = ModuleType("aiohttp")
    module.TraceConfig = fake_trace
    monkeypatch.setitem(sys.modules, "aiohttp", module)
    callback = probe.http_trace("org.example.test", "ab" * 20).on_request_start[0]
    params = SimpleNamespace(url=SimpleNamespace(host=host, scheme="https", port=443), headers={"Existing": "value"})
    asyncio.run(callback(None, None, params))
    assert params.headers["Existing"] == "value"
    assert ("X-Android-Package" in params.headers) is expected
    assert ("X-Android-Cert" in params.headers) is expected
    if expected:
        assert params.headers["X-Android-Package"] == "org.example.test"
        assert params.headers["X-Android-Cert"] == "AB" * 20
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("scheme,port", [("http", 80), ("https", 8443)])
def test_android_headers_reject_unexpected_transport(monkeypatch, scheme, port):
    module = ModuleType("aiohttp")
    module.TraceConfig = fake_trace
    monkeypatch.setitem(sys.modules, "aiohttp", module)
    callback = probe.http_trace("org.example.test", "ab" * 20).on_request_start[0]
    params = SimpleNamespace(
        url=SimpleNamespace(host="firebaseinstallations.googleapis.com", scheme=scheme, port=port), headers={}
    )
    with pytest.raises(ValueError):
        asyncio.run(callback(None, None, params))
    assert params.headers == {}


def test_firebase_redirect_is_rejected(monkeypatch):
    module = ModuleType("aiohttp")
    module.TraceConfig = fake_trace
    monkeypatch.setitem(sys.modules, "aiohttp", module)
    callback = probe.http_trace("org.example.test", "ab" * 20).on_request_redirect[0]
    params = SimpleNamespace(url=SimpleNamespace(host="firebaseinstallations.googleapis.com"))
    with pytest.raises(RuntimeError, match="redirect"):
        asyncio.run(callback(None, None, params))


@pytest.mark.parametrize("cert", ["", "ab" * 19, "ab:" * 20, "gg" * 20, "ab" * 20 + "\r\nSecret: value"])
def test_invalid_android_certificate_is_rejected(monkeypatch, cert):
    module = ModuleType("aiohttp")
    module.TraceConfig = fake_trace
    monkeypatch.setitem(sys.modules, "aiohttp", module)
    with pytest.raises(ValueError):
        probe.http_trace("org.example.test", cert)


@pytest.mark.parametrize("old_event", [False, True])
@pytest.mark.parametrize("capture_raw", [False, True, "write-error"])
def test_receive_registers_once_reuses_state_and_closes(tmp_path, config, monkeypatch, capsys, old_event, capture_raw):
    instances = []
    registrations = []
    sessions = []
    credentials = {
        "gcm": {"android_id": "123", "security_token": "SECRET"},
        "fcm": {"registration": {"token": "FAKE-TOKEN"}},
        "keys": {"private": "SECRET"},
    }

    class Session:
        def __init__(self, **kwargs):
            assert "trace_configs" in kwargs

        async def __aenter__(self):
            self.closed = False
            sessions.append(self)
            return self

        async def __aexit__(self, *args):
            self.closed = True

    class Registration:
        def __init__(self, *args, **kwargs):
            pass

        async def register(self):
            registrations.append(True)
            return credentials

        async def gcm_check_in(self, *args):
            return {"androidId": "123"}

    class Client:
        def __init__(self, callback, *args, **kwargs):
            instances.append(self)
            self.callback = callback
            self.run_state = SimpleNamespace(name="CREATED")
            self.persistent_ids = kwargs["received_persistent_ids"]
            self.stopped = False

        async def start(self):
            self.run_state.name = "STARTED"
            self.tasks = [asyncio.create_task(asyncio.Event().wait())]
            notification = {"data": {"pushVersion": "v2", "UID": "TEST", "ALMTYPE": "OTHER", "token": "SECRET"}}
            if old_event:
                notification["data"]["SRVTIME"] = "2000-01-01T00:00:00Z"
            self.callback(notification, "delivery-1", None)
            self.callback(notification, "delivery-1", None)

        def is_started(self):
            return True

        async def stop(self):
            self.stopped = True

    aiohttp = ModuleType("aiohttp")
    aiohttp.ClientSession = Session
    aiohttp.TraceConfig = fake_trace
    library = ModuleType("firebase_messaging")
    library.FcmPushClient = Client
    library.FcmPushClientConfig = SimpleNamespace
    library.FcmRegisterConfig = SimpleNamespace
    register_module = ModuleType("firebase_messaging.fcmregister")
    register_module.FcmRegister = Registration
    monkeypatch.setitem(sys.modules, "aiohttp", aiohttp)
    monkeypatch.setitem(sys.modules, "firebase_messaging", library)
    monkeypatch.setitem(sys.modules, "firebase_messaging.fcmregister", register_module)
    path = tmp_path / "receiver.json"
    raw_path = tmp_path / "raw.jsonl"
    with probe.capture_notifications(raw_path if capture_raw else None) as capture:
        if capture_raw == "write-error":

            def fail_capture(*args):
                raise OSError("PRIVATE disk failure")

            with pytest.raises(RuntimeError, match="Callback failed"):
                asyncio.run(probe.receive(config, path, 1, on_raw_notification=fail_capture))
            assert all(client.stopped and all(task.done() for task in client.tasks) for client in instances)
            assert all(session.closed for session in sessions)
            assert probe.load_state(path, config)["ids"] == []
            assert "PRIVATE" not in capsys.readouterr().out
            return
        assert asyncio.run(probe.receive(config, path, 1, on_raw_notification=capture)) == 0
        first = probe.load_state(path, config)
        assert asyncio.run(probe.receive(config, path, 1, existing_only=True, on_raw_notification=capture)) == 0
    if capture_raw:
        records = [json.loads(line) for line in raw_path.read_text().splitlines()]
        assert len(records) == 4  # Includes duplicates and events older than watch start.
        assert all(row["notification"]["data"]["ALMTYPE"] == "OTHER" for row in records)
        assert all(row["persistent_id"] == "delivery-1" for row in records)
        assert "FAKE-TOKEN" not in raw_path.read_text()
        assert "security_token" not in raw_path.read_text()
    else:
        assert not raw_path.exists()
    second = probe.load_state(path, config)
    assert first["client_id"] == second["client_id"]
    assert second["ids"] == ["delivery-1"]
    assert len(registrations) == 1
    assert all(client.stopped and all(task.done() for task in client.tasks) for client in instances)
    assert all(session.closed for session in sessions)
    output = capsys.readouterr().out
    assert "SECRET" not in output and "FAKE-TOKEN" not in output
    assert output.count('"status": "event"') == (0 if old_event else 1)
    if old_event:
        assert output.count('"status": "previous-event-skipped"') == 1


def test_cli_refuses_unbounded_run():
    with pytest.raises(SystemExit):
        probe.main(["--duration", "0"])


def test_raw_capture_flushes_appends_and_preserves_unknown_fields(tmp_path):
    path = tmp_path / "nested" / "capture.jsonl"
    notification = {"data": {"ALMTYPE": "LPC", "alert": "Low battery\nCharge now", "extra": {"a": [1, 2]}}}
    for _ in range(2):
        with probe.capture_notifications(path) as capture:
            capture(notification, "delivery")
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            assert rows[-1]["notification"] == notification
            assert rows[-1]["received_at"].endswith("+00:00")
    assert len(rows) == 2


def test_raw_capture_rejects_non_jsonl_without_changing_file(tmp_path):
    path = tmp_path / "receiver.json"
    path.write_text("preserved")
    with pytest.raises(ValueError, match="jsonl"):
        with probe.capture_notifications(path):
            pass
    assert path.read_text() == "preserved"


@pytest.mark.parametrize("outcome", ["ok", "timeout", "cancel", "oserror"])
def test_tls_close_is_bounded_and_aborts_when_needed(outcome, capsys):
    class Writer:
        def __init__(self):
            self.closed = False
            self.aborted = False
            self.transport = SimpleNamespace(abort=self.abort)

        def abort(self):
            self.aborted = True

        def close(self):
            self.closed = True

        async def wait_closed(self):
            if outcome == "timeout":
                await asyncio.Event().wait()
            if outcome == "cancel":
                raise asyncio.CancelledError()
            if outcome == "oserror":
                raise OSError("SECRET")

    async def run():
        writer = Writer()
        client = probe.BoundedClose()
        client.writer_close_timeout = 0.01
        client.writer = writer
        if outcome == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await client._do_writer_close()
        else:
            await client._do_writer_close()
        assert client.writer is None
        assert writer.closed
        assert writer.aborted is (outcome != "ok")
        await client._do_writer_close()

    asyncio.run(run())
    assert "SECRET" not in capsys.readouterr().out


@pytest.mark.parametrize("parameter,raw", [("dh", b"\x04" + bytes(range(64))), ("salt", bytes(range(16)))])
@pytest.mark.parametrize("style", ["padded", "unpadded", "bare", "quoted", "multiple", "reversed"])
def test_crypto_header_normalization(parameter, raw, style):
    encoded = urlsafe_b64encode(raw).decode()
    unpadded = encoded.rstrip("=")
    values = {
        "padded": f"{parameter}={encoded}",
        "unpadded": f"{parameter}={unpadded}",
        "bare": unpadded,
        "quoted": f'{parameter}="{unpadded}"',
        "multiple": f"{parameter}={unpadded}; p256ecdsa=IGNORED",
        "reversed": f"p256ecdsa=IGNORED; {parameter}={unpadded}",
    }
    normalized = probe.normalize_crypto_header(values[style], parameter)
    assert normalized == f"{parameter}={encoded}"
    assert b64decode(normalized[len(parameter) + 1 :], altchars=b"-_", validate=True) == raw


@pytest.mark.parametrize(
    "value",
    ["", "salt=a;salt=b", "dh=abcd", "salt=***", "salt=a\r\nSecret: x", "salt=abcd", "salt=a", "x" * 4097, None],
    ids=["empty", "duplicate", "missing", "alphabet", "newline", "size", "padding", "long", "none"],
)
def test_bad_crypto_headers_rejected_without_values(value):
    with pytest.raises(ValueError):
        probe.normalize_crypto_header(value, "salt")


def test_header_adapter_keeps_upstream_missing_field_behavior():
    class Base:
        def _app_data_by_key(self, message, key, do_not_raise=False):
            for item in message.app_data:
                if item.key == key:
                    return item.value
            if do_not_raise:
                return ""
            raise RuntimeError(f"couldn't find in app_data {key}")

    class Client(probe.DataMessageDiagnostics, Base):
        pass

    message = SimpleNamespace(app_data=[])
    assert Client()._app_data_by_key(message, "message_type", True) == ""
    with pytest.raises(RuntimeError, match="crypto-key"):
        Client()._app_data_by_key(message, "crypto-key")
    salt = SimpleNamespace(key="encryption", value="salt=" + urlsafe_b64encode(b"x" * 16).decode().rstrip("="))
    message.app_data = [salt]
    assert Client()._app_data_by_key(message, "encryption").endswith("==")
    message.app_data.append(salt)
    with pytest.raises(ValueError, match="Ambiguous"):
        Client()._app_data_by_key(message, "encryption")
