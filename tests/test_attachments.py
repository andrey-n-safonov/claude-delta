"""
Plain-assert test (no pytest, see test_prompt_detect.py). Run with:

    python3 tests/test_attachments.py

Pure filesystem, no deltachat needed.
"""
import os
import pathlib
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from claude_delta import attachments


def run():
    failures = []

    # --- safe_name: no path escape, no spaces/metachars, extension kept ---
    cases = {
        "report.pdf": "report.pdf",
        "Отчёт за август.xlsx": "Отчёт_за_август.xlsx",
        "../../etc/passwd": "passwd",
        "..\\..\\evil.sh": "evil.sh",
        "a b;rm -rf $HOME.txt": "a_b_rm_-rf_HOME.txt",
        "": "file",
        None: "file",
        "...": "file",
        ".bashrc": "bashrc",
    }
    for src, want in cases.items():
        got = attachments.safe_name(src)
        if got != want:
            failures.append(f"safe_name({src!r}): got {got!r}, want {want!r}")
    long = attachments.safe_name("x" * 500 + ".pdf")
    if len(long) > 100 or not long.endswith(".pdf"):
        failures.append(f"safe_name long: len={len(long)} name={long[-10:]!r}")

    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "files")
        src = os.path.join(tmp, "my doc.pdf")
        pathlib.Path(src).write_bytes(b"x" * 2048)

        # --- session_dir: scratchpad when the cwd-slug dir exists, else fallback ---
        tmp_base = os.path.join(tmp, "claude-uid")
        cwd = "/home/ans/obsidian_vault/02 Projects/Разработка Клод"
        slug = "-home-ans-obsidian-vault-02-Projects-" + "-" * len("Разработка Клод")
        got = attachments.session_dir("sid-1", cwd, root, 7, tmp_base=tmp_base)
        if got != os.path.join(root, "7"):
            failures.append(f"session_dir without slug dir must fall back, got {got}")
        os.makedirs(os.path.join(tmp_base, slug))
        got = attachments.session_dir("sid-1", cwd, root, 7, tmp_base=tmp_base)
        want = os.path.join(tmp_base, slug, "sid-1", "scratchpad", "received")
        if got != want:
            failures.append(f"session_dir scratchpad: got {got}, want {want}")
        if attachments.session_dir("sid-1", None, root, 7, tmp_base=tmp_base) != os.path.join(root, "7"):
            failures.append("session_dir without cwd must fall back")

        # --- save: copy into dest_dir as <msg>_<name>, private perms ---
        dest = attachments.save(src, os.path.join(root, "7"), 42, 10_000)
        if dest != os.path.join(root, "7", "42_my_doc.pdf"):
            failures.append(f"save dest: {dest}")
        if not os.path.exists(dest) or os.path.getsize(dest) != 2048:
            failures.append("save: copy missing or wrong size")
        if oct(os.stat(dest).st_mode & 0o777) != "0o600":
            failures.append(f"save perms: {oct(os.stat(dest).st_mode & 0o777)}")

        # source removed (as delete_processed does) — copy must survive
        os.unlink(src)
        if not os.path.exists(dest):
            failures.append("save: copy must be independent of the source blob")

        # --- size limit: refused before anything is written ---
        big = os.path.join(tmp, "big.bin")
        pathlib.Path(big).write_bytes(b"y" * 5000)
        try:
            attachments.save(big, os.path.join(root, "7"), 43, 4096)
            failures.append("save: over-limit file was accepted")
        except attachments.TooLarge as e:
            if e.args[0] != 5000:
                failures.append(f"TooLarge carries size, got {e.args}")
        if os.path.exists(os.path.join(root, "7", "43_big.bin")):
            failures.append("save: over-limit file leaked to disk")

        # --- cleanup: by age, empty chat dirs dropped, fresh kept ---
        old_dest = attachments.save(big, os.path.join(root, "8"), 1, 10_000)
        old = time.time() - 30 * 86400
        os.utime(old_dest, (old, old))
        removed = attachments.cleanup(root, keep_days=14)
        if removed != 1:
            failures.append(f"cleanup removed {removed}, want 1")
        if os.path.exists(old_dest) or os.path.exists(os.path.dirname(old_dest)):
            failures.append("cleanup: old file / empty chat dir still there")
        if not os.path.exists(dest):
            failures.append("cleanup: fresh file was removed")
        if attachments.cleanup(os.path.join(tmp, "nope"), 14) != 0:
            failures.append("cleanup on missing root should be a no-op")

    for n, want in [(0, "0 Б"), (1023, "1023 Б"), (1536, "1.5 КБ"), (5 * 1024 * 1024, "5.0 МБ")]:
        if attachments.human_size(n) != want:
            failures.append(f"human_size({n}): {attachments.human_size(n)!r} != {want!r}")

    if failures:
        print("FAIL")
        for f in failures:
            print(" -", f)
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(run())
