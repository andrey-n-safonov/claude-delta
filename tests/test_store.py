"""
Plain-assert test (no pytest, see test_prompt_detect.py). Run with:

    python3 tests/test_store.py

Covers the reliability-critical store.py behaviors added/fixed in the
2026-08-10 reliability pass — previously store.py had no test coverage
at all (flagged in review).
"""
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from claude_delta import store


def _fresh_db():
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
    tmp.close()
    return tmp.name


def run():
    failures = []

    # --- H4: off -> on must not leave a half-armed state ---
    db = _fresh_db()
    store.request_session(db, "s1", "topic")
    store.fulfill_session_request(db, "s1", chat_id=42)
    if store.get_session(db, "s1")["status"] != "armed":
        failures.append("fulfill_session_request did not arm the session")

    store.disarm_session(db, "s1")
    if store.get_session(db, "s1")["status"] != "disarmed":
        failures.append("disarm_session did not disarm")

    # This is what cmd_create_session now does for an existing (any
    # status) session instead of round-tripping through session_requests
    # again — the bug was that the stale 'ready' row there made a second
    # arm *look* successful without ever flipping sessions.status back.
    chat_id = store.rearm_session(db, "s1")
    if chat_id != 42:
        failures.append(f"rearm_session returned wrong chat_id: {chat_id!r}")
    if store.get_session(db, "s1")["status"] != "armed":
        failures.append("rearm_session did not re-arm")
    if store.rearm_session(db, "no-such-session") is not None:
        failures.append("rearm_session should return None for an unknown session_id")

    # --- H1: a message must not be lost if delivery fails between read and confirm ---
    db = _fresh_db()
    store.store_inbox_message(db, chat_id=7, dc_msg_id=1, text="hello")
    peeked = store.peek_unconsumed(db, chat_id=7)
    if len(peeked) != 1:
        failures.append(f"peek_unconsumed: expected 1 message, got {len(peeked)}")
    # Simulate a failed delivery: peek again without marking consumed —
    # must still be there (the whole point of splitting peek/mark).
    peeked_again = store.peek_unconsumed(db, chat_id=7)
    if len(peeked_again) != 1:
        failures.append("peek_unconsumed lost a message that was never mark_consumed'd")
    store.mark_consumed(db, "dispatcher:s1", [peeked[0]["id"]])
    if store.peek_unconsumed(db, chat_id=7):
        failures.append("mark_consumed did not actually remove the message from the unconsumed set")
    # A second consumer must not be able to steal an already-consumed row.
    store.store_inbox_message(db, chat_id=7, dc_msg_id=2, text="world")
    row_id = store.peek_unconsumed(db, chat_id=7)[0]["id"]
    store.mark_consumed(db, "dispatcher:s1", [row_id])
    store.mark_consumed(db, "some-other-consumer", [row_id])  # should be a no-op
    with store.connect(db) as conn:
        consumed_by = conn.execute("SELECT consumed_by FROM inbox WHERE id = ?", (row_id,)).fetchone()["consumed_by"]
    if consumed_by != "dispatcher:s1":
        failures.append(f"mark_consumed let a second consumer overwrite the first: {consumed_by!r}")

    # fetch_unconsumed (the CLI's atomic read-and-claim path) must still
    # work as before — a different code path than peek/mark, used by
    # `check` for non-tmux sessions.
    db = _fresh_db()
    store.store_inbox_message(db, chat_id=9, dc_msg_id=1, text="a")
    store.store_inbox_message(db, chat_id=9, dc_msg_id=2, text="b")
    got = store.fetch_unconsumed(db, "s1", chat_id=9)
    if [m["text"] for m in got] != ["a", "b"]:
        failures.append(f"fetch_unconsumed returned unexpected content/order: {got}")
    if store.fetch_unconsumed(db, "s1", chat_id=9):
        failures.append("fetch_unconsumed did not mark messages consumed on read")

    # --- M5: outbox must stop retrying after OUTBOX_MAX_ATTEMPTS, not hot-loop forever ---
    db = _fresh_db()
    outbox_id = store.enqueue_outbox(db, "s1", chat_id=9, text="hi")
    for _ in range(store.OUTBOX_MAX_ATTEMPTS):
        pending = store.pending_outbox(db)
        if not any(p["id"] == outbox_id for p in pending):
            failures.append("pending_outbox dropped the message before it hit OUTBOX_MAX_ATTEMPTS")
            break
        store.mark_outbox_error(db, outbox_id, "simulated failure")
    still_pending = [p["id"] for p in store.pending_outbox(db)]
    if outbox_id in still_pending:
        failures.append("pending_outbox kept retrying past OUTBOX_MAX_ATTEMPTS")

    # A message that does send should never show up again regardless of attempts.
    db = _fresh_db()
    ok_id = store.enqueue_outbox(db, "s1", chat_id=9, text="hi")
    store.mark_outbox_sent(db, ok_id)
    if any(p["id"] == ok_id for p in store.pending_outbox(db)):
        failures.append("pending_outbox re-offered an already-sent message")

    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)

    print("OK — rearm/peek-mark/outbox-backoff checks passed")


if __name__ == "__main__":
    run()
