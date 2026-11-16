"""
herdr backend for mux.py — wrapper over the `herdr` CLI (socket API of
the herdr terminal workspace manager, https://herdr.dev). Same role as
tmux.py: herdr's server holds the master end of every pane's pty and
exposes `pane read` / `pane send-text` / `pane send-keys` to external
processes. Checked live against herdr 0.9.5 (2026-11-16).

Addressing — herdr pane-id (`$HERDR_PANE_ID`, e.g. 'w1:p3'), stored in
the DB with a 'herdr:' prefix (see mux.parse_target). Unlike tmux, a
pane-id is NOT stable for the pane's whole life: `herdr pane move` into
another workspace gives the pane a new id. find_by_session() covers that
— herdr's own Claude Code integration (`herdr integration install
claude`, a SessionStart hook) reports the Claude session id per pane.

The CLI prints JSON results to stdout and JSON errors
(`{"error": {"code": ...}}`) to stderr with exit code 1. Failures raise
subprocess.CalledProcessError (stderr attached) — callers (mux.wait_ready,
the daemon) rely on that type.
"""
import json
import logging
import os
import shlex
import subprocess

log = logging.getLogger("claude_delta.herdr")

NAME = "herdr"

# Workspace that daemon-spawned sessions (/new-session) open tabs in;
# created on demand if missing.
SPAWN_WORKSPACE = os.environ.get("DELTA_HERDR_WORKSPACE", "delta")

# mux key name -> herdr send-keys key name (`shift+tab` verified live:
# Claude Code cycles its permission mode on it; `btab` is rejected).
_KEYS = {"enter": "enter", "shift-tab": "shift+tab"}

# Error codes that mean "this pane is gone for good". server_not_running
# included on purpose: herdr's server owns every pane's pty, so with no
# server there is no process left in any pane — same as tmux's
# has-session failing when no tmux server runs.
_GONE_CODES = frozenset({"pane_not_found", "server_not_running"})


def _run(*args: str) -> str:
    result = subprocess.run(["herdr", *args], capture_output=True, text=True, check=True)
    return result.stdout


def _json(*args: str) -> dict:
    return json.loads(_run(*args))["result"]


def error_code(stderr: str | None) -> str | None:
    """Parses herdr's JSON error from stderr; None if it isn't one."""
    try:
        return json.loads(stderr or "")["error"]["code"]
    except (ValueError, KeyError, TypeError):
        return None


def capture(pane: str) -> str:
    """Text of the pane's visible area — `--source visible` matches
    `tmux capture-pane -p` (screen rows, soft-wrapped lines stay split),
    which is what prompt_detect's normalization was built against."""
    return _run("pane", "read", pane, "--source", "visible")


def send_text(pane: str, text: str) -> None:
    _run("pane", "send-text", pane, text)


def press(pane: str, key: str) -> None:
    _run("pane", "send-keys", pane, _KEYS[key])


def _find_workspace(label: str) -> str | None:
    for ws in _json("workspace", "list")["workspaces"]:
        if ws.get("label") == label:
            return ws["workspace_id"]
    return None


def spawn(command: str, name: str) -> str:
    """Opens a new tab `name` in workspace SPAWN_WORKSPACE (creating the
    workspace if needed), runs `command` in it, returns the pane-id.

    herdr has no "start a tab running X" call — a tab always starts a
    shell — so the command is typed into that shell with `exec`: when
    `command` exits, the shell is gone too and herdr closes the pane and
    the tab (verified live), same self-cleanup as a tmux session created
    with a command. `sh -c` because `command` is a shell line ("cd X &&
    wrapper ..."), as tmux new-session runs it. Typing right after
    creation is safe: the pty buffers input until the shell reads it.

    --no-focus: the daemon must not yank the user's view to a new tab.
    """
    ws = _find_workspace(SPAWN_WORKSPACE)
    if ws is None:
        created = _json("workspace", "create", "--label", SPAWN_WORKSPACE,
                        "--cwd", os.path.expanduser("~"), "--no-focus")
        pane = created["root_pane"]["pane_id"]
        _run("tab", "rename", created["tab"]["tab_id"], name)
    else:
        created = _json("tab", "create", "--workspace", ws, "--label", name, "--no-focus")
        pane = created["root_pane"]["pane_id"]
    _run("pane", "run", pane, "exec sh -c " + shlex.quote(command))
    return pane


def alive(pane: str) -> bool:
    """False only when herdr says the pane is gone (see _GONE_CODES) or
    herdr isn't installed at all. Any other failure (timeout, unknown
    error) counts as alive: the daemon disarms the session and deletes
    its chat on False, which must not happen over a hiccup."""
    try:
        subprocess.run(["herdr", "pane", "get", pane], capture_output=True, text=True,
                       check=True, timeout=10)
        return True
    except FileNotFoundError:
        return False
    except subprocess.CalledProcessError as e:
        code = error_code(e.stderr)
        if code in _GONE_CODES:
            return False
        log.warning("herdr pane get %s: неожиданная ошибка %r, считаю панель живой", pane, code or e.stderr)
        return True
    except subprocess.TimeoutExpired:
        log.warning("herdr pane get %s: таймаут, считаю панель живой", pane)
        return True


def cwd(pane: str) -> str | None:
    """Working directory of the pane's foreground process."""
    try:
        info = _json("pane", "get", pane)["pane"]
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError, KeyError):
        return None
    return info.get("foreground_cwd") or info.get("cwd") or None


def find_by_session(session_id: str) -> str | None:
    """Pane currently running Claude session `session_id`, per herdr's
    Claude integration hook (`agent_session.value`); None if none does,
    or herdr can't be asked."""
    try:
        agents = _json("agent", "list")["agents"]
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError, KeyError):
        return None
    for agent in agents:
        if (agent.get("agent_session") or {}).get("value") == session_id:
            return agent.get("pane_id")
    return None
