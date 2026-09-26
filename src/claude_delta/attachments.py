"""
Persistent copies of files received in a session chat, so the armed
session can open them by path (Read tool, shell) for analysis.

Why a copy at all: deltachat-core keeps the downloaded blob only until
Bridge.delete_processed() removes the message (right after inbox
processing), and that path lives inside the core's own accounts dir —
not a place a session should be pointed at. Voice and images are already
converted to text by stt.py/ocr.py; this covers everything else (PDF,
office documents, logs, archives, ...) and lets an image keep its
original next to the OCR text, for sessions whose model can actually see it.

Where (2026-09-26, on request: "together with the session's artifacts"):
the session's own scratchpad, /tmp/claude-<uid>/<cwd-slug>/<session_id>/
scratchpad/received/ — Claude Code's per-session artifact dir, readable
by the session without permission prompts and gone together with it.
That path is a Claude Code convention, not an API, so session_dir()
verifies the cwd-slug dir already exists (i.e. the guess matches what
Claude Code really created) and otherwise falls back to a daemon-owned
root, <root>/<chat_id>/, which cleanup() then ages out. The scratchpad
itself is Claude Code's to manage — cleanup() never touches it.

File name: <msg_id>_<sanitized-name>. msg_id prefix makes
names unique without overwrite checks; sanitized name has no spaces or
shell/path metacharacters, because the path is injected into the pane as
plain typed text (newlines are collapsed there, see tmux.send_keys) and
an unquoted path with a space would split into two arguments.
"""
import logging
import os
import pathlib
import re
import shutil
import time

log = logging.getLogger("claude_delta.attachments")

DEFAULT_ROOT = "~/.local/share/claude-delta/files"

_UNSAFE_CHARS_RE = re.compile(r"[^\w.\-]+", re.UNICODE)  # \w keeps Cyrillic
_MAX_NAME_LEN = 100


class TooLarge(Exception):
    pass


def safe_name(original: str | None) -> str:
    """Basename only (a hostile sender-chosen name must not escape the
    target dir), unsafe runs collapsed to '_', length-capped keeping the
    extension — the extension is what tells a session (and Read) what
    kind of file this is."""
    base = os.path.basename((original or "").replace("\\", "/")).strip()
    stem, ext = os.path.splitext(base)
    stem = _UNSAFE_CHARS_RE.sub("_", stem).strip("._") or "file"
    ext = _UNSAFE_CHARS_RE.sub("", ext.lstrip("."))[:16]
    stem = stem[: _MAX_NAME_LEN - len(ext) - 1]
    return f"{stem}.{ext}" if ext else stem


def human_size(n: int) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024 or unit == "ГБ":
            return f"{n} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024


def session_dir(session_id: str, cwd: str | None, fallback_root: str, chat_id: int,
                tmp_base: str | None = None) -> str:
    """Directory for this session's received files — the session's
    scratchpad if it can be located reliably, else <fallback_root>/<chat_id>.
    Claude Code names the cwd-slug by turning every non-ASCII-alphanumeric
    character into '-' (verified against real dirs, Cyrillic and spaces
    included)."""
    if cwd:
        base = tmp_base or f"/tmp/claude-{os.getuid()}"
        slug = re.sub(r"[^A-Za-z0-9]", "-", cwd)
        if os.path.isdir(os.path.join(base, slug)):
            return os.path.join(base, slug, session_id, "scratchpad", "received")
    return os.path.join(os.path.expanduser(fallback_root), str(chat_id))


def save(src_path: str, dest_dir: str, msg_id: int, max_bytes: int) -> str:
    """Copies src_path into dest_dir, returns the absolute destination
    path. Raises TooLarge (before copying) or OSError."""
    size = os.path.getsize(src_path)
    if size > max_bytes:
        raise TooLarge(size)
    dest_dir = pathlib.Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    dest = dest_dir / f"{msg_id}_{safe_name(os.path.basename(src_path))}"
    shutil.copyfile(src_path, dest)
    dest.chmod(0o600)
    return str(dest)


def cleanup(root: str, keep_days: float, now: float | None = None) -> int:
    """Removes files older than keep_days (by mtime) and empty chat
    dirs; returns how many files were removed. Best-effort per file."""
    base = pathlib.Path(os.path.expanduser(root))
    if not base.is_dir():
        return 0
    cutoff = (now if now is not None else time.time()) - keep_days * 86400
    removed = 0
    for chat_dir in base.iterdir():
        if not chat_dir.is_dir():
            continue
        for f in chat_dir.iterdir():
            try:
                if f.is_file() and f.stat().st_mtime < cutoff:
                    f.unlink()
                    removed += 1
            except OSError:
                log.exception("не удалось удалить %s", f)
        try:
            chat_dir.rmdir()  # only succeeds if empty
        except OSError:
            pass
    return removed
