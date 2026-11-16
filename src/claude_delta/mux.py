"""
Terminal-multiplexer facade: the daemon and CLI talk to panes only
through this module; which multiplexer actually holds a pane is decided
per target, not globally.

Targets (the sessions.tmux_target column — name kept from when tmux was
the only backend, no migration needed):
  '%12'           tmux pane-id — legacy form, every target registered
                  before this module existed looks like this
  'tmux:%12'      same, explicit
  'herdr:w1:p3'   herdr pane-id

Because the backend travels with the target, tmux and herdr sessions can
be live at the same time, and changing DELTA_MUX never strands an
already-registered session. DELTA_MUX (tmux|herdr, default tmux) only
picks where the daemon spawns new sessions (/new-session) — see
default_backend().

Backend modules (tmux.py, herdr.py) implement just the primitives:
capture, send_text, press (closed key set: 'enter', 'shift-tab'), spawn,
alive, cwd, find_by_session. Everything built on top of them is here,
once, for both.
"""
import os
import time
import subprocess

from . import herdr, tmux

_BACKENDS = {tmux.NAME: tmux, herdr.NAME: herdr}

# Substrings of the harness's own status line, one per permission mode.
# Four distinct modes, confirmed by a clean 6-press run (2026-09-08,
# see journal) with the *correct* line read each time (an earlier pass
# misread the pane — grabbed the header line instead of the status line
# — and that bug is what made "accept edits" and "auto" look like the
# same flickering label; they are not, the user caught this live). Fixed
# ring order, Shift-Tab always moves forward:
#   manual -> accept-edits -> plan -> auto -> manual -> ...
# Same ring under herdr (verified live 2026-11-16).
_MODE_MARKERS = {
    "manual": ("manual mode on",),
    "accept-edits": ("accept edits on",),
    "plan": ("plan mode on",),
    "auto": ("auto mode on", "bypass permissions"),
}


def parse_target(target: str):
    """'herdr:w1:p3' -> (herdr, 'w1:p3'); anything else -> (tmux, target),
    with an optional 'tmux:' prefix stripped. Unprefixed means tmux by
    definition: that's every target stored before this module existed,
    and tmux itself accepts more than '%N' ('sess:win.pane', via
    `register-tmux --target`) — so no colon-based guessing here."""
    for backend in (herdr, tmux):
        prefix = backend.NAME + ":"
        if target.startswith(prefix):
            return backend, target[len(prefix):]
    return tmux, target


def format_target(backend, pane: str) -> str:
    """Inverse of parse_target. tmux stays unprefixed — the form already
    stored for every existing session, and what `tmux -t` takes."""
    return pane if backend is tmux else f"{backend.NAME}:{pane}"


def default_backend():
    """Backend for daemon-spawned sessions, from DELTA_MUX (tmux|herdr).
    Read on every call, not at import — tests and the CLI set it late."""
    name = os.environ.get("DELTA_MUX", tmux.NAME).strip().lower()
    if name not in _BACKENDS:
        raise ValueError(f"DELTA_MUX={name!r}: есть {', '.join(_BACKENDS)}")
    return _BACKENDS[name]


def target_from_env(env=os.environ) -> str | None:
    """Target of the pane the calling process runs in. herdr wins over
    tmux: herdr sets HERDR_ENV=1 + HERDR_PANE_ID in every pane it opens;
    a herdr pane started from inside tmux would carry both."""
    if env.get("HERDR_ENV") == "1" and env.get("HERDR_PANE_ID"):
        return format_target(herdr, env["HERDR_PANE_ID"])
    if env.get("TMUX_PANE"):
        return format_target(tmux, env["TMUX_PANE"])
    return None


def backend_name(target: str) -> str:
    return parse_target(target)[0].NAME


def capture_pane(target: str) -> str:
    """Text of the pane's visible area (no colors/escape sequences)."""
    backend, pane = parse_target(target)
    return backend.capture(pane)


def send_keys(target: str, text: str) -> None:
    """Types text into the pane as if a human typed it, then presses Enter.

    Newlines inside text are collapsed to spaces first: a literal "\\n"
    goes through as its own Enter keypress (tmux `send-keys -l`, herdr
    send-text alike), so a multi-line message (a multi-line voice
    transcript, or just a phone message with line breaks) previously
    landed as several separate turns plus one trailing Enter, not the
    single message it was meant to be (found in review, reliability pass
    2026-08-10). One message in = one line typed = one Enter, matching
    what a single chat message typed at the keyboard would produce.
    """
    backend, pane = parse_target(target)
    backend.send_text(pane, " ".join(text.splitlines()))
    backend.press(pane, "enter")


def cycle_permission_mode(target: str) -> None:
    """Presses Shift-Tab in the pane — the harness's own keybinding for
    cycling its permission mode (manual/auto/plan/...). Deliberately a
    single hardcoded key, not a generic "send any key" primitive: the
    whole point of a remote chat channel is that its messages are data,
    not commands, and a function that types free-form key names into
    someone's terminal is exactly the kind of primitive that turns "data"
    back into "commands" if the chat pipeline is ever compromised. This
    one physically can't do anything but press Shift-Tab, regardless of
    what triggered the call (backends' press() only knows 'enter' and
    'shift-tab')."""
    backend, pane = parse_target(target)
    backend.press(pane, "shift-tab")


def _parse_mode(pane_tail: str) -> str | None:
    """Pure string-matching over the last few lines of a captured pane —
    split out from current_mode() so it's unit-testable without an actual
    multiplexer process (see tests/test_mux.py)."""
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


def spawn_session(command: str, name: str) -> str:
    """Starts `command` in a new pane of the DELTA_MUX backend, returns
    its target. Used only for daemon-initiated session creation (the
    control-chat "/new-session" command, see daemon._handle_new_command)
    — every other function here addresses a pane that already exists.
    The pane closes by itself when `command` exits, in both backends."""
    backend = default_backend()
    return format_target(backend, backend.spawn(command, name))


def wait_ready(target: str, timeout_sec: float = 20.0, poll_interval_sec: float = 0.5) -> bool:
    """Polls current_mode(target) until the harness's own status line
    shows a known permission-mode marker — the signal that its TUI has
    finished starting and switched stdin to raw mode, i.e. it is actually
    reading keystrokes now, not just that the pty exists.

    Used right after spawn_session(), before typing the first message into
    a brand-new pane (see daemon._handle_new_command). A blind sleep
    before this guessed at startup latency instead of observing it —
    unmeasured MCP-server spawn alone took ~1s in a live session, total
    time to a ready prompt is unknown and probably differs by backend
    (each wrapper does its own proxy/env setup before even exec'ing
    `claude`, see claude-deep/claude-mimo).

    Returns False on timeout — pane may still be starting, or never will
    (wrong API key, network down for that backend) — rather than
    guessing further; the caller decides what to do instead of typing
    into a pane nobody is reading yet. Also returns False (not raise) if
    the pane dies mid-poll — both backends' capture exits non-zero once
    the target is gone, and a spawned pane dying before ever becoming
    ready is exactly the kind of failure this function exists to report,
    not to propagate as a crash (confirmed live 2026-09-14: an unhandled
    CalledProcessError here escaped all the way out of the daemon's
    command dispatcher, which then never marked the triggering message
    consumed — same message got reprocessed, and re-spawned, every loop
    tick forever; see design.md).
    """
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            if current_mode(target) is not None:
                return True
        except subprocess.CalledProcessError:
            return False
        time.sleep(poll_interval_sec)
    return False


def pane_alive(target: str) -> bool:
    """False if the pane is gone — avoids crashes in the daemon loop."""
    backend, pane = parse_target(target)
    return backend.alive(pane)


def pane_path(target: str) -> str | None:
    """Working directory of the pane's foreground process (the session's
    cwd), None if the multiplexer can't say."""
    backend, pane = parse_target(target)
    return backend.cwd(pane)


def relocate(target: str, session_id: str) -> str | None:
    """New target for a session whose pane id went stale, or None.
    Only herdr can move a pane to a new id (`pane move` across
    workspaces); it finds it again by the Claude session id its Claude
    integration hook reports per pane. Searches the same backend the old
    target belonged to — a session never jumps multiplexers."""
    backend, _ = parse_target(target)
    pane = backend.find_by_session(session_id)
    return format_target(backend, pane) if pane else None
