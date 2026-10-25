"""Tiny DB layer: Postgres when DATABASE_URL is set (Railway), SQLite otherwise (local dev).

Queries are written with ? placeholders and portable SQL; timestamps are epoch seconds.
"""
import os
import sqlite3
import threading

DATABASE_URL = os.environ.get("DATABASE_URL", "")
USERS_DB = os.environ.get("USERS_DB", "users.db")
IS_PG = DATABASE_URL.startswith("postgres")

_lock = threading.Lock()

SCHEMA = [
    "CREATE TABLE IF NOT EXISTS users ("
    "email TEXT PRIMARY KEY, first_seen BIGINT NOT NULL, last_seen BIGINT NOT NULL, "
    "login_count INTEGER NOT NULL DEFAULT 0)",
    "CREATE TABLE IF NOT EXISTS login_codes ("
    "email TEXT PRIMARY KEY, code_hash TEXT NOT NULL, expires_at BIGINT NOT NULL, "
    "attempts INTEGER NOT NULL DEFAULT 0)",
    "CREATE TABLE IF NOT EXISTS sessions ("
    "token_hash TEXT PRIMARY KEY, email TEXT NOT NULL, expires_at BIGINT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS code_requests ("
    "ip TEXT NOT NULL, email TEXT NOT NULL, created_at BIGINT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS runs ("
    "email TEXT NOT NULL, created_at BIGINT NOT NULL, journeys INTEGER NOT NULL, "
    "miles REAL NOT NULL)",
]


def _connect():
    if IS_PG:
        import psycopg
        return psycopg.connect(DATABASE_URL)
    return sqlite3.connect(USERS_DB)


def execute(sql, params=(), fetch=None):
    """Run one statement and commit. fetch: None | 'one' | 'all'."""
    if IS_PG:
        sql = sql.replace("?", "%s")
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(sql, params)
            result = None
            if fetch == "one":
                result = cur.fetchone()
            elif fetch == "all":
                result = cur.fetchall()
            conn.commit()
            return result
        finally:
            conn.close()


def init():
    for stmt in SCHEMA:
        execute(stmt)
