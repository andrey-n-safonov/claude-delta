"""
CLI for the Claude Code session: talks only to the sqlite drop-box
(store.py), never directly to Delta Chat — that's the daemon's monopoly
(daemon.py).

Commands:
  create-session [session_id] [--name NAME] [--message TEXT]  -> prints chat_id
  register-tmux [session_id] [--target TARGET]  -> prints target (defaults
      to $TMUX_PANE) — turns on the tmux dispatcher for the session:
      permission prompts get forwarded to the chat, replies get injected
      back via tmux send-keys, without any action from the session's code
      (see daemon.py, _process_tmux_prompts/_process_tmux_delivery)
  send [session_id] <text>
  check [session_id]                          -> prints new messages (JSON lines)
  close [session_id]

session_id is optional everywhere and defaults to $CLAUDE_CODE_SESSION_ID
(see _session_id()) — deliberately, so the Claude Code caller never has
to spell the variable out as a shell expansion in the command it runs;
a literal "$CLAUDE_CODE_SESSION_ID" in the command text makes Claude
Code's permission classifier prompt every time no matter how the Bash
allowlist is set up, since the substituted value isn't known until
execution.

A new Delta Chat group chat stays "unpromoted" (invisible to the peer)
until at least one message has been sent — so create-session without
--message creates a chat the user will never see. --message is required
by design (a short session topic).
"""
import argparse
import json
import os
import sys
import time

from . import store

DB_PATH = os.environ.get("DELTA_STORE_DB", "./bridge.sqlite3")
REQUEST_TIMEOUT_SEC = 20


def _session_id(args):
    """session_id positional arg, falling back to $CLAUDE_CODE_SESSION_ID.

    The fallback matters beyond convenience: a literal "$CLAUDE_CODE_SESSION_ID"
    in the shell command is a variable expansion, which Claude Code's
    permission classifier never blanket-approves via a Bash prefix allow
    rule (the substituted value is unknown at match time) — every call
    prompts for confirmation regardless of allowlisting. Omitting the arg
    and letting the CLI read the already-inherited env var itself keeps
    the command a plain literal that a prefix rule can match cleanly.
    """
    sid = args.session_id or os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not sid:
        print(
            "session_id не передан и $CLAUDE_CODE_SESSION_ID не установлена",
            file=sys.stderr,
        )
        sys.exit(1)
    return sid


def cmd_create_session(args):
    session_id = _session_id(args)
    name = args.name or f"Claude Code — {session_id}"

    existing = store.get_session(DB_PATH, session_id)
    if existing and existing["status"] == "armed":
        print(existing["chat_id"])
        return 0
    if existing:
        # off -> on: the chat already exists, no need to round-trip
        # through the daemon's session_requests queue at all — just flip
        # status back to armed in place. Reliability note (2026-08-10):
        # going through request_session() here used to no-op (INSERT OR
        # IGNORE against the still-'ready' row from the *first* on), so
        # the wait loop below reported success immediately — but nothing
        # ever set sessions.status back to 'armed', so the chat looked
        # alive (send still worked, same chat_id) while delivery/
        # forwarding silently never resumed. See store.rearm_session.
        chat_id = store.rearm_session(DB_PATH, session_id)
        store.enqueue_outbox(DB_PATH, session_id, chat_id, args.message or name)
        print(chat_id)
        return 0

    store.request_session(DB_PATH, session_id, name)

    deadline = time.time() + REQUEST_TIMEOUT_SEC
    while time.time() < deadline:
        status = store.get_session_request_status(DB_PATH, session_id)
        if status and status["status"] == "ready":
            chat_id = status["chat_id"]
            # Without a first message the group chat stays "unpromoted"
            # and never shows up for the peer.
            store.enqueue_outbox(DB_PATH, session_id, chat_id, args.message or name)
            print(chat_id)
            return 0
        if status and status["status"] == "error":
            print(f"ошибка создания сессии: {status['error']}", file=sys.stderr)
            return 1
        time.sleep(0.5)

    print("таймаут: демон не ответил — запущен ли claude-delta-daemon?", file=sys.stderr)
    return 1


def cmd_register_tmux(args):
    session_id = _session_id(args)
    sess = store.get_session(DB_PATH, session_id)
    if not sess:
        print("нет такой сессии — сначала create-session", file=sys.stderr)
        return 1
    target = args.target or os.environ.get("TMUX_PANE")
    if not target:
        print(
            "не в tmux и --target не передан — permission-промпты форвардиться не будут "
            "(нужен $TMUX_PANE или явный --target)",
            file=sys.stderr,
        )
        return 1
    store.register_tmux(DB_PATH, session_id, target)
    print(target)
    return 0


def cmd_send(args):
    session_id = _session_id(args)
    sess = store.get_session(DB_PATH, session_id)
    if not sess:
        print("нет такой сессии — сначала create-session", file=sys.stderr)
        return 1
    store.enqueue_outbox(DB_PATH, session_id, sess["chat_id"], args.text)
    return 0


def cmd_check(args):
    session_id = _session_id(args)
    sess = store.get_session(DB_PATH, session_id)
    if not sess:
        print("нет такой сессии", file=sys.stderr)
        return 1
    msgs = store.fetch_unconsumed(DB_PATH, session_id, sess["chat_id"])
    for m in msgs:
        print(json.dumps({"text": m["text"], "received_at": m["received_at"]}, ensure_ascii=False))
    return 0


def cmd_status(args):
    session_id = _session_id(args)
    sess = store.get_session(DB_PATH, session_id)
    if not sess:
        print("нет такой сессии", file=sys.stderr)
        return 1
    print(f"status={sess['status']} chat_id={sess['chat_id']} tmux_target={sess.get('tmux_target')}")
    return 0


def cmd_close(args):
    session_id = _session_id(args)
    sess = store.get_session(DB_PATH, session_id)
    if not sess:
        print("нет такой сессии", file=sys.stderr)
        return 1
    store.enqueue_outbox(
        DB_PATH, session_id, sess["chat_id"],
        "— сессия неактивна, дальше сюда можно не писать —",
    )
    store.disarm_session(DB_PATH, session_id)
    store.clear_tmux(DB_PATH, session_id)  # dispatcher stops touching the pane
    return 0


def main():
    parser = argparse.ArgumentParser(prog="claude-delta")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create-session")
    p.add_argument("session_id", nargs="?", default=None,
                    help="по умолчанию — $CLAUDE_CODE_SESSION_ID из окружения")
    p.add_argument("--name")
    p.add_argument("--message", help="первое сообщение — без него чат не появится у собеседника")
    p.set_defaults(func=cmd_create_session)

    p = sub.add_parser("register-tmux")
    p.add_argument("session_id", nargs="?", default=None,
                    help="по умолчанию — $CLAUDE_CODE_SESSION_ID из окружения")
    p.add_argument("--target", help="tmux pane-id, по умолчанию $TMUX_PANE")
    p.set_defaults(func=cmd_register_tmux)

    p = sub.add_parser("send")
    p.add_argument("session_id", nargs="?", default=None,
                    help="по умолчанию — $CLAUDE_CODE_SESSION_ID из окружения")
    p.add_argument("text")
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("check")
    p.add_argument("session_id", nargs="?", default=None,
                    help="по умолчанию — $CLAUDE_CODE_SESSION_ID из окружения")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("close")
    p.add_argument("session_id", nargs="?", default=None,
                    help="по умолчанию — $CLAUDE_CODE_SESSION_ID из окружения")
    p.set_defaults(func=cmd_close)

    p = sub.add_parser("status")
    p.add_argument("session_id", nargs="?", default=None,
                    help="по умолчанию — $CLAUDE_CODE_SESSION_ID из окружения")
    p.set_defaults(func=cmd_status)

    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
