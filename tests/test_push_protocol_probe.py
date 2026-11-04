"""Offline contracts only: synthetic data and fake cameras, no FCM traffic."""

import json
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest

from examples.push_protocol_probe import (
    APPROVED_CAMERA,
    MAX_EVENT_LINE,
    PUSH_INFO_ID,
    legacy_payload,
    main,
    normalize_event,
    register_legacy,
    replay,
)
from pyneolink.config import CameraConfig

CLIENT_ID = "0123456789ABCDEF0123456789ABCDEF"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import socket

    def forbidden(*args, **kwargs):
        raise AssertionError("Push unit tests must not use the network")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def test_legacy_xml_structure_and_escaping():
    root = ET.fromstring(legacy_payload("fake<&>token", CLIENT_ID))
    assert root.tag == "body"
    assert root.findtext("PushInfo/token") == "fake<&>token"
    assert root.findtext("PushInfo/phoneType") == "reo_fcm"
    assert root.findtext("PushInfo/clientID") == CLIENT_ID
    assert len(root) == 1


@pytest.mark.parametrize("token", ["", None, "a\nb", "a b", "x" * 4097, "a\x00b"])
def test_invalid_token(token):
    with pytest.raises(ValueError):
        legacy_payload(token, CLIENT_ID)


@pytest.mark.parametrize("client_id", [None, "", "arbitrary", CLIENT_ID.lower()])
def test_invalid_client_id(client_id):
    with pytest.raises(ValueError):
        legacy_payload("fake-token", client_id)


def test_legacy_event_preserves_raw_times():
    event = normalize_event({"pushVersion": "v2", "UID": "test", "ALMTYPE": "OTHER", "ALMTIME": "123"})
    assert event["camera_uid"] == "test"
    assert event["event_type"] == "OTHER"
    assert event["device_time_raw"] == "123"
    assert event["server_time_raw"] is None


@pytest.mark.parametrize("version", ["v2a", "system"])
def test_modern_event_is_not_assumed_to_be_motion(version):
    event = normalize_event({"pushVersion": version, "type": "unrecognized-kind", "dup": "1"})
    assert event["event_type"] == "unrecognized-kind"
    assert event["camera_uid"] is None
    assert event["duplicate_hint"] is True


@pytest.mark.parametrize(
    "data", [{}, {"pushVersion": "future"}, {"pushVersion": "v2", "UID": []}, {"pushVersion": "v2a", "dup": "yes"}]
)
def test_unsupported_or_malformed_event(data):
    with pytest.raises(ValueError):
        normalize_event(data)


def test_no_sensitive_extra_fields_in_output():
    event = normalize_event({"pushVersion": "system", "token": "SECRET", "imageUrl": "SECRET", "body": "SECRET"})
    assert "SECRET" not in json.dumps(event)


def test_replay_synthetic_fixture(capsys):
    assert replay(Path(__file__).parent / "fixtures/push/events.jsonl") == 0
    assert "Offline events parsed: 4" in capsys.readouterr().out


def test_replay_line_size_is_bounded(tmp_path):
    path = tmp_path / "large.jsonl"
    path.write_text("x" * (MAX_EVENT_LINE + 1), encoding="utf-8")
    with pytest.raises(ValueError):
        replay(path)


@pytest.mark.parametrize("allow,name", [(False, APPROVED_CAMERA), (True, "Friend"), (True, "other")])
def test_registration_guard_precedes_camera_construction(allow, name):
    def forbidden(*args, **kwargs):
        pytest.fail("Camera must not be constructed")

    with pytest.raises(ValueError):
        register_legacy(
            CameraConfig(name=name), "fake-token", CLIENT_ID, allow_registration=allow, camera_factory=forbidden
        )


@pytest.mark.parametrize("response", [200, 400])
@pytest.mark.parametrize("failure", [None, "enter", "command"])
def test_single_session_closed_on_success_rejection_and_failure(response, failure):
    cameras = []

    class FakeCamera:
        def __init__(self, config, **kwargs):
            assert kwargs == {"debug": False, "state_path": None}
            self.closed = False
            self.calls = []
            cameras.append(self)

        def __enter__(self):
            if failure == "enter":
                raise TimeoutError("fake")
            return self

        def __exit__(self, *args):
            self.close()

        def close(self):
            self.closed = True

        def command(self, msg_id, payload, **kwargs):
            self.calls.append(msg_id)
            assert msg_id == PUSH_INFO_ID
            assert kwargs == {"retry_on_timeout": False, "reconnect_retries": 0}
            assert ET.fromstring(payload).findtext("PushInfo/token") == "fake-token"
            if failure == "command":
                raise TimeoutError("fake")
            return SimpleNamespace(header=SimpleNamespace(msg_id=msg_id, response_code=response))

    def run():
        return register_legacy(
            CameraConfig(name=APPROVED_CAMERA),
            "fake-token",
            CLIENT_ID,
            allow_registration=True,
            camera_factory=FakeCamera,
        )

    if failure:
        with pytest.raises(TimeoutError):
            run()
    else:
        assert run()["response_code"] == response
    assert len(cameras) == 1
    assert cameras[0].closed
    assert len(cameras[0].calls) == (0 if failure == "enter" else 1)


def test_cli_requires_confirmation_before_reading_credentials():
    with pytest.raises(SystemExit) as exc:
        main(["register-legacy", "--credentials", "does-not-exist.json"])
    assert exc.value.code == 2


def test_cli_does_not_print_credentials_on_parse_failure(tmp_path, capsys):
    path = tmp_path / "credentials.json"
    path.write_text('{"fcm_token": "SECRET", broken', encoding="utf-8")
    assert main(["register-legacy", "--credentials", str(path), "--allow-registration"]) == 1
    output = capsys.readouterr()
    assert "SECRET" not in output.out + output.err
