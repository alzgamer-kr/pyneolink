"""Offline coverage for the single-command v2 client-ID experiment."""

import json
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest

from pyneolink.internal import push_registration as probe
from pyneolink.config import CameraConfig


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import socket

    def forbidden(*args, **kwargs):
        raise AssertionError("No network in v2 probe tests")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket.socket, "sendto", forbidden)


def test_payload_matches_observed_serializer():
    root = ET.fromstring(probe.client_id_payload("fake<&token"))
    assert root.tag == "body"
    request = root.find("PushClientID")
    assert request.attrib == {"version": "1.1"}
    assert [(item.tag, item.text) for item in request] == [("clientType", "reo_fcm"), ("pushToken", "fake<&token")]


def test_client_state_payload():
    root = ET.fromstring(probe.client_state_payload("private<&id"))
    request = root.find("PushClientState")
    assert request.attrib == {"version": "1.1"}
    assert request.findtext("clientID") == "private<&id"
    assert len(request.find("channelIdList")) == 0


@pytest.mark.parametrize(
    "channels,expected",
    [
        ("<channelId>TESTUID</channelId>", "subscribed"),
        ("", "unsubscribed"),
        ("<channelId>OTHER</channelId>", "unsubscribed"),
    ],
)
def test_client_state_membership(channels, expected):
    payload = (
        "<body><PushClientState><clientID>private</clientID>"
        f"<channelIdList>{channels}</channelIdList></PushClientState></body>"
    ).encode()
    result = probe.parse_client_state(payload, "private", "TESTUID")
    assert result["subscription_status"] == expected
    assert "private" not in json.dumps(result)


@pytest.mark.parametrize(
    "fragment",
    [
        "<clientID>wrong</clientID><channelIdList/>",
        "<clientID>private</clientID>",
        "<clientID>private</clientID><channelIdList/><channelIdList/>",
        "<clientID>private</clientID><channelIdList><wrong>TESTUID</wrong></channelIdList>",
        "<clientID>private</clientID><channelIdList>TESTUID</channelIdList>",
    ],
)
def test_incomplete_client_state_is_not_unsubscribed(fragment):
    with pytest.raises(ValueError):
        probe.parse_client_state(
            f"<body><PushClientState>{fragment}</PushClientState></body>".encode(), "private", "TESTUID"
        )


@pytest.mark.parametrize("outcome", ["ok", "rejected", "wrong-id", "malformed", "enter-failure"])
def test_camera_status_one_command_and_cleanup(outcome):
    calls = []
    closed = []

    class FakeCamera:
        def __init__(self, config, **kwargs):
            assert kwargs == {"debug": False, "state_path": None}

        def __enter__(self):
            if outcome == "enter-failure":
                raise TimeoutError("private")
            return self

        def __exit__(self, *args):
            pass

        def close(self):
            closed.append(True)

        def command(self, msg_id, payload, **kwargs):
            assert msg_id == 526
            assert kwargs == {"retry_on_timeout": False, "reconnect_retries": 0}
            calls.append(msg_id)
            return SimpleNamespace(
                header=SimpleNamespace(
                    msg_id=524 if outcome == "wrong-id" else 526, response_code=400 if outcome == "rejected" else 200
                ),
                payload=b"bad"
                if outcome == "malformed"
                else (
                    b"<body><PushClientState><clientID>private</clientID><channelIdList>"
                    b"<channelId>TESTUID</channelId></channelIdList></PushClientState></body>"
                ),
            )

    config = CameraConfig(name=probe.APPROVED_CAMERA, uid="TESTUID")
    if outcome == "enter-failure":
        with pytest.raises(TimeoutError):
            probe.request_client_state(config, "private", camera_factory=FakeCamera)
    else:
        result = probe.request_client_state(config, "private", camera_factory=FakeCamera)
        assert result["subscription_status"] == ("subscribed" if outcome == "ok" else "unknown")
        assert calls == [526]
        assert "private" not in json.dumps(result)
    assert closed == [True]


@pytest.mark.parametrize(
    "token", ["", "x" * 256, "bad token", "\r\nsecret", None], ids=["empty", "long", "space", "newline", "none"]
)
def test_invalid_token(token):
    with pytest.raises(ValueError):
        probe.client_id_payload(token)


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"broken",
        b"<body/>",
        b"<PushClientID><clientID>id</clientID></PushClientID>",
        b"<body><PushClientID><clientID/></PushClientID></body>",
        b"<body><PushClientID><clientID>a</clientID><clientID>b</clientID></PushClientID></body>",
        b"<body><PushClientID><clientID>a b</clientID></PushClientID></body>",
        b"<body><PushClientID><clientID><child/></clientID></PushClientID></body>",
        b"x" * 65537,
    ],
    ids=["empty", "malformed", "missing", "root", "empty-id", "duplicate", "space", "nested", "oversize"],
)
def test_invalid_response(payload):
    with pytest.raises((ValueError, ET.ParseError)):
        probe.parse_client_id(payload)


@pytest.mark.parametrize("allow,channel", [(False, 0), (True, 1)])
def test_registration_guards(tmp_path, allow, channel):
    def forbidden(*args, **kwargs):
        pytest.fail("Camera constructed")

    with pytest.raises(ValueError):
        probe.request_client_id(
            CameraConfig(name="TEST", channel_id=channel),
            "fake-token",
            tmp_path / "out.json",
            allow_registration=allow,
            camera_factory=forbidden,
        )


@pytest.mark.parametrize("outcome", ["ok", "rejected", "wrong-id", "bad-xml", "enter", "command"])
def test_single_request_persistence_and_cleanup(tmp_path, outcome, capsys):
    cameras = []
    target = tmp_path / "v2.json"

    class FakeCamera:
        def __init__(self, config, **kwargs):
            assert kwargs == {"debug": False, "state_path": None}
            self.closed = False
            self.calls = []
            cameras.append(self)

        def __enter__(self):
            if outcome == "enter":
                raise TimeoutError("SECRET")
            return self

        def __exit__(self, *args):
            self.close()

        def close(self):
            self.closed = True

        def command(self, msg_id, payload, **kwargs):
            self.calls.append(msg_id)
            assert msg_id == 522
            assert kwargs == {"retry_on_timeout": False, "reconnect_retries": 0}
            if outcome == "command":
                raise TimeoutError("SECRET")
            return SimpleNamespace(
                header=SimpleNamespace(
                    msg_id=523 if outcome == "wrong-id" else 522, response_code=55534 if outcome == "rejected" else 200
                ),
                payload=b"broken"
                if outcome == "bad-xml"
                else b'<body><PushClientID version="1.1"><clientID>PRIVATE-ID</clientID></PushClientID></body>',
            )

    def run():
        return probe.request_client_id(
            CameraConfig(name=probe.APPROVED_CAMERA),
            "fake-token",
            target,
            allow_registration=True,
            camera_factory=FakeCamera,
        )

    if outcome in ("enter", "command"):
        with pytest.raises(TimeoutError):
            run()
    else:
        result = run()
        assert result["client_id_saved"] is (outcome == "ok")
        assert result["listener_created"] is False
        assert "PRIVATE-ID" not in json.dumps(result)
    assert cameras[0].closed
    assert cameras[0].calls == ([] if outcome == "enter" else [522])
    assert not target.with_suffix(".running").exists()
    assert target.exists() is (outcome == "ok")
    if outcome == "ok":
        data = target.read_bytes()
        assert json.loads(data)["client_id"] == "PRIVATE-ID"
        assert "fake-token" not in data.decode()
        with pytest.raises(FileExistsError):
            run()
        assert len(cameras) == 1
        assert target.read_bytes() == data
    assert capsys.readouterr().out == ""


def test_cli_dry_run_and_credentials_preserved(tmp_path, capsys):
    path = tmp_path / "receiver.json"
    state = {
        "purpose": probe.PURPOSE,
        "client_id": "A" * 32,
        "fcm_token": "SECRET",
        "fcm_credentials": {"fcm": {"registration": {"token": "SECRET"}}},
    }
    path.write_text(json.dumps(state))
    before = path.read_bytes()
    out = tmp_path / "v2.json"
    assert probe.main(["--credentials", str(path), "--out", str(out)]) == 0
    assert not out.exists()
    assert path.read_bytes() == before
    assert "SECRET" not in capsys.readouterr().out
    assert probe.main(["--credentials", str(path), "--out", str(path), "--allow-registration"]) == 1
    assert path.read_bytes() == before


def test_listener_payload_uid_and_empty_tag():
    root = ET.fromstring(probe.listener_payload("PRIVATE<&ID", "TESTCAMERA"))
    request = root.find("PushCreateListener")
    assert request.attrib == {"version": "1.1"}
    assert [node.tag for node in request] == ["clientID", "tag", "channelId"]
    assert request.findtext("clientID") == "PRIVATE<&ID"
    assert request.findtext("tag") == ""
    assert request.findtext("channelId") == "TESTCAMERA"


@pytest.fixture
def listener_state(tmp_path):
    path = tmp_path / "v2.json"
    state = {
        "purpose": probe.V2_PURPOSE,
        "camera": probe.APPROVED_CAMERA,
        "client_id": "PRIVATE-ID",
        "fcm_token_sha256": probe.hashlib.sha256(b"fake-token").hexdigest(),
        "listener_created": False,
    }
    path.write_text(json.dumps(state))
    return path, state


@pytest.mark.parametrize("fault", ["approval", "camera", "channel", "uid", "purpose", "token", "created", "pending"])
def test_listener_guards_before_network(listener_state, fault):
    path, state = listener_state
    config = CameraConfig(name=probe.APPROVED_CAMERA, uid="TESTCAMERA")
    allow = True
    token = "fake-token"
    if fault == "approval":
        allow = False
    elif fault == "camera":
        config.name = "Friend"
    elif fault == "channel":
        config.channel_id = 1
    elif fault == "uid":
        config.uid = None
    elif fault == "purpose":
        state["purpose"] = "wrong"
    elif fault == "token":
        token = "other-token"
    elif fault == "created":
        state["listener_created"] = True
    elif fault == "pending":
        state["listener_status"] = "pending-or-unknown"
    path.write_text(json.dumps(state))
    original = path.read_bytes()

    def forbidden(*args, **kwargs):
        pytest.fail("Camera must not be constructed")

    with pytest.raises(ValueError):
        probe.create_listener(config, token, path, allow_registration=allow, camera_factory=forbidden)
    assert path.read_bytes() == original


@pytest.mark.parametrize("outcome", ["ok", "rejected", "wrong-id", "timeout", "enter"])
@pytest.mark.parametrize("resubscribe", [False, True])
def test_listener_single_send_and_persisted_uncertainty(listener_state, outcome, capsys, resubscribe):
    path, initial = listener_state
    if resubscribe:
        initial.update(
            listener_created=True,
            listener_status="accepted",
            camera_uid="TESTCAMERA",
            unsubscribe={
                "status": "verified-removed",
                "verification": {
                    "subscription_status": "unsubscribed",
                    "verified_by": "camera-client-state",
                },
            },
        )
        path.write_text(json.dumps(initial))
    cameras = []

    class FakeCamera:
        def __init__(self, config, **kwargs):
            assert kwargs == {"debug": False, "state_path": None}
            self.closed = False
            self.calls = []
            cameras.append(self)

        def __enter__(self):
            if outcome == "enter":
                raise TimeoutError("SECRET")
            return self

        def __exit__(self, *args):
            self.close()

        def close(self):
            self.closed = True

        def command(self, msg_id, payload, **kwargs):
            assert json.loads(path.read_text())["listener_status"] == "pending-or-unknown"
            self.calls.append(msg_id)
            assert msg_id == 524
            assert kwargs == {"retry_on_timeout": False, "reconnect_retries": 0}
            assert ET.fromstring(payload).findtext("PushCreateListener/channelId") == "TESTCAMERA"
            if outcome == "timeout":
                raise TimeoutError("SECRET")
            return SimpleNamespace(
                header=SimpleNamespace(
                    msg_id=523 if outcome == "wrong-id" else 524, response_code=400 if outcome == "rejected" else 200
                )
            )

    def run():
        return probe.create_listener(
            CameraConfig(name=probe.APPROVED_CAMERA, uid="TESTCAMERA"),
            "fake-token",
            path,
            allow_registration=True,
            camera_factory=FakeCamera,
        )

    if outcome in ("timeout", "enter"):
        with pytest.raises(TimeoutError):
            run()
    else:
        result = run()
        assert result["listener_created"] is (outcome == "ok")
        assert result["delivery_proven"] is False
        assert "PRIVATE-ID" not in json.dumps(result)
    state = json.loads(path.read_text())
    assert state["client_id"] == initial["client_id"]
    assert state["fcm_token_sha256"] == initial["fcm_token_sha256"]
    assert cameras[0].closed
    assert cameras[0].calls == ([] if outcome == "enter" else [524])
    if outcome == "enter":
        assert state == initial
    else:
        if resubscribe:
            assert "unsubscribe" not in state
            assert state["last_unsubscribe"] == initial["unsubscribe"]
        assert state["listener_status"] == {"ok": "accepted", "timeout": "pending-or-unknown"}.get(
            outcome, "not-accepted"
        )
        with pytest.raises(ValueError):
            run()
        assert len(cameras) == 1
    assert not path.with_suffix(".running").exists()
    assert capsys.readouterr().out == ""
