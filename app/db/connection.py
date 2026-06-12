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
