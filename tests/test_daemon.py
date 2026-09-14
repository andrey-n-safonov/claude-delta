"""
Plain-assert test (no pytest, see test_prompt_detect.py). Run with:

    .venv/bin/python3 tests/test_daemon.py

Needs the venv, unlike test_store.py/test_tmux.py — daemon.py imports
bridge.py at module level, which needs deltachat_rpc_client installed.

Covers _parse_name_value_pairs (DELTA_SPAWN_BACKENDS/_FOLDERS parsing)
and _split_folder_and_task — the pure, non-IO pieces of the 2026-09-14
control-protocol addition worth locking down; everything else in that
change touches Bridge/tmux/sqlite and is exercised live instead (see
design.md).
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from claude_delta import daemon


def run():
    failures = []

    got = daemon._parse_name_value_pairs("proxy=claude-proxy,deep=claude-deep,mimo=claude-mimo")
    want = {"proxy": "claude-proxy", "deep": "claude-deep", "mimo": "claude-mimo"}
    if got != want:
        failures.append(f"_parse_backends basic case: got {got}, want {want}")

    if daemon._parse_name_value_pairs("") != {}:
        failures.append("_parse_backends('') should be empty, not raise or default")

    # Malformed entries (no '=', empty name, empty command) are dropped,
    # not raised on — a typo in the env file should degrade to "backend
    # unavailable", never crash the daemon at startup.
    got = daemon._parse_name_value_pairs("a=b, c=d , bad, e=, =f")
    if got != {"a": "b", "c": "d"}:
        failures.append(f"_parse_backends malformed entries: got {got}")

    # --- _split_folder_and_task: registry lookup, not fuzzy guessing ---
    # Monkeypatch module state directly rather than env+reimport — same
    # variables _handle_new_command reads at call time.
    daemon._FOLDERS = {"vault": "~/obsidian_vault", "pirelli": "~/work/pirelli"}
    daemon.DEFAULT_FOLDER_NAME = "vault"

    cases = [
        ("", ("vault", "")),
        ("почини баг", ("vault", "почини баг")),
        ("pirelli", ("pirelli", "")),
        ("pirelli почини баг", ("pirelli", "почини баг")),
        ("pirelli   лишние   пробелы", ("pirelli", "лишние   пробелы")),
    ]
    for rest, want in cases:
        got = daemon._split_folder_and_task(rest)
        if got != want:
            failures.append(f"_split_folder_and_task({rest!r}): got {got}, want {want}")

    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)

    print("OK — _parse_backends/_split_folder_and_task checks passed")


if __name__ == "__main__":
    run()
