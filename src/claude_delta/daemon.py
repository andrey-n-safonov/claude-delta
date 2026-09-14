"""
Bridge daemon: the only process holding an open connection to Delta Chat
(deltachat-rpc-server). Serves the pending queue from sqlite (store.py) —
session chat creation, sending, receiving.

Configuration — environment variables:
  DELTA_ADDR, DELTA_PASSWORD  — bot mailbox
  DELTA_PEER_ADDR             — user's address (personal account)
  DELTA_ACCOUNTS_DIR          — where deltachat-core keeps its own db
  DELTA_STORE_DB              — path to the drop-box sqlite (store.py)
  DELTA_SPAWN_BACKENDS        — control-chat "/new-session" backends, "name=cmd,..."
                                 (default: proxy=claude-proxy,deep=claude-deep,
                                 mimo=claude-mimo — this deployment's own
                                 wrapper scripts, override or clear for another)
  DELTA_SPAWN_TMUX_SESSION    — tmux session "/new-session" opens windows in (default: main)
  DELTA_SPAWN_FOLDERS         — control-chat "/new-session"/"/list-folders" working
                                 directories, "name=path,..." (default: vault=~/obsidian_vault
                                 only — this deployment's own project folders, each
                                 presumably with its own .mcp.json; every path must already
                                 be trust-accepted for every backend in DELTA_SPAWN_BACKENDS)
  DELTA_SPAWN_DEFAULT_FOLDER  — which DELTA_SPAWN_FOLDERS name "/new-session" uses when
                                 none is given explicitly (default: vault)
  DELTA_SPAWN_READY_TIMEOUT_SEC — how long to wait for a spawned pane to
                                 start reading input before giving up (default: 20)

Loop: every iteration (a few seconds) processes pending session_requests
and pending outbox, then checks for new messages across all armed
sessions. Once every FALLBACK_RESTART_SEC the daemon forces
stop_io()/start_io() — a safety net in case the core's internal IMAP
scheduler silently got stuck (see docs/design.md in the vault).
"""
import hashlib
import logging
import os
import re
import shlex
import signal
import sys
import time
import uuid

from . import ocr, sleepinhibit, store, stt, tmux
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
IMAGE_VIEW_TYPES = {"Image", "Sticker"}

LOOP_INTERVAL_SEC = 5
FALLBACK_RESTART_SEC = 10 * 60  # 10 minutes, see design.md

# /mode <target> — a chat command the daemon handles itself, never typed
# into the pane as a regular message (see _process_mode_commands). Only
# an explicit slash-command is recognized, deliberately not free-form
# text like "switch to manual": the daemon has no NLU, and matching
# loosely (bare "auto"/"manual" words) risks firing on an ordinary chat
# reply that happens to contain one.
# Four distinct modes (confirmed live 2026-09-08, see journal — an
# earlier version of this code wrongly merged "accept-edits" into
# "auto", a misread of the pane that made two real, different
# permission levels look like one flickering label). Ring order,
# Shift-Tab always moves forward: manual -> accept-edits -> plan -> auto.
_MODE_COMMAND_RE = re.compile(r"^/mode\s+(\S+)\s*$", re.IGNORECASE)
_MODE_ALIASES = {"acceptedits": "accept-edits", "edits": "accept-edits", "bypass": "auto"}
MODE_CYCLE_MAX_PRESSES = 6  # one full lap of the 4-state ring is 4, plus margin

# Sent as its own injected line right after every delivered batch (never
# merged into the delivered text itself — that stays verbatim, see
# tmux.send_keys). Long sessions lose track of the standing /delta-chat
# rule (context compaction, or it just wasn't salient when the reply
# actually mattered) and answer normally instead of via `send` — the
# reply then never reaches the phone. Repeating the rule at the moment
# it's actually needed is cheaper than hoping it's still in attention
# from whenever `on` ran (reported live 2026-08-11: session replied
# in-pane only, chat never got a response).
#
# Deliberately just a stable, language-neutral tag, not the instruction
# text itself — the daemon has no business knowing what language a given
# session/skill runs in, and duplicating the rule's wording in both
# daemon.py and the skill doc would drift. What the tag *means* is
# documented exactly once, in deploy/commands/delta-chat.md, which
# `/delta-chat on` already reads and follows regardless of language.
_DELTA_CHAT_REMINDER_TAG = "[delta-chat:reminder]"

# Control-chat protocol (2026-09-14, see design.md "План: control-протокол
# через личные сообщения"): the bot's own 1:1 chat with the known peer —
# resolved once at startup via Bridge.control_chat_id() — accepts a small
# set of session-management commands, parsed here and never passed
# through to any tmux pane (there isn't one for this chat_id anyway, it's
# not in the sessions table). Same "no separate sender check" reasoning
# as the rest of this module's trust model: a 1:1 chat can only ever
# contain the bot and that one contact.
# Leading "/" optional and a short alias accepted alongside the full
# name (2026-09-14, on request — typing exact hyphenated command names
# on a phone keyboard was the actual complaint). Safe to loosen here
# specifically: this is the one chat that never reaches a tmux pane (see
# the trust-model comment above), so a false-positive match here costs
# nothing worse than an unexpected reply, never a misfired keystroke
# into someone's session. Unmatched text still falls through to
# _CONTROL_HELP (see _process_control_commands) — every miss is
# self-documenting, not a silent no-op.
_LIST_BACKENDS_COMMAND_RE = re.compile(r"^/?(?:list-backends|lb)\s*$", re.IGNORECASE)
_LIST_FOLDERS_COMMAND_RE = re.compile(r"^/?(?:list-folders|lf)\s*$", re.IGNORECASE)
_LIST_SESSIONS_COMMAND_RE = re.compile(r"^/?(?:list-sessions|ls)\s*$", re.IGNORECASE)
_DELETE_SESSION_COMMAND_RE = re.compile(r"^/?(?:delete-session|ds)\s+(\S+)\s*$", re.IGNORECASE)
_NEW_SESSION_COMMAND_RE = re.compile(r"^/?(?:new-session|ns)\s+(\S+)(?:\s+(.+))?$", re.IGNORECASE | re.DOTALL)

def _parse_name_value_pairs(spec: str) -> dict[str, str]:
    """"name=value,name=value" -> {name: value}. Shared parser for every
    control-chat registry below (backends, folders) — never hardcode
    these dicts themselves in source (2026-09-14 review: the backend set
    and the folder set are entirely this deployment's own choices, not
    something the daemon's logic should know by name) — the env file is
    the only place either is actually spelled out, same as every other
    host-specific value (DELTA_ADDR, DELTA_PEER_ADDR, ...)."""
    pairs = {}
    for pair in spec.split(","):
        name, sep, value = pair.strip().partition("=")
        if sep and name and value:
            pairs[name] = value
    return pairs


# Every backend wrapper this deployment knows how to spawn for the
# control-chat "/new-session" command, and the actual command each name runs.
# Each one carries its own CLAUDE_CONFIG_DIR (this daemon's default,
# battle-tested locally: proxy -> ~/.claude, deep -> ~/.claude-deepseek,
# mimo -> ~/.claude-mimo) — spawning the wrapper *script* by name here,
# never `claude` with flags reproduced in this file, so a config change
# in one wrapper (model, proxy, env) never needs mirroring here. Override
# via DELTA_SPAWN_BACKENDS in the env file for a different deployment
# (different wrapper names, or none at all) without touching source.
_BACKENDS = _parse_name_value_pairs(os.environ.get(
    "DELTA_SPAWN_BACKENDS", "proxy=claude-proxy,deep=claude-deep,mimo=claude-mimo",
))

# tmux session new windows get opened in — see tmux.spawn_window. A
# session name, not a pane — must already exist (the daemon does not
# create tmux sessions, only windows inside one).
SPAWN_TMUX_SESSION = os.environ.get("DELTA_SPAWN_TMUX_SESSION", "main")

# Working directories "/new-session" can spawn into, by name — this
# deployment's project folders, presumably each with its own .mcp.json
# (2026-09-14, on request). Every path here must already be
# trust-accepted for every backend in _BACKENDS: a fresh cwd's first-run
# trust dialog is a real risk — it isn't one of the shapes
# is_permission_prompt()/is_limit_notice() recognize, so it would sit on
# screen unforwarded and undetected, silently wedging the new session
# forever with nobody able to answer it. DEFAULT_FOLDER_NAME picks which
# entry "/new-session <backend> <task>" (no folder token) uses.
_FOLDERS = _parse_name_value_pairs(os.environ.get(
    "DELTA_SPAWN_FOLDERS", "vault=~/obsidian_vault",
))
DEFAULT_FOLDER_NAME = os.environ.get("DELTA_SPAWN_DEFAULT_FOLDER", "vault")

# Upper bound on tmux.wait_ready() after opening a fresh pane — see its
# docstring for why this is a poll, not a sleep. A pane that never
# becomes ready within this bound (dead backend, bad API key, network
# down) gets reported back to the control chat instead of typed into blind.
SPAWN_READY_TIMEOUT_SEC = float(os.environ.get("DELTA_SPAWN_READY_TIMEOUT_SEC", "20"))

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

# session_ids where the *previous* poll saw neither shape (prompt nor
# limit banner) but a hash was still on record, so the clear hasn't been
# confirmed yet — mirrors _pending_prompt_hash's stability guard, but for
# the opposite transition. Without this, a single blank render frame
# between two polls of the *same still-open* prompt (tmux redraw/scroll
# racing the capture) got read as "resolved", and the prompt reappearing
# next poll then looked brand new and got re-forwarded — confirmed from
# logs 2026-08-20: chat 26 forwarded an identical #773636 prompt three
# times in ~90s (reliability pass 2026-08-27).
_pending_clear: set[str] = set()


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


def _process_renames(bridge: Bridge, db_path: str):
    for item in store.pending_renames(db_path):
        try:
            bridge.rename_chat(item["chat_id"], item["name"])
            store.mark_rename_applied(db_path, item["id"])
            log.info("сессия %s: чат %s переименован в %r", item["session_id"], item["chat_id"], item["name"])
        except Exception as e:
            store.mark_rename_error(db_path, item["id"], repr(e))
            log.exception("rename #%s: ошибка переименования", item["id"])


def _process_deletions(bridge: Bridge, db_path: str):
    for item in store.pending_deletions(db_path):
        try:
            bridge.delete_chat(item["chat_id"])
            store.mark_deletion_applied(db_path, item["id"])
            # No chat left to ever rearm back into — unlike disarm, this
            # drops the row entirely (see store.remove_session).
            store.remove_session(db_path, item["session_id"])
            log.info("сессия %s: чат %s удалён по команде", item["session_id"], item["chat_id"])
        except Exception as e:
            store.mark_deletion_error(db_path, item["id"], repr(e))
            log.exception("deletion #%s: ошибка удаления чата", item["id"])


def _format_backend_list() -> str:
    if not _BACKENDS:
        return "бэкенды не настроены (DELTA_SPAWN_BACKENDS пуст)"
    return "\n".join(f"{name} -> {cmd}" for name, cmd in _BACKENDS.items())


def _format_folder_list() -> str:
    if not _FOLDERS:
        return "папки не настроены (DELTA_SPAWN_FOLDERS пуст)"
    lines = []
    for name, path in _FOLDERS.items():
        default_tag = " (по умолчанию)" if name == DEFAULT_FOLDER_NAME else ""
        lines.append(f"{name} -> {path}{default_tag}")
    return "\n".join(lines)


def _split_folder_and_task(rest: str) -> tuple[str, str]:
    """rest is everything "/new-session <backend>" received after the
    backend name. If its first word names a configured folder (_FOLDERS),
    that's the folder and everything after it is the task; otherwise the
    whole thing is the task and DEFAULT_FOLDER_NAME applies. Same
    "look it up in the registry, don't guess" pattern as backend
    selection — no fuzzy distinction between "a folder name" and "the
    first word of a task that happens to also be one", the registry
    itself decides."""
    rest = rest.strip()
    if not rest:
        return DEFAULT_FOLDER_NAME, ""
    first, _, remainder = rest.partition(" ")
    if first in _FOLDERS:
        return first, remainder.strip()
    return DEFAULT_FOLDER_NAME, rest


def _format_session_list(db_path: str) -> str:
    sessions = store.all_sessions(db_path)
    if not sessions:
        return "нет ни одной сессии"
    lines = [
        f"{s['session_id'][:8]}  {s['status']}  chat={s['chat_id']}  tmux={s.get('tmux_target') or '—'}"
        for s in sessions
    ]
    return "\n".join(lines)


def _handle_delete_command(db_path: str, prefix: str) -> str:
    sess = store.find_session_by_prefix(db_path, prefix)
    if sess is None:
        return f"не нашёл однозначную сессию по {prefix!r} — сверься с /list-sessions"
    store.enqueue_deletion(db_path, sess["session_id"], sess["chat_id"])
    return f"удаляю чат сессии {prefix} (chat_id={sess['chat_id']})"


def _handle_new_command(bridge: Bridge, db_path: str, backend: str, rest: str) -> str:
    """rest is everything after the backend name — "[folder] [task]", see
    _split_folder_and_task. Both folder and task are optional independent
    of each other: "/new-session deep" (default folder, no task),
    "/new-session deep pirelli" (folder, no task), "/new-session deep
    pirelli чини баг" (both), "/new-session deep чини баг" (default
    folder, task — "чини" isn't a configured folder name so the whole
    rest stays the task). No task just spins the session up and leaves it
    waiting; the user then talks to it normally through its own (freshly
    created) group chat, same as any other session — skips the
    wait_ready/send_keys dance entirely in that case, nothing to type yet
    so nothing to wait ready for."""
    cmd = _BACKENDS.get(backend.lower())
    if cmd is None:
        return f"не знаю бэкенд {backend!r} — есть {', '.join(_BACKENDS)}"

    folder_name, task = _split_folder_and_task(rest)
    cwd = _FOLDERS.get(folder_name)
    if cwd is None:
        return f"не знаю папку {folder_name!r} — есть {', '.join(_FOLDERS)} (проверь DELTA_SPAWN_DEFAULT_FOLDER)"

    session_id = str(uuid.uuid4())
    # expanduser THEN quote — folder names/paths can (and do: project
    # names in this deployment are Russian phrases with spaces) contain
    # spaces; quoting a literal "~/..." would break tilde expansion
    # since that only happens unquoted, so expand first, quote the
    # already-absolute result.
    cwd_arg = shlex.quote(os.path.expanduser(cwd))
    spawn_cmd = f"cd {cwd_arg} && {cmd} --session-id {session_id}"
    try:
        target = tmux.spawn_window(spawn_cmd, session=SPAWN_TMUX_SESSION)
    except Exception:
        log.exception("сессия %s: не удалось поднять tmux-окно (%s)", session_id, cmd)
        return "не получилось поднять новую сессию (tmux) — см. лог демона"

    chat_name = task[:60] if task else f"{cmd} — {session_id[:8]}"
    try:
        chat_id = bridge.create_session_group(chat_name)
    except Exception:
        log.exception("сессия %s: чат не создан, но tmux-окно уже открыто", session_id)
        return "tmux-окно поднято, но чат создать не вышло — см. лог демона (окно осталось, прибрать вручную)"

    store.create_session_direct(db_path, session_id, chat_id, target)

    if not task:
        # Nothing to type in — the pane is left exactly as the backend
        # started it, and this session behaves like any other armed one
        # from here on: the user just writes into its (now-promoted)
        # group chat, normal delivery picks it up.
        store.enqueue_outbox(db_path, session_id, chat_id, f"сессия {session_id[:8]} поднята ({cmd}), жду задачу")
        log.info("сессия %s: создана по команде /new-session %s %s без задачи, панель %s",
                 session_id, backend, folder_name, target)
        return f"поднимаю {cmd} в {folder_name}, session={session_id[:8]}, без задачи — пиши прямо в её чат"

    # Block here until the pane's status line proves the TUI is actually
    # reading keystrokes (see tmux.wait_ready) — typing blind into a pane
    # that hasn't switched stdin to raw mode yet risked losing the first
    # message outright (see design.md). This holds up the daemon's main
    # loop for the wait, same trade-off cycle_to_mode already makes
    # elsewhere in this codebase for tmux interactions.
    if not tmux.wait_ready(target, timeout_sec=SPAWN_READY_TIMEOUT_SEC):
        log.warning("сессия %s: панель %s не отдала признаков готовности за %sс",
                     session_id, target, SPAWN_READY_TIMEOUT_SEC)
        store.enqueue_outbox(
            db_path, session_id, chat_id,
            f"сессия поднята, но панель не откликнулась за {SPAWN_READY_TIMEOUT_SEC:.0f}с — "
            "возможно, ещё стартует или бэкенд не поднялся; напиши сюда сама задача, когда будет видно ответ",
        )
        return f"session={session_id[:8]}: панель не ответила за {SPAWN_READY_TIMEOUT_SEC:.0f}с — см. чат"

    try:
        tmux.send_keys(target, task)
    except Exception:
        log.exception("сессия %s: чат и окно созданы, но задачу напечатать не вышло", session_id)
        store.enqueue_outbox(
            db_path, session_id, chat_id,
            "сессия поднята, но задачу напечатать не вышло — набери её сюда ещё раз",
        )
        return f"session={session_id[:8]} поднята, но задачу напечатать не вышло — см. чат"

    store.enqueue_outbox(db_path, session_id, chat_id, f"сессия {session_id[:8]} поднята ({cmd})")
    log.info("сессия %s: создана по команде /new-session %s %s, панель %s", session_id, backend, folder_name, target)
    return f"поднимаю {cmd} в {folder_name}, session={session_id[:8]}, задача отправлена"


_CONTROL_HELP = (
    "команды (слэш необязателен):\n"
    "list-backends / lb\n"
    "list-folders / lf\n"
    "list-sessions / ls\n"
    "delete-session <id> / ds <id>\n"
    "new-session <backend> [папка] [задача] / ns <backend> [папка] [задача]"
)


def _process_control_commands(bridge: Bridge, db_path: str, control_chat_id: int):
    """Control-chat command dispatcher — see the module-level comment
    above _LIST_BACKENDS_COMMAND_RE for the trust model. Runs
    independently of the armed_sessions loop that every other _process_*
    function here iterates: this chat_id is deliberately never a
    session's chat_id, so _process_tmux_delivery would never reach it
    even if left unconsumed."""
    msgs = store.peek_unconsumed(db_path, control_chat_id)
    if not msgs:
        return
    for m in msgs:
        text = m["text"].strip()
        try:
            if _LIST_BACKENDS_COMMAND_RE.match(text):
                reply = _format_backend_list()
            elif _LIST_FOLDERS_COMMAND_RE.match(text):
                reply = _format_folder_list()
            elif _LIST_SESSIONS_COMMAND_RE.match(text):
                reply = _format_session_list(db_path)
            elif match := _DELETE_SESSION_COMMAND_RE.match(text):
                reply = _handle_delete_command(db_path, match.group(1))
            elif match := _NEW_SESSION_COMMAND_RE.match(text):
                rest = (match.group(2) or "").strip()
                reply = _handle_new_command(bridge, db_path, match.group(1), rest)
            else:
                reply = _CONTROL_HELP
        except Exception:
            # Defense in depth on top of tmux.wait_ready's own fix
            # (2026-09-14): whatever the cause, an unhandled exception
            # here must never leave the message unconsumed — that's what
            # turned one bad "/new-session" into a spawn-a-pane-every-tick retry
            # storm that ran for ~6 minutes and created 64 chats before
            # anyone noticed (see design.md). mark_consumed below is
            # unconditional specifically so this can never repeat for
            # *any* future exception, not just the one already fixed.
            log.exception("control-команда %r: необработанная ошибка", text[:80])
            reply = "внутренняя ошибка при выполнении команды — см. лог демона"
        store.mark_consumed(db_path, "control", [m["id"]])
        store.enqueue_outbox(db_path, "control", control_chat_id, reply)


def _process_outbox(bridge: Bridge, db_path: str):
    for item in store.pending_outbox(db_path):
        try:
            bridge.send_text(item["chat_id"], item["text"])
            store.mark_outbox_sent(db_path, item["id"])
            log.info("outbox #%s отправлен в чат %s", item["id"], item["chat_id"])
        except Exception as e:
            store.mark_outbox_error(db_path, item["id"], repr(e))
            log.exception("outbox #%s: ошибка отправки", item["id"])


def _process_inbox(bridge: Bridge, db_path: str, control_chat_id: int | None = None):
    """One account-wide fetch (see Bridge.fetch_all_fresh_messages),
    routed to armed sessions by chat_id — replaces the old per-session
    chat.get_messages() loop (reliability pass 2026-08-10, see the
    docstring on fetch_all_fresh_messages for why that scaled badly).

    control_chat_id (2026-09-14): the one extra chat_id worth storing
    messages for even though it's never in the sessions table — without
    it, _process_control_commands would starve forever (peek_unconsumed
    on a chat_id nothing ever wrote to). Also why this function can no
    longer bail out early just because there are no armed sessions: the
    control chat matters *most* exactly when nothing is armed yet (that's
    when "/new-session" gets used)."""
    sessions_by_chat = {s["chat_id"]: s for s in store.armed_sessions(db_path)}
    if not sessions_by_chat and control_chat_id is None:
        return

    processed_by_chat: dict[int, list[int]] = {}
    for msg in bridge.fetch_all_fresh_messages():
        chat_id = msg["chat_id"]
        sess = sessions_by_chat.get(chat_id)
        if sess is None and chat_id != control_chat_id:
            # Not an armed session's chat and not the control chat (old
            # test/closed chat) — fetch_all_fresh_messages already marked
            # it seen account-wide; nothing else to do with it here.
            continue
        log_label = sess["session_id"] if sess else "control"
        text = msg["text"]
        if msg["view_type"] in VOICE_VIEW_TYPES and msg["file"]:
            try:
                transcript = stt.transcribe(msg["file"])
                text = f"[голосовое] {transcript}"
            except Exception:
                log.exception("сессия %s: ошибка распознавания msg_id=%s", log_label, msg["id"])
                text = "[голосовое — распознать не удалось]"
        elif msg["view_type"] in IMAGE_VIEW_TYPES and msg["file"]:
            caption = msg["text"].strip() if msg["text"] else ""
            try:
                recognized = ocr.recognize(msg["file"])
                body = " — ".join(p for p in (caption, recognized) if p) or "текст не найден"
            except Exception:
                log.exception("сессия %s: ошибка OCR msg_id=%s", log_label, msg["id"])
                body = f"{caption} (распознать не удалось)" if caption else "распознать не удалось"
            text = f"[изображение] {body}"
        store.store_inbox_message(db_path, chat_id, msg["id"], text)
        log.info("сессия %s: новое сообщение (msg_id=%s) %r", log_label, msg["id"], text[:60])
        processed_by_chat.setdefault(chat_id, []).append(msg["id"])

    # Delete only after everything (including STT) has been processed —
    # see the comment in Bridge.delete_processed.
    for chat_id, ids in processed_by_chat.items():
        try:
            bridge.delete_processed(ids)
        except Exception:
            log.exception("чат %s: ошибка удаления обработанных сообщений", chat_id)


def _clear_pane_state(db_path: str, session_id: str, chat_id: int):
    """A session's tmux pane was confirmed gone (tmux.pane_alive() ==
    False, the only condition either call site below uses this under) —
    disarms it and queues its chat for deletion, on top of dropping every
    tmux-registration-scoped bit of state (both dedup hashes, both
    stability-pending entries).

    Healthcheck added 2026-09-14, on request: previously this only
    cleared tmux_target and left the session 'armed' forever — nothing
    was ever listening again, but nothing said so either. Two sessions
    sat exactly like that for about a month before being found by manual
    /list-sessions inspection and cleaned by hand. Same deletion queue
    /delete-session and cli.cmd_close already use (store.enqueue_deletion)
    — actual chat.delete() happens later, in _process_deletions; it is
    local to the bot's own account only (see Bridge.delete_chat)."""
    store.disarm_session(db_path, session_id)
    store.clear_tmux(db_path, session_id)
    store.enqueue_deletion(db_path, session_id, chat_id)
    _pending_prompt_hash.pop(f"{session_id}:last_prompt_hash", None)
    _pending_prompt_hash.pop(f"{session_id}:last_limit_hash", None)
    _pending_clear.discard(session_id)


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
            log.info("сессия %s: панель %s больше не существует, разоружаю и удаляю чат", session_id, target)
            _clear_pane_state(db_path, session_id, chat_id)
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
            # Whatever transient blank frame may have been on record
            # (the pane clearly shows a prompt right now) is moot —
            # discard it so a later real blank frame gets its own full
            # two-poll confirmation instead of inheriting this one.
            _pending_clear.discard(session_id)
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
            _pending_clear.discard(session_id)
            hash_field = "last_limit_hash"
            hash_source = extract_limit_notice(text)
            forward_text = format_limit_notice(text)
        else:
            # Neither shape is showing — whatever was last known is
            # *probably* resolved (by *some* means, not necessarily our
            # own injection, e.g. answered directly at the keyboard).
            # Stale "still waiting" state otherwise breaks the next
            # distinct occurrence's dedup and misleads delivery-time
            # decisions. But a single blank poll is exactly as unreliable
            # here as it is for a freshly-appearing prompt (mid-render
            # capture, tmux redraw/scroll) — clearing on it immediately
            # made the *same* still-open prompt look brand new the
            # moment it reappeared next poll, and get re-forwarded
            # (confirmed 2026-08-20, see _pending_clear's docstring). So
            # this needs the same two-poll confirmation _pending_prompt_hash
            # already gives new prompts, just for the opposite transition.
            if sess.get("last_prompt_hash") or sess.get("last_limit_hash"):
                if session_id in _pending_clear:
                    store.set_last_prompt_hash(db_path, session_id, None)
                    store.set_last_limit_hash(db_path, session_id, None)
                    _pending_prompt_hash.pop(f"{session_id}:last_prompt_hash", None)
                    _pending_prompt_hash.pop(f"{session_id}:last_limit_hash", None)
                    _pending_clear.discard(session_id)
                else:
                    _pending_clear.add(session_id)
            else:
                _pending_clear.discard(session_id)
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
def _process_mode_commands(db_path: str):
    """Intercepts /mode <target> chat messages before regular tmux
    delivery would type them into the pane as a normal message — handled
    entirely here instead, via tmux.cycle_to_mode(), and answered with a
    chat reply (never injected into the pane).

    This exists specifically to keep the running Claude Code session out
    of the loop: a session pressing Shift-Tab itself and landing mid-ring
    on Plan Mode gets gated by the harness on its very next tool call,
    turning one mode switch into an expensive forced ExitPlanMode
    round-trip (confirmed live, repeatedly, 2026-09-08 — see journal).
    The daemon is a plain process with no such gate; tmux.cycle_to_mode()
    can press through Plan Mode as many times as it needs and only ever
    reports the final state once it's done, so the session's next tool
    call (whenever that happens) sees a settled mode, never a transient
    one, regardless of what the ring did in between."""
    for sess in store.armed_sessions(db_path):
        target = sess.get("tmux_target")
        if not target:
            continue
        session_id, chat_id = sess["session_id"], sess["chat_id"]

        msgs = store.peek_unconsumed(db_path, chat_id)
        if not msgs:
            continue
        if not tmux.pane_alive(target):
            continue  # _process_tmux_prompts/_process_tmux_delivery already log+clear this

        for m in msgs:
            match = _MODE_COMMAND_RE.match(m["text"].strip())
            if not match:
                continue
            requested = match.group(1).lower()
            want = _MODE_ALIASES.get(requested, requested)
            if want not in tmux._MODE_MARKERS:
                reply = f"не знаю режим {requested!r} — есть {', '.join(tmux._MODE_MARKERS)}"
                reached = None
            elif is_permission_prompt(tmux.capture_pane(target)):
                # Found live 2026-09-08: pressing Shift-Tab while a Y/N
                # permission prompt is showing overwrites the status
                # line with the prompt itself — cycle_to_mode's parser
                # sees neither manual/auto/plan text at all and burns
                # every attempt returning None, and what Shift-Tab
                # actually does to an *open* prompt's own key handling is
                # unknown (could silently pick an option) — not worth
                # risking. Refuse instead of pressing blindly; the user
                # resolves the prompt (through the forwarded prompt
                # message itself) and just resends /mode.
                reply = "сейчас открыт permission-промпт — сначала ответь на него, потом снова /mode"
                reached = None
            else:
                try:
                    reached = tmux.cycle_to_mode(target, want, max_presses=MODE_CYCLE_MAX_PRESSES)
                except Exception:
                    log.exception("сессия %s: ошибка cycle_to_mode(%s)", session_id, want)
                    reached = None
                reply = (
                    f"режим: {reached}" if reached == want
                    else f"не дожал до {want} за {MODE_CYCLE_MAX_PRESSES} попыток, сейчас: {reached}"
                )
            # A distinct consumer tag, same reasoning as
            # dispatcher:<session_id> in _process_tmux_delivery — this
            # message must not also be typed into the pane by that path.
            store.mark_consumed(db_path, f"mode-command:{session_id}", [m["id"]])
            store.enqueue_outbox(db_path, session_id, chat_id, reply)
            log.info("сессия %s: /mode %s -> %s", session_id, requested, reached)


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
            log.info("сессия %s: панель %s больше не существует, разоружаю и удаляю чат", session_id, target)
            _clear_pane_state(db_path, session_id, chat_id)
            continue

        consumer = f"dispatcher:{session_id}"
        delivered_any = False
        for m in msgs:
            try:
                tmux.send_keys(target, m["text"])
            except Exception:
                log.exception("сессия %s: ошибка send-keys в %s", session_id, target)
                break  # leave this and later messages unconsumed — retried next tick, in order
            store.mark_consumed(db_path, consumer, [m["id"]])
            log.info("сессия %s: ответ инжектирован в %s: %r", session_id, target, m["text"][:60])
            delivered_any = True

        if delivered_any:
            try:
                tmux.send_keys(target, _DELTA_CHAT_REMINDER_TAG)
            except Exception:
                log.exception("сессия %s: не удалось отправить напоминание в %s", session_id, target)
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
        # Resolved once, not per-loop-iteration — it's a stable 1:1 chat
        # with a contact that isn't going to change mid-run. Best-effort:
        # missing key-contact (no prior secure-join) shouldn't take the
        # rest of the daemon down, just the control-chat feature.
        try:
            control_chat_id = bridge.control_chat_id()
            log.info("control-чат: id=%s", control_chat_id)
        except Exception:
            log.exception("не удалось получить control-чат — команды /list-backends,/list-sessions,/delete-session,/new-session недоступны")
            control_chat_id = None

        while _running:
            try:
                _process_session_requests(bridge, db_path)
                _process_renames(bridge, db_path)
                _process_tmux_prompts(db_path)  # may add to outbox — before _process_outbox
                _process_outbox(bridge, db_path)  # before deletions: let a pending "session closed" notice out first
                _process_deletions(bridge, db_path)
                _process_inbox(bridge, db_path, control_chat_id)
                _process_mode_commands(db_path)  # claims /mode messages before regular delivery below
                if control_chat_id is not None:
                    _process_control_commands(bridge, db_path, control_chat_id)
                _process_tmux_delivery(db_path)  # consumes what _process_inbox stored above
                sleepinhibit.update(should_hold=bool(store.armed_sessions(db_path)))
            except Exception:
                log.exception("ошибка в цикле демона")

            if time.time() - last_restart > FALLBACK_RESTART_SEC:
                log.info("периодический перезапуск IO (fallback, см. design.md)")
                bridge.account.stop_io()
                bridge.account.start_io()
                last_restart = time.time()

            time.sleep(LOOP_INTERVAL_SEC)

    sleepinhibit.shutdown()
    log.info("демон остановлен")


if __name__ == "__main__":
    sys.exit(run())
