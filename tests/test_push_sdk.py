"""Public push API/CLI checks with no physical camera or cloud traffic."""

import asyncio
import errno
import hashlib
import json
import time
from contextlib import nullcontext
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from pyneolink import PushClient, PushError, PushEvent, PushResult, push
from pyneolink.cli import CLI
from pyneolink.config import CameraConfig
from pyneolink.core.storage import read_document


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import socket

    connect = socket.socket.connect

    def local_only(sock, address):
        if isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1"):
            return connect(sock, address)
        pytest.fail("No camera or internet traffic in SDK tests")

    monkeypatch.setattr(socket.socket, "connect", local_only)
    monkeypatch.setattr(socket.socket, "sendto", lambda *a: pytest.fail("No UDP"))


@pytest.fixture
def client(tmp_path):
    item = PushClient(state_dir=tmp_path, camera_name="TEST")
    config = {key: "TEST" for key in ("project_id", "app_id", "api_key", "messaging_sender_id", "bundle_id")}
    item.store.put("push.firebase", config)
    item.store.put("push.receiver", {})
    return item


def notification():
    return {
        "time": datetime.now(timezone.utc).isoformat(),
        "status": "event",
        "event": {"push_version": "v2", "camera_uid": "TEST", "channel_id": "TEST", "event_type": "PEOPLE"},
    }


def test_watch_has_no_camera_and_is_silent(client, monkeypatch, capsys):
    calls = []

    async def receive(config, path, duration, **kwargs):
        calls.append((path, duration, kwargs))
        push.fcm.EVENT_SINK.get()(notification())
        return 0

    monkeypatch.setattr(push.fcm, "receive", receive)
    with client.watch() as events:
        result = list(events)
    assert len(result) == 1 and isinstance(result[0], PushEvent)
    assert result[0].event_type == "PEOPLE"
    assert calls[0][1] is None
    assert calls[0][2]["existing_only"] is True
    assert calls[0][2]["skip_previous"] is True
    assert events._thread is not None and not events._thread.is_alive()
    assert not list(client.state_dir.glob("*.running"))
    assert capsys.readouterr().out == ""


def test_watch_close_cancels_wait_and_keeps_credentials(client, monkeypatch):
    before = client.store.get("push.receiver")
    cleaned = []

    async def receive(*args, **kwargs):
        push.fcm.EVENT_SINK.get()(notification())
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.append(True)

    monkeypatch.setattr(push.fcm, "receive", receive)
    with client.watch(skip_previous=False) as events:
        assert next(events).camera_uid == "TEST"
    assert cleaned == [True]
    assert client.store.get("push.receiver") == before
    assert not events._thread.is_alive()


def test_watch_interrupted_cleanup_releases_lease_before_restart(client, monkeypatch):
    cleaned = []

    async def receive(*args, **kwargs):
        push.fcm.EVENT_SINK.get()(notification())
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0.05)
            cleaned.append(True)

    monkeypatch.setattr(push.fcm, "receive", receive)
    events = client.watch()
    assert next(events).event_type == "PEOPLE"
    interrupted = []

    def interrupt_once(wait):
        def wrapper(*args, **kwargs):
            if not interrupted:
                interrupted.append(True)
                raise KeyboardInterrupt
            return wait(*args, **kwargs)

        return wrapper

    # Exercise the old join path as well as the explicit cleanup-event path.
    monkeypatch.setattr(events._thread, "join", interrupt_once(events._thread.join))
    monkeypatch.setattr(events._done, "wait", interrupt_once(events._done.wait))
    try:
        with pytest.raises(KeyboardInterrupt):
            events.close()
        assert events._done.is_set()
        assert cleaned == [True]
        assert client.store.get("push.receiver") == {}
        with client.watch() as restarted:
            assert next(restarted).event_type == "PEOPLE"
        assert cleaned == [True, True]
    finally:
        events.close()


def test_watch_interrupted_start_releases_lease(client, monkeypatch):
    start = push.threading.Thread.start

    def interrupted_start(thread):
        start(thread)
        if thread.name == "pyneolink-push":
            raise KeyboardInterrupt

    async def receive(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(push.fcm, "receive", receive)
    monkeypatch.setattr(push.threading.Thread, "start", interrupted_start)
    events = client.watch()
    try:
        with pytest.raises(KeyboardInterrupt):
            with events:
                pytest.fail("Interrupted entry must not enter the body")
        assert events._done.is_set()
        with client.store.lease("push.receiver"):
            pass
    finally:
        events.close()


def test_watch_stopping_one_camera_preserves_other_receiver(client, tmp_path, monkeypatch):
    other = PushClient(state_dir=tmp_path, camera_name="OTHER")
    other.store.put("push.firebase", client.store.get("push.firebase"))
    other.store.put("push.receiver", {})

    async def receive(*args, **kwargs):
        push.fcm.EVENT_SINK.get()(notification())
        await asyncio.Event().wait()

    monkeypatch.setattr(push.fcm, "receive", receive)
    with other.watch() as active:
        assert next(active).event_type == "PEOPLE"
        for _ in range(2):
            with client.watch() as events:
                assert next(events).event_type == "PEOPLE"
            assert events._done.is_set()
            assert not active._done.is_set()
            with pytest.raises(FileExistsError):
                with other.store.lease("push.receiver"):
                    pytest.fail("An active camera lease must not be removed")


def test_watch_bounded_queue_fails_instead_of_growing(client, monkeypatch):
    async def receive(*args, **kwargs):
        for _ in range(129):
            push.fcm.EVENT_SINK.get()(notification())
        return 0

    monkeypatch.setattr(push.fcm, "receive", receive)
    with client.watch() as events:
        assert events._done.wait(5)
        assert events._events.qsize() == 128
        with pytest.raises(PushError, match="queue is full"):
            list(events)


def test_watch_network_failure_retries_without_resetting_cutoff(client, monkeypatch):
    calls = []
    sleeps = []

    async def receive(*args, **kwargs):
        calls.append(kwargs["started_at"])
        if len(calls) < 3:
            raise ConnectionError("SECRET")
        push.fcm.EVENT_SINK.get()(notification())
        return 0

    async def sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(push.fcm, "receive", receive)
    monkeypatch.setattr(push.asyncio, "sleep", sleep)
    with client.watch(reconnect_delay=30) as events:
        assert len(list(events)) == 1
    assert calls[0] == calls[1] == calls[2]
    assert sleeps == [30, 60]


@pytest.mark.parametrize("error", [PermissionError(), FileNotFoundError(), OSError(errno.ENOSPC, "full"), ValueError()])
def test_watch_does_not_retry_local_failures(client, monkeypatch, error):
    calls = []

    async def receive(*args, **kwargs):
        calls.append(True)
        raise error

    monkeypatch.setattr(push.fcm, "receive", receive)
    with client.watch() as events:
        with pytest.raises(PushError):
            list(events)
    assert calls == [True]


def test_duration_bounds_connection_setup(client, monkeypatch):
    cleaned = []

    async def receive(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.append(True)

    monkeypatch.setattr(push.fcm, "receive", receive)
    with client.watch(duration=0.05) as events:
        assert list(events) == []
    assert cleaned == [True]


@pytest.mark.parametrize("duration", [0, -1, float("inf"), float("nan")])
def test_invalid_duration(client, duration):
    with pytest.raises(ValueError):
        client.watch(duration=duration)


@pytest.mark.parametrize("delay", [0, -1, 301, float("inf")])
def test_invalid_reconnect_delay(client, delay):
    with pytest.raises(ValueError):
        client.watch(reconnect_delay=delay)


def test_watch_never_initializes_missing_credentials(client):
    client.store.put("push.receiver", None)
    with pytest.raises(ValueError, match="never registers"):
        with client.watch():
            pass


def test_public_methods_return_structured_results(client, monkeypatch):
    client.config = SimpleNamespace(name="TEST")
    monkeypatch.setattr(
        push.lifecycle,
        "subscribe",
        lambda *a, **kw: {
            "outcome": "already-subscribed",
            "subscription_verified": True,
        },
    )
    monkeypatch.setattr(
        push.subscription,
        "unsubscribe",
        lambda *a, **kw: {
            "subscription_status": "subscribed",
            "removal_verified": True,
        },
    )
    assert client.subscribe().status == "already-subscribed"
    assert client.status().ok
    assert client.unsubscribe().status == "unsubscribed"


def test_initialize_reuses_existing_credentials_without_dependencies(tmp_path, monkeypatch):
    item = PushClient(state_dir=tmp_path, camera_name="TEST")
    config = {key: "TEST" for key in ("project_id", "app_id", "api_key", "messaging_sender_id", "bundle_id")}
    state = {
        "purpose": push.fcm.PURPOSE,
        "identity": push.fcm.identity(config),
        "client_id": "A" * 32,
        "fcm_token": "FAKE-TOKEN",
        "ids": [],
        "fcm_credentials": {"fcm": {"registration": {"token": "FAKE-TOKEN"}}},
    }
    item.store.put("push.receiver", state)
    before = item.store.get("push.receiver")
    assert item.initialize(config).ok
    assert item.store.get("push.receiver") == before
    assert item._firebase_config() == config


def test_cli_watch_no_camera_config_or_registration(monkeypatch, capsys):
    calls = []

    class Client:
        def __init__(self, config, **kwargs):
            assert config is None

        def watch(self, **kwargs):
            calls.append(kwargs)
            return nullcontext(iter([PushEvent("PEOPLE", "TEST", "TEST", "v2")]))

    monkeypatch.setattr(push, "PushClient", Client)
    assert CLI(["push", "watch", "--config", "DOES-NOT-EXIST", "--json"]).run() == 0
    assert calls == [{"skip_previous": True, "duration": None, "reconnect_delay": 30, "raw_output": None}]
    assert json.loads(capsys.readouterr().out)["event_type"] == "PEOPLE"


def test_cli_watch_raw_output_is_opt_in(monkeypatch, capsys, tmp_path):
    path = tmp_path / "capture.jsonl"

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def watch(self, **kwargs):
            assert kwargs["raw_output"] == path
            return nullcontext(iter([]))

    monkeypatch.setattr(push, "PushClient", Client)
    assert CLI(["push", "watch", "--raw-output", str(path), "--json"]).run() == 0
    output = capsys.readouterr()
    assert not output.out
    assert "private text and URLs" in output.err


def test_sdk_watch_raw_capture_survives_reconnect(client, monkeypatch, tmp_path):
    path = tmp_path / "capture.jsonl"
    attempts = []

    async def receive(*args, **kwargs):
        attempts.append(True)
        kwargs["on_raw_notification"]({"data": {"ALMTYPE": "LPC", "alert": "PRIVATE"}}, str(len(attempts)))
        if len(attempts) == 1:
            raise ConnectionError("test")
        return 0

    async def sleep(delay):
        pass

    monkeypatch.setattr(push.fcm, "receive", receive)
    monkeypatch.setattr(push.asyncio, "sleep", sleep)
    with client.watch(raw_output=path) as events:
        assert list(events) == []
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [row["persistent_id"] for row in rows] == ["1", "2"]
    assert events._raw_capture is None
    # The raw payload is not added to the normalized inbox or credentials.
    assert client.read(sync=False) == []
    assert client.store.get("push.receiver") == {}


def test_sdk_invalid_raw_output_does_not_start_receiver(client):
    with pytest.raises(ValueError, match="jsonl"):
        client.watch(raw_output="receiver.json")


def test_cli_status_compact_and_json(monkeypatch, capsys):
    import pyneolink.cli as cli

    monkeypatch.setattr(cli, "load_config", lambda *a: SimpleNamespace(camera=lambda *a: object()))

    class Client:
        def __init__(self, *a, **kw):
            pass

        def status(self):
            return PushResult("status", "subscribed", True, {"msg_id": 526})

    monkeypatch.setattr(push, "PushClient", Client)
    assert CLI(["push", "status"]).run() == 0
    assert capsys.readouterr().out == "Subscribed.\n"
    assert CLI(["push", "status", "--json"]).run() == 0
    assert json.loads(capsys.readouterr().out)["msg_id"] == 526


def test_read_offline_returns_only_unread_bounded_batch(client):
    for index in range(3):
        event = PushEvent("PEOPLE", "TEST", "0", "v2", event_id=str(index)).info()
        client.store.record_event(event, persistent_id=str(index), sent_at=time.time() - 10)
    assert len(client.read(sync=False, limit=2)) == 2
    assert len(client.read(sync=False)) == 1
    assert client.read(sync=False) == []


def test_read_sync_keeps_newer_events_for_next_read(client, monkeypatch):
    async def receive(*args, **kwargs):
        sink = push.fcm.EVENT_SINK.get()
        sink({"status": "receiver-state", "state": "STARTED"})
        for index, sent in enumerate((time.time() - 100, time.time() + 100)):
            item = notification()
            item["event"]["server_time_raw"] = datetime.fromtimestamp(sent, timezone.utc).isoformat()
            kwargs["on_notification"](item["event"], str(index))
            sink(item)
        await asyncio.Event().wait()

    monkeypatch.setattr(push.fcm, "receive", receive)
    events = client.read(sync_timeout=1, idle_timeout=0.05)
    assert len(events) == 1
    assert client.read(sync=False) == []
    remaining = client.store.read_events(cutoff=time.time() + 200)
    assert len(remaining) == 1


def test_read_connection_failure_does_not_consume_existing_events(client, monkeypatch):
    client.store.record_event(PushEvent("MD", "TEST", "0", "v2").info(), persistent_id="one", sent_at=1)

    async def receive(*args, **kwargs):
        raise ValueError("invalid receiver")

    monkeypatch.setattr(push.fcm, "receive", receive)
    with pytest.raises(PushError):
        client.read(sync_timeout=0.2, idle_timeout=0.1)
    assert len(client.read(sync=False)) == 1


def test_watch_consumed_events_not_returned_by_read(client, monkeypatch):
    async def receive(*args, **kwargs):
        push.fcm.EVENT_SINK.get()(notification())
        return 0

    monkeypatch.setattr(push.fcm, "receive", receive)
    with client.watch() as events:
        assert len(list(events)) == 1
    assert client.read(sync=False) == []


def test_watch_skipped_history_is_available_to_read(client, monkeypatch):
    async def receive(*args, **kwargs):
        event = notification()["event"]
        event["server_time_raw"] = "2026-01-01T00:00:00Z"
        kwargs["on_notification"](event, "old")
        push.fcm.deliver_event(event, skip_previous=True, started_at=datetime.now(timezone.utc))
        return 0

    monkeypatch.setattr(push.fcm, "receive", receive)
    with client.watch() as events:
        assert list(events) == []
    assert len(client.read(sync=False)) == 1


def test_cli_read_is_finite_and_does_not_load_camera_config(monkeypatch, capsys):
    class Client:
        def __init__(self, config, **kwargs):
            assert config is None

        def read(self, **kwargs):
            assert kwargs == {"sync": False, "sync_timeout": 10, "idle_timeout": 2, "limit": 5, "raw_output": None}
            return [PushEvent("MD", "TEST", "0", "v2")]

    monkeypatch.setattr(push, "PushClient", Client)
    assert CLI(["push", "read", "--no-sync", "--limit", "5", "--json"]).run() == 0
    assert json.loads(capsys.readouterr().out)["event_type"] == "MD"


def test_read_full_response_and_raw_capture(client, monkeypatch, tmp_path):
    path = tmp_path / "read.jsonl"
    raw = {"data": {"ALMTYPE": "LPC", "alert": "PRIVATE", "customConfig": "{}"}}

    async def receive(*args, **kwargs):
        sink = push.fcm.EVENT_SINK.get()
        sink({"status": "receiver-state", "state": "STARTED"})
        record = notification()
        record["event"]["server_time_raw"] = datetime.fromtimestamp(time.time() - 60, timezone.utc).isoformat()
        kwargs["on_raw_notification"](raw, "full-one")
        kwargs["on_notification"](record["event"], "full-one")
        sink(record)
        return 0

    monkeypatch.setattr(push.fcm, "receive", receive)
    events = client.read(sync_timeout=1, idle_timeout=0.05, raw_output=path)
    assert len(events) == 1
    assert events[0].response == raw
    assert "response" not in events[0].info()
    assert "PRIVATE" not in repr(events[0])
    assert events[0].info(full_response=True)["response"] == raw
    assert json.loads(path.read_text())["notification"] == raw
    assert client.read(sync=False) == []


def test_read_offline_raw_capture_rejected_before_consuming(client, tmp_path):
    client.store.record_event(PushEvent("MD", "TEST", "0", "v2").info(), persistent_id="one", sent_at=1)
    with pytest.raises(ValueError, match="synchronization"):
        client.read(sync=False, raw_output=tmp_path / "capture.jsonl")
    event = client.read(sync=False)[0]
    assert event.info(full_response=True)["response"] is None


def test_cli_read_full_response_and_raw_output(monkeypatch, capsys, tmp_path):
    path = tmp_path / "read.jsonl"
    raw = {"data": {"ALMTYPE": "LPC", "alert": "PRIVATE"}}

    class Client:
        def __init__(self, config, **kwargs):
            assert config is None

        def read(self, **kwargs):
            assert kwargs["raw_output"] == path
            assert kwargs["sync"] is True
            return [PushEvent("LPC", "TEST", "0", "v2", response=raw)]

    monkeypatch.setattr(push, "PushClient", Client)
    assert CLI(["push", "read", "--full-response", "--raw-output", str(path)]).run() == 0
    output = capsys.readouterr()
    assert json.loads(output.out)["response"] == raw
    assert "private text and URLs" in output.err


def test_raw_response_does_not_change_deduplication(client):
    event = PushEvent("LPC", "TEST", "0", "v2", event_id="same").info()
    first = client.store.record_event(event, persistent_id=None, sent_at=1)
    second = client.store.record_event({**event, "response": {"private": "text"}}, persistent_id=None, sent_at=1)
    assert first == second


@pytest.mark.parametrize(
    "response, expected",
    [
        ({"data": {"title": "LOW BATTERY WARNING"}, "notification": {"title": "fallback"}}, "LOW BATTERY WARNING"),
        ({"data": {"title": 42}, "notification": {"title": "fallback"}}, "fallback"),
        ({"data": {"title": " "}, "notification": {"title": "fallback"}}, "fallback"),
        ({"data": None, "notification": None}, None),
        (None, None),
    ],
)
def test_event_title_in_summary(response, expected):
    event = PushEvent("LPC", "TEST", "0", "v2", response=response)
    assert event.title == expected
    assert event.info()["title"] == expected
    assert "response" not in event.info()


@pytest.mark.parametrize("code", ["PEOPLE", "MD", "VEHICLE", "TEST", "LPC", "LP", "UNKNOWN", None])
def test_event_classification(code):
    event = PushEvent(code, "TEST", "0", "v2")
    assert event.is_people() == (code == "PEOPLE")
    assert event.is_motion() == (code in ("MD", "PEOPLE", "VEHICLE"))
    assert event.is_general_motion() == (code == "MD")
    assert event.is_vehicle() == (code == "VEHICLE")
    assert event.is_test() == (code == "TEST")
    assert event.low_battery() == (code in ("LPC", "LP"))


def test_event_getters_preserve_complete_response():
    raw = {
        "data": {
            "title": "WARNING",
            "alert": "Please recharge",
            "language": "en",
            "DEVNAME": "Camera",
            "CHNAME": "Channel",
            "DEVMOD": "Argus Eco",
            "options": '{"nested": {"unknown": 1}}',
            "customConfig": {"future": [1]},
        },
        "unknown-envelope-field": {"items": [1]},
    }
    event = PushEvent("LPC", "TEST", "0", "v2", server_time_raw="2026-11-06T13:50:28.451Z", response=raw)
    assert event.body == "Please recharge"
    assert event.language == "en"
    assert event.camera_name == "Camera"
    assert event.channel_name == "Channel"
    assert event.device_model == "Argus Eco"
    assert event.server_time == datetime(2026, 11, 6, 13, 50, 28, 451000, tzinfo=timezone.utc)
    assert event.options == {"nested": {"unknown": 1}}
    assert event.custom_config == {"future": [1]}
    event.custom_config["future"].append(2)
    event.get_response()["unknown-envelope-field"]["items"].append(2)
    event.info(full_response=True)["response"]["data"]["alert"] = "changed"
    assert event.get_response() == raw
    assert event.body == "Please recharge"
    assert raw["data"]["customConfig"] == {"future": [1]}
    assert raw["unknown-envelope-field"]["items"] == [1]


@pytest.mark.parametrize("value", [None, 42, [], "not-json", "[]", '"string"'])
def test_event_optional_objects_are_tolerant(value):
    event = PushEvent(
        "UNKNOWN",
        None,
        None,
        "system",
        server_time_raw="bad",
        response={
            "data": {"options": value, "customConfig": value, "alert": 42},
            "notification": {"body": "fallback"},
        },
    )
    assert event.options is None
    assert event.custom_config is None
    assert event.body == "fallback"
    assert event.language is None
    assert event.server_time is None


@pytest.mark.parametrize("flags", [[], ["--json"], ["--full-response"], ["--json", "--full-response"]])
def test_cli_read_watch_identical_event_output(monkeypatch, capsys, flags):
    events = [
        PushEvent(
            "LPC",
            "TEST",
            "0",
            "v2",
            server_time_raw="2026-11-06T13:50:28Z",
            received_at="2026-11-06T14:50:28Z",
            response={"data": {"title": "LOW\nBATTERY", "alert": "PRIVATE"}},
        ),
        PushEvent("UNKNOWN", None, None, "system", received_at="2026-11-06T15:00:00Z"),
    ]

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def watch(self, **kwargs):
            return nullcontext(iter(events))

        def read(self, **kwargs):
            return events

    monkeypatch.setattr(push, "PushClient", Client)
    assert CLI(["push", "read", *flags]).run() == 0
    read = capsys.readouterr().out
    assert CLI(["push", "watch", *flags]).run() == 0
    watch = capsys.readouterr().out
    assert read == watch
    assert len(read.splitlines()) == 2
    if flags:
        rows = [json.loads(line) for line in read.splitlines()]
        assert ("response" in rows[0]) == ("--full-response" in flags)
    else:
        assert "2026-11-06T13:50:28Z LPC TEST | LOW BATTERY" in read


def test_cli_empty_read_jsonl(monkeypatch, capsys):
    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def read(self, **kwargs):
            return []

    monkeypatch.setattr(push, "PushClient", Client)
    assert CLI(["push", "read", "--json"]).run() == 0
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("action", ["init", "subscribe", "status", "unsubscribe", "read", "watch"])
@pytest.mark.parametrize("option", ["--storage-dir", "--storage_dir"])
def test_cli_storage_directory_aliases(action, option):
    from pathlib import Path

    args = CLI().parser.parse_args(["push", action, option, "./storage"])
    assert args.storage_dir == Path("storage")


def test_sdk_project_local_storage(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = PushClient(camera_name="TEST", storage_dir="./storage")
    assert client.store.path == tmp_path / "storage" / "TEST.db"
    client.store.put("test.document", {"retained": True})
    again = PushClient(camera_name="TEST", storage_dir="./storage")
    assert again.store.get("test.document") == {"retained": True}
    assert client.read(sync=False) == []


def test_cli_storage_alias_forwarded(monkeypatch, capsys, tmp_path):
    from pathlib import Path

    monkeypatch.chdir(tmp_path)
    seen = []

    class Client:
        def __init__(self, config, **kwargs):
            seen.append(kwargs["storage_dir"])

        def read(self, **kwargs):
            return []

    monkeypatch.setattr(push, "PushClient", Client)
    assert CLI(["push", "read", "--storage_dir", "./storage", "--no-sync", "--json"]).run() == 0
    assert seen == [Path("storage")]
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("action", ["watch", "read"])
@pytest.mark.parametrize("json_output", [False, True])
def test_cli_title_without_full_response(monkeypatch, capsys, action, json_output):
    event = PushEvent(
        "LPC",
        "TEST",
        "0",
        "v2",
        response={
            "data": {"title": "LOW BATTERY WARNING", "alert": "PRIVATE BODY"},
        },
    )

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def watch(self, **kwargs):
            return nullcontext(iter([event]))

        def read(self, **kwargs):
            return [event]

    monkeypatch.setattr(push, "PushClient", Client)
    args = ["push", action] + (["--json"] if json_output else [])
    assert CLI(args).run() == 0
    output = capsys.readouterr().out
    assert "LOW BATTERY WARNING" in output
    assert "PRIVATE BODY" not in output
    assert '"response"' not in output


def test_read_recovers_title_from_existing_response(client):
    old = PushEvent("LPC", "TEST", "0", "v2", event_id="old").info()
    old.pop("title")
    old["response"] = {"data": {"title": "LOW BATTERY WARNING"}}
    first = client.store.record_event(old, persistent_id=None, sent_at=1)
    second = client.store.record_event({**old, "title": "LOW BATTERY WARNING"}, persistent_id=None, sent_at=1)
    assert first == second
    assert client.read(sync=False)[0].title == "LOW BATTERY WARNING"


def test_legacy_push_import_preserves_identity_and_originals(tmp_path):
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "reolink-v2.json").write_text(json.dumps({"camera": "TEST", "camera_uid": "UID"}))
    (legacy / "receiver.json").write_text('{"token":"TEST"}')
    config = SimpleNamespace(name="TEST", uid="UID")
    client = PushClient(config, storage_dir=tmp_path / "db", legacy_state_dir=legacy)
    assert client.store.get("push.receiver") == {"token": "TEST"}
    (legacy / "receiver.json").write_text("invalid")
    (legacy / "reolink-v2.json").write_text("invalid")
    again = PushClient(config, storage_dir=tmp_path / "db", legacy_state_dir=legacy)
    assert again.store.get("push.receiver") == {"token": "TEST"}


def test_legacy_push_import_rejects_different_uid(tmp_path):
    (tmp_path / "reolink-v2.json").write_text(json.dumps({"camera": "TEST", "camera_uid": "OTHER"}))
    with pytest.raises(ValueError, match="different camera"):
        PushClient(SimpleNamespace(name="TEST", uid="UID"), state_dir=tmp_path)


def test_database_only_open_uses_original_camera_name(tmp_path):
    client = PushClient(camera_name="Camera / One", storage_dir=tmp_path)
    reopened = PushClient(database=client.database_path)
    assert reopened.store.get("camera.identity")["name"] == "Camera / One"


def test_sqlite_subscription_cycle_preserves_receiver_identity(client, monkeypatch):
    client.config = CameraConfig(name="TEST", uid="UID")
    receiver = {
        "purpose": push.fcm.PURPOSE,
        "client_id": "A" * 32,
        "fcm_token": "FAKE-TOKEN",
        "fcm_credentials": {"fcm": {"registration": {"token": "FAKE-TOKEN"}}},
    }
    client.store.put("push.receiver", receiver)
    client.store.put(
        "push.subscription",
        {
            "purpose": push.lifecycle.registration.V2_PURPOSE,
            "camera": "TEST",
            "client_id": "PRIVATE-ID",
            "listener_created": False,
            "fcm_token_sha256": hashlib.sha256(b"FAKE-TOKEN").hexdigest(),
        },
    )
    active = [False]

    class Camera:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def close(self):
            pass

        def command(self, msg_id, payload, **kwargs):
            assert msg_id == 524
            assert client.store.get("push.subscription")["listener_status"] == "pending-or-unknown"
            active[0] = True
            return SimpleNamespace(header=SimpleNamespace(msg_id=msg_id, response_code=200))

    create = push.lifecycle.registration.create_listener
    monkeypatch.setattr(
        push.lifecycle.registration,
        "create_listener",
        lambda *args, **kwargs: create(*args, **kwargs, camera_factory=Camera),
    )

    def state(*args):
        return {
            "subscription_status": "subscribed" if active[0] else "unsubscribed",
            "verified_by": "camera-client-state",
        }

    monkeypatch.setattr(push.lifecycle.registration, "request_client_state", state)
    monkeypatch.setattr(push.subscription, "request_client_state", state)

    def remove(client_id, uid):
        assert (client_id, uid) == ("PRIVATE-ID", "UID")
        active[0] = False
        return 200

    unsubscribe = push.subscription.unsubscribe
    monkeypatch.setattr(push.subscription, "unsubscribe", lambda *a, **kw: unsubscribe(*a, **kw, send=remove))
    assert client.subscribe().status == "subscription-accepted"
    started = client.store.get("push.subscription")["subscription_started_at"]
    assert client.status().status == "subscribed"
    assert client.subscribe().status == "already-subscribed"
    assert client.unsubscribe().status == "unsubscribed"
    assert client.status().status == "unsubscribed"
    assert client.subscribe().status == "subscription-accepted"
    assert client.store.get("push.subscription")["subscription_started_at"] >= started
    assert read_document(client.credentials_path) == receiver
    assert not list(client.state_dir.glob("*.json"))


def test_diagnostic_output_cannot_overwrite_sqlite(client):
    with pytest.raises(ValueError, match="must not overwrite"):
        push.subscription.unsubscribe(
            None,
            client.credentials_path,
            client.subscription_path,
            check_subscription=True,
            diagnostic_path=client.database_path,
        )


@pytest.mark.parametrize("source", ["bundled", "saved", "explicit"])
def test_initialize_profile_precedence_without_android_export(tmp_path, monkeypatch, source):
    from pyneolink.internal.push_profile import default_firebase_config

    client = PushClient(camera_name="TEST", storage_dir=tmp_path)
    default = default_firebase_config()
    saved = {**default, "project_id": "saved-project"}
    explicit = {**default, "project_id": "explicit-project"}
    if source != "bundled":
        client.store.put("push.firebase", saved)
    called = []

    async def initialize(config, destination):
        called.append(dict(config))
        assert destination is client.credentials_path

    monkeypatch.setattr(push.fcm, "initialize_receiver", initialize)
    monkeypatch.setattr(push.fcm, "firebase_config", lambda *a: pytest.fail("No Android export should be read"))
    result = client.initialize(explicit if source == "explicit" else None)
    expected = {"bundled": default, "saved": saved, "explicit": explicit}[source]
    assert result.ok
    assert called == [expected]
    assert client.store.get("push.firebase") == expected


def test_bundled_profile_is_independent_for_each_caller():
    from pyneolink.internal.push_profile import ANDROID_CERT_SHA1, default_firebase_config

    first = default_firebase_config()
    first["project_id"] = "modified"
    second = default_firebase_config()
    assert second["project_id"] != "modified"
    assert second["bundle_id"] == "com.mcu.reolink"
    assert set(second) == {"project_id", "app_id", "api_key", "messaging_sender_id", "bundle_id"}
    assert all(isinstance(value, str) and value for value in second.values())
    assert len(ANDROID_CERT_SHA1) == 40


def test_bundled_profile_does_not_replace_mismatched_receiver(tmp_path, monkeypatch):
    client = PushClient(camera_name="TEST", storage_dir=tmp_path)
    receiver = {"purpose": push.fcm.PURPOSE, "identity": {"project_id": "other"}}
    client.store.put("push.receiver", receiver)
    monkeypatch.setattr(push.fcm, "initialize_receiver", lambda *a: pytest.fail("Must not register again"))
    with pytest.raises(ValueError, match="identity mismatch"):
        client.initialize()
    assert client.store.get("push.receiver") == receiver
    assert client.store.get("push.firebase") is None


def test_default_init_reuses_existing_receiver_offline(tmp_path):
    from pyneolink.internal.push_profile import default_firebase_config

    client = PushClient(camera_name="TEST", storage_dir=tmp_path)
    config = default_firebase_config()
    receiver = {
        "purpose": push.fcm.PURPOSE,
        "identity": push.fcm.identity(config),
        "client_id": "A" * 32,
        "fcm_token": "FAKE-TOKEN",
        "ids": ["one"],
        "fcm_credentials": {"fcm": {"registration": {"token": "FAKE-TOKEN"}}},
    }
    client.store.put("push.receiver", receiver)
    assert client.initialize().info()["reused"] is True
    assert client.store.get("push.receiver") == receiver
    assert client.store.get("push.firebase") == config


def test_initialize_is_idempotent_across_client_instances(tmp_path, monkeypatch):
    original_initialize = push.fcm.initialize_receiver
    first_client = PushClient(camera_name="TEST", storage_dir=tmp_path)

    async def first_registration(config, destination):
        state = push.fcm.load_state(destination, config)
        state.update(
            fcm_token="FAKE-TOKEN",
            fcm_credentials={"fcm": {"registration": {"token": "FAKE-TOKEN"}}},
        )
        push.fcm.save_state(destination, state)

    monkeypatch.setattr(push.fcm, "initialize_receiver", first_registration)
    first_result = first_client.initialize()
    assert first_result.ok and first_result.info()["reused"] is False
    receiver = first_client.store.get("push.receiver")
    subscription = {"client_id": "PRESERVED", "listener_created": True}
    first_client.store.put("push.subscription", subscription)

    monkeypatch.setattr(push.fcm, "initialize_receiver", original_initialize)
    monkeypatch.setattr(push.fcm, "prepare_receiver", lambda *a: pytest.fail("No registration or check-in on repeat"))
    clients = (first_client, PushClient(camera_name="TEST", storage_dir=tmp_path))
    for client in clients:
        result = client.initialize()
        assert result.ok and result.status == "receiver-ready"
        assert result.info()["reused"] is True
        assert result.info()["camera_connections"] == 0
        assert client.store.get("push.receiver") == receiver
        assert client.store.get("push.subscription") == subscription


def test_default_init_keeps_camera_receiver_identities_separate(tmp_path, monkeypatch):
    async def initialize(config, destination):
        state = push.fcm.load_state(destination, config)
        push.fcm.save_state(destination, state)

    monkeypatch.setattr(push.fcm, "initialize_receiver", initialize)
    first = PushClient(camera_name="One", storage_dir=tmp_path)
    second = PushClient(camera_name="Two", storage_dir=tmp_path)
    assert first.initialize().ok and second.initialize().ok
    assert first.database_path != second.database_path
    assert first.store.get("push.receiver")["client_id"] != second.store.get("push.receiver")["client_id"]
    assert first.store.get("push.firebase") == second.store.get("push.firebase")


def test_init_failure_reports_safe_google_stage_without_debug(tmp_path, monkeypatch, capsys):
    client = PushClient(camera_name="TEST", storage_dir=tmp_path)

    async def initialize(*args):
        push.fcm.emit("http-response", stage="gcm_checkin", http_status=200)
        push.fcm.emit("http-response", stage="gcm_register", http_status=200)
        push.fcm.emit("dependency-warning", stage="gcm_register", google_error="PHONE_REGISTRATION_ERROR")
        push.fcm.emit("dependency-warning", stage="gcm_register", level="ERROR")
        raise RuntimeError("SECRET token and raw response")

    monkeypatch.setattr(push.fcm, "initialize_receiver", initialize)
    with pytest.raises(PushError) as result:
        client.initialize()
    message = str(result.value)
    assert "stage=gcm_register" in message
    assert "google_error=PHONE_REGISTRATION_ERROR" in message
    assert "http_status=200" in message
    assert "SECRET" not in message
    assert "credentials were retained" not in message
    assert client.store.get("push.receiver") is None
    assert capsys.readouterr().out == ""


def test_error_summary_drops_prior_stage_error():
    with push._diagnostics(None) as summary:
        push.fcm.emit("dependency-warning", stage="gcm_register", google_error="PHONE_REGISTRATION_ERROR")
        push.fcm.emit("http-response", stage="firebase_installation", http_status=403)
        push.fcm.emit("dependency-warning", stage="fcm_install", google_reasons=["API_KEY_ANDROID_APP_BLOCKED"])
        error = push._safe_error(RuntimeError("SECRET"), summary)
    assert "PHONE_REGISTRATION_ERROR" not in str(error)
    assert "stage=firebase_installation" in str(error)
    assert "API_KEY_ANDROID_APP_BLOCKED" in str(error)


def test_error_summary_does_not_expose_unrecognized_values():
    error = push._safe_error(
        RuntimeError("SECRET"),
        {
            "stage": "SECRET",
            "http_status": "SECRET",
            "google_error": "SECRET",
            "google_reasons": ["SECRET", {}],
        },
    )
    assert "SECRET" not in str(error)
