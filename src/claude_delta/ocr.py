"""
Local OCR via tesseract (rus+eng) — a text-only conversion for image
messages, so recognition works uniformly for any downstream model
regardless of vision capability (mirrors stt.py's role for voice
messages: the daemon converts media to text before it ever reaches an
armed session, instead of relying on the session's own model to see the
image — DeepSeek/mimo-backed sessions have no vision at all, and even a
vision-capable one only gets the raw blob for as long as it survives
Bridge.delete_processed(), which runs right after inbox processing).
"""
import logging
import subprocess

log = logging.getLogger("claude_delta.ocr")

_LANGS = "rus+eng"
_TIMEOUT_SEC = 30


def recognize(file_path: str) -> str:
    result = subprocess.run(
        ["tesseract", file_path, "stdout", "-l", _LANGS],
        capture_output=True, text=True, timeout=_TIMEOUT_SEC,
    )
    if result.returncode != 0:
        raise RuntimeError(f"tesseract exited {result.returncode}: {result.stderr.strip()}")
    text = result.stdout.strip()
    log.info("OCR распознано (%d симв.): %r", len(text), text[:80])
    return text
