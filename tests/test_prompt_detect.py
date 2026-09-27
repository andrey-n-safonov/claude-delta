"""
Plain-assert test (no pytest — keeps the project's dependency list
minimal, see pyproject.toml). Run with:

    python3 tests/test_prompt_detect.py

Fixtures in tests/fixtures/ are real captures (tmux capture-pane) from
live tmux panes on 2026-08-10, used to calibrate is_permission_prompt
against the harness's actual UI rather than a guessed shape — see the
module docstring in prompt_detect.py.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from claude_delta.prompt_detect import (
    extract_context,
    extract_limit_notice,
    format_for_chat,
    format_limit_notice,
    hashable_tail,
    is_limit_notice,
    is_permission_prompt,
)

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"

POSITIVE = [
    "permission_prompt_bash.txt",    # Bash command approval, no rule line above
    "mcp_tool_use.txt",              # MCP tool-call approval, rule line above
    "bash_command_with_rule.txt",    # Bash command approval, rule line above
    "trust_folder_prompt.txt",       # one-time workspace-trust prompt
    "limit_dialog_with_overline_rule.txt",  # rule drawn with "▔", not "─"/"—"/"-"
    "model_switch_menu.txt",         # /model, 11 options — cursor on single-digit #5
    "model_switch_menu_cursor_on_10.txt",  # same menu, cursor arrowed down to double-digit #10
]

NEGATIVE = [
    "normal_output.txt",             # plain shell output, no prompt
    "normal_numbered_list.txt",      # markdown numbered list, no cursor glyph
]


def run():
    failures = []

    for name in POSITIVE:
        text = (FIXTURES / name).read_text()
        if not is_permission_prompt(text):
            failures.append(f"expected PROMPT, got none: {name}")

    for name in NEGATIVE:
        text = (FIXTURES / name).read_text()
        if is_permission_prompt(text):
            failures.append(f"expected NO prompt, got one: {name}")

    # Near-duplicate captures of the same still-open prompt (differing
    # only in an animating indicator elsewhere in the pane) must hash
    # identically — this is the exact bug found during calibration.
    # hashable_tail was the original fix (still true, still tested in
    # isolation); daemon.py now hashes extract_context instead (see
    # below) — same guarantee must hold for it too, since it went
    # further back into the capture to fix the collision bug.
    a = (FIXTURES / "mcp_tool_use.txt").read_text()
    b = a.replace("Calling netbox 9 times…", " Calling netbox 9 times…")  # simulate the spinner frame flip
    if hashable_tail(a) != hashable_tail(b):
        failures.append("hashable_tail is not stable against unrelated scrollback changes")
    if extract_context(a) != extract_context(b):
        failures.append("extract_context (daemon.py's actual hash source) is not stable against the spinner flip")

    # Regression: hashable_tail alone collides between different prompts
    # that happen to share the same options+hint (confirmed live
    # 2026-08-10 — a second, different Bash approval silently never got
    # forwarded because its hash matched the first one's). daemon.py
    # switched the dedup hash source to extract_context, which includes
    # the command/tool detail — verify it actually distinguishes every
    # positive fixture pairwise.
    contexts = [extract_context((FIXTURES / name).read_text()) for name in POSITIVE]
    if len(set(contexts)) != len(contexts):
        failures.append("extract_context collides between at least two distinct positive fixtures")

    # extract_context must not be empty and must include the question,
    # not just the raw option lines.
    ctx = extract_context((FIXTURES / "mcp_tool_use.txt").read_text())
    if "Do you want to proceed?" not in ctx:
        failures.append("extract_context dropped the question line")
    if "Calling netbox 9 times" in ctx:
        failures.append("extract_context leaked volatile scrollback above the rule line")

    # format_for_chat: no cursor glyph, no key-hint line, header present,
    # options still there (checked across all positive fixtures except
    # the trust prompt, which has no "Esc to cancel" hint to drop).
    for name in POSITIVE:
        text = (FIXTURES / name).read_text()
        formatted = format_for_chat(text)
        if "❯" in formatted:
            failures.append(f"format_for_chat left the cursor glyph in: {name}")
        if not formatted.startswith("🔐"):
            failures.append(f"format_for_chat missing the header: {name}")
        if "1." not in formatted:
            failures.append(f"format_for_chat dropped the options: {name}")
    if "Esc to cancel" in format_for_chat((FIXTURES / "mcp_tool_use.txt").read_text()):
        failures.append("format_for_chat kept the key-hint line")

    # Regression (2026-09-27): a 10+-option menu (/model has 11) must not
    # get truncated right after option 9 — the original [1-9]-only regex
    # treated "10." as non-option text, so format_for_chat's "find the
    # last option line" logic stopped one line early and dropped
    # everything from "10." onward, silently. Both fixtures (cursor on
    # #5, cursor arrowed down to #10) must forward the full list either way.
    for name in ("model_switch_menu.txt", "model_switch_menu_cursor_on_10.txt"):
        formatted = format_for_chat((FIXTURES / name).read_text())
        if "10. Opus 4.6" not in formatted:
            failures.append(f"format_for_chat truncated a 10+-option menu before option 10: {name}")
        if "9.  Opus 4.7" not in formatted:
            failures.append(f"format_for_chat lost option 9 in a 10+-option menu: {name}")

    # Regression: an overline-rule ("▔") above the prompt must stop the
    # backward walk just like a "─" rule does — previously it didn't,
    # leaking an unrelated earlier reply and the user's own echoed input
    # into the forwarded message (found live 2026-08-10).
    overline_ctx = extract_context((FIXTURES / "limit_dialog_with_overline_rule.txt").read_text())
    if "понятно" in overline_ctx or "Brewed for 8s" in overline_ctx:
        failures.append("extract_context leaked scrollback past an overline (▔) rule")

    # Limit banner: a plain status line, no menu — different detector
    # (is_permission_prompt must NOT fire on it, only is_limit_notice).
    banner = (FIXTURES / "limit_notice_banner.txt").read_text()
    if is_permission_prompt(banner):
        failures.append("is_permission_prompt false-positived on a plain limit banner")
    if not is_limit_notice(banner):
        failures.append("is_limit_notice missed a real limit banner")
    if "session limit" not in extract_limit_notice(banner).lower():
        failures.append("extract_limit_notice dropped the actual notice line")
    if not format_limit_notice(banner).startswith("⏳"):
        failures.append("format_limit_notice missing its header")
    for name in POSITIVE:
        if is_limit_notice((FIXTURES / name).read_text()):
            failures.append(f"is_limit_notice false-positived on a permission prompt: {name}")

    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)

    print(f"OK — {len(POSITIVE)} positive, {len(NEGATIVE)} negative, hash-stability and context checks passed")


if __name__ == "__main__":
    run()
