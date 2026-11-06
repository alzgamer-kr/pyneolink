"""Per-camera SQLite state, automatic transactional migrations, and durable inbox."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Column,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    delete,
    insert,
    inspect,
    select,
    update,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import URL, Connection
from sqlalchemy.pool import NullPool

DEFAULT_STORAGE_DIR = Path.home() / "pyneolink"
SCHEMA_VERSION = 3
METADATA = MetaData()
VERSIONS = Table(
    "schema_version", METADATA, Column("id", Integer, primary_key=True), Column("version", Integer, nullable=False)
)
STATE = Table("state", METADATA, Column("key", String, primary_key=True), Column("value", JSON, nullable=False))
INBOX = Table(
    "push_inbox",
    METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("fingerprint", String, nullable=False, unique=True),
    Column("event", JSON, nullable=False),
    Column("sent_at", Float),
    Column("received_at", Float, nullable=False),
    Column("read_at", Float),
)
LEASES = Table(
    "leases",
    METADATA,
    Column("key", String, primary_key=True),
    Column("owner", String, nullable=False),
    Column("expires", Float, nullable=False),
)
IMPORTS = Table(
    "imports", METADATA, Column("source", String, primary_key=True), Column("imported_at", Float, nullable=False)
)


class StorageError(RuntimeError):
    """Storage cannot be safely opened or migrated; no automatic reset is attempted."""


def camera_database(camera_name: str, directory: str | Path = DEFAULT_STORAGE_DIR) -> Path:
    """Keep readable camera names while preventing path traversal and collisions."""
    if not isinstance(camera_name, str) or not camera_name.strip():
        raise ValueError("A camera name is required for its database")
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", camera_name).strip(" .")[:100]
    reserved = re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", safe)
    if safe != camera_name or reserved or not safe:
        safe = f"{safe or 'camera'}-{hashlib.sha256(camera_name.encode()).hexdigest()[:12]}"
    return Path(directory).expanduser() / f"{safe}.db"


def connection_state_path(
    camera_name: str,
    state_path: str | Path | None,
    storage_dir: str | Path | None,
) -> str | Path | None:
    """Resolve a common storage directory while preserving legacy overrides.

    Explicit state_path=None disables the cache; a nondefault state_path keeps
    its existing file/import behavior. Otherwise storage_dir selects the same
    per-camera SQLite file used by PushClient, even for directory names with
    extensions such as storage.db. No existing data is moved or deleted.
    """
    if state_path is None or storage_dir is None:
        return state_path
    if Path(state_path).expanduser() != DEFAULT_STORAGE_DIR:
        return state_path
    return camera_database(camera_name, Path(storage_dir).expanduser().absolute())


def _migration_1(connection: Connection) -> None:
    """Create the structured state document store."""
    STATE.create(connection)


def _migration_2(connection: Connection) -> None:
    """Add the durable push inbox and its unread-event index."""
    INBOX.create(connection)
    connection.exec_driver_sql("CREATE INDEX ix_push_inbox_unread ON push_inbox(read_at, id)")


def _migration_3(connection: Connection) -> None:
    """Add receiver ownership leases and the legacy import journal."""
    LEASES.create(connection)
    IMPORTS.create(connection)


MIGRATIONS = {1: _migration_1, 2: _migration_2, 3: _migration_3}


class CameraStore:
    """Local SQLite database; short SQLAlchemy connections are never shared across threads."""

    def __init__(self, path: str | Path) -> None:
        """Open or create a private database and apply supported migrations.

        :param path: SQLite file path; missing parent directories are created.
        :raises StorageError: The schema is unknown, newer, or incompatible.
        """
        self.path = Path(path).expanduser().absolute()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        self.engine = create_engine(
            URL.create("sqlite", database=str(self.path)),
            connect_args={"timeout": 30},
            poolclass=NullPool,
            hide_parameters=True,
        )
        self._lease_failures: dict[str, threading.Event] = {}
        self._lease_lock = threading.Lock()
        self._migrate()

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        """Explicit BEGIN also makes SQLite DDL transactional on Python 3.11+."""
        with self.engine.connect() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def _migrate(self) -> None:
        """Upgrade known schemas atomically and validate required tables/columns."""
        with self.transaction() as connection:
            tables = inspect(connection).get_table_names()
            if "schema_version" not in tables:
                if tables:
                    raise StorageError("Database has an unknown schema; refusing to overwrite it")
                VERSIONS.create(connection)
                connection.execute(insert(VERSIONS).values(id=1, version=0))
            version = connection.execute(select(VERSIONS.c.version).where(VERSIONS.c.id == 1)).scalar_one()
            if version > SCHEMA_VERSION or version < 0:
                raise StorageError("Database schema is not supported by this package version")
            for target in range(version + 1, SCHEMA_VERSION + 1):
                MIGRATIONS[target](connection)
                connection.execute(update(VERSIONS).where(VERSIONS.c.id == 1).values(version=target))
            inspector = inspect(connection)
            for table in METADATA.sorted_tables:
                if not inspector.has_table(table.name):
                    raise StorageError(f"Database is missing required table {table.name}")
                columns = {column["name"] for column in inspector.get_columns(table.name)}
                if not set(table.columns.keys()).issubset(columns):
                    raise StorageError(f"Database has incompatible columns in {table.name}")

    def get(self, key: str, default: object = None) -> object:
        """Read a JSON document, returning default if the key is absent."""
        self.assert_leases()
        with self.engine.connect() as connection:
            row = connection.execute(select(STATE.c.value).where(STATE.c.key == key)).first()
            return default if row is None else row[0]

    def put(self, key: str, value: object) -> None:
        """Atomically replace a named JSON-compatible document."""
        self.assert_leases()
        with self.transaction() as connection:
            self._put(connection, key, value)

    @staticmethod
    def _put(connection: Connection, key: str, value: object) -> None:
        """Upsert one document inside an existing transaction."""
        statement = sqlite_insert(STATE).values(key=key, value=value)
        connection.execute(statement.on_conflict_do_update(index_elements=[STATE.c.key], set_={"value": value}))

    def bind_camera(self, name: str, uid: str | None = None) -> None:
        """Bind or validate camera identity, refusing accidental cross-camera use."""
        with self.transaction() as connection:
            old = connection.execute(select(STATE.c.value).where(STATE.c.key == "camera.identity")).scalar_one_or_none()
            if old and (old["name"] != name or (uid and old.get("uid") and uid != old["uid"])):
                raise StorageError("Database belongs to a different camera; choose a different database path")
            self._put(connection, "camera.identity", {"name": name, "uid": uid or (old or {}).get("uid")})

    def import_documents(self, documents: dict[str, Path]) -> None:
        """Import a group atomically, once; retain originals and never replace DB state."""
        with self.transaction() as connection:
            for key, path in documents.items():
                source = f"{path.resolve()}:{key}"
                imported = connection.execute(select(IMPORTS.c.source).where(IMPORTS.c.source == source)).first()
                if imported is not None or not path.is_file():
                    continue
                value = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(value, dict):
                    raise StorageError("Legacy state must contain JSON objects")
                statement = sqlite_insert(STATE).values(key=key, value=value)
                connection.execute(statement.on_conflict_do_nothing(index_elements=[STATE.c.key]))
                connection.execute(insert(IMPORTS).values(source=source, imported_at=time.time()))

    def has_imported(self, key: str, path: Path) -> bool:
        """Check the import journal without reopening a legacy credentials file."""
        with self.engine.connect() as connection:
            return (
                connection.execute(
                    select(IMPORTS.c.source).where(IMPORTS.c.source == f"{path.resolve()}:{key}")
                ).first()
                is not None
            )

    @contextmanager
    def lease(self, key: str) -> Iterator[None]:
        """Cross-process lease with heartbeat; abandoned leases expire after 120s."""
        owner = uuid4().hex
        stop = threading.Event()
        lost = threading.Event()
        with self.transaction() as connection:
            connection.execute(delete(LEASES).where(LEASES.c.key == key, LEASES.c.expires < time.time()))
            statement = sqlite_insert(LEASES).values(key=key, owner=owner, expires=time.time() + 120)
            if connection.execute(statement.on_conflict_do_nothing()).rowcount != 1:
                raise FileExistsError("This camera's state is already in use by another operation")
        with self._lease_lock:
            self._lease_failures[owner] = lost

        def heartbeat() -> None:
            """Renew this owner's lease and flag loss for the active operation."""
            while not stop.wait(20):
                try:
                    with self.transaction() as connection:
                        result = connection.execute(
                            update(LEASES)
                            .where(
                                LEASES.c.key == key,
                                LEASES.c.owner == owner,
                            )
                            .values(expires=time.time() + 120)
                        )
                        if result.rowcount != 1:
                            lost.set()
                            return
                except Exception:
                    lost.set()
                    return

        thread = threading.Thread(target=heartbeat, name="pyneolink-state-lease", daemon=True)
        thread.start()
        try:
            yield
            if lost.is_set():
                raise StorageError("State lease was lost; stop the receiver before retrying")
        finally:
            stop.set()
            thread.join(timeout=35)
            with self._lease_lock:
                self._lease_failures.pop(owner, None)
            with self.transaction() as connection:
                connection.execute(delete(LEASES).where(LEASES.c.key == key, LEASES.c.owner == owner))

    def assert_leases(self) -> None:
        """Stop active operations promptly when a lease heartbeat fails."""
        with self._lease_lock:
            if any(event.is_set() for event in self._lease_failures.values()):
                raise StorageError("State lease was lost; receiver must stop")

    def record_event(self, event: dict, *, persistent_id: str | None, sent_at: float | None) -> int:
        """Persist a delivery once and return its stable inbox row ID.

        Transport IDs take precedence over normalized identity fingerprints.
        Display titles and raw envelopes do not alter deduplication identity.
        """
        self.assert_leases()
        identity = {
            key: value
            for key, value in event.items()
            if key not in ("received_at", "age_filter", "duplicate_hint", "response", "title")
        }
        encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"))
        identifiable = any(identity.get(key) for key in ("event_id", "server_time_raw", "device_time_raw"))
        delivery_key = persistent_id or (encoded if identifiable else uuid4().hex)
        fingerprint = hashlib.sha256(delivery_key.encode()).hexdigest()
        with self.transaction() as connection:
            statement = sqlite_insert(INBOX).values(
                fingerprint=fingerprint,
                event=event,
                sent_at=sent_at,
                received_at=time.time(),
            )
            connection.execute(statement.on_conflict_do_nothing(index_elements=[INBOX.c.fingerprint]))
            return connection.execute(select(INBOX.c.id).where(INBOX.c.fingerprint == fingerprint)).scalar_one()

    def mark_read(self, event_id: int) -> bool:
        """Claim an event once, including concurrent local read/watch consumers."""
        with self.transaction() as connection:
            result = connection.execute(
                update(INBOX).where(INBOX.c.id == event_id, INBOX.c.read_at.is_(None)).values(read_at=time.time())
            )
            return result.rowcount == 1

    def read_events(self, *, cutoff: float, since: float = 0, limit: int = 1000) -> list[dict]:
        """Atomically claim a bounded batch, oldest first; repeated read excludes it."""
        if not 1 <= limit <= 10000:
            raise ValueError("Read limit must be between 1 and 10000")
        from sqlalchemy import func

        timestamp = func.coalesce(INBOX.c.sent_at, INBOX.c.received_at)
        with self.transaction() as connection:
            rows = connection.execute(
                select(INBOX.c.id, INBOX.c.event)
                .where(
                    INBOX.c.read_at.is_(None),
                    timestamp <= cutoff,
                    timestamp >= since,
                )
                .order_by(INBOX.c.id)
                .limit(limit)
            ).all()
            if rows:
                connection.execute(
                    update(INBOX).where(INBOX.c.id.in_([row.id for row in rows])).values(read_at=time.time())
                )
            return [row.event for row in rows]


@dataclass(frozen=True)
class StateDocument:
    """Named structured document in a camera database, not a filesystem path."""

    store: CameraStore
    key: str


def document_identity(document: Path | StateDocument) -> object:
    """Return a comparable identity for a legacy file or SQLite document."""
    if isinstance(document, StateDocument):
        return document.store.path.resolve(), document.key
    return document.resolve()


def document_exists(document: Path | StateDocument) -> bool:
    """Check presence without creating or overwriting a stored document."""
    if isinstance(document, StateDocument):
        return document.store.get(document.key) is not None
    return document.exists()


def read_document(document: Path | StateDocument) -> dict:
    """Read a structured document; raise FileNotFoundError when absent."""
    if isinstance(document, StateDocument):
        value = document.store.get(document.key)
        if value is None:
            raise FileNotFoundError("State document does not exist")
        return value
    return json.loads(document.read_text(encoding="utf-8"))
