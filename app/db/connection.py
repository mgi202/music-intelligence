"""
SQLite connection factory.

All database access in this system goes through get_connection().
WAL mode and foreign keys are enforced on every connection.
Row factory is set so rows behave like dicts.
"""

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_DEFAULT_DB = os.getenv("SQLITE_PATH", "data/sqlite/library.db")


def get_connection(db_path: str | None = None) -> sqlite3.Connection:
    """
    Open and configure a SQLite connection.

    WAL mode, foreign keys, and dict-style row access are set on every
    connection. Callers are responsible for closing or using as a context
    manager.
    """
    path = db_path or _DEFAULT_DB
    Path(path).parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    # Wait up to 5s for a competing writer before raising SQLITE_BUSY. The
    # worker (writer) and API (reader/writer) can contend; WAL allows
    # concurrent readers but only one writer, so this avoids spurious locks.
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def db_conn(db_path: str | None = None):
    """
    Context manager that yields a connection and commits on clean exit,
    rolls back on exception.

    Usage:
        with db_conn() as conn:
            conn.execute(...)
    """
    conn = get_connection(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
