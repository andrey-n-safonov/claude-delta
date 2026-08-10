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

log = logging.getLogger("claude_delta.tmux")


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


def pane_alive(target: str) -> bool:
    """False if the pane was closed by hand — avoids crashes in the daemon loop."""
    result = subprocess.run(
        ["tmux", "has-session", "-t", target],
        capture_output=True,
    )
    return result.returncode == 0
