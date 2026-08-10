"""
Bridge daemon: the only process holding an open connection to Delta Chat
(deltachat-rpc-server). Serves the pending queue from sqlite (store.py) —
session chat creation, sending, receiving.

Configuration — environment variables:
  DELTA_ADDR, DELTA_PASSWORD  — bot mailbox
  DELTA_PEER_ADDR             — user's address (personal account)
  DELTA_ACCOUNTS_DIR          — where deltachat-core keeps its own db
  DELTA_STORE_DB              — path to the drop-box sqlite (store.py)

Loop: every iteration (a few seconds) processes pending session_requests
and pending outbox, then checks for new messages across all armed
sessions. Once every FALLBACK_RESTART_SEC the daemon forces
stop_io()/start_io() — a safety net in case the core's internal IMAP
scheduler silently got stuck (see docs/design.md in the vault).
"""
import hashlib
import logging
import os
import signal
import sys
import time

from . import store, stt, tmux
from .bridge import Bridge
from .prompt_detect import (
    extract_context,
    extract_limit_notice,
    format_for_chat,
    format_limit_notice,
    is_limit_notice,
    is_permission_prompt,
)

VOICE_VIEW_TYPES = {"Voice", "Audio"}

LOOP_INTERVAL_SEC = 5
FALLBACK_RESTART_SEC = 10 * 60  # 10 minutes, see design.md

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("claude_delta.daemon")

_running = True

# session_id -> hash of the captured text seen on the *previous* poll but
# not yet confirmed stable (see _process_tmux_prompts). Not persisted —
# a daemon restart just costs one extra stability-wait cycle, harmless.
_pending_prompt_hash: dict[str, str] = {}


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()


def _handle_signal(signum, frame):
    global _running
    log.info("получен сигнал %s, завершаюсь", signum)
    _running = False


def _process_session_requests(bridge: Bridge, db_path: str):
    for req in store.pending_session_requests(db_path):
        session_id, name = req["session_id"], req["name"]
        try:
            chat_id = bridge.create_session_group(name)
            store.fulfill_session_request(db_path, session_id, chat_id)
            log.info("сессия %s: создан чат id=%s", session_id, chat_id)
        except Exception as e:
            store.fail_session_request(db_path, session_id, repr(e))
            log.exception("сессия %s: ошибка создания чата", session_id)


def _process_outbox(bridge: Bridge, db_path: str):
    for item in store.pending_outbox(db_path):
        try:
            bridge.send_text(item["chat_id"], item["text"])
            store.mark_outbox_sent(db_path, item["id"])
            log.info("outbox #%s отправлен в чат %s", item["id"], item["chat_id"])
        except Exception as e:
            store.mark_outbox_error(db_path, item["id"], repr(e))
            log.exception("outbox #%s: ошибка отправки", item["id"])


def _process_inbox(bridge: Bridge, db_path: str):
    """One account-wide fetch (see Bridge.fetch_all_fresh_messages),
    routed to armed sessions by chat_id — replaces the old per-session
    chat.get_messages() loop (reliability pass 2026-08-10, see the
    docstring on fetch_all_fresh_messages for why that scaled badly)."""
    sessions_by_chat = {s["chat_id"]: s for s in store.armed_sessions(db_path)}
    if not sessions_by_chat:
        return

    processed_by_chat: dict[int, list[int]] = {}
    for msg in bridge.fetch_all_fresh_messages():
        chat_id = msg["chat_id"]
        sess = sessions_by_chat.get(chat_id)
        if sess is None:
            # Not an armed session's chat (old test/closed chat) —
            # fetch_all_fresh_messages already marked it seen account-
            # wide; nothing else to do with it here.
            continue
        text = msg["text"]
        if msg["view_type"] in VOICE_VIEW_TYPES and msg["file"]:
            try:
                transcript = stt.transcribe(msg["file"])
                text = f"[голосовое] {transcript}"
            except Exception:
                log.exception("сессия %s: ошибка распознавания msg_id=%s", sess["session_id"], msg["id"])
                text = "[голосовое — распознать не удалось]"
        store.store_inbox_message(db_path, chat_id, msg["id"], text)
        log.info("сессия %s: новое сообщение (msg_id=%s) %r", sess["session_id"], msg["id"], text[:60])
        processed_by_chat.setdefault(chat_id, []).append(msg["id"])

    # Delete only after everything (including STT) has been processed —
    # see the comment in Bridge.delete_processed.
    for chat_id, ids in processed_by_chat.items():
        try:
            bridge.delete_processed(ids)
        except Exception:
            log.exception("чат %s: ошибка удаления обработанных сообщений", chat_id)


def _clear_pane_state(db_path: str, session_id: str):
    """Drops every tmux-registration-scoped bit of state for a session:
    both dedup hashes, both stability-pending entries. Used both when the
    pane is confirmed gone and when neither shape is currently showing."""
    store.clear_tmux(db_path, session_id)
    _pending_prompt_hash.pop(f"{session_id}:last_prompt_hash", None)
    _pending_prompt_hash.pop(f"{session_id}:last_limit_hash", None)


def _process_tmux_prompts(db_path: str):
    """tmux dispatcher: detects the harness's permission prompt in
    registered panes and forwards it verbatim to Delta Chat — without
    this step the session physically cannot reach the chat itself while
    it's blocked on the dialog (see design.md, "Architectural pivot")."""
    for sess in store.armed_sessions(db_path):
        target = sess.get("tmux_target")
        if not target:
            continue
        session_id, chat_id = sess["session_id"], sess["chat_id"]

        if not tmux.pane_alive(target):
            log.info("сессия %s: панель %s больше не существует, снимаю регистрацию", session_id, target)
            _clear_pane_state(db_path, session_id)
            continue

        try:
            text = tmux.capture_pane(target)
        except Exception:
            log.exception("сессия %s: ошибка capture-pane на %s", session_id, target)
            continue

        # Two independent shapes worth forwarding: an interactive
        # permission prompt (cursor + numbered options — needs a
        # decision), or a passive usage-limit banner (no options — just
        # tells the human why the session went quiet, see
        # prompt_detect.is_limit_notice for why it needs its own check).
        # Separate hash columns (last_prompt_hash/last_limit_hash) — a
        # banner can sit underneath a prompt that opens and resolves on
        # top of it; sharing one column meant the banner's dedup got
        # clobbered by the prompt's and got re-sent every time the prompt
        # cleared (reliability pass 2026-08-10).
        if is_permission_prompt(text):
            hash_field = "last_prompt_hash"
            # Hashing extract_context (includes the command/tool detail),
            # not just the cursor-line-onward tail — two different Bash
            # approvals that happen to share the same "1. Yes / 2. No"
            # options previously hashed identically and the second one
            # silently never got forwarded (confirmed on real fixtures,
            # reliability pass 2026-08-10). extract_context already stops
            # at the harness's rule line, so it's not reintroducing the
            # earlier animated-scrollback instability — that lived above
            # the rule line, outside extract_context's range.
            hash_source = extract_context(text)
            forward_text = format_for_chat(text)
        elif is_limit_notice(text):
            hash_field = "last_limit_hash"
            hash_source = extract_limit_notice(text)
            forward_text = format_limit_notice(text)
        else:
            # Neither shape is showing — whatever was last known is
            # resolved (by *some* means, not necessarily our own
            # injection, e.g. answered directly at the keyboard). Stale
            # "still waiting" state otherwise breaks the next distinct
            # occurrence's dedup and misleads delivery-time decisions.
            if sess.get("last_prompt_hash") or sess.get("last_limit_hash"):
                store.set_last_prompt_hash(db_path, session_id, None)
                store.set_last_limit_hash(db_path, session_id, None)
            _pending_prompt_hash.pop(f"{session_id}:last_prompt_hash", None)
            _pending_prompt_hash.pop(f"{session_id}:last_limit_hash", None)
            continue

        h = _hash_text(hash_source)
        pending_key = f"{session_id}:{hash_field}"
        if h == sess.get(hash_field):
            continue  # this exact prompt/banner was already forwarded
        if _pending_prompt_hash.get(pending_key) != h:
            # First time seeing this text — wait for confirmation on the
            # next poll that it's stable (guards against capturing
            # mid-render).
            _pending_prompt_hash[pending_key] = h
            continue

        try:
            # Into the outbox, the same path as regular outgoing messages
            # (_process_outbox) — don't duplicate error handling/retries.
            store.enqueue_outbox(db_path, session_id, chat_id, forward_text)
        except Exception:
            log.exception("сессия %s: ошибка форвардинга промпта", session_id)
            continue
        if hash_field == "last_prompt_hash":
            store.set_last_prompt_hash(db_path, session_id, h)
        else:
            store.set_last_limit_hash(db_path, session_id, h)
        _pending_prompt_hash.pop(pending_key, None)
        log.info("сессия %s: %s форварднут в чат %s", session_id, hash_field, chat_id)


# Design note (2026-08-10): earlier versions of this function tagged
# non-dialog injections ("[Delta Chat] ...", later with an embedded
# `send` command) so the session could tell a phone message apart from
# direct keyboard input. Reverted on request — the point of tmux
# injection is to be *indistinguishable* from someone physically typing:
# raw text in, Enter, exactly what a human at the keyboard would produce,
# queued by the harness's own input handling rather than landing wherever
# the cursor happens to be. What the session does with any given turn
# (mirror a reply back to the chat, stay quiet, etc.) is entirely
# delta-chat.md's judgment call — no code-side tagging or nudging.
def _process_tmux_delivery(db_path: str):
    """Injects Delta Chat replies directly into the tmux pane — replaces
    the in-session /loop poll entirely for tmux-registered sessions.

    Uses peek_unconsumed/mark_consumed rather than the atomic
    fetch_unconsumed: a message must only count as delivered once
    send_keys has actually run. Marking it consumed at read time (the
    original approach — same pattern manual CLI `check` still uses,
    fine there since printing to stdout can't itself fail) meant a dead
    pane between the read and the send lost the message outright — and
    by then the only other copy, on the bot's mail server, had already
    been deleted by _process_inbox (reliability pass 2026-08-10)."""
    for sess in store.armed_sessions(db_path):
        target = sess.get("tmux_target")
        if not target:
            continue
        session_id, chat_id = sess["session_id"], sess["chat_id"]

        # A separate "consumer" tag — doesn't collide with manual CLI
        # `check` (that one uses consumed_by=session_id; here — its own
        # prefix) *for messages neither has claimed yet*. Once either
        # side claims a message it's gone for the other — by design,
        # each message should be delivered through exactly one path for
        # a given session, not both.
        msgs = store.peek_unconsumed(db_path, chat_id)
        if not msgs:
            continue

        if not tmux.pane_alive(target):
            log.info("сессия %s: панель %s больше не существует, снимаю регистрацию", session_id, target)
            _clear_pane_state(db_path, session_id)
            continue

        consumer = f"dispatcher:{session_id}"
        for m in msgs:
            try:
                tmux.send_keys(target, m["text"])
            except Exception:
                log.exception("сессия %s: ошибка send-keys в %s", session_id, target)
                break  # leave this and later messages unconsumed — retried next tick, in order
            store.mark_consumed(db_path, consumer, [m["id"]])
            log.info("сессия %s: ответ инжектирован в %s: %r", session_id, target, m["text"][:60])
        # Deliberately NOT resetting last_prompt_hash/last_limit_hash
        # here (earlier versions did, unconditionally, "the pane
        # changed"). If the reply didn't actually resolve the dialog —
        # free text the harness didn't accept, or delivery raced a
        # detection poll — the prompt is still showing next tick with
        # the *same* content, and resetting the hash made it look "new"
        # again: same prompt re-forwarded roughly every 10s until
        # someone answers it for real (confirmed live 2026-08-10 — the
        # rapid-fire "форварднут" bursts during testing). Letting
        # _process_tmux_prompts's own detection be the sole authority —
        # it already clears both hashes the moment neither shape is
        # showing anymore — means a still-open prompt is correctly
        # recognized as already-forwarded, and a genuinely new one (the
        # pane necessarily passes through a non-prompt state first) gets
        # detected fresh regardless.


def run():
    addr = os.environ["DELTA_ADDR"]
    password = os.environ["DELTA_PASSWORD"]
    peer_addr = os.environ["DELTA_PEER_ADDR"]
    accounts_dir = os.environ.get("DELTA_ACCOUNTS_DIR", "./accounts")
    db_path = os.environ.get("DELTA_STORE_DB", "./bridge.sqlite3")

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    log.info("старт демона, ящик=%s, peer=%s", addr, peer_addr)

    # Eager, not lazy — see stt.preload docstring. Blocks startup for a
    # few seconds (longer, once, if the model still needs downloading)
    # instead of blocking the main loop the first time a voice message
    # arrives, for every armed session, mid-session.
    stt.preload()

    last_restart = time.time()
    with Bridge(accounts_dir, addr, password, peer_addr) as bridge:
        while _running:
            try:
                _process_session_requests(bridge, db_path)
                _process_tmux_prompts(db_path)  # may add to outbox — before _process_outbox
                _process_outbox(bridge, db_path)
                _process_inbox(bridge, db_path)
                _process_tmux_delivery(db_path)  # consumes what _process_inbox stored above
            except Exception:
                log.exception("ошибка в цикле демона")

            if time.time() - last_restart > FALLBACK_RESTART_SEC:
                log.info("периодический перезапуск IO (fallback, см. design.md)")
                bridge.account.stop_io()
                bridge.account.start_io()
                last_restart = time.time()

            time.sleep(LOOP_INTERVAL_SEC)

    log.info("демон остановлен")


if __name__ == "__main__":
    sys.exit(run())
