"""
Plain-assert test (no pytest, see test_prompt_detect.py). Run with:

    python3 tests/test_tmux.py

Covers tmux._parse_mode — the pure string-matching piece of mode
detection, split out specifically so it's testable without an actual
tmux process. current_mode()/cycle_to_mode() themselves are thin
subprocess wrappers (capture_pane/send-keys) — not unit-tested, per
project convention (infra adapters aren't worth testing, the logic
around them is).
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from claude_delta import tmux


def run():
    failures = []

    cases = [
        ("  ⏸ manual mode on · ← 1 agent", "manual"),
        ("  ⏵⏵ accept edits on (shift+tab to cycle) · ← 1 agent", "accept-edits"),
        ("  ⏸ plan mode on (shift+tab to cycle) · ← 1 agent", "plan"),
        ("  ⏵⏵ auto mode on (shift+tab to cycle) · ← 1 agent", "auto"),
        ("some unrelated pane content\nwith no status line at all", None),
        ("", None),
    ]
    for pane_tail, expected in cases:
        got = tmux._parse_mode(pane_tail)
        if got != expected:
            failures.append(f"_parse_mode({pane_tail!r}) = {got!r}, expected {expected!r}")

    # Case-insensitivity — the real status line is always lowercase, but
    # the parser shouldn't silently depend on that.
    if tmux._parse_mode("MANUAL MODE ON") != "manual":
        failures.append("_parse_mode is not case-insensitive")

    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)

    print("OK — mode-parsing checks passed")


if __name__ == "__main__":
    run()
