"""
Thin wrapper managing a single `systemd-inhibit --what=idle:sleep` child
process, held for as long as at least one session is armed.

Without this the host suspends on its own idle timer regardless of chat
state — reported live 2026-08-12: laptop went to sleep overnight with an
armed session still waiting on a reply, breaking delivery until someone
woke it back up by hand. The very first design (docs/design.md, "Session
control: arm/disarm") called for this at `on`/`off` time in the session's
own CLI invocation, but that step never survived the 2026-08-09/10 pivot
to the tmux dispatcher — `register-tmux`/`create-session` in cli.py never
gained it, so it silently went missing.

Centralized in the daemon's main loop instead of the CLI: the daemon is
the one process that's always running and already polls `armed_sessions`
every tick, so it's a single ~free `poll()` check per iteration rather
than a lock owned by a CLI invocation that might crash without calling
`off` (see design.md's own caveat about exactly that). `sleep <CAP>`
(not `sleep infinity`) is the bound for that same crash case: if the
daemon itself dies without running the shutdown cleanup below, the
inhibitor expires on its own within CAP_SEC instead of wedging the host
awake forever.
"""
import logging
import subprocess

log = logging.getLogger("claude_delta.sleepinhibit")

WHY = "Delta Chat: активная сессия Claude Code ожидает ответа"
CAP_SEC = 3600  # safety bound if the daemon dies without cleanup; refreshed every tick below

_proc: subprocess.Popen | None = None


def _alive() -> bool:
    return _proc is not None and _proc.poll() is None


def update(should_hold: bool) -> None:
    """Call once per daemon loop tick with whether any session is armed
    right now. Starts a fresh inhibitor if needed and none is running
    (also covers the CAP_SEC expiry case — a dead child gets replaced on
    the next tick as long as still needed); stops it the tick after the
    last session disarms."""
    global _proc

    if should_hold:
        if not _alive():
            try:
                _proc = subprocess.Popen(
                    ["systemd-inhibit", "--what=idle:sleep", f"--why={WHY}",
                     "sleep", str(CAP_SEC)],
                )
                log.info("systemd-inhibit запущен (pid=%s)", _proc.pid)
            except Exception:
                log.exception("не удалось запустить systemd-inhibit")
    else:
        if _alive():
            _proc.terminate()
            log.info("systemd-inhibit снят (pid=%s)", _proc.pid)
        _proc = None


def shutdown() -> None:
    """Called on daemon exit — don't leave the inhibitor running past the
    process it was guarding."""
    global _proc
    if _alive():
        _proc.terminate()
        try:
            _proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _proc.kill()
    _proc = None
