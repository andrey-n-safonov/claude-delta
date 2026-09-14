"""
Thin wrapper over the `tmux` CLI — the only legal channel for injecting
keystrokes into someone else's session on this machine (see design.md,
"Architectural pivot 2026-08-09"): `TIOCSTI` is blocked by the kernel
(`dev.tty.legacy_tiocsti=0`), `xdotool` does not work under Wayland. tmux
itself holds the master end of the pty and officially exposes
`capture-pane`/`send-keys` to external processes.

Addressing — pane-id (`$TMUX_PANE`, e.g. '%12'): globally unique and
stable regardless of the tmux session's name/layout, no naming scheme
required.
"""
import logging
import subprocess
import time

log = logging.getLogger("claude_delta.tmux")

# Substrings of the harness's own status line, one per permission mode.
# Four distinct modes, confirmed by a clean 6-press run (2026-09-08,
# see journal) with the *correct* line read each time (an earlier pass
# misread the pane — grabbed the header line instead of the status line
# — and that bug is what made "accept edits" and "auto" look like the
# same flickering label; they are not, the user caught this live). Fixed
# ring order, Shift-Tab always moves forward:
#   manual -> accept-edits -> plan -> auto -> manual -> ...
_MODE_MARKERS = {
    "manual": ("manual mode on",),
    "accept-edits": ("accept edits on",),
    "plan": ("plan mode on",),
    "auto": ("auto mode on", "bypass permissions"),
}


def capture_pane(target: str) -> str:
    """Text of the pane's visible area (no colors/escape sequences)."""
    result = subprocess.run(
        ["tmux", "capture-pane", "-p", "-t", target],
        capture_output=True, text=True, check=True,
    )
    return result.stdout


def send_keys(target: str, text: str) -> None:
    """Types text into the pane as if a human typed it, then presses Enter.

    Newlines inside text are collapsed to spaces first: `send-keys -l`
    passes a literal "\\n" through as its own Enter keypress, so a
    multi-line message (a multi-line voice transcript, or just a phone
    message with line breaks) previously landed as several separate
    turns plus one trailing Enter, not the single message it was meant
    to be (found in review, reliability pass 2026-08-10). One message in
    = one line typed = one Enter, matching what a single chat message
    typed at the keyboard would produce.
    """
    text = " ".join(text.splitlines())
    subprocess.run(
        ["tmux", "send-keys", "-t", target, "-l", "--", text],
        check=True,
    )
    subprocess.run(["tmux", "send-keys", "-t", target, "Enter"], check=True)


def cycle_permission_mode(target: str) -> None:
    """Presses Shift-Tab in the pane — the harness's own keybinding for
    cycling its permission mode (manual/auto/plan/...). Deliberately a
    single hardcoded key, not a generic "send any key" primitive: the
    whole point of a remote chat channel is that its messages are data,
    not commands, and a function that types free-form key names into
    someone's terminal is exactly the kind of primitive that turns "data"
    back into "commands" if the chat pipeline is ever compromised. This
    one physically can't do anything but press Shift-Tab, regardless of
    what triggered the call."""
    subprocess.run(["tmux", "send-keys", "-t", target, "BTab"], check=True)


def _parse_mode(pane_tail: str) -> str | None:
    """Pure string-matching over the last few lines of a captured pane —
    split out from current_mode() so it's unit-testable without an actual
    tmux process (see tests/test_tmux.py)."""
    lowered = pane_tail.lower()
    for mode, markers in _MODE_MARKERS.items():
        if any(marker in lowered for marker in markers):
            return mode
    return None


def current_mode(target: str) -> str | None:
    """Current permission mode, read from the pane's status line — pure
    observation, no keys pressed. None if the status line isn't showing
    any of the known markers (harness version drift, or the line scrolled
    out of the captured tail)."""
    tail = "\n".join(capture_pane(target).rstrip().splitlines()[-3:])
    return _parse_mode(tail)


def cycle_to_mode(target: str, want: str, max_presses: int = 6) -> str | None:
    """Presses Shift-Tab until current_mode() reports `want`, or gives up
    after max_presses (one full lap of the 4-state ring is 4 presses
    worst-case; 6 gives a bit of margin). Runs entirely in the daemon
    process, outside any Claude Code tool-call boundary — the whole
    point of moving this here instead
    of leaving it to the session's own cycle-mode: a session pressing
    itself past Plan Mode mid-cycle still gets gated by it on its very
    next tool call (confirmed live, several costly round-trips), while
    this loop's intermediate presses are invisible to that gate — nothing
    about them is a session tool call at all.

    Returns the mode actually reached (which may not equal `want`, if
    max_presses ran out) so the caller can report accurately instead of
    assuming success."""
    mode = current_mode(target)
    for _ in range(max_presses):
        if mode == want:
            return mode
        cycle_permission_mode(target)
        time.sleep(0.4)
        mode = current_mode(target)
    return mode


def spawn_window(command: str, session: str = "main") -> str:
    """Opens a new tmux window in `session` running `command`, returns its
    pane-id. Used only for daemon-initiated session creation (the
    control-chat "/new" command, see daemon._handle_new) — every other
    function in this module addresses a pane that already exists.

    -d: don't switch a currently-attached client to the new window — this
    runs from the daemon, not from inside any client, but a user could be
    attached and watching another pane right now.
    """
    result = subprocess.run(
        ["tmux", "new-window", "-d", "-P", "-F", "#{pane_id}", "-t", session, command],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def wait_ready(target: str, timeout_sec: float = 20.0, poll_interval_sec: float = 0.5) -> bool:
    """Polls current_mode(target) until the harness's own status line
    shows a known permission-mode marker — the signal that its TUI has
    finished starting and switched stdin to raw mode, i.e. it is actually
    reading keystrokes now, not just that the pty exists.

    Used right after spawn_window(), before typing the first message into
    a brand-new pane (see daemon._handle_new_command). A blind sleep
    before this guessed at startup latency instead of observing it —
    unmeasured MCP-server spawn alone took ~1s in a live session, total
    time to a ready prompt is unknown and probably differs by backend
    (each wrapper does its own proxy/env setup before even exec'ing
    `claude`, see claude-deep/claude-mimo).

    Returns False on timeout — pane may still be starting, or never will
    (wrong API key, network down for that backend) — rather than
    guessing further; the caller decides what to do instead of typing
    into a pane nobody is reading yet.
    """
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if current_mode(target) is not None:
            return True
        time.sleep(poll_interval_sec)
    return False


def pane_alive(target: str) -> bool:
    """False if the pane was closed by hand — avoids crashes in the daemon loop."""
    result = subprocess.run(
        ["tmux", "has-session", "-t", target],
        capture_output=True,
    )
    return result.returncode == 0
