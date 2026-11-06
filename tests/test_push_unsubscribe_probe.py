"""Offline tests: no camera connections and no real cloud requests."""

import hashlib
import json
from email.message import Message
from io import BytesIO
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from pyneolink.internal import push_subscription as probe
from pyneolink.config import CameraConfig


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import socket

    def forbidden(*args, **kwargs):
        raise AssertionError("No network in unsubscribe tests")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "sendto", forbidden)


@pytest.fixture
def setup(tmp_path):
    receiver = tmp_path / "receiver.json"
    state = tmp_path / "reolink-v2.json"
    receiver.write_text(
        json.dumps(
            {
                "purpose": probe.PURPOSE,
                "client_id": "A" * 32,
                "fcm_token": "FAKE-TOKEN",
                "fcm_credentials": {"fcm": {"registration": {"token": "FAKE-TOKEN"}}},
            }
        )
    )
    state.write_text(
        json.dumps(
            {
                "purpose": probe.V2_PURPOSE,
                "client_id": "PRIVATE-ID",
                "camera": probe.APPROVED_CAMERA,
                "camera_uid": "TESTUID",
                "listener_created": True,
                "listener_status": "accepted",
                "fcm_token_sha256": hashlib.sha256(b"FAKE-TOKEN").hexdigest(),
            }
        )
    )
    return CameraConfig(name=probe.APPROVED_CAMERA, uid="TESTUID"), receiver, state


def test_dry_run_preserves_files(setup):
    config, receiver, state = setup
    originals = receiver.read_bytes(), state.read_bytes()
    result = probe.unsubscribe(config, receiver, state)
    assert result == {"network": False, "camera_connections": 0, "ready": True}
    assert (receiver.read_bytes(), state.read_bytes()) == originals


@pytest.mark.parametrize("result_code", [200, 204, 302, 400, 401, 500, None])
def test_single_target_and_preserved_credentials(setup, result_code):
    config, receiver, state = setup
    before = receiver.read_bytes()
    calls = []

    def send(client_id, uid):
        assert json.loads(state.read_text())["unsubscribe"]["status"] == "pending-or-unknown"
        calls.append((client_id, uid))
        if result_code is None:
            raise TimeoutError("SECRET")
        return result_code

    if result_code is None:
        with pytest.raises(TimeoutError):
            probe.unsubscribe(config, receiver, state, allow_unsubscribe=True, send=send)
    else:
        result = probe.unsubscribe(config, receiver, state, allow_unsubscribe=True, send=send)
        assert result["removal_accepted"] is (result_code in (200, 204))
        assert result["removal_verified"] is False
        assert "PRIVATE-ID" not in json.dumps(result)
    assert calls == [("PRIVATE-ID", "TESTUID")]
    assert receiver.read_bytes() == before
    assert json.loads(state.read_text())["client_id"] == "PRIVATE-ID"
    with pytest.raises(ValueError):
        probe.unsubscribe(config, receiver, state, allow_unsubscribe=True, send=send)
    assert len(calls) == 1
    assert not receiver.with_suffix(".running").exists()
    assert not state.with_suffix(".running").exists()


@pytest.mark.parametrize("fault", ["camera", "uid", "token", "purpose", "channel", "running"])
def test_guards(setup, fault):
    config, receiver, state = setup
    document = json.loads(state.read_text())
    if fault == "camera":
        config.name = "Friend"
    elif fault == "uid":
        config.uid = "OTHER"
    elif fault == "token":
        document["fcm_token_sha256"] = "other"
    elif fault == "purpose":
        document["purpose"] = "other"
    elif fault == "channel":
        config.channel_id = 1
    elif fault == "running":
        receiver.with_suffix(".running").write_text("123")
    state.write_text(json.dumps(document))
    before = state.read_bytes()
    with pytest.raises((ValueError, FileExistsError)):
        probe.unsubscribe(config, receiver, state, allow_unsubscribe=True)
    assert state.read_bytes() == before


def test_http_request_is_scoped_no_redirects_or_retries(monkeypatch):
    calls = []

    class Response:
        code = 204

        def read(self, limit):
            assert limit == probe.MAX_RESPONSE_BYTES + 1
            return b""

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    def open_request(req, timeout):
        calls.append(req)
        assert req.full_url == probe.DELETE_URL
        assert req.method == "POST"
        assert req.get_header("Authorization") == "Bearer PRIVATE-ID"
        assert req.get_header("User-agent") == probe.USER_AGENT
        assert json.loads(req.data) == {"uid": "TESTUID", "channelIds": ["TESTUID"]}
        assert timeout == 30
        return Response()

    def build(*handlers):
        assert any(isinstance(item, probe.NoRedirect) for item in handlers)
        return SimpleNamespace(open=open_request)

    monkeypatch.setattr(probe, "build_opener", build)
    assert probe.post_removal("PRIVATE-ID", "TESTUID")["http_status"] == 204
    assert len(calls) == 1
    assert probe.NoRedirect().redirect_request(None, None, 302, None, None, "https://elsewhere") is None


def test_read_only_check_after_rejected_removal_preserves_state(setup):
    config, receiver, state = setup
    document = json.loads(state.read_text())
    document["unsubscribe"] = {"status": "not-accepted", "http_status": 403}
    state.write_text(json.dumps(document))
    before = receiver.read_bytes(), state.read_bytes()
    calls = []

    def inspect(client_id):
        calls.append(client_id)
        return {"http_status": 403, "read_only": True}

    def forbidden(*args):
        pytest.fail("Inspection must not delete anything")

    result = probe.unsubscribe(
        config,
        receiver,
        state,
        check_subscription=True,
        inspect=inspect,
        send=forbidden,
    )
    assert result["read_only"] is True
    assert calls == ["PRIVATE-ID"]
    assert (receiver.read_bytes(), state.read_bytes()) == before


@pytest.mark.parametrize(
    "status,body",
    [
        (200, b'[{"uid":"SECRET"}]'),
        (403, b'{"code":403,"message":"SECRET"}'),
        (403, b"<html>SECRET</html>"),
        (403, b"SECRET" * 12000),
    ],
    ids=["list", "json-error", "html-error", "oversized"],
)
def test_read_only_http_is_bounded_and_redacted(monkeypatch, status, body):
    calls = []
    headers = Message()
    headers["Content-Type"] = "application/json"
    headers["WWW-Authenticate"] = "SECRET"
    response = HTTPError(probe.LIST_URL, status, "SECRET", headers, BytesIO(body))

    def open_request(request, timeout):
        calls.append(request)
        assert request.method == "GET"
        assert request.data is None
        assert request.full_url == probe.LIST_URL
        assert request.get_header("Authorization") == "Bearer PRIVATE-ID"
        assert timeout == 30
        if status >= 400:
            raise response
        return response

    def build(*handlers):
        assert any(isinstance(handler, probe.NoRedirect) for handler in handlers)
        return SimpleNamespace(open=open_request)

    monkeypatch.setattr(probe, "build_opener", build)
    result = probe.get_subscription_status("PRIVATE-ID")
    assert len(calls) == 1
    assert result["http_status"] == status
    assert result["body_bytes_read"] <= probe.MAX_RESPONSE_BYTES + 1
    assert result["body_truncated"] == (len(body) > probe.MAX_RESPONSE_BYTES)
    assert result["removal_verified"] is False
    assert "SECRET" not in json.dumps(result)
    assert "PRIVATE-ID" not in json.dumps(result)


def test_inspection_and_deletion_cannot_be_combined(setup):
    with pytest.raises(ValueError):
        probe.unsubscribe(*setup, check_subscription=True, allow_unsubscribe=True)


@pytest.mark.parametrize("kind", ["captcha/1", "pow/1", "SECRET"])
def test_nested_challenge_diagnostics_redact_values(kind):
    result = probe.describe_json_error(
        {
            "error": {
                "code": 8214,
                "message": "SECRET",
                "metadata": {
                    "challenge": {"type": kind, "id": "SECRET", "url": "SECRET"},
                },
            },
            "SECRET": "SECRET",
        }
    )
    assert result["error_code"] == 8214
    assert result["api_challenge_required"] is True
    assert result["api_challenge_present"] is True
    assert result["api_challenge_type"] == (kind if kind != "SECRET" else "unknown")
    assert result["known_root_fields"] == ["error"]
    assert "SECRET" not in json.dumps(result)


@pytest.mark.parametrize("error", [None, "SECRET", [], {"code": "SECRET"}, {"code": True}, {"code": 10**20}])
def test_error_diagnostics_handle_unknown_schema(error):
    result = probe.describe_json_error({"error": error, "message": "SECRET"})
    assert "error_code" not in result
    assert not result.get("api_challenge_required")
    assert "SECRET" not in json.dumps(result)


def test_private_response_capture_is_bounded_and_not_printed(monkeypatch, tmp_path):
    headers = Message()
    headers["Content-Type"] = "application/json"
    body = b'{"detail":"PRIVATE-ID PRIVATE-DETAIL"}' + b" " * probe.MAX_RESPONSE_BYTES
    response = HTTPError(probe.LIST_URL, 403, "private", headers, BytesIO(body))
    calls = []

    def open_request(request, timeout):
        calls.append(request)
        assert request.method == "GET"
        raise response

    monkeypatch.setattr(probe, "build_opener", lambda *args: SimpleNamespace(open=open_request))
    path = tmp_path / "diagnostic.json"
    result = probe.get_subscription_status("PRIVATE-ID", diagnostic_path=path)
    captured = json.loads(path.read_text())
    assert len(calls) == 1
    assert captured["body_truncated"] is True
    assert "PRIVATE-DETAIL" in captured["response_body"]
    assert "PRIVATE-ID" not in captured["response_body"]
    assert len(captured["response_body"]) < probe.MAX_RESPONSE_BYTES + 32
    assert result["diagnostic_saved"] is True
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.parametrize("target", ["receiver", "state", "without-check"])
def test_diagnostic_cannot_overwrite_identity_or_run_without_get(setup, tmp_path, target):
    config, receiver, state = setup
    before = receiver.read_bytes(), state.read_bytes()
    path = {"receiver": receiver, "state": state}.get(target, tmp_path / "diagnostic.json")
    with pytest.raises(ValueError):
        probe.unsubscribe(
            config,
            receiver,
            state,
            diagnostic_path=path,
            check_subscription=target != "without-check",
        )
    assert (receiver.read_bytes(), state.read_bytes()) == before


@pytest.mark.parametrize(
    "arguments,check,confirm",
    [
        (["status"], True, False),
        (["unsubscribe"], False, False),
        (["unsubscribe", "--confirm"], False, True),
        (["--check-subscription"], True, False),
        (["--allow-unsubscribe"], False, True),
    ],
)
def test_cli_actions(monkeypatch, capsys, arguments, check, confirm):
    monkeypatch.setattr(probe, "load_config", lambda path: SimpleNamespace(camera=lambda name: object()))

    def run(*args, **kwargs):
        assert kwargs["check_subscription"] is check
        assert kwargs["allow_unsubscribe"] is confirm
        return {"http_status": 403, "subscription_status": "unknown"} if check else {"ready": True}

    monkeypatch.setattr(probe, "unsubscribe", run)
    assert probe.main(arguments) == (1 if check else 0)
    result = json.loads(capsys.readouterr().out)
    if check:
        assert result["subscription_status"] == "unknown"


def test_previous_attempt_has_actionable_safe_cli_result(monkeypatch, capsys):
    monkeypatch.setattr(probe, "load_config", lambda path: SimpleNamespace(camera=lambda name: object()))

    def run(*args, **kwargs):
        raise probe.PreviousRemovalAttempt("SECRET")

    monkeypatch.setattr(probe, "unsubscribe", run)
    assert probe.main(["unsubscribe", "--confirm"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["reason"] == "previous-removal-attempt"
    assert result["network"] is False
    assert "SECRET" not in json.dumps(result)


def test_cloudflare_block_is_reported_without_private_fields():
    result = probe.describe_json_error(
        {
            "cloudflare_error": True,
            "error_code": 1010,
            "retryable": False,
            "detail": "SECRET",
        }
    )
    assert result["blocked_by"] == "cloudflare"
    assert result["cloudflare_code"] == 1010
    assert result["server_retryable"] is False
    assert "SECRET" not in json.dumps(result)


@pytest.mark.parametrize("listener_status", ["accepted", "pending-or-unknown", "not-accepted"])
def test_camera_status_reuses_binding_checks_without_http(setup, monkeypatch, listener_status):
    config, receiver, state = setup
    document = json.loads(state.read_text())
    document["unsubscribe"] = {"status": "not-accepted", "http_status": 403}
    document.update(listener_status=listener_status, listener_created=listener_status == "accepted")
    state.write_text(json.dumps(document))
    before = receiver.read_bytes(), state.read_bytes()
    calls = []

    def inspect_camera(cfg, client_id):
        calls.append((cfg, client_id))
        return {"subscription_status": "subscribed"}

    monkeypatch.setattr(probe, "request_client_state", inspect_camera)
    result = probe.unsubscribe(config, receiver, state, check_subscription=True, via_camera=True)
    assert result["subscription_status"] == "subscribed"
    assert calls == [(config, "PRIVATE-ID")]
    assert (receiver.read_bytes(), state.read_bytes()) == before


@pytest.mark.parametrize("listener_status", ["pending-or-unknown", "not-accepted"])
def test_uncertain_listener_still_cannot_be_deleted(setup, listener_status):
    config, receiver, state = setup
    document = json.loads(state.read_text())
    document.update(listener_status=listener_status, listener_created=False)
    state.write_text(json.dumps(document))
    before = state.read_bytes()
    with pytest.raises(ValueError, match="accepted PC listener"):
        probe.unsubscribe(config, receiver, state, allow_unsubscribe=True)
    assert state.read_bytes() == before


@pytest.mark.parametrize("camera_status", ["subscribed", "unsubscribed", "unknown", "error"])
@pytest.mark.parametrize("code", [204, 403, None])
def test_explicit_retry_and_verification(setup, monkeypatch, camera_status, code):
    config, receiver, state = setup
    document = json.loads(state.read_text())
    document["unsubscribe"] = {"status": "not-accepted", "http_status": 403}
    state.write_text(json.dumps(document))
    original_receiver = receiver.read_bytes()
    calls = []

    def send(client_id, uid):
        calls.append("post")
        assert (client_id, uid) == ("PRIVATE-ID", "TESTUID")
        assert json.loads(state.read_text())["unsubscribe"]["status"] == "pending-or-unknown"
        if code is None:
            raise TimeoutError("PRIVATE")
        return {"http_status": code}

    def inspect_camera(cfg, client_id):
        calls.append("verify")
        assert (cfg, client_id) == (config, "PRIVATE-ID")
        if camera_status == "error":
            raise TimeoutError("PRIVATE")
        return {"subscription_status": camera_status}

    monkeypatch.setattr(probe, "request_client_state", inspect_camera)
    result = probe.unsubscribe(
        config,
        receiver,
        state,
        allow_unsubscribe=True,
        retry_rejected=True,
        verify_via_camera=True,
        send=send,
    )
    assert calls == ["post", "verify"]
    assert result["removal_verified"] is (camera_status == "unsubscribed")
    assert result["removal_accepted"] is (code == 204)
    assert result["camera_connections"] == 1
    assert "PRIVATE" not in json.dumps(result)
    assert receiver.read_bytes() == original_receiver
    saved = json.loads(state.read_text())["unsubscribe"]
    assert saved["attempt"] == 2
    if camera_status == "unsubscribed":
        assert saved["status"] == "verified-removed"
    elif code is None:
        assert saved["status"] == "pending-or-unknown"


@pytest.mark.parametrize(
    "status,code",
    [
        ("pending-or-unknown", None),
        ("accepted", 200),
        ("not-accepted", 500),
        ("verified-removed", 204),
    ],
)
def test_retry_never_repeats_uncertain_or_successful_mutation(setup, status, code):
    config, receiver, state = setup
    document = json.loads(state.read_text())
    document["unsubscribe"] = {"status": status, "http_status": code}
    state.write_text(json.dumps(document))
    before = state.read_bytes()
    with pytest.raises(probe.PreviousRemovalAttempt):
        probe.unsubscribe(config, receiver, state, allow_unsubscribe=True, retry_rejected=True)
    assert state.read_bytes() == before


@pytest.mark.parametrize("option", ["retry_rejected", "verify_via_camera"])
def test_retry_and_verification_need_confirmation(setup, option):
    with pytest.raises(ValueError):
        probe.unsubscribe(*setup, **{option: True})


def test_post_failure_diagnostic_redacts_response(monkeypatch):
    headers = Message()
    headers["Content-Type"] = "application/json"
    body = json.dumps({"cloudflare_error": True, "error_code": 1010, "detail": "PRIVATE-ID"}).encode()

    def open_request(request, timeout):
        raise HTTPError(probe.DELETE_URL, 403, "PRIVATE-ID", headers, BytesIO(body))

    monkeypatch.setattr(probe, "build_opener", lambda *args: SimpleNamespace(open=open_request))
    result = probe.post_removal("PRIVATE-ID", "TESTUID")
    assert result["http_status"] == 403
    assert result["blocked_by"] == "cloudflare"
    assert "PRIVATE-ID" not in json.dumps(result)
