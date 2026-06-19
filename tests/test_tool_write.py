"""Tests for the `write` tool (v0.0.26): whole-file create / overwrite.

`write` is a text-protocol tag (`<write path="...">FULL CONTENTS</write>`) parsed
by `parse()`, executed via `Tools.write`, and threaded through the read-before-edit
guard: creating a NEW file needs no read; overwriting an EXISTING file requires a
current read first (don't blind-clobber a file you haven't seen). Network-free.
"""

from __future__ import annotations

from types import SimpleNamespace

from relay.config import ModelConfig
from relay.loop import STATUS_COMPLETED, guarded_execute_action
from relay.orchestrator import run_planned
from relay.protocol import Action, parse
from relay.tools import Tools

CFG = ModelConfig(brain="vendor/brain", hands="vendor/hands")


# --- parsing ----------------------------------------------------------------


def test_parse_write_tag():
    result = parse('<write path="a/b.txt">line1\nline2</write>')
    assert [a.kind for a in result.actions] == ["write"]
    action = result.actions[0]
    assert action.path == "a/b.txt"
    assert action.content == "line1\nline2"


def test_write_body_masks_nested_tags():
    # A write body containing tag-like text is NOT parsed as loose actions.
    result = parse('<write path="x.html"><bash>echo hi</bash>\n<read path="y"/></write>')
    assert [a.kind for a in result.actions] == ["write"]
    assert "<bash>echo hi</bash>" in result.actions[0].content


# --- Tools.write ------------------------------------------------------------


def test_write_creates_file_and_parent_dirs(tmp_path):
    obs = Tools(tmp_path).write("deep/nested/f.txt", "hello")
    assert (tmp_path / "deep" / "nested" / "f.txt").read_text(encoding="utf-8") == "hello"
    assert obs == "wrote deep/nested/f.txt (5 bytes, 1 lines)"


def test_write_overwrites_existing(tmp_path):
    (tmp_path / "f.txt").write_text("OLD", encoding="utf-8")
    Tools(tmp_path).write("f.txt", "NEW")
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "NEW"


# --- v0.0.31: a whole-file replacement is VISIBLE (so a fragment-clobber isn't silent) ---


def test_overwriting_existing_file_reveals_the_replacement(tmp_path):
    """Overwriting an existing file reveals the whole-file replacement + the prior size,
    so a tiny fragment that just clobbered a large file is VISIBLE (not a normal write)."""
    big = "\n".join(f"line {i}" for i in range(100)) + "\n"  # a sizeable existing file
    f = tmp_path / "page.html"
    f.write_text(big, encoding="utf-8")
    prior_bytes = len(f.read_bytes())  # whatever the FS stored (CRLF-agnostic)
    obs = Tools(tmp_path).write("page.html", "fragment")  # a tiny fragment clobbers it
    assert obs.startswith("wrote page.html (8 bytes, 1 lines)")
    assert "replaced entire file" in obs
    assert f"was {prior_bytes} bytes" in obs
    assert "100 lines" in obs
    assert f.read_text(encoding="utf-8") == "fragment"


def test_new_file_write_has_no_replaced_note(tmp_path):
    """A brand-new file destroyed nothing -- the observation stays plain (no note)."""
    obs = Tools(tmp_path).write("fresh.txt", "hi")
    assert obs == "wrote fresh.txt (2 bytes, 1 lines)"
    assert "replaced" not in obs


def test_edit_overwriting_existing_file_reveals_the_replacement(tmp_path):
    """edit has the same whole-file semantics as write -- it reveals the replacement too."""
    f = tmp_path / "f.txt"
    f.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    prior_bytes = len(f.read_bytes())
    obs = Tools(tmp_path).edit("f.txt", "x")
    assert "replaced entire file" in obs
    assert f"was {prior_bytes} bytes" in obs
    assert "3 lines" in obs


# --- the read-before-edit guard extends to write ----------------------------


def test_new_file_write_needs_no_read(tmp_path):
    tools = Tools(tmp_path)
    reads: dict[str, str] = {}
    obs = guarded_execute_action(tools, Action(kind="write", path="new.txt", content="N"), reads)
    assert obs.startswith("wrote ")
    assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "N"


def test_existing_file_write_without_read_is_refused(tmp_path):
    tools = Tools(tmp_path)
    reads: dict[str, str] = {}
    (tmp_path / "f.txt").write_text("ORIGINAL", encoding="utf-8")

    obs = guarded_execute_action(tools, Action(kind="write", path="f.txt", content="CLOBBER"), reads)
    assert "Refused: you must read" in obs
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "ORIGINAL"  # untouched


def test_read_then_write_existing_file_applies(tmp_path):
    tools = Tools(tmp_path)
    reads: dict[str, str] = {}
    (tmp_path / "f.txt").write_text("ORIGINAL", encoding="utf-8")

    guarded_execute_action(tools, Action(kind="read", path="f.txt"), reads)
    guarded_execute_action(tools, Action(kind="write", path="f.txt", content="UPDATED"), reads)
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "UPDATED"


# --- end-to-end through the executor loop -----------------------------------


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


def test_write_new_file_through_run_planned(tmp_path):
    client = RoutedClient(
        brain=["<plan><step>create main.py</step></plan>"],
        hands=['<write path="main.py">print("hi")</write>\n<done>created</done>'],
    )
    result = run_planned("g", tmp_path, models=CFG, client=client, supervise=False)
    assert result.status == STATUS_COMPLETED
    assert (tmp_path / "main.py").read_text(encoding="utf-8") == 'print("hi")'
