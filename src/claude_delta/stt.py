"""
Local voice transcription via faster-whisper (CTranslate2, no PyTorch).
The model is loaded once and kept in the daemon's memory.
"""
import logging

log = logging.getLogger("claude_delta.stt")

_model = None


def _get_model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel

        log.info("загружаю модель whisper (small, CPU, int8)...")
        _model = WhisperModel("small", device="cpu", compute_type="int8")
        log.info("модель whisper загружена")
    return _model


def preload() -> None:
    """Loads the model eagerly. Call once at daemon startup — without
    this, the *first* voice message anywhere hits the lazy load inline
    in the single-threaded daemon loop (model download/init, ~500MB) and
    blocks prompt forwarding/delivery for every armed session for as
    long as that takes (reliability pass 2026-08-10). Doesn't fully
    remove per-message blocking during transcription itself — see
    design.md for the deferred full fix (background thread/queue)."""
    _get_model()


def transcribe(file_path: str) -> str:
    model = _get_model()
    segments, info = model.transcribe(file_path, language="ru")
    text = " ".join(seg.text.strip() for seg in segments)
    log.info("распознано (%.1fs, lang=%s): %r", info.duration, info.language, text[:80])
    return text.strip()
