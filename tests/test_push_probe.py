"""Offline coverage for the unified experimental push CLI."""

import hashlib
import json
from types import SimpleNamespace

import pytest

from pyneolink.internal import push_lifecycle as probe
from pyneolink.config import CameraConfig


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    import socket

    def forbidden(*args, **kwargs):
        pytest.fail("No real camera/cloud connections in CLI tests")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "sendto", forbidden)


@pytest.fixture
def identity(tmp_path):
    credentials = tmp_path / "receiver.json"
    credentials.write_text(
        json.dumps(
            {
                "purpose": probe.PURPOSE,
                "client_id": "A" * 32,
                "fcm_token": "FAKE-TOKEN",
                "fcm_credentials": {"fcm": {"registration": {"token": "FAKE-TOKEN"}}},
            }
        )
    )
    state = tmp_path / "v2.json"
    document = {
        "purpose": probe.registration.V2_PURPOSE,
        "camera": probe.APPROVED_CAMERA,
        "client_id": "PRIVATE-ID",
        "listener_created": False,
        "fcm_token_sha256": hashlib.sha256(b"FAKE-TOKEN").hexdigest(),
    }
    return CameraConfig(name=probe.APPROVED_CAMERA, uid="TESTUID"), credentials, state, document


@pytest.mark.parametrize("exists", [False, True])
def test_subscribe_preflight_is_offline(identity, exists):
    config, credentials, state, document = identity
    if exists:
        state.write_text(json.dumps(document))
    before = credentials.read_bytes()
    result = probe.subscribe(config, credentials, state)
    assert result["planned_msg_ids"] == ([526, 524] if exists else [522, 524])
    assert result["network"] is False
    assert state.exists() is exists
    assert credentials.read_bytes() == before


def test_subscribe_orders_steps_and_preserves_credentials(identity, monkeypatch):
    config, credentials, state, document = identity
    before = credentials.read_bytes()
    calls = []

    def client_id(cfg, token, output, *, allow_registration):
        assert cfg is config and token == "FAKE-TOKEN" and allow_registration
        calls.append(522)
        output.write_text(json.dumps(document))
        return {"client_id_saved": True}

    def listener(cfg, token, output, *, allow_registration):
        assert output.exists() and allow_registration
        calls.append(524)
        return {"listener_created": True}

    monkeypatch.setattr(probe.registration, "request_client_id", client_id)
    monkeypatch.setattr(probe.registration, "create_listener", listener)
    assert probe.subscribe(config, credentials, state, confirm=True)["listener_created"]
    assert calls == [522, 524]
    assert credentials.read_bytes() == before


def test_failed_identity_does_not_create_listener(identity, monkeypatch):
    config, credentials, state, _ = identity
    monkeypatch.setattr(probe.registration, "request_client_id", lambda *a, **kw: {"client_id_saved": False})
    monkeypatch.setattr(probe.registration, "create_listener", lambda *a, **kw: pytest.fail("No 524 after failure"))
    assert probe.subscribe(config, credentials, state, confirm=True)["stage"] == "client-id"


@pytest.mark.parametrize("status", ["accepted", "pending-or-unknown", "not-accepted", "unsubscribe"])
def test_existing_attempt_is_never_repeated(identity, status):
    config, credentials, state, document = identity
    document.update(listener_status=status, listener_created=status == "accepted", camera_uid=config.uid)
    if status == "unsubscribe":
        document["unsubscribe"] = {"http_status": 403}
    state.write_text(json.dumps(document))
    before = state.read_bytes()
    if status in ("pending-or-unknown", "not-accepted"):
        with pytest.raises(ValueError):
            probe.subscribe(config, credentials, state)
    else:
        result = probe.subscribe(config, credentials, state)
        assert result["network"] is False
        assert result.get("blocked") or result["server_status_verified"] is False
    assert state.read_bytes() == before


@pytest.mark.parametrize("fault", ["token", "camera", "channel", "uid", "same-path", "running"])
def test_subscribe_guards(identity, fault):
    config, credentials, state, document = identity
    document["camera_uid"] = config.uid
    if fault == "token":
        document["fcm_token_sha256"] = "wrong"
    elif fault == "camera":
        config.name = "Friend"
    elif fault == "channel":
        config.channel_id = 1
    elif fault == "uid":
        document["camera_uid"] = "OTHER"
    elif fault == "running":
        credentials.with_suffix(".running").write_text("1")
    state.write_text(json.dumps(document))
    if fault == "same-path":
        state = credentials
    with pytest.raises((ValueError, FileExistsError)):
        probe.subscribe(config, credentials, state, confirm=True)


@pytest.mark.parametrize(
    "arguments",
    [
        ["status"],
        ["status", "--save-diagnostic"],
        ["unsubscribe"],
        ["unsubscribe", "--confirm"],
        ["unsubscribe", "--confirm", "--retry-rejected", "--verify-via-camera"],
    ],
)
def test_status_and_unsubscribe_delegate_without_camera(monkeypatch, arguments):
    calls = []
    monkeypatch.setattr(probe.removal, "main", lambda args: calls.append(args) or 0)
    assert probe.main(arguments) == 0
    assert len(calls) == 1
    assert calls[0][0] == arguments[0]
    assert ("--confirm" in calls[0]) == ("--confirm" in arguments)
    for flag in ("--retry-rejected", "--verify-via-camera"):
        assert (flag in calls[0]) == (flag in arguments)


def test_status_rejects_mutation_flag():
    with pytest.raises(SystemExit) as exc:
        probe.main(["status", "--confirm"])
    assert exc.value.code == 2


def test_subscribe_reuses_identity_after_verified_removal(identity, monkeypatch):
    config, credentials, state, document = identity
    document.update(listener_created=True, listener_status="accepted", camera_uid=config.uid)
    document["unsubscribe"] = {
        "status": "verified-removed",
        "verification": {
            "subscription_status": "unsubscribed",
            "verified_by": "camera-client-state",
        },
    }
    state.write_text(json.dumps(document))
    originals = credentials.read_bytes(), state.read_bytes()
    assert probe.subscribe(config, credentials, state)["planned_msg_ids"] == [526, 524]
    assert (credentials.read_bytes(), state.read_bytes()) == originals
    monkeypatch.setattr(probe.registration, "request_client_id", lambda *a, **kw: pytest.fail("No new identity"))
    monkeypatch.setattr(probe.registration, "request_client_state", lambda *a: {"subscription_status": "unsubscribed"})
    calls = []
    monkeypatch.setattr(
        probe.registration,
        "create_listener",
        lambda *a, **kw: (
            calls.append(a)
            or {
                "listener_created": True,
            }
        ),
    )
    assert probe.subscribe(config, credentials, state, confirm=True)["listener_created"]
    assert len(calls) == 1


@pytest.mark.parametrize(
    "local,remote",
    [
        (local, remote)
        for local in ("accepted", "pending-or-unknown", "not-accepted", "removed")
        for remote in ("subscribed", "unsubscribed", "unknown", "timeout")
        if (local, remote) != ("removed", "unsubscribed")
    ],
)
def test_subscribe_checks_existing_state_without_repeating_creation(identity, monkeypatch, local, remote):
    config, credentials, path, state = identity
    state.update(listener_created=local == "accepted", listener_status=local, camera_uid=config.uid)
    if local == "removed":
        state["unsubscribe"] = {"status": "not-accepted", "http_status": 403}
    path.write_text(json.dumps(state))
    before = path.read_bytes()
    credentials_before = credentials.read_bytes()
    calls = []

    def inspect(cfg, client_id):
        assert cfg == config and client_id == "PRIVATE-ID"
        calls.append(526)
        if remote == "timeout":
            raise TimeoutError("PRIVATE-ID")
        return {"subscription_status": remote, "verified_by": "camera-client-state"}

    monkeypatch.setattr(probe.registration, "request_client_state", inspect)
    monkeypatch.setattr(probe.registration, "create_listener", lambda *a, **kw: pytest.fail("No second 524"))
    result = probe.subscribe(config, credentials, path, confirm=True)
    assert calls == [526]
    assert result["create_request_sent"] is False
    assert "PRIVATE-ID" not in json.dumps(result)
    assert credentials.read_bytes() == credentials_before
    if remote == "subscribed":
        assert result["outcome"] == "already-subscribed"
        saved = json.loads(path.read_text())
        assert saved["listener_status"] == "accepted"
        assert saved["listener_created"] is True
        assert "unsubscribe" not in saved
    else:
        assert result["outcome"] in ("not-subscribed", "verification-unavailable")
        assert path.read_bytes() == before


@pytest.mark.parametrize("remote", ["subscribed", "unsubscribed", "timeout"])
def test_creation_timeout_is_verified_not_repeated(identity, monkeypatch, remote):
    config, credentials, path, state = identity
    path.write_text(json.dumps(state))
    calls = []

    def inspect(cfg, client_id):
        calls.append(526)
        if len(calls) == 1:
            return {"subscription_status": "unsubscribed"}
        if remote == "timeout":
            raise TimeoutError("SECRET")
        return {"subscription_status": remote, "verified_by": "camera-client-state"}

    def create(*args, **kwargs):
        calls.append(524)
        state.update(listener_status="pending-or-unknown", camera_uid=config.uid)
        path.write_text(json.dumps(state))
        raise TimeoutError("SECRET")

    monkeypatch.setattr(probe.registration, "request_client_state", inspect)
    monkeypatch.setattr(probe.registration, "create_listener", create)
    result = probe.subscribe(config, credentials, path, confirm=True)
    assert calls == [526, 524, 526]
    assert result["outcome"] == ("subscribed-after-timeout" if remote == "subscribed" else "creation-unconfirmed")
    assert result["subscription_verified"] is (remote == "subscribed")
    assert "SECRET" not in json.dumps(result)
    assert json.loads(path.read_text())["listener_status"] == (
        "accepted" if remote == "subscribed" else "pending-or-unknown"
    )


@pytest.mark.parametrize(
    "outcome,verified,exit_code",
    [
        ("already-subscribed", True, 0),
        ("subscribed-after-timeout", True, 0),
        ("verification-unavailable", False, 1),
        ("creation-unconfirmed", False, 1),
    ],
)
def test_subscribe_cli_reports_verified_outcomes(monkeypatch, capsys, outcome, verified, exit_code):
    monkeypatch.setattr(probe, "load_config", lambda *a: SimpleNamespace(camera=lambda *a: object()))
    monkeypatch.setattr(
        probe,
        "subscribe",
        lambda *a, **kw: {
            "outcome": outcome,
            "subscription_verified": verified,
        },
    )
    assert probe.main(["subscribe", "--confirm"]) == exit_code
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "subscribe-result"
    assert output["outcome"] == outcome


@pytest.mark.parametrize("removal_status", ["accepted", "not-accepted", "pending-or-unknown"])
@pytest.mark.parametrize("creation_timeout", [False, True])
def test_subscribe_verifies_prior_removal_then_creates_once(identity, monkeypatch, removal_status, creation_timeout):
    config, credentials, path, state = identity
    state.update(listener_status="accepted", listener_created=True, camera_uid=config.uid)
    state["unsubscribe"] = {"status": removal_status, "http_status": 200}
    path.write_text(json.dumps(state))
    original_credentials = credentials.read_bytes()
    calls = []
    closed = []

    def inspect(*args):
        calls.append(526)
        return {"subscription_status": "unsubscribed", "verified_by": "camera-client-state"}

    class Camera:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def close(self):
            closed.append(True)

        def command(self, msg_id, payload, **kwargs):
            calls.append(msg_id)
            saved = json.loads(path.read_text())
            assert saved["last_unsubscribe"]["status"] == "verified-removed"
            assert saved["listener_status"] == "pending-or-unknown"
            assert "unsubscribe" not in saved
            if creation_timeout:
                raise TimeoutError("SECRET")
            return SimpleNamespace(header=SimpleNamespace(msg_id=524, response_code=200))

    create = probe.registration.create_listener
    monkeypatch.setattr(probe.registration, "request_client_state", inspect)
    monkeypatch.setattr(probe.registration, "create_listener", lambda *a, **kw: create(*a, **kw, camera_factory=Camera))
    result = probe.subscribe(config, credentials, path, confirm=True)
    assert calls == ([526, 524, 526] if creation_timeout else [526, 524])
    assert closed == [True]
    assert result["outcome"] == ("creation-unconfirmed" if creation_timeout else "subscription-accepted")
    saved = json.loads(path.read_text())
    assert saved["listener_status"] == ("pending-or-unknown" if creation_timeout else "accepted")
    assert saved["client_id"] == state["client_id"]
    assert credentials.read_bytes() == original_credentials


@pytest.mark.parametrize("command", ["watch", "read"])
@pytest.mark.parametrize("backlog", [False, True])
def test_watch_only_delegates_to_existing_fcm_receiver(monkeypatch, command, backlog):
    calls = []
    monkeypatch.setattr(probe.receiver_probe, "main", lambda args: calls.append(args) or 0)
    monkeypatch.setattr(probe, "load_config", lambda *args: pytest.fail("Watch must not load camera config"))
    args = [command, "--duration", "1800"] + (["--no-skip-previous"] if backlog else [])
    assert probe.main(args) == 0
    assert len(calls) == 1
    assert "--existing-only" in calls[0]
    assert "--allow-network" in calls[0]
    assert ("--no-skip-previous" if backlog else "--skip-previous") in calls[0]
