from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.models import Base


def _sqlite_connect(engine: Engine) -> None:
    if engine.dialect.name != "sqlite":
        return

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()


def make_engine(settings: Settings):
    connect_args = {}
    if settings.database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
        db_path = settings.database_url.removeprefix("sqlite:///")
        if db_path and db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(settings.database_url, echo=False, connect_args=connect_args)
    _sqlite_connect(engine)
    return engine


def init_db(engine) -> None:
    Base.metadata.create_all(engine)


def reset_database(settings: Settings) -> Path:
    """Delete SQLite DB files so schema/data start clean."""
    db_url = settings.database_url
    if not db_url.startswith("sqlite:///"):
        raise RuntimeError("reset_database only supports sqlite:/// URLs")
    db_path = Path(db_url.removeprefix("sqlite:///"))
    for suffix in ("", "-wal", "-shm", "-journal"):
        path = Path(str(db_path) + suffix) if suffix else db_path
        if path.exists():
            path.unlink()
    return db_path


def make_session_factory(engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
