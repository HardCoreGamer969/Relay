"""v0.0.27 Part 2: cross-platform `mkdir` tool + platform-aware executor prompt.

The hands runs on Windows but emits Unix-isms (`mkdir -p`, which Windows reads as a
folder named "-p"). Three coordinated fixes: a shell-free `mkdir` tool, the platform
injected into the executor prompt, and a note that `write` auto-creates parent dirs.
Network-free.
"""

from __future__ import annotations

from types import SimpleNamespace

from relay.config import ModelConfig
from relay.investigation import _WRITE_KINDS
from relay.loop import STATUS_COMPLETED, describe_action, execute_action
from relay.orchestrator import (
    EXECUTOR_SYSTEM_PROMPT,
    build_executor_system_prompt,
    run_planned,
)
from relay.protocol import Action, parse
from relay.tools import ToolError, Tools

CFG = ModelConfig(brain="vendor/brain", hands="vendor/hands")


# --- parsing + plumbing -----------------------------------------------------


def test_parse_mkdir_tag():
    result = parse('<mkdir path="a/b/c"/>')
    assert [a.kind for a in result.actions] == ["mkdir"]
    assert result.actions[0].path == "a/b/c"


def test_describe_action_mkdir():
    assert describe_action(Action(kind="mkdir", path="x/y")) == 'mkdir path="x/y"'


# --- Tools.mkdir (cross-platform, idempotent, no shell) ---------------------


def test_mkdir_creates_nested_path(tmp_path):
    obs = Tools(tmp_path).mkdir("a/b/c")
    assert (tmp_path / "a" / "b" / "c").is_dir()
    assert obs == "created directory a/b/c"


def test_mkdir_is_idempotent(tmp_path):
    tools = Tools(tmp_path)
    tools.mkdir("d")
    obs = tools.mkdir("d")  # second call: clean "already exists", not an error
    assert obs == "directory already exists: d"
    assert (tmp_path / "d").is_dir()


def test_mkdir_refuses_when_a_file_is_in_the_way(tmp_path):
    (tmp_path / "f").write_text("x", encoding="utf-8")
    try:
        Tools(tmp_path).mkdir("f")
        assert False, "expected ToolError"
    except ToolError as exc:
        assert "not a directory" in str(exc)


def test_mkdir_via_execute_action(tmp_path):
    obs = execute_action(Tools(tmp_path), Action(kind="mkdir", path="nested/dir"))
    assert obs == "created directory nested/dir"
    assert (tmp_path / "nested" / "dir").is_dir()


# --- write auto-creates parent dirs (confirmed; advertised) -----------------


def test_write_creates_parent_dirs(tmp_path):
    Tools(tmp_path).write("does/not/exist/yet/f.txt", "hi")
    assert (tmp_path / "does" / "not" / "exist" / "yet" / "f.txt").read_text(encoding="utf-8") == "hi"


# --- the executor prompt: platform line + write-creates-dirs note + mkdir ----


def test_executor_prompt_states_platform_windows():
    prompt = build_executor_system_prompt(system="Windows", platform_str="Windows-10")
    assert "running on Windows" in prompt
    assert "no `mkdir -p`" in prompt
    assert '<mkdir path="..."/>' in prompt


def test_executor_prompt_states_platform_generic():
    prompt = build_executor_system_prompt(system="Linux", platform_str="Linux-6.1")
    assert "running on Linux" in prompt
    assert "Linux-6.1" in prompt


def test_executor_prompt_notes_write_creates_dirs_and_advertises_mkdir():
    text = " ".join(EXECUTOR_SYSTEM_PROMPT.split())
    assert "creates parent directories automatically" in text
    assert "<mkdir" in text
    # The live platform line is present (built at import).
    assert "running on" in text


# --- the read-only brain refuses mkdir (it is a write) ----------------------


def test_mkdir_is_a_write_kind_for_the_brain():
    assert "mkdir" in _WRITE_KINDS


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


def test_mkdir_through_run_planned(tmp_path):
    client = RoutedClient(
        brain=["<plan><step>create the package dir</step></plan>"],
        hands=['<mkdir path="pkg/sub"/>\n<done>made dirs</done>'],
    )
    result = run_planned("g", tmp_path, models=CFG, client=client, supervise=False)
    assert result.status == STATUS_COMPLETED
    assert (tmp_path / "pkg" / "sub").is_dir()
