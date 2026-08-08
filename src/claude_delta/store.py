"""
Drop-box: общее sqlite-хранилище между демоном (единственный держатель
соединения с Delta Chat) и CLI/сессией. Демон — единственный, кто говорит
с deltachat-core напрямую; всё остальное — через эти таблицы.
"""
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS session_requests (
    session_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | ready | error
    chat_id INTEGER,
    error TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'armed',  -- armed | disarmed
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    created_at REAL NOT NULL,
    sent INTEGER NOT NULL DEFAULT 0,
    error TEXT
);

CREATE TABLE IF NOT EXISTS inbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    dc_msg_id INTEGER NOT NULL UNIQUE,
    text TEXT,
    received_at REAL NOT NULL,
    consumed_by TEXT
);
"""


@contextmanager
def connect(db_path: str):
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


# --- Запросы на создание сессии (CLI -> демон) ---

def request_session(db_path: str, session_id: str, name: str) -> None:
    with connect(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO session_requests (session_id, name, created_at) "
            "VALUES (?, ?, ?)",
            (session_id, name, time.time()),
        )
        conn.commit()


def get_session_request_status(db_path: str, session_id: str):
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT status, chat_id, error FROM session_requests WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return dict(row) if row else None


def pending_session_requests(db_path: str):
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT session_id, name FROM session_requests WHERE status = 'pending'"
        ).fetchall()
        return [dict(r) for r in rows]


def fulfill_session_request(db_path: str, session_id: str, chat_id: int) -> None:
    with connect(db_path) as conn:
        now = time.time()
        conn.execute(
            "UPDATE session_requests SET status = 'ready', chat_id = ? WHERE session_id = ?",
            (chat_id, session_id),
        )
        conn.execute(
            "INSERT OR REPLACE INTO sessions (session_id, chat_id, status, created_at, updated_at) "
            "VALUES (?, ?, 'armed', ?, ?)",
            (session_id, chat_id, now, now),
        )
        conn.commit()


def fail_session_request(db_path: str, session_id: str, error: str) -> None:
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE session_requests SET status = 'error', error = ? WHERE session_id = ?",
            (error, session_id),
        )
        conn.commit()


# --- Сессии (arm/disarm) ---

def get_session(db_path: str, session_id: str):
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        return dict(row) if row else None


def armed_sessions(db_path: str):
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM sessions WHERE status = 'armed'"
        ).fetchall()
        return [dict(r) for r in rows]


def disarm_session(db_path: str, session_id: str) -> None:
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE sessions SET status = 'disarmed', updated_at = ? WHERE session_id = ?",
            (time.time(), session_id),
        )
        conn.commit()


# --- Исходящие (CLI -> демон) ---

def enqueue_outbox(db_path: str, session_id: str, chat_id: int, text: str) -> int:
    with connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO outbox (session_id, chat_id, text, created_at) VALUES (?, ?, ?, ?)",
            (session_id, chat_id, text, time.time()),
        )
        conn.commit()
        return cur.lastrowid


def pending_outbox(db_path: str):
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM outbox WHERE sent = 0"
        ).fetchall()
        return [dict(r) for r in rows]


def mark_outbox_sent(db_path: str, outbox_id: int) -> None:
    with connect(db_path) as conn:
        conn.execute("UPDATE outbox SET sent = 1 WHERE id = ?", (outbox_id,))
        conn.commit()


def mark_outbox_error(db_path: str, outbox_id: int, error: str) -> None:
    with connect(db_path) as conn:
        conn.execute("UPDATE outbox SET error = ? WHERE id = ?", (error, outbox_id))
        conn.commit()


# --- Входящие (демон -> CLI/сессия) ---

def store_inbox_message(db_path: str, chat_id: int, dc_msg_id: int, text: str) -> None:
    with connect(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO inbox (chat_id, dc_msg_id, text, received_at) "
            "VALUES (?, ?, ?, ?)",
            (chat_id, dc_msg_id, text, time.time()),
        )
        conn.commit()


def fetch_unconsumed(db_path: str, session_id: str, chat_id: int):
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM inbox WHERE chat_id = ? AND consumed_by IS NULL "
            "ORDER BY id ASC",
            (chat_id,),
        ).fetchall()
        result = [dict(r) for r in rows]
        if result:
            ids = [r["id"] for r in result]
            conn.executemany(
                "UPDATE inbox SET consumed_by = ? WHERE id = ?",
                [(session_id, i) for i in ids],
            )
            conn.commit()
        return result
