"""SQLite migration, isolation and inbox checks without camera or cloud access."""

import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import insert, select

from pyneolink.core import storage
from pyneolink.core.state import ConnectionState
from pyneolink.core.storage import CameraStore, StorageError, camera_database


def test_fresh_database_and_reopen(tmp_path):
    path = tmp_path / "Camera.db"
    store = CameraStore(path)
    store.put("receiver", {"ids": ["one"], "nested": {"token": "TEST"}})
    assert CameraStore(path).get("receiver") == store.get("receiver")
    with store.engine.connect() as connection:
        assert connection.execute(select(storage.VERSIONS.c.version)).scalar_one() == storage.SCHEMA_VERSION


def old_database(path):
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE schema_version (id INTEGER PRIMARY KEY, version INTEGER NOT NULL)")
        connection.execute("INSERT INTO schema_version VALUES (1, 1)")
        connection.execute("CREATE TABLE state (key VARCHAR PRIMARY KEY, value JSON NOT NULL)")
        connection.execute("INSERT INTO state VALUES (?, ?)", ("receiver", '{"token":"TEST"}'))


def test_old_schema_upgrades_and_preserves_data(tmp_path):
    path = tmp_path / "old.db"
    old_database(path)
    store = CameraStore(path)
    assert store.get("receiver") == {"token": "TEST"}
    assert store.read_events(cutoff=time.time()) == []


def test_failed_migration_rolls_back_ddl_and_version(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    old_database(path)

    def broken(connection):
        connection.exec_driver_sql("CREATE TABLE unfinished (id INTEGER)")
        raise RuntimeError("test migration failure")

    monkeypatch.setitem(storage.MIGRATIONS, 3, broken)
    with pytest.raises(RuntimeError, match="test migration"):
        CameraStore(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchone() == (1,)
        assert {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")} == {
            "schema_version",
            "state",
        }
        assert connection.execute("SELECT value FROM state").fetchone() == ('{"token":"TEST"}',)


def test_future_schema_is_not_downgraded(tmp_path):
    path = tmp_path / "future.db"
    store = CameraStore(path)
    with store.transaction() as connection:
        connection.execute(storage.VERSIONS.update().values(version=999))
    with pytest.raises(StorageError, match="not supported"):
        CameraStore(path)


@pytest.mark.parametrize("table", ["state", "push_inbox", "leases", "imports"])
def test_missing_table_is_not_silently_reset(tmp_path, table):
    path = tmp_path / "broken.db"
    store = CameraStore(path)
    with store.transaction() as connection:
        storage.METADATA.tables[table].drop(connection)
    with pytest.raises(StorageError, match="missing required table"):
        CameraStore(path)


def test_concurrent_first_open_and_updates(tmp_path):
    path = tmp_path / "concurrent.db"

    def write(index):
        store = CameraStore(path)
        store.put(str(index), {"index": index})

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(16)))
    store = CameraStore(path)
    assert all(store.get(str(index)) == {"index": index} for index in range(16))


def test_legacy_import_is_atomic_once_and_retains_originals(tmp_path):
    first, second = tmp_path / "first.json", tmp_path / "second.json"
    first.write_text('{"old":1}', encoding="utf-8")
    second.write_text("invalid", encoding="utf-8")
    store = CameraStore(tmp_path / "camera.db")
    with pytest.raises(ValueError):
        store.import_documents({"first": first, "second": second})
    assert store.get("first") is None
    second.write_text('{"old":2}', encoding="utf-8")
    store.import_documents({"first": first, "second": second})
    store.put("first", {"new": 3})
    first.write_text("now invalid", encoding="utf-8")
    store.import_documents({"first": first, "second": second})
    assert store.get("first") == {"new": 3}
    assert first.exists() and second.exists()


@pytest.mark.parametrize("name", ["../camera", "a/b", "a\\b", "CON", "a.", "a?", "a*"])
def test_camera_filename_is_safe(tmp_path, name):
    path = camera_database(name, tmp_path)
    assert path.parent == tmp_path
    assert path.suffix == ".db"
    assert path.name != name + ".db"
    assert path != camera_database(name + "different", tmp_path)


def test_identity_rejects_name_or_uid_collision(tmp_path):
    store = CameraStore(tmp_path / "camera.db")
    store.bind_camera("Camera", "UID1")
    store.bind_camera("Camera")
    assert store.get("camera.identity")["uid"] == "UID1"
    with pytest.raises(StorageError):
        store.bind_camera("Camera", "UID2")
    with pytest.raises(StorageError):
        store.bind_camera("Other", "UID1")


def test_lease_exclusion_release_and_crash_expiry(tmp_path):
    path = tmp_path / "camera.db"
    first, second = CameraStore(path), CameraStore(path)
    with first.lease("receiver"):
        with pytest.raises(FileExistsError):
            with second.lease("receiver"):
                pass
    with second.lease("receiver"):
        pass
    with first.transaction() as connection:
        connection.execute(insert(storage.LEASES).values(key="receiver", owner="dead", expires=time.time() - 1))
    with second.lease("receiver"):
        pass


def test_lost_lease_is_visible_to_receiver(tmp_path):
    store = CameraStore(tmp_path / "camera.db")
    with pytest.raises(StorageError, match="lease was lost"):
        with store.lease("receiver"):
            next(iter(store._lease_failures.values())).set()
            store.assert_leases()


def test_inbox_deduplication_and_unread_cutoff(tmp_path):
    store = CameraStore(tmp_path / "camera.db")
    first = store.record_event({"event_type": "PEOPLE", "received_at": "one"}, persistent_id="id1", sent_at=10)
    assert store.record_event({"event_type": "PEOPLE"}, persistent_id="id1", sent_at=10) == first
    old = store.record_event({"event_type": "MD", "event_id": "md1"}, persistent_id=None, sent_at=5)
    assert (
        store.record_event({"event_type": "MD", "event_id": "md1", "received_at": "two"}, persistent_id=None, sent_at=5)
        == old
    )
    store.record_event({"event_type": "TEST"}, persistent_id="id2", sent_at=30)
    assert [event["event_type"] for event in store.read_events(cutoff=20, since=8)] == ["PEOPLE"]
    assert store.read_events(cutoff=20, since=8) == []
    assert [event["event_type"] for event in store.read_events(cutoff=40, since=8)] == ["TEST"]


def test_unidentified_events_are_not_falsely_deduplicated(tmp_path):
    store = CameraStore(tmp_path / "camera.db")
    first = store.record_event({"event_type": "MD"}, persistent_id=None, sent_at=None)
    second = store.record_event({"event_type": "MD"}, persistent_id=None, sent_at=None)
    assert first != second


def test_concurrent_reads_claim_each_event_once(tmp_path):
    path = tmp_path / "camera.db"
    store = CameraStore(path)
    for index in range(20):
        store.record_event({"index": index}, persistent_id=str(index), sent_at=1)

    def read(_):
        return CameraStore(path).read_events(cutoff=10, limit=5)

    with ThreadPoolExecutor(max_workers=4) as pool:
        batches = list(pool.map(read, range(4)))
    assert sorted(event["index"] for batch in batches for event in batch) == list(range(20))
    assert store.read_events(cutoff=10) == []


def test_connection_cache_import_and_multi_camera_updates(tmp_path):
    legacy = tmp_path / "state.json"
    legacy.write_text(json.dumps({"cameras": {"one": {"address": "192.0.2.1", "transport": "udp"}}}))
    first, second = ConnectionState(legacy), ConnectionState(legacy)
    assert first.get_address("one", transport="udp") == "192.0.2.1"
    first.update_address("one", "192.0.2.2", transport="udp")
    second.update_address("two", "192.0.2.3", transport="udp")
    assert first.get_address("two", transport="udp") == "192.0.2.3"
    assert second.get_address("one", transport="udp") == "192.0.2.2"
    assert json.loads(legacy.read_text())["cameras"]["one"]["address"] == "192.0.2.1"


@pytest.mark.parametrize("directory", ["storage", "storage.db"])
def test_shared_camera_push_sqlite_directory(tmp_path, monkeypatch, directory):
    from pyneolink import Camera, PushClient
    from pyneolink.config import CameraConfig

    monkeypatch.chdir(tmp_path)
    config = CameraConfig(name="TEST", username="admin", password="test", uid="TEST-UID")
    camera = Camera(config, storage_dir=directory)
    camera.state.update_address(config.name, "192.0.2.1", uid=config.uid, transport="udp")
    push = PushClient(config, storage_dir=directory)
    push.store.put("push.receiver", {"synthetic": True})
    assert push.store.get("connection:TEST")["address"] == "192.0.2.1"
    assert camera.state._store(config.name).get("push.receiver") == {"synthetic": True}
    assert push.store.path == tmp_path / directory / "TEST.db"
    assert sorted(path.name for path in (tmp_path / directory).iterdir()) == ["TEST.db"]
    assert not list(tmp_path.glob("*.json"))
    assert not list(tmp_path.rglob("*.lock"))


def test_shared_storage_isolates_camera_databases(tmp_path):
    from pyneolink import Camera, PushClient

    first = Camera(name="One", storage_dir=tmp_path)
    second = Camera(name="Two", storage_dir=tmp_path)
    first.state.update_address("One", "192.0.2.1")
    second.state.update_address("Two", "192.0.2.2")
    assert PushClient(camera_name="One", storage_dir=tmp_path).store.get("connection:Two") is None
    assert PushClient(camera_name="Two", storage_dir=tmp_path).store.get("connection:One") is None
    assert sorted(path.name for path in tmp_path.glob("*.db")) == ["One.db", "Two.db"]


def test_shared_storage_preserves_disabled_and_explicit_state(tmp_path):
    from pyneolink import Camera

    assert Camera(name="TEST", storage_dir=tmp_path, state_path=None).state is None
    legacy = tmp_path / "old.json"
    legacy.write_text('{"cameras":{"TEST":{"address":"192.0.2.1"}}}')
    camera = Camera(name="TEST", storage_dir=tmp_path / "storage", state_path=legacy)
    assert camera.state.get_address("TEST") == "192.0.2.1"
    camera.state.update_address("TEST", "192.0.2.2")
    assert (tmp_path / "old.db").exists()
    assert json.loads(legacy.read_text())["cameras"]["TEST"]["address"] == "192.0.2.1"
    assert not (tmp_path / "storage").exists()


@pytest.mark.parametrize(
    "argv",
    [
        ["--storage_dir", "./storage", "info"],
        ["info", "--storage-dir", "./storage"],
        ["--storage-dir", "./storage", "push", "read"],
        ["push", "--storage_dir", "./storage", "read"],
        ["push", "read", "--storage_dir", "./storage"],
        ["serve", "--storage_dir", "./storage"],
    ],
)
def test_common_cli_storage_directory(argv):
    from pyneolink.cli import CLI

    assert CLI().parser.parse_args(argv).storage_dir == Path("storage")


def test_cli_camera_forwards_shared_storage(monkeypatch, capsys):
    import pyneolink.cli as cli

    calls = []
    config = object()
    monkeypatch.setattr(cli, "load_config", lambda *args: SimpleNamespace(camera=lambda *a: config))

    class Camera:
        def __init__(self, cfg, **kwargs):
            assert cfg is config
            calls.append(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def info(self, **kwargs):
            return {}

    monkeypatch.setattr(cli, "Camera", Camera)
    assert cli.CLI(["info", "--storage_dir", "./storage"]).run() == 0
    assert calls[0]["storage_dir"] == Path("storage")


def test_cli_serve_forwards_shared_storage(monkeypatch):
    import pyneolink.cli as cli

    calls = []
    monkeypatch.setattr(cli, "serve_streams", lambda *args, **kwargs: calls.append(kwargs))
    assert cli.CLI(["serve", "--storage_dir", "./storage"]).run() == 0
    assert calls[0]["storage_dir"] == Path("storage")


def test_stream_server_storage_directory_offline(tmp_path, monkeypatch):
    from pyneolink import stream_server
    from pyneolink.config import Config, CameraConfig

    config = Config(cameras=[CameraConfig(name="TEST", username="admin", password="test")])
    server = stream_server.StreamServer(config, storage_dir=tmp_path / "storage")
    fake = SimpleNamespace(server_address=("127.0.0.1", 8080))
    monkeypatch.setattr(stream_server, "_StreamServer", lambda *args: fake)
    actual = server._create_server()
    assert actual.storage_dir == tmp_path / "storage"
    state = storage.connection_state_path("TEST", actual.state_path, actual.storage_dir)
    assert state == tmp_path / "storage" / "TEST.db"
