"""
tmux backend for mux.py — thin wrapper over the `tmux` CLI, one of the
legal channels for injecting keystrokes into someone else's session on
this machine (see design.md, "Architectural pivot 2026-08-09"): `TIOCSTI`
is blocked by the kernel (`dev.tty.legacy_tiocsti=0`), `xdotool` does not
work under Wayland. tmux itself holds the master end of the pty and
officially exposes `capture-pane`/`send-keys` to external processes.

Addressing — pane-id (`$TMUX_PANE`, e.g. '%12'): globally unique and
stable regardless of the tmux session's name/layout, no naming scheme
required. Stored in the DB as-is (no prefix) — that's what every target
registered before mux.py existed looks like, see mux.parse_target.

Only primitives live here; everything built on top of them (mode
detection, cycle_to_mode, wait_ready, newline collapsing) is shared
across backends in mux.py. Failures raise subprocess.CalledProcessError
— callers (mux.wait_ready, the daemon) rely on that type.
"""
import subprocess

NAME = "tmux"

# mux key name -> tmux send-keys key name. Closed set on purpose, see
# mux.cycle_permission_mode for why there's no "send any key" primitive.
_KEYS = {"enter": "Enter", "shift-tab": "BTab"}


def capture(pane: str) -> str:
    """Text of the pane's visible area (no colors/escape sequences)."""
    result = subprocess.run(
        ["tmux", "capture-pane", "-p", "-t", pane],
        capture_output=True, text=True, check=True,
    )
    return result.stdout


def send_text(pane: str, text: str) -> None:
    """Types text literally (`-l`: no key-name interpretation)."""
    subprocess.run(["tmux", "send-keys", "-t", pane, "-l", "--", text], check=True)


def press(pane: str, key: str) -> None:
    subprocess.run(["tmux", "send-keys", "-t", pane, _KEYS[key]], check=True)


def spawn(command: str, name: str) -> str:
    """Creates a new, dedicated detached tmux session `name` running
    `command`, returns its pane-id.

    One session per spawn, not a window inside a shared session: an
    earlier version opened a window in a fixed session (default "main")
    that had to already exist and stay alive for spawning to work at all
    — confirmed broken live 2026-09-15 on a host where nobody had a tmux
    server running at the time (no interactive login since boot), so
    every "/new-session" from the phone failed outright. A session tmux
    creates on demand has no such precondition, and self-destructs when
    `command` exits.

    -d: don't switch a currently-attached client to the new session — this
    runs from the daemon, not from inside any client, but a user could be
    attached and watching another session right now.
    """
    result = subprocess.run(
        ["tmux", "new-session", "-d", "-P", "-F", "#{pane_id}", "-s", name, command],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def alive(pane: str) -> bool:
    """False if the pane was closed by hand — avoids crashes in the daemon loop."""
    result = subprocess.run(["tmux", "has-session", "-t", pane], capture_output=True)
    return result.returncode == 0


def cwd(pane: str) -> str | None:
    """Working directory of the pane's foreground process (the session's
    cwd), None if tmux can't say."""
    result = subprocess.run(
        ["tmux", "display-message", "-p", "-t", pane, "#{pane_current_path}"],
        capture_output=True, text=True,
    )
    path = result.stdout.strip()
    return path if result.returncode == 0 and path else None


def find_by_session(session_id: str) -> str | None:
    """tmux knows nothing about which Claude session runs in a pane, and
    tmux pane-ids never change anyway — nothing to relocate."""
    return None
