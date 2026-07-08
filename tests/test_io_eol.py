"""Tests for the v0.0.32 byte-exact I/O + EOL preservation.

The bug: ``read_text``/``write_text`` with default ``newline=None`` runs every
read and write through universal-newlines, which silently translates CRLF to LF
on read and LF to ``os.linesep`` on write. Consequences: a ``has_crlf`` check
after a read is always False (CRLF was already stripped), a CRLF file becomes
LF after any edit on POSIX, and an LF file becomes CRLF after any edit on
Windows. These tests pin the fixed behavior end-to-end (read / grep / write /
edit / apply_patch), so a future stdlib change can't reintroduce the bug at
one of the call sites without a failing test.
"""

from __future__ import annotations

import os

from relay.tools import OBSERVATION_LINE_CAP, Tools, _cap_observation


def test_read_preserves_crlf(tmp_path):
    """``read`` must return CRLF intact -- apply_patch's ``has_crlf`` and the
    content-hash freshness check both depend on this."""
    f = tmp_path / "crlf.txt"
    f.write_bytes(b"line1\r\nline2\r\nline3\r\n")
    text = Tools(tmp_path).read("crlf.txt")
    assert text == "line1\r\nline2\r\nline3\r\n"


def test_write_preserves_crlf_on_overwrite(tmp_path):
    """Overwriting a CRLF file must KEEP CRLF, not rewrite to LF."""
    f = tmp_path / "crlf.txt"
    f.write_bytes(b"alpha\r\nbeta\r\n")
    Tools(tmp_path).write("crlf.txt", "alpha\nbeta\n")  # model emits LF
    # The file on disk must still be CRLF -- the v0.0.31 bug would write LF here.
    assert f.read_bytes() == b"alpha\r\nbeta\r\n"


def test_edit_preserves_crlf_on_overwrite(tmp_path):
    """Same property for ``edit`` (also a whole-file overwrite)."""
    f = tmp_path / "crlf.txt"
    f.write_bytes(b"x\r\ny\r\n")
    Tools(tmp_path).edit("crlf.txt", "x\ny\n")
    assert f.read_bytes() == b"x\r\ny\r\n"


def test_new_file_defaults_to_lf(tmp_path):
    """A brand-new file (no prior EOL) gets LF -- the universal default."""
    f = tmp_path / "fresh.txt"
    Tools(tmp_path).write("fresh.txt", "a\nb\n")
    assert f.read_bytes() == b"a\nb\n"  # LF, not CRLF


def test_overwriting_lf_file_stays_lf(tmp_path):
    """Overwriting an LF file must stay LF, not become CRLF (Windows regression)."""
    f = tmp_path / "lf.txt"
    f.write_bytes(b"line1\nline2\n")
    Tools(tmp_path).edit("lf.txt", "new1\nnew2\n")
    assert f.read_bytes() == b"new1\nnew2\n"
    assert b"\r\n" not in f.read_bytes()


def test_wrote_observation_reports_real_on_disk_bytes(tmp_path):
    """The ``wrote`` observation must report the on-disk byte count, not the
    char count. For ASCII content with no CRLF, they match; for CRLF, the
    on-disk count is larger (this is the bug ``len(content)`` masked)."""
    f = tmp_path / "crlf.txt"
    f.write_bytes(b"a\r\nb\r\n")  # 6 bytes on disk
    obs = Tools(tmp_path).write("crlf.txt", "a\nb\n")
    # 4 chars in the str; 6 bytes on disk after CRLF conversion. The fix must
    # report 6 (the real on-disk size), not 4 (the char count).
    assert "(6 bytes, 2 lines)" in obs


def test_wrote_observation_byte_count_for_unicode(tmp_path):
    """A multi-byte unicode char in a UTF-8 file is more bytes than chars.
    The on-disk byte count must reflect the actual encoded bytes."""
    f = tmp_path / "u.txt"
    f.write_bytes("héllo\n".encode("utf-8"))  # 7 bytes (é = 2 bytes in UTF-8)
    obs = Tools(tmp_path).write("u.txt", "héllo\n")
    assert "(7 bytes, 1 lines)" in obs


def test_apply_patch_update_preserves_crlf(tmp_path):
    """An ``Update`` hunk on a CRLF file must write CRLF back, not LF."""
    f = tmp_path / "f.py"
    f.write_bytes(b"def f():\r\n    return 1\r\n")
    patch = (
        "*** Begin Patch\n"
        "*** Update File: f.py\n"
        "@@ def f():\n"
        "-    return 1\n"
        "+    return 2\n"
        "*** End Patch\n"
    )
    obs = Tools(tmp_path).apply_patch(patch)
    assert obs.startswith("applied patch")
    assert f.read_bytes() == b"def f():\r\n    return 2\r\n"


def test_apply_patch_add_writes_lf_for_new_file(tmp_path):
    """A new Add section defaults to LF."""
    patch = (
        "*** Begin Patch\n"
        "*** Add File: new.txt\n"
        "+alpha\n"
        "+beta\n"
        "*** End Patch\n"
    )
    obs = Tools(tmp_path).apply_patch(patch)
    assert obs.startswith("applied patch")
    assert (tmp_path / "new.txt").read_bytes() == b"alpha\nbeta\n"


def test_grep_returns_matching_lines_even_with_crlf(tmp_path):
    """grep reads via the EOL-preserving helper; the splitlines() at line 487
    still yields the right lines (splitlines handles all EOLs), so a regex
    match against a CRLF file must return the line without the trailing \\r."""
    f = tmp_path / "crlf.txt"
    f.write_bytes(b"alpha\r\nbeta\r\ngamma\r\n")
    out = Tools(tmp_path).grep("beta", "crlf.txt")
    assert "beta" in out
    # No stray CR in the output (splitlines() stripped it; that's correct -- the
    # agent wants the line content, not the on-disk terminator).
    assert "\r" not in out


# --- v0.0.32: observation capping (0.4) -------------------------------------
#
# read / grep / bash observations are capped head+tail with a "(N lines
# truncated)" marker so a verbose build log or a recursive grep across
# node_modules can't blow up the loop's context window. The cap is per-
# observation: a small text is unchanged (the common path is a no-op).


def test_observation_cap_passthrough_when_small():
    """Below the cap, the text is returned unchanged (no marker, no wrapping)."""
    small = "\n".join(f"line {i}" for i in range(50))  # well under OBSERVATION_LINE_CAP
    out = _cap_observation(small)
    assert out == small
    assert "truncated" not in out


def test_observation_cap_keeps_head_and_tail_with_marker():
    """Above the line cap, keep the head, mark the gap, keep the tail."""
    n = OBSERVATION_LINE_CAP + 500
    big = "\n".join(f"line {i}" for i in range(n))
    out = _cap_observation(big)
    lines = out.splitlines()
    # head + marker + tail, with marker reporting how many lines were omitted.
    assert "truncated" in out
    assert lines[0] == "line 0"  # head preserved
    assert lines[-1] == f"line {n - 1}"  # tail preserved
    # No more than head + tail + 1 marker line.
    assert len(lines) <= 2 * (OBSERVATION_LINE_CAP // 2) + 1 + 1


def test_observation_cap_char_cap_catches_one_giant_line():
    """A single very long line is caught by the char cap (the lines-cap won't fire)."""
    one_line = "x" * (50_000 + 100)
    out = _cap_observation(one_line)
    assert "truncated" in out
    # The char cap is the only thing that fires; the result must mention chars.
    assert "chars" in out


def test_read_caps_a_very_large_file(tmp_path):
    """End-to-end: a multi-thousand-line ``read`` returns head + marker + tail.

    Sized to clear the LINE cap (200) without overflowing the CHAR cap
    (50_000): 250 short lines at ~10 chars each = ~2500 chars, comfortably
    under both. The line cap is the one that fires -- a different scenario
    from the ``test_observation_cap_char_cap_catches_one_giant_line`` test.
    """
    n = 250
    f = tmp_path / "big.txt"
    f.write_text("\n".join(f"L{i}" for i in range(n)) + "\n", encoding="utf-8")
    out = Tools(tmp_path).read("big.txt")
    assert "truncated" in out
    # The head and tail are preserved.
    assert "L0" in out
    assert f"L{n - 1}" in out


def test_bash_caps_a_very_verbose_command(tmp_path):
    """End-to-end: a verbose bash command is capped before its output is
    returned, so a noisy build can't flood the loop. Uses a portable python
    one-liner to produce > OBSERVATION_LINE_CAP lines without assuming any
    other tools are installed."""
    # Note: the inner {i} must be shell-side, NOT Python f-string interpolation.
    # We use a plain string + .format only for the literal count.
    n = 1_000
    cmd = "python -c \"import sys; sys.stdout.write('\\n'.join('L%d' % i for i in range({n})))\"".format(n=n)
    out = Tools(tmp_path).bash(cmd)
    if "No such file" in out or "not recognized" in out or "cannot find" in out.lower():
        # python missing in this env -- still exercise the cap path on a fake
        # large stdout so the test isn't a no-op in environments without python.
        synthetic = "\n".join(f"L{i}" for i in range(n))
        out = _cap_observation(synthetic)
    assert "truncated" in out
