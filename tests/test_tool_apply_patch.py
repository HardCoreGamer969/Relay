"""Tests for `apply_patch` (v0.0.26): OpenCode's exact envelope, applied atomically.

`apply_patch` is ADDITIVE -- `edit` stays for surgical single changes; `apply_patch`
is for multi-file / multi-location / rename / delete in one atomic op. It is a
text-protocol tag (`<apply_patch>*** Begin Patch ... *** End Patch</apply_patch>`)
parsed by `parse()`, validated WHOLE then applied all-or-nothing via `Tools.apply_patch`,
and honors the read-before-edit guard per Update section. Network-free.
"""

from __future__ import annotations

from types import SimpleNamespace

from relay.config import ModelConfig
from relay.loop import STATUS_COMPLETED, guarded_execute_action, record_read
from relay.orchestrator import run_planned
from relay.protocol import Action, parse
from relay.tools import PatchError, Tools, parse_patch

CFG = ModelConfig(brain="vendor/brain", hands="vendor/hands")


def _envelope(*body_lines):
    return "\n".join(["*** Begin Patch", *body_lines, "*** End Patch"])


# --- parsing: the tag + the envelope ----------------------------------------


def test_parse_apply_patch_tag_carries_envelope_body():
    text = '<apply_patch>*** Begin Patch\n*** Delete File: x.txt\n*** End Patch</apply_patch>'
    result = parse(text)
    assert [a.kind for a in result.actions] == ["apply_patch"]
    assert result.actions[0].content.startswith("*** Begin Patch")


def test_parse_patch_envelope_sections():
    sections = parse_patch(_envelope(
        "*** Add File: a.txt",
        "+hello",
        "*** Update File: b.py",
        "*** Move to: c.py",
        "@@ def f():",
        "-    return 1",
        "+    return 2",
        "*** Delete File: old.txt",
    ))
    assert [(s.op, s.path) for s in sections] == [
        ("add", "a.txt"), ("update", "b.py"), ("delete", "old.txt"),
    ]
    update = sections[1]
    assert update.move_to == "c.py"
    assert update.hunks[0].before == ["    return 1"]
    assert update.hunks[0].after == ["    return 2"]


def test_parse_patch_rejects_missing_end():
    try:
        parse_patch("*** Begin Patch\n*** Delete File: x")
        assert False, "expected PatchError"
    except PatchError as exc:
        assert "End Patch" in str(exc)


def test_parse_patch_rejects_unknown_header():
    try:
        parse_patch(_envelope("*** Frobnicate File: x"))
        assert False, "expected PatchError"
    except PatchError as exc:
        assert "unknown patch line" in str(exc)


# --- Tools.apply_patch: a full multi-op patch applies atomically ------------


def test_multi_op_patch_applies_atomically(tmp_path):
    (tmp_path / "app.py").write_text("def greet():\n    return 1\n", encoding="utf-8")
    (tmp_path / "obsolete.txt").write_text("bye", encoding="utf-8")
    tools = Tools(tmp_path)

    obs = tools.apply_patch(_envelope(
        "*** Add File: hello.txt",
        "+Hello world",
        "*** Update File: app.py",
        "*** Move to: main.py",
        "@@ def greet():",
        "-    return 1",
        "+    return 2",
        "*** Delete File: obsolete.txt",
    ))

    assert obs == "applied patch: +hello.txt, ~main.py (renamed from app.py), -obsolete.txt"
    assert (tmp_path / "hello.txt").read_text(encoding="utf-8") == "Hello world\n"
    assert (tmp_path / "main.py").read_text(encoding="utf-8") == "def greet():\n    return 2\n"
    assert not (tmp_path / "app.py").exists()           # renamed away
    assert not (tmp_path / "obsolete.txt").exists()     # deleted


def test_delete_of_missing_file_fails_whole_patch(tmp_path):
    (tmp_path / "keep.txt").write_text("v1", encoding="utf-8")
    tools = Tools(tmp_path)
    obs = tools.apply_patch(_envelope(
        "*** Add File: created.txt",
        "+x",
        "*** Delete File: ghost.txt",
    ))
    assert obs.startswith("apply_patch failed")
    assert "ghost.txt" in obs
    assert not (tmp_path / "created.txt").exists()  # NOTHING applied (atomic)


def test_nonlocating_anchor_fails_atomically(tmp_path):
    (tmp_path / "f.py").write_text("alpha\nbeta\n", encoding="utf-8")
    tools = Tools(tmp_path)
    obs = tools.apply_patch(_envelope(
        "*** Add File: side.txt",
        "+x",
        "*** Update File: f.py",
        "@@ def nonexistent():",
        "-alpha",
        "+ALPHA",
    ))
    assert obs.startswith("apply_patch failed")
    assert "anchor not found" in obs
    assert (tmp_path / "f.py").read_text(encoding="utf-8") == "alpha\nbeta\n"  # untouched
    assert not (tmp_path / "side.txt").exists()  # the valid Add did NOT apply either


def test_add_to_existing_path_fails(tmp_path):
    (tmp_path / "exists.txt").write_text("here", encoding="utf-8")
    obs = Tools(tmp_path).apply_patch(_envelope("*** Add File: exists.txt", "+new"))
    assert obs.startswith("apply_patch failed") and "already exists" in obs
    assert (tmp_path / "exists.txt").read_text(encoding="utf-8") == "here"


def test_malformed_envelope_fails_cleanly(tmp_path):
    obs = Tools(tmp_path).apply_patch("not a patch at all")
    assert obs.startswith("apply_patch failed")
    assert "Begin Patch" in obs


# --- the read-before-edit guard extends to apply_patch Update sections ------


def test_update_without_current_read_refuses_whole_patch(tmp_path):
    (tmp_path / "app.py").write_text("alpha\n", encoding="utf-8")
    tools = Tools(tmp_path)
    reads: dict[str, str] = {}
    action = Action(kind="apply_patch", content=_envelope(
        "*** Add File: new.txt",
        "+x",
        "*** Update File: app.py",
        "@@ alpha",
        "-alpha",
        "+beta",
    ))
    obs = guarded_execute_action(tools, action, reads)
    assert "Refused: you must read" in obs and "app.py" in obs
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "alpha\n"  # untouched
    assert not (tmp_path / "new.txt").exists()  # whole patch refused (atomic)


def test_update_with_current_read_applies(tmp_path):
    (tmp_path / "app.py").write_text("alpha\n", encoding="utf-8")
    tools = Tools(tmp_path)
    reads: dict[str, str] = {}
    record_read(tools, "app.py", reads)  # the hands read it first
    action = Action(kind="apply_patch", content=_envelope(
        "*** Update File: app.py",
        "@@ alpha",
        "-alpha",
        "+beta",
    ))
    obs = guarded_execute_action(tools, action, reads)
    assert obs.startswith("applied patch")
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "beta\n"


def test_add_section_needs_no_read(tmp_path):
    tools = Tools(tmp_path)
    reads: dict[str, str] = {}
    action = Action(kind="apply_patch", content=_envelope("*** Add File: fresh.txt", "+hi"))
    obs = guarded_execute_action(tools, action, reads)
    assert obs.startswith("applied patch")
    assert (tmp_path / "fresh.txt").read_text(encoding="utf-8") == "hi\n"


def test_apply_patch_invalidates_reads_so_a_second_blind_patch_refuses(tmp_path):
    (tmp_path / "app.py").write_text("alpha\n", encoding="utf-8")
    tools = Tools(tmp_path)
    reads: dict[str, str] = {}
    record_read(tools, "app.py", reads)
    first = Action(kind="apply_patch", content=_envelope(
        "*** Update File: app.py", "@@ alpha", "-alpha", "+beta",
    ))
    assert guarded_execute_action(tools, first, reads).startswith("applied patch")
    # The patch changed app.py -> the recorded read is stale; a second patch refuses.
    second = Action(kind="apply_patch", content=_envelope(
        "*** Update File: app.py", "@@ beta", "-beta", "+gamma",
    ))
    assert "Refused: you must read" in guarded_execute_action(tools, second, reads)


# --- v0.0.31: apply_patch no-op honesty (don't report "applied" for no change) ---


def test_noop_patch_reports_no_change_not_applied(tmp_path):
    """A context-only hunk LOCATES but changes nothing -- it must NOT report 'applied'
    (which would send the hands into a read->patch->re-read loop), but 'no change'."""
    (tmp_path / "app.py").write_text("alpha\n", encoding="utf-8")
    tools = Tools(tmp_path)
    reads: dict[str, str] = {}
    record_read(tools, "app.py", reads)
    action = Action(kind="apply_patch", content=_envelope(
        "*** Update File: app.py",
        "@@ alpha",
        " alpha",          # leading space = context-only: no -/+ so nothing changes
    ))
    obs = guarded_execute_action(tools, action, reads)
    assert not obs.startswith("applied patch")          # NOT treated as success
    assert "no change" in obs
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "alpha\n"  # untouched


def test_before_equals_after_hunk_is_a_noop(tmp_path):
    """A -/+ pair whose removed line equals the added line is also a no-op."""
    (tmp_path / "app.py").write_text("alpha\n", encoding="utf-8")
    obs = Tools(tmp_path).apply_patch(_envelope(
        "*** Update File: app.py", "@@ alpha", "-alpha", "+alpha",
    ))
    assert not obs.startswith("applied patch")
    assert "no change" in obs
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "alpha\n"


def test_real_patch_still_applies_after_the_noop_check(tmp_path):
    """The no-op check must NOT alter a patch that genuinely changes the file."""
    (tmp_path / "app.py").write_text("alpha\n", encoding="utf-8")
    obs = Tools(tmp_path).apply_patch(_envelope(
        "*** Update File: app.py", "@@ alpha", "-alpha", "+beta",
    ))
    assert obs == "applied patch: ~app.py"
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "beta\n"


def test_mixed_patch_applies_the_real_section_and_flags_the_noop(tmp_path):
    """A patch with one real Update and one no-op Update: report the real change as
    applied AND be honest that the other section changed nothing."""
    (tmp_path / "a.py").write_text("alpha\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("beta\n", encoding="utf-8")
    obs = Tools(tmp_path).apply_patch(_envelope(
        "*** Update File: a.py",
        "@@ alpha",
        "-alpha",
        "+ALPHA",
        "*** Update File: b.py",
        "@@ beta",
        " beta",            # context-only: no change to b.py
    ))
    assert obs.startswith("applied patch")
    assert "~a.py" in obs
    assert "no change" in obs and "b.py" in obs
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "ALPHA\n"
    assert (tmp_path / "b.py").read_text(encoding="utf-8") == "beta\n"  # untouched


def test_noop_patch_leaves_the_recorded_read_valid(tmp_path):
    """A no-op changed nothing, so the recorded read stays current -- a real follow-up
    patch applies without a needless re-read (the guard was not invalidated)."""
    (tmp_path / "app.py").write_text("alpha\n", encoding="utf-8")
    tools = Tools(tmp_path)
    reads: dict[str, str] = {}
    record_read(tools, "app.py", reads)
    noop = Action(kind="apply_patch", content=_envelope(
        "*** Update File: app.py", "@@ alpha", " alpha",
    ))
    guarded_execute_action(tools, noop, reads)
    real = Action(kind="apply_patch", content=_envelope(
        "*** Update File: app.py", "@@ alpha", "-alpha", "+omega",
    ))
    assert guarded_execute_action(tools, real, reads).startswith("applied patch")
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "omega\n"


# --- end-to-end through run_planned -----------------------------------------


def _resp(content):
    usage = SimpleNamespace(prompt_tokens=6, completion_tokens=4, total_tokens=10, cost=0.00002)
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))], usage=usage)


class _Completions:
    def __init__(self, brain, hands):
        self.brain = list(brain)
        self.hands = list(hands)

    def create(self, *, model, **kwargs):
        queue = self.brain if model == "vendor/brain" else self.hands
        return _resp(queue.pop(0))


class RoutedClient:
    def __init__(self, brain=(), hands=()):
        self.chat = SimpleNamespace(completions=_Completions(brain, hands))


def test_apply_patch_through_run_planned_read_then_patch(tmp_path):
    (tmp_path / "app.py").write_text("alpha\n", encoding="utf-8")
    patch = _envelope("*** Update File: app.py", "@@ alpha", "-alpha", "+beta")
    client = RoutedClient(
        brain=["<plan><step>patch app.py</step></plan>"],
        hands=[f'<read path="app.py"/>\n<apply_patch>{patch}</apply_patch>\n<done>patched</done>'],
    )
    result = run_planned("g", tmp_path, models=CFG, client=client, supervise=False)
    assert result.status == STATUS_COMPLETED
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "beta\n"
