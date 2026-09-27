"""
Plain-assert test (no pytest, see test_prompt_detect.py). Run with:

    python3 tests/test_stt.py

Pure timing logic, no real model loaded.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from claude_delta import stt


def run():
    failures = []

    if stt.is_loaded():
        failures.append("module-level state leaked a loaded model before any test ran")

    # unload_if_idle on an unloaded model is a cheap no-op, not an error
    if stt.unload_if_idle(600, now=1000.0):
        failures.append("unload_if_idle must return False when nothing is loaded")

    # Simulate "just used" — still within the idle window
    stt._model = object()
    stt._last_used = 1000.0
    if stt.unload_if_idle(600, now=1000.0 + 599):
        failures.append("unload_if_idle fired before the idle window elapsed")
    if not stt.is_loaded():
        failures.append("model must still be loaded within the idle window")

    # Past the window — unloads exactly once
    if not stt.unload_if_idle(600, now=1000.0 + 601):
        failures.append("unload_if_idle should have fired past the idle window")
    if stt.is_loaded():
        failures.append("model must be gone after unload_if_idle fires")
    if stt.unload_if_idle(600, now=1000.0 + 601):
        failures.append("unload_if_idle must be a no-op once already unloaded")

    if failures:
        print("FAIL")
        for f in failures:
            print(" -", f)
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(run())
