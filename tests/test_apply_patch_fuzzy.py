"""v0.0.27: apply_patch fuzzy context matching (the four-pass cascade).

The hands emits patches whose ``@@`` context / old-lines near-miss the file --
trailing whitespace, leading indentation, smart quotes, em-dashes, non-breaking
spaces. Exact-only matching failed on the first real apply_patch use ("a hunk's
context did not match the file"). The cascade (exact -> rstrip -> trim ->
unicode-normalized) makes these near-misses apply, while a GENUINE mismatch still
fails atomically. Network-free.
"""

from __future__ import annotations

from relay.tools import (
    Tools,
    _cmp_normalized,
    _normalize_unicode,
    _seek_sequence,
)


def _envelope(*body):
    return "\n".join(["*** Begin Patch", *body, "*** End Patch"])


# --- the normalizer + cascade helpers in isolation --------------------------


def test_normalize_unicode_maps_punctuation_to_ascii():
    assert _normalize_unicode("‘a’ “b”") == "'a' \"b\""   # quotes
    assert _normalize_unicode("a—b – c") == "a-b - c"                  # dashes
    assert _normalize_unicode("done…") == "done..."                          # ellipsis
    assert _normalize_unicode("a b") == "a b"                                  # NBSP


def test_seek_sequence_cascade_passes():
    lines = ["def greet():", "    return 'hi'", "", "x = 1"]
    # exact
    assert _seek_sequence(lines, ["def greet():"], 0) == 0
    # rstrip (trailing whitespace on the pattern)
    assert _seek_sequence(lines, ["def greet():   "], 0) == 0
    # trim (leading indentation differs)
    assert _seek_sequence(lines, ["return 'hi'"], 0) == 1
    # unicode-normalized (smart quotes in the pattern)
    assert _seek_sequence(lines, ["    return ‘hi’"], 0) == 1
    # genuine miss
    assert _seek_sequence(lines, ["nope"], 0) == -1


def test_seek_sequence_trailing_empty_line_retry():
    lines = ["alpha", "beta"]
    # The pattern has a trailing empty line the file lacks; the retry drops it.
    assert _seek_sequence(lines, ["alpha", ""], 0) == 0


def test_cmp_normalized_is_whole_line_not_substring():
    # The cascade compares whole lines -- it must NOT match a substring.
    assert _cmp_normalized("hello world", "hello") is False
    assert _cmp_normalized("hello", "hello") is True


# --- end-to-end: each near-miss now applies via apply_patch -----------------


def test_trailing_whitespace_difference_applies(tmp_path):
    (tmp_path / "f.py").write_text("alpha\nbeta\n", encoding="utf-8")
    # The patch's context line has trailing whitespace the file lacks.
    obs = Tools(tmp_path).apply_patch(_envelope(
        "*** Update File: f.py",
        "@@ alpha   ",
        "-alpha   ",
        "+ALPHA",
    ))
    assert obs.startswith("applied patch")
    assert (tmp_path / "f.py").read_text(encoding="utf-8") == "ALPHA\nbeta\n"


def test_leading_indentation_difference_applies(tmp_path):
    (tmp_path / "f.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    # The locator/removed line omits the file's leading indentation (the near-miss);
    # the trim pass still locates it. The "+" line carries the intended content.
    obs = Tools(tmp_path).apply_patch(_envelope(
        "*** Update File: f.py",
        "@@ def f():",
        "-return 1",
        "+    return 2",
    ))
    assert obs.startswith("applied patch")
    assert (tmp_path / "f.py").read_text(encoding="utf-8") == "def f():\n    return 2\n"


def test_smart_quotes_vs_straight_quotes_applies(tmp_path):
    (tmp_path / "f.py").write_text("msg = 'hello'\n", encoding="utf-8")
    # The patch uses smart quotes; the file has straight quotes.
    obs = Tools(tmp_path).apply_patch(_envelope(
        "*** Update File: f.py",
        "@@ msg = ‘hello’",
        "-msg = ‘hello’",
        "+msg = 'goodbye'",
    ))
    assert obs.startswith("applied patch")
    assert (tmp_path / "f.py").read_text(encoding="utf-8") == "msg = 'goodbye'\n"


def test_em_dash_vs_hyphen_applies(tmp_path):
    (tmp_path / "doc.md").write_text("a - b\n", encoding="utf-8")
    # The patch uses an em-dash where the file has a hyphen.
    obs = Tools(tmp_path).apply_patch(_envelope(
        "*** Update File: doc.md",
        "@@ a — b",
        "-a — b",
        "+a and b",
    ))
    assert obs.startswith("applied patch")
    assert (tmp_path / "doc.md").read_text(encoding="utf-8") == "a and b\n"


def test_non_breaking_space_vs_space_applies(tmp_path):
    (tmp_path / "f.txt").write_text("x = 1\n", encoding="utf-8")
    # The patch uses a non-breaking space between tokens; the file has a normal one.
    obs = Tools(tmp_path).apply_patch(_envelope(
        "*** Update File: f.txt",
        "@@ x = 1",
        "-x = 1",
        "+x = 2",
    ))
    assert obs.startswith("applied patch")
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "x = 2\n"


def test_genuine_mismatch_still_fails_atomically(tmp_path):
    (tmp_path / "f.py").write_text("alpha\nbeta\n", encoding="utf-8")
    (tmp_path / "other.txt").write_text("keep", encoding="utf-8")
    obs = Tools(tmp_path).apply_patch(_envelope(
        "*** Add File: created.txt",
        "+new",
        "*** Update File: f.py",
        "@@ totally different content",
        "-totally different content",
        "+x",
    ))
    assert obs.startswith("apply_patch failed")
    assert "f.py" in obs
    # Atomic: the valid Add did NOT apply, f.py untouched.
    assert not (tmp_path / "created.txt").exists()
    assert (tmp_path / "f.py").read_text(encoding="utf-8") == "alpha\nbeta\n"
