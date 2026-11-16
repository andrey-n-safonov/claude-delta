"""
Plain-assert test (no pytest, see test_prompt_detect.py). Run with:

    python3 tests/test_mux.py

Covers the pure pieces of mux.py and the decisions in the herdr backend
that would cost a chat if wrong: target parsing (which backend a stored
target belongs to — legacy unprefixed tmux targets must stay tmux),
env-based target detection, herdr error classification (only "pane
gone" may make alive() False — the daemon deletes the session's chat on
False), newline collapsing in send_keys, relocation by session id.
Real tmux/herdr processes are not started: subprocess.run is stubbed
where a backend shells out; the primitives themselves were verified
live (see journal 2026-11-16).
"""
import json
import os
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from claude_delta import herdr, mux, tmux

failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


def test_parse_mode():
    cases = [
        ("  ⏸ manual mode on · ← 1 agent", "manual"),
        ("  ⏵⏵ accept edits on (shift+tab to cycle) · ← 1 agent", "accept-edits"),
        ("  ⏸ plan mode on (shift+tab to cycle) · ← 1 agent", "plan"),
        ("  ⏵⏵ auto mode on (shift+tab to cycle) · ← 1 agent", "auto"),
        ("some unrelated pane content\nwith no status line at all", None),
        ("", None),
    ]
    for pane_tail, expected in cases:
        got = mux._parse_mode(pane_tail)
        check(got == expected, f"_parse_mode({pane_tail!r}) = {got!r}, expected {expected!r}")
    # The real status line is always lowercase, but the parser shouldn't
    # silently depend on that.
    check(mux._parse_mode("MANUAL MODE ON") == "manual", "_parse_mode is not case-insensitive")


def test_parse_target():
    cases = [
        ("%12", tmux, "%12"),                # legacy form, every pre-mux row
        ("tmux:%12", tmux, "%12"),
        ("mysess:0.1", tmux, "mysess:0.1"),  # tmux's own session:window.pane syntax
        ("herdr:w1:p3", herdr, "w1:p3"),
    ]
    for target, backend, pane in cases:
        got = mux.parse_target(target)
        check(got == (backend, pane), f"parse_target({target!r}) = {got!r}")
        check(mux.parse_target(mux.format_target(backend, pane)) == (backend, pane),
              f"format/parse roundtrip broke for {target!r}")
    check(mux.format_target(tmux, "%5") == "%5", "tmux target must stay unprefixed")
    check(mux.format_target(herdr, "w2:p1") == "herdr:w2:p1", "herdr target must be prefixed")


def test_target_from_env():
    cases = [
        ({"HERDR_ENV": "1", "HERDR_PANE_ID": "w1:p2"}, "herdr:w1:p2"),
        ({"TMUX_PANE": "%7"}, "%7"),
        # herdr pane started from inside tmux: the pane we're really in is herdr's
        ({"HERDR_ENV": "1", "HERDR_PANE_ID": "w1:p2", "TMUX_PANE": "%7"}, "herdr:w1:p2"),
        # HERDR_PANE_ID without HERDR_ENV=1 (exported by hand) is not trusted
        ({"HERDR_PANE_ID": "w1:p2", "TMUX_PANE": "%7"}, "%7"),
        ({}, None),
    ]
    for env, expected in cases:
        got = mux.target_from_env(env)
        check(got == expected, f"target_from_env({env}) = {got!r}, expected {expected!r}")


def test_default_backend():
    saved = os.environ.pop("DELTA_MUX", None)
    try:
        check(mux.default_backend() is tmux, "DELTA_MUX unset must default to tmux")
        os.environ["DELTA_MUX"] = " Herdr "
        check(mux.default_backend() is herdr, "DELTA_MUX=herdr not honored")
        os.environ["DELTA_MUX"] = "screen"
        try:
            mux.default_backend()
            failures.append("DELTA_MUX=screen must raise")
        except ValueError:
            pass
    finally:
        os.environ.pop("DELTA_MUX", None)
        if saved is not None:
            os.environ["DELTA_MUX"] = saved


def _herdr_err(code):
    return json.dumps({"id": "cli:pane:get", "error": {"code": code, "message": "x"}})


def test_herdr_alive():
    real_run = subprocess.run

    def stub(outcome):
        def run(cmd, **kw):
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        return run

    cases = [
        ("ok", subprocess.CompletedProcess(["herdr"], 0, "{}", ""), True),
        ("pane_not_found", subprocess.CalledProcessError(1, ["herdr"], "", _herdr_err("pane_not_found")), False),
        ("server_not_running", subprocess.CalledProcessError(1, ["herdr"], "", _herdr_err("server_not_running")), False),
        # anything else must NOT be read as "gone" — that would delete the chat
        ("other code", subprocess.CalledProcessError(1, ["herdr"], "", _herdr_err("internal_error")), True),
        ("garbage stderr", subprocess.CalledProcessError(1, ["herdr"], "", "boom"), True),
        ("timeout", subprocess.TimeoutExpired(["herdr"], 10), True),
        ("herdr not installed", FileNotFoundError("herdr"), False),
    ]
    try:
        for label, outcome, expected in cases:
            herdr.subprocess.run = stub(outcome)
            got = herdr.alive("w1:p1")
            check(got == expected, f"herdr.alive [{label}] = {got}, expected {expected}")
    finally:
        herdr.subprocess.run = real_run


def test_error_code():
    check(herdr.error_code(_herdr_err("pane_not_found")) == "pane_not_found", "error_code: valid JSON")
    for junk in ("", None, "not json", '{"result": {}}', '["x"]'):
        check(herdr.error_code(junk) is None, f"error_code({junk!r}) must be None")


class FakeBackend:
    NAME = "fake"

    def __init__(self):
        self.calls = []
        self.session_panes = {}

    def send_text(self, pane, text):
        self.calls.append(("text", pane, text))

    def press(self, pane, key):
        self.calls.append(("press", pane, key))

    def find_by_session(self, session_id):
        return self.session_panes.get(session_id)


def test_send_keys_and_relocate():
    fake = FakeBackend()
    real_parse, real_format = mux.parse_target, mux.format_target
    mux.parse_target = lambda target: (fake, target.split(":", 1)[1])
    mux.format_target = lambda backend, pane: f"fake:{pane}"
    try:
        mux.send_keys("fake:p1", "строка 1\nстрока 2\n")
        check(fake.calls == [("text", "p1", "строка 1 строка 2"), ("press", "p1", "enter")],
              f"send_keys must type one line then press enter once, got {fake.calls}")

        fake.calls.clear()
        mux.cycle_permission_mode("fake:p1")
        check(fake.calls == [("press", "p1", "shift-tab")], f"cycle_permission_mode: {fake.calls}")

        fake.session_panes["sid-1"] = "p9"
        check(mux.relocate("fake:p1", "sid-1") == "fake:p9", "relocate must find the moved pane")
        check(mux.relocate("fake:p1", "sid-2") is None, "relocate must return None for unknown session")
    finally:
        mux.parse_target, mux.format_target = real_parse, real_format

    check(tmux.find_by_session("anything") is None, "tmux never relocates")


def test_herdr_find_by_session():
    real_json = herdr._json
    herdr._json = lambda *a: {"agents": [
        {"pane_id": "w1:p1", "agent_session": None},
        {"pane_id": "w3:p2", "agent_session": {"kind": "id", "value": "sid-1"}},
    ]}
    try:
        check(herdr.find_by_session("sid-1") == "w3:p2", "find_by_session: match by agent_session.value")
        check(herdr.find_by_session("sid-x") is None, "find_by_session: no match")
    finally:
        herdr._json = real_json


def run():
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("OK — mux checks passed")


if __name__ == "__main__":
    run()
