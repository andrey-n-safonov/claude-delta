"""
Detects the Claude Code harness's permission prompt in text captured
from a tmux pane (see design.md, "Architectural pivot 2026-08-09").

Detection is structural, not phrase-based: every confirmation dialog
observed in real captures (tests/fixtures/, calibrated 2026-08-10 against
live tmux panes — Bash-command approval, MCP tool-call approval,
workspace-trust prompt) renders as a numbered option list with a cursor
glyph ("❯") in front of the selected option, near the end of the visible
pane. The exact question wording varies (English, Russian, and presumably
future harness versions/locales) — the cursor+numbered-list shape is the
one thing all of them share, so that's what we key on instead of
hardcoding phrases like "do you want to proceed".

False positives aren't critical — just an extra message in the chat;
missing one is critical — so the window/thresholds below lean permissive.
"""
import re

# The harness's TUI cursor marking the currently selected option, right
# before a numbered choice — e.g. "❯ 1. Yes".
_CURSOR_OPTION_RE = re.compile(r"(?m)^\s*❯\s*[1-9]\.\s+\S")

# Any numbered option line, cursor or not — e.g. "  2. No".
_OPTION_LINE_RE = re.compile(r"(?m)^\s*[❯>]?\s*[1-9]\.\s+\S")

# How close to the end of the capture the option block must be to count
# as the *current* interactive prompt rather than an already-resolved one
# still visible higher up in the same viewport (a tall pane can show more
# than one exchange at once).
_TRAILING_WINDOW_LINES = 15


def _tail_lines(text: str) -> list[str]:
    return text.splitlines()[-_TRAILING_WINDOW_LINES:]


def is_permission_prompt(text: str) -> bool:
    tail = "\n".join(_tail_lines(text))
    has_cursor_option = bool(_CURSOR_OPTION_RE.search(tail))
    option_count = len(_OPTION_LINE_RE.findall(tail))
    return has_cursor_option and option_count >= 2


def _cursor_line_index(lines: list[str]) -> int | None:
    """Index of the *last* cursor-marked option line — a captured pane
    can contain an older, already-resolved prompt higher up; only the
    most recent one is still open."""
    idx = None
    for i, line in enumerate(lines):
        if _CURSOR_OPTION_RE.match(line):
            idx = i
    return idx


# Real-world calibration note (2026-08-10, live capture from a tmux pane
# mid-task): the pane's scrollback above an open prompt can contain a
# still-animating indicator (a pulsing bullet on the last assistant
# paragraph) that changes every poll even though the prompt itself is
# fully static. Hashing the *whole* capture for dedup/stability made a
# genuinely stable, unanswered prompt look "new" on every single poll —
# it would never pass the stability check and never get forwarded. Only
# the cursor-option line onward (remaining options + hint line) is
# guaranteed static while waiting for input; hash only that.

def hashable_tail(text: str) -> str:
    """The part of the capture that's safe to hash for dedup/stability —
    see calibration note above. Falls back to the full text if no cursor
    option line is found (caller is expected to have already checked
    is_permission_prompt first)."""
    lines = text.splitlines()
    idx = _cursor_line_index(lines)
    if idx is None:
        return text
    return "\n".join(lines[idx:])


# How far back (in lines) to look for the tool/command context above the
# prompt when building what actually gets sent to the human — bounded so
# a long scrollback above the prompt doesn't get forwarded wholesale
# (irrelevant, and may contain unrelated prior output).
_CONTEXT_LOOKBACK_LINES = 20

# A horizontal-rule line: any single non-space character repeated 4+
# times with nothing else on the line. Calibration note (2026-08-10,
# live capture): an earlier version only matched "─"/"—"/"-" — real
# output also draws these with "▔" (overline blocks, seen above the
# input box specifically) and presumably other box-drawing glyphs
# depending on context/version. Matching *any* repeated character avoids
# hardcoding which one, same reasoning as the cursor+list structural
# detector above — a rule the code doesn't recognize means the backward
# walk sails past it into unrelated scrollback (real bug: pulled in a
# previous unrelated reply + the user's own echoed input before this fix).
_RULE_LINE_RE = re.compile(r"^(\S)\1{3,}$")


def extract_context(text: str) -> str:
    """Prompt text to actually forward to Delta Chat: the cursor-option
    line's paragraph onward, plus a bounded amount of the tool/command
    context above it (stops at a horizontal rule line if the harness drew
    one, otherwise at _CONTEXT_LOOKBACK_LINES) — enough for a human to
    make the call without dumping the whole pane's scrollback into the
    chat (which may contain unrelated prior output, possibly secrets)."""
    lines = text.splitlines()
    idx = _cursor_line_index(lines)
    if idx is None:
        return text.strip()
    start = idx
    for back in range(1, _CONTEXT_LOOKBACK_LINES + 1):
        candidate = idx - back
        if candidate < 0:
            break
        if _RULE_LINE_RE.match(lines[candidate].strip()):
            start = candidate + 1
            break
        start = candidate
    return "\n".join(lines[start:]).strip()


# Header that visually sets a forwarded prompt apart from an ordinary
# chat message — plain-text distinction, doesn't depend on the peer
# client rendering Markdown.
_CHAT_HEADER = "🔐 Подтверждение действия"

# Strips the TUI cursor glyph ("❯") from an option line — over chat there
# is no "currently selected" option, just a plain numbered list to pick
# from by replying with a number.
_CURSOR_GLYPH_RE = re.compile(r"[❯>]\s*(?=[1-9]\.)")


def format_for_chat(text: str) -> str:
    """extract_context() output, reshaped for a chat audience: cursor
    glyph stripped from the option lines (there's nothing to be "the
    currently selected option" over chat — just a plain list), the
    trailing key-hint line dropped (its keys — Esc, Tab — don't apply to
    a phone typing a reply), and a header so the message reads as a
    permission request at a glance instead of blending in with ordinary
    status updates."""
    body = extract_context(text)
    lines = body.splitlines()

    last_option = None
    for i, line in enumerate(lines):
        if _OPTION_LINE_RE.match(line):
            last_option = i
    if last_option is not None:
        lines = lines[: last_option + 1]  # drop the hint line(s) after it

    # Stripping just the cursor glyph leaves the option it marked one
    # column shorter than the others ("❯ 1." -> " 1." vs "  2.") —
    # normalize every option line to the same leading indent instead of
    # only removing the glyph in place.
    lines = [
        f" {_CURSOR_GLYPH_RE.sub('', line).lstrip()}" if _OPTION_LINE_RE.match(line) else line
        for line in lines
    ]

    body = "\n".join(lines).strip()
    return f"{_CHAT_HEADER}\n\n{body}"


# Usage/rate-limit banners ("You've hit your session limit · resets
# 2:10pm") are a different shape from a permission prompt: a plain
# status line, no cursor, no numbered options — the harness's own choice
# menu ("1. Stop and wait" / "2. Upgrade your plan") that sometimes comes
# with it is already caught by is_permission_prompt/format_for_chat. This
# is specifically for the *residual* banner left behind once that menu is
# dismissed, which otherwise never reaches the chat — found live
# 2026-08-10 (session limit hit mid-task, banner sat in the pane with no
# way for the phone to know why the session had gone quiet).
#
# No structural signal distinguishes a passive banner from any other
# line of assistant output, so unlike the prompt detector above this one
# is intentionally phrase-based — narrow on purpose to avoid forwarding
# unrelated lines that happen to share a word.
_LIMIT_PHRASES = (
    "session limit",
    "usage limit",
    "hit your limit",
    "лимит сессии",
    "лимит использования",
)


def is_limit_notice(text: str) -> bool:
    tail = "\n".join(_tail_lines(text)).lower()
    return any(p in tail for p in _LIMIT_PHRASES)


def extract_limit_notice(text: str) -> str:
    return "\n".join(
        line.strip() for line in _tail_lines(text)
        if any(p in line.lower() for p in _LIMIT_PHRASES)
    )


_LIMIT_HEADER = "⏳ Лимит сессии Claude Code"


def format_limit_notice(text: str) -> str:
    return f"{_LIMIT_HEADER}\n\n{extract_limit_notice(text)}"
