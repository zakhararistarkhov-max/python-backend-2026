import os
import sqlite3
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import Connection


def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL environment variable is required, "
            "e.g. postgresql+psycopg://user:password@host:5432/db"
        )
    return url


def create_database_engine(url: str, **options: Any) -> Engine:
    if not url.startswith("sqlite"):
        return create_engine(url, pool_pre_ping=True, **options)
    connect_args = {"check_same_thread": False, "timeout": 30, **options.pop("connect_args", {})}
    engine = create_engine(url, pool_pre_ping=True, connect_args=connect_args, **options)
    event.listen(engine, "connect", _configure_sqlite)
    event.listen(engine, "begin", _begin_immediate)
    return engine


def _configure_sqlite(connection: sqlite3.Connection, record: Any) -> None:
    connection.isolation_level = None
    connection.execute("PRAGMA foreign_keys = ON")


def _begin_immediate(connection: Connection) -> None:
    connection.exec_driver_sql("BEGIN IMMEDIATE")
