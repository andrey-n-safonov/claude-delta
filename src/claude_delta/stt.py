"""
Локальное распознавание голосовых через faster-whisper (CTranslate2,
без PyTorch). Модель грузится один раз и держится в памяти демона.
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


def transcribe(file_path: str) -> str:
    model = _get_model()
    segments, info = model.transcribe(file_path, language="ru")
    text = " ".join(seg.text.strip() for seg in segments)
    log.info("распознано (%.1fs, lang=%s): %r", info.duration, info.language, text[:80])
    return text.strip()
