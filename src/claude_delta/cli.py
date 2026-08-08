"""
CLI для сессии Claude Code: говорит только с sqlite drop-box (store.py),
никогда напрямую с Delta Chat — это монополия демона (daemon.py).

Команды:
  create-session <session_id> [--name NAME]   -> печатает chat_id
  send <session_id> <text>
  check <session_id>                          -> печатает новые сообщения (JSON lines)
  close <session_id>
"""
import argparse
import json
import os
import sys
import time

from . import store

DB_PATH = os.environ.get("DELTA_STORE_DB", "./bridge.sqlite3")
REQUEST_TIMEOUT_SEC = 20


def cmd_create_session(args):
    session_id = args.session_id
    name = args.name or f"Claude Code — {session_id}"

    existing = store.get_session(DB_PATH, session_id)
    if existing and existing["status"] == "armed":
        print(existing["chat_id"])
        return 0

    store.request_session(DB_PATH, session_id, name)

    deadline = time.time() + REQUEST_TIMEOUT_SEC
    while time.time() < deadline:
        status = store.get_session_request_status(DB_PATH, session_id)
        if status and status["status"] == "ready":
            print(status["chat_id"])
            return 0
        if status and status["status"] == "error":
            print(f"ошибка создания сессии: {status['error']}", file=sys.stderr)
            return 1
        time.sleep(0.5)

    print("таймаут: демон не ответил — запущен ли claude-delta-daemon?", file=sys.stderr)
    return 1


def cmd_send(args):
    sess = store.get_session(DB_PATH, args.session_id)
    if not sess:
        print("нет такой сессии — сначала create-session", file=sys.stderr)
        return 1
    store.enqueue_outbox(DB_PATH, args.session_id, sess["chat_id"], args.text)
    return 0


def cmd_check(args):
    sess = store.get_session(DB_PATH, args.session_id)
    if not sess:
        print("нет такой сессии", file=sys.stderr)
        return 1
    msgs = store.fetch_unconsumed(DB_PATH, args.session_id, sess["chat_id"])
    for m in msgs:
        print(json.dumps({"text": m["text"], "received_at": m["received_at"]}, ensure_ascii=False))
    return 0


def cmd_close(args):
    sess = store.get_session(DB_PATH, args.session_id)
    if not sess:
        print("нет такой сессии", file=sys.stderr)
        return 1
    store.enqueue_outbox(
        DB_PATH, args.session_id, sess["chat_id"],
        "— сессия неактивна, дальше сюда можно не писать —",
    )
    store.disarm_session(DB_PATH, args.session_id)
    return 0


def main():
    parser = argparse.ArgumentParser(prog="claude-delta")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create-session")
    p.add_argument("session_id")
    p.add_argument("--name")
    p.set_defaults(func=cmd_create_session)

    p = sub.add_parser("send")
    p.add_argument("session_id")
    p.add_argument("text")
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("check")
    p.add_argument("session_id")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("close")
    p.add_argument("session_id")
    p.set_defaults(func=cmd_close)

    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
