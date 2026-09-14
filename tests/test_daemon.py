"""
Plain-assert test (no pytest, see test_prompt_detect.py). Run with:

    .venv/bin/python3 tests/test_daemon.py

Needs the venv, unlike test_store.py/test_tmux.py — daemon.py imports
bridge.py at module level, which needs deltachat_rpc_client installed.

Covers _parse_backends (DELTA_SPAWN_BACKENDS parsing) — the one pure,
non-IO piece of the 2026-09-14 control-protocol addition worth locking
down; everything else in that change touches Bridge/tmux/sqlite and is
exercised live instead (see design.md).
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from claude_delta import daemon


def run():
    failures = []

    got = daemon._parse_backends("proxy=claude-proxy,deep=claude-deep,mimo=claude-mimo")
    want = {"proxy": "claude-proxy", "deep": "claude-deep", "mimo": "claude-mimo"}
    if got != want:
        failures.append(f"_parse_backends basic case: got {got}, want {want}")

    if daemon._parse_backends("") != {}:
        failures.append("_parse_backends('') should be empty, not raise or default")

    # Malformed entries (no '=', empty name, empty command) are dropped,
    # not raised on — a typo in the env file should degrade to "backend
    # unavailable", never crash the daemon at startup.
    got = daemon._parse_backends("a=b, c=d , bad, e=, =f")
    if got != {"a": "b", "c": "d"}:
        failures.append(f"_parse_backends malformed entries: got {got}")

    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)

    print("OK — _parse_backends checks passed")


if __name__ == "__main__":
    run()
