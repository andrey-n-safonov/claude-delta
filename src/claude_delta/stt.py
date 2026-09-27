"""
Local voice transcription via faster-whisper (CTranslate2, no PyTorch).

On-demand, not eager (2026-09-27, reversing the 2026-08-10 preload
decision — see design.md "STT: on-demand loading"): the model
(~500MB resident) is loaded on first use and unloaded again after
IDLE_UNLOAD_SEC without a transcription, trading a small load delay on
the first voice message after a quiet spell for not holding ~500MB in
the daemon's memory the rest of the time (voice messages are rare
compared to how long the daemon runs). daemon.py must call
unload_if_idle() periodically from its own loop — this module has no
timer of its own.

This does reintroduce a version of the exact problem 2026-08-10 fixed
(a lazy load blocking the single-threaded daemon loop for every armed
session, not just the one with the voice message) — but scoped down: a
warm re-load from local cache takes ~2-3s (see the daemon startup log),
not the original "model not downloaded yet" case, which could take much
longer on first-ever run. See design.md for the accepted trade-off.
"""
import logging
import time

log = logging.getLogger("claude_delta.stt")

IDLE_UNLOAD_SEC = 600  # 10 min without a transcription -> free the ~500MB

_model = None
_last_used = 0.0


def _get_model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel

        log.info("загружаю модель whisper (small, CPU, int8)...")
        _model = WhisperModel("small", device="cpu", compute_type="int8")
        log.info("модель whisper загружена")
    return _model


def is_loaded() -> bool:
    return _model is not None


def unload_if_idle(idle_sec: float = IDLE_UNLOAD_SEC, now: float | None = None) -> bool:
    """Frees the model if it's been sitting unused for idle_sec — call
    this every daemon loop iteration (cheap: one comparison when nothing
    is loaded). Returns True if it actually unloaded something."""
    global _model
    if _model is None:
        return False
    if (now if now is not None else time.time()) - _last_used < idle_sec:
        return False
    log.info("выгружаю модель whisper (простой > %.0fс)", idle_sec)
    _model = None
    return True


def transcribe(file_path: str) -> str:
    global _last_used
    model = _get_model()
    segments, info = model.transcribe(file_path, language="ru")
    text = " ".join(seg.text.strip() for seg in segments)
    _last_used = time.time()
    log.info("распознано (%.1fs, lang=%s): %r", info.duration, info.language, text[:80])
    return text.strip()
