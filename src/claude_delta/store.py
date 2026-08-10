"""
Drop-box: shared sqlite store between the daemon (the only holder of the
Delta Chat connection) and the CLI/session. The daemon is the only thing
that talks to deltachat-core directly; everything else goes through these
tables.
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
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0
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


# tmux dispatcher (see design.md, 2026-08-09 pivot): columns added on top
# of the already-deployed schema via ALTER TABLE, because CREATE TABLE IF
# NOT EXISTS does not touch an existing sessions table.
_MIGRATIONS = (
    "ALTER TABLE sessions ADD COLUMN tmux_target TEXT",
    "ALTER TABLE sessions ADD COLUMN last_prompt_hash TEXT",
    # Separate from last_prompt_hash (see daemon.py calibration note,
    # 2026-08-10 reliability pass): a permission prompt and a limit
    # banner can each be showing at different times in the same pane:
    # dedup for one must not clobber dedup for the other, or the banner
    # gets needlessly re-sent every time an unrelated prompt resolves.
    "ALTER TABLE sessions ADD COLUMN last_limit_hash TEXT",
    "ALTER TABLE outbox ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0",
)


def _migrate(conn: sqlite3.Connection) -> None:
    for stmt in _MIGRATIONS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e):
                raise


@contextmanager
def connect(db_path: str):
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    # WAL: daemon and CLI open independent connections concurrently
    # (daemon holds one for its whole run; CLI opens one per invocation)
    # — WAL lets a CLI read/write proceed without blocking on whatever
    # the daemon's mid-cycle connection is doing, and vice versa.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


# --- Session creation requests (CLI -> daemon) ---

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


# --- Sessions (arm/disarm) ---

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


def rearm_session(db_path: str, session_id: str) -> int | None:
    """Re-arms an existing (armed or disarmed) session in place, without
    going through the daemon's session_requests queue — the chat already
    exists, nothing needs creating. Returns chat_id, or None if there's
    no row for this session_id at all (a genuinely new session, which
    does need the daemon roundtrip).

    Without this, off -> on left the *previous* session_requests row
    (status='ready') in place; a fresh request_session() no-ops via
    INSERT OR IGNORE, the CLI's wait loop reads the stale 'ready' status
    and reports success immediately — but sessions.status was never
    flipped back to 'armed', so delivery/forwarding silently never
    resumes. The chat looks alive (send still works, same chat_id) while
    actually being half-dead (see design.md, reliability pass 2026-08-10)."""
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT chat_id FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE sessions SET status = 'armed', updated_at = ? WHERE session_id = ?",
            (time.time(), session_id),
        )
        conn.commit()
        return row["chat_id"]


# --- tmux dispatcher (session_id <-> tmux pane target) ---

def register_tmux(db_path: str, session_id: str, target: str) -> None:
    """Binds a session to a tmux pane (pane-id, e.g. '%12').

    Resets last_prompt_hash/last_limit_hash — new pane, detection starts
    fresh.
    """
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE sessions SET tmux_target = ?, last_prompt_hash = NULL, "
            "last_limit_hash = NULL, updated_at = ? WHERE session_id = ?",
            (target, time.time(), session_id),
        )
        conn.commit()


def clear_tmux(db_path: str, session_id: str) -> None:
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE sessions SET tmux_target = NULL, last_prompt_hash = NULL, "
            "last_limit_hash = NULL, updated_at = ? WHERE session_id = ?",
            (time.time(), session_id),
        )
        conn.commit()


def set_last_prompt_hash(db_path: str, session_id: str, prompt_hash: str | None) -> None:
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE sessions SET last_prompt_hash = ? WHERE session_id = ?",
            (prompt_hash, session_id),
        )
        conn.commit()


def set_last_limit_hash(db_path: str, session_id: str, limit_hash: str | None) -> None:
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE sessions SET last_limit_hash = ? WHERE session_id = ?",
            (limit_hash, session_id),
        )
        conn.commit()


# --- Outbox (CLI -> daemon) ---

def enqueue_outbox(db_path: str, session_id: str, chat_id: int, text: str) -> int:
    with connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO outbox (session_id, chat_id, text, created_at) VALUES (?, ?, ?, ?)",
            (session_id, chat_id, text, time.time()),
        )
        conn.commit()
        return cur.lastrowid


OUTBOX_MAX_ATTEMPTS = 5  # see mark_outbox_error — was an unbounded retry loop


def pending_outbox(db_path: str):
    """Rows still worth trying: not sent, and not given up on (see
    mark_outbox_error) — an unreachable chat/expired credential used to
    retry every 5s forever, hot-looping mail attempts and spamming the
    log (reliability pass 2026-08-10)."""
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM outbox WHERE sent = 0 AND attempts < ?",
            (OUTBOX_MAX_ATTEMPTS,),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_outbox_sent(db_path: str, outbox_id: int) -> None:
    with connect(db_path) as conn:
        conn.execute("UPDATE outbox SET sent = 1 WHERE id = ?", (outbox_id,))
        conn.commit()


def mark_outbox_error(db_path: str, outbox_id: int, error: str) -> None:
    """Records the error and counts the attempt — pending_outbox() stops
    retrying past OUTBOX_MAX_ATTEMPTS instead of hot-looping forever on a
    message that can never send (dead chat, revoked credential, ...)."""
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE outbox SET error = ?, attempts = attempts + 1 WHERE id = ?",
            (error, outbox_id),
        )
        conn.commit()


# --- Inbox (daemon -> CLI/session) ---

def store_inbox_message(db_path: str, chat_id: int, dc_msg_id: int, text: str) -> None:
    with connect(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO inbox (chat_id, dc_msg_id, text, received_at) "
            "VALUES (?, ?, ?, ?)",
            (chat_id, dc_msg_id, text, time.time()),
        )
        conn.commit()


def fetch_unconsumed(db_path: str, session_id: str, chat_id: int, limit: int | None = None):
    """Reads unconsumed messages and marks them consumed in the same
    step — fine for the CLI (`check` prints to stdout immediately, that
    can't itself fail) but NOT for the tmux dispatcher, which has a
    real step (send_keys) that can fail between reading and delivering.
    See peek_unconsumed/mark_consumed for that path."""
    with connect(db_path) as conn:
        query = "SELECT * FROM inbox WHERE chat_id = ? AND consumed_by IS NULL ORDER BY id ASC"
        params: list = [chat_id]
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        rows = conn.execute(query, params).fetchall()
        result = [dict(r) for r in rows]
        if result:
            ids = [r["id"] for r in result]
            conn.executemany(
                "UPDATE inbox SET consumed_by = ? WHERE id = ?",
                [(session_id, i) for i in ids],
            )
            conn.commit()
        return result


def peek_unconsumed(db_path: str, chat_id: int):
    """Like fetch_unconsumed, but read-only — doesn't mark anything
    consumed. Pair with mark_consumed() once delivery actually
    succeeded, so a tmux send-keys failure leaves the message pending
    for retry instead of silently disappearing (reliability pass
    2026-08-10 — found live: consume-then-deliver meant a dead pane
    between the read and the send lost the message for good, and by
    then the only other copy — on the bot's mail server — was already
    deleted)."""
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM inbox WHERE chat_id = ? AND consumed_by IS NULL ORDER BY id ASC",
            (chat_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_consumed(db_path: str, consumer: str, msg_ids: list[int]) -> None:
    if not msg_ids:
        return
    with connect(db_path) as conn:
        conn.executemany(
            "UPDATE inbox SET consumed_by = ? WHERE id = ? AND consumed_by IS NULL",
            [(consumer, i) for i in msg_ids],
        )
        conn.commit()
