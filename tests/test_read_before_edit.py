"""The read-before-edit guard (v0.0.23): the hands cannot <edit> an existing file
it has not read (with a still-current read) earlier in this run.

Two layers are tested: the guard HELPERS in isolation (content-hash freshness), and
the EXECUTOR behavior driven through ``run_planned`` with a network-free fake client
(refuse a blind edit on an existing file, allow read-then-edit, allow new-file
creation, refuse after a stale read, and recover -- the refusal is a normal
observation, never a step abort). The guard is the hands' only; the brain is
read-only and untouched.
"""

from __future__ import annotations

from types import SimpleNamespace

from relay.config import ModelConfig
from relay.loop import (
    STATUS_COMPLETED,
    guarded_execute_action,
    invalidate,
    read_is_current,
    record_read,
)
from relay.orchestrator import EXECUTOR_SYSTEM_PROMPT, run_planned
from relay.protocol import Action
from relay.tools import Tools

CFG = ModelConfig(brain="vendor/brain", hands="vendor/hands")


# ============================================================================
# Guard helpers in isolation (content-hash freshness)
# ============================================================================


def test_read_is_current_lifecycle(tmp_path):
    tools = Tools(tmp_path)
    reads: dict[str, str] = {}
    (tmp_path / "f.txt").write_text("v1", encoding="utf-8")

    # Never read -> not current.
    assert read_is_current(tools, "f.txt", reads) is False

    # Right after a read -> current.
    record_read(tools, "f.txt", reads)
    assert read_is_current(tools, "f.txt", reads) is True

    # The file's CONTENT changes (hash mismatch) -> stale, not current.
    (tmp_path / "f.txt").write_text("v2-different", encoding="utf-8")
    assert read_is_current(tools, "f.txt", reads) is False

    # Re-read reflecting the new state -> current again.
    record_read(tools, "f.txt", reads)
    assert read_is_current(tools, "f.txt", reads) is True


def test_edit_invalidates_the_read(tmp_path):
    """An edit invalidates the file's recorded read, so a second edit without a
    re-read would be refused (the entry is cleared)."""
    tools = Tools(tmp_path)
    reads: dict[str, str] = {}
    (tmp_path / "f.txt").write_text("v1", encoding="utf-8")
    record_read(tools, "f.txt", reads)
    assert read_is_current(tools, "f.txt", reads) is True

    invalidate(tools, "f.txt", reads)
    assert read_is_current(tools, "f.txt", reads) is False


def test_freshness_is_content_based_not_mtime(tmp_path):
    """Identical content hashes identically; only a content change flips freshness --
    so an unchanged file never needs a pointless re-read (robust where mtime isn't)."""
    tools = Tools(tmp_path)
    (tmp_path / "a.txt").write_text("same bytes", encoding="utf-8")
    h1 = tools.content_hash("a.txt")
    # Rewrite identical content (a new mtime, same bytes) -> same hash.
    (tmp_path / "a.txt").write_text("same bytes", encoding="utf-8")
    assert tools.content_hash("a.txt") == h1
    # Different content -> different hash.
    (tmp_path / "a.txt").write_text("other bytes", encoding="utf-8")
    assert tools.content_hash("a.txt") != h1


def test_content_hash_and_record_read_missing_file_are_safe(tmp_path):
    tools = Tools(tmp_path)
    reads: dict[str, str] = {}
    assert tools.content_hash("nope.txt") is None
    record_read(tools, "nope.txt", reads)  # no-op, no entry recorded
    assert reads == {}
    assert read_is_current(tools, "nope.txt", reads) is False


def test_guarded_execute_action_refuses_blind_edit_of_existing_file(tmp_path):
    """Unit-level: editing an existing, unread file is refused and the file is
    left untouched; reading then editing applies; creating a new file is allowed."""
    tools = Tools(tmp_path)
    reads: dict[str, str] = {}
    (tmp_path / "e.txt").write_text("ORIGINAL", encoding="utf-8")

    obs = guarded_execute_action(tools, Action(kind="edit", path="e.txt", content="BLIND"), reads)
    assert "Refused: you must read" in obs
    assert (tmp_path / "e.txt").read_text(encoding="utf-8") == "ORIGINAL"  # untouched

    guarded_execute_action(tools, Action(kind="read", path="e.txt"), reads)
    guarded_execute_action(tools, Action(kind="edit", path="e.txt", content="UPDATED"), reads)
    assert (tmp_path / "e.txt").read_text(encoding="utf-8") == "UPDATED"

    # A brand-new file: no read needed.
    guarded_execute_action(tools, Action(kind="edit", path="new.txt", content="NEW"), reads)
    assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "NEW"


# ============================================================================
# Executor behavior (driven through run_planned with a fake client)
# ============================================================================


def _resp(content):
    usage = SimpleNamespace(prompt_tokens=6, completion_tokens=4, total_tokens=10, cost=0.00002)
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))], usage=usage)


class _Completions:
    def __init__(self, brain, hands):
        self.brain = list(brain)
        self.hands = list(hands)
        self.calls: list[dict] = []

    def create(self, *, model, **kwargs):
        self.calls.append({"model": model, **kwargs})
        queue = self.brain if model == "vendor/brain" else self.hands
        assert queue, f"ran out of {'brain' if model == 'vendor/brain' else 'hands'} replies"
        return _resp(queue.pop(0))


class RoutedClient:
    def __init__(self, brain=(), hands=()):
        self.chat = SimpleNamespace(completions=_Completions(brain, hands))

    @property
    def calls(self):
        return self.chat.completions.calls


def _hands_calls(client):
    return [c for c in client.calls if c["model"] == "vendor/hands"]


def _exec_observations(result):
    return [e.payload.get("observation", "") for e in result.events if e.kind == "exec_action"]


def test_blind_edit_of_existing_file_is_refused_and_file_unchanged(tmp_path):
    (tmp_path / "app.py").write_text("ORIGINAL", encoding="utf-8")
    client = RoutedClient(
        brain=["<plan><step>tweak app.py</step></plan>"],
        hands=['<edit path="app.py">HACKED</edit>\n<done>edited</done>'],
    )
    result = run_planned("g", tmp_path, models=CFG, client=client, supervise=False)

    # The edit was refused; the file on disk is untouched.
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "ORIGINAL"
    assert any("Refused: you must read" in o for o in _exec_observations(result))


def test_read_then_edit_existing_file_applies(tmp_path):
    (tmp_path / "app.py").write_text("ORIGINAL", encoding="utf-8")
    client = RoutedClient(
        brain=["<plan><step>update app.py</step></plan>"],
        hands=['<read path="app.py"/>\n<edit path="app.py">UPDATED</edit>\n<done>edited</done>'],
    )
    result = run_planned("g", tmp_path, models=CFG, client=client, supervise=False)

    assert result.status == STATUS_COMPLETED
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "UPDATED"


def test_creating_a_new_file_needs_no_read(tmp_path):
    client = RoutedClient(
        brain=["<plan><step>create new.py</step></plan>"],
        hands=['<edit path="new.py">CONTENT</edit>\n<done>created</done>'],
    )
    result = run_planned("g", tmp_path, models=CFG, client=client, supervise=False)

    assert result.status == STATUS_COMPLETED
    assert (tmp_path / "new.py").read_text(encoding="utf-8") == "CONTENT"


def test_stale_read_after_an_edit_refuses_until_reread(tmp_path):
    """A read goes stale once the file is edited; a subsequent edit without a re-read
    is refused (file keeps the first edit's content)."""
    (tmp_path / "app.py").write_text("ORIGINAL", encoding="utf-8")
    client = RoutedClient(
        brain=["<plan><step>edit app.py twice</step></plan>"],
        hands=[
            '<read path="app.py"/>\n<edit path="app.py">V2</edit>',  # turn 1: read+edit (no done)
            '<edit path="app.py">V3</edit>\n<done>done</done>',      # turn 2: blind re-edit -> refused
        ],
    )
    result = run_planned("g", tmp_path, models=CFG, client=client, supervise=False)

    # V2 applied (read was current); V3 refused (the edit invalidated the read).
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "V2"
    assert any("Refused: you must read" in o for o in _exec_observations(result))


def test_reread_after_a_stale_read_allows_the_edit(tmp_path):
    (tmp_path / "app.py").write_text("ORIGINAL", encoding="utf-8")
    client = RoutedClient(
        brain=["<plan><step>edit app.py twice</step></plan>"],
        hands=[
            '<read path="app.py"/>\n<edit path="app.py">V2</edit>',
            '<read path="app.py"/>\n<edit path="app.py">V3</edit>\n<done>done</done>',
        ],
    )
    result = run_planned("g", tmp_path, models=CFG, client=client, supervise=False)

    assert result.status == STATUS_COMPLETED
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "V3"  # the re-read allowed V3


def test_refusal_is_a_normal_observation_not_a_step_abort(tmp_path):
    """A refused edit does NOT abort the step as a parse failure -- the loop continues
    and the hands recovers by reading first."""
    (tmp_path / "app.py").write_text("ORIGINAL", encoding="utf-8")
    client = RoutedClient(
        brain=["<plan><step>fix app.py</step></plan>"],
        hands=[
            '<edit path="app.py">BLIND</edit>',                                       # refused, no done
            '<read path="app.py"/>\n<edit path="app.py">FIXED</edit>\n<done>ok</done>',  # recovers
        ],
    )
    result = run_planned("g", tmp_path, models=CFG, client=client, supervise=False)

    assert result.status == STATUS_COMPLETED
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "FIXED"
    assert len(_hands_calls(client)) == 2  # the refusal did not abort; it recovered
    assert "exec_parse_failure" not in [e.kind for e in result.events]


# ============================================================================
# The prompt nudge (catch-the-brain)
# ============================================================================


def test_executor_prompt_has_read_before_edit_guidance():
    text = " ".join(EXECUTOR_SYSTEM_PROMPT.split())
    assert "read it first" in text
    assert "Before editing an existing file" in text


def test_executor_prompt_steers_whole_file_write_vs_apply_patch():
    """v0.0.31: the hands is told write/edit replace the WHOLE file (a fragment destroys
    it) and to use apply_patch to MODIFY part of an existing file."""
    text = " ".join(EXECUTOR_SYSTEM_PROMPT.split())
    assert "REPLACE THE WHOLE FILE" in text
    assert "DELETES the rest of the file" in text
    assert "To MODIFY part of an existing file" in text and "apply_patch" in text


def test_executor_prompt_advertises_the_new_tools():
    """The hands must be told about write/glob/apply_patch/webfetch and that the
    read-first rule extends to existing-file write + apply_patch Update sections."""
    text = " ".join(EXECUTOR_SYSTEM_PROMPT.split())
    assert "<write" in text and "<glob" in text
    assert "<apply_patch>" in text and "*** Begin Patch" in text
    assert "<webfetch" in text
    # The read-first rule is explicitly extended to write + apply_patch Update.
    assert "existing-file write" in text and "apply_patch Update" in text
