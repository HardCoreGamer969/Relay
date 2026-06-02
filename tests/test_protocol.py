"""Network-free tests for the text action protocol parser."""

from __future__ import annotations

from relay.protocol import parse


def test_parses_each_tag_type():
    text = """
    <thinking>read then edit</thinking>
    <read path="a.txt"/>
    <list path="src"/>
    <grep pattern="TODO" path="src"/>
    <edit path="b.txt">hello world</edit>
    <bash>echo hi</bash>
    """
    result = parse(text)

    assert [a.kind for a in result.actions] == ["read", "list", "grep", "edit", "bash"]
    assert result.thinking == ["read then edit"]
    assert not result.is_parse_failure

    by_kind = {a.kind: a for a in result.actions}
    assert by_kind["read"].path == "a.txt"
    assert by_kind["list"].path == "src"
    assert by_kind["grep"].pattern == "TODO" and by_kind["grep"].path == "src"
    assert by_kind["edit"].path == "b.txt" and by_kind["edit"].content == "hello world"
    assert by_kind["bash"].content == "echo hi"


def test_multiple_actions_kept_in_document_order():
    text = '<edit path="x">1</edit> then <read path="y"/> then <bash>ls</bash>'
    result = parse(text)
    assert [a.kind for a in result.actions] == ["edit", "read", "bash"]


def test_done_is_detected():
    result = parse("All set. <done>created the file</done>")
    assert result.is_done
    assert not result.is_parse_failure
    assert result.actions[0].kind == "done"
    assert result.actions[0].content == "created the file"


def test_parse_failure_when_no_action_present():
    result = parse("I think I should read the file but I'm not sure how.")
    assert result.is_parse_failure
    assert result.actions == []


def test_thinking_only_is_a_parse_failure():
    result = parse("<thinking>just musing, no action yet</thinking>")
    assert result.is_parse_failure
    assert result.thinking == ["just musing, no action yet"]


def test_list_path_defaults_to_dot():
    result = parse("<list/>")
    assert len(result.actions) == 1
    assert result.actions[0].kind == "list"
    assert result.actions[0].path == "."


def test_ignores_surrounding_prose_and_trims_edit_newlines():
    text = 'Sure! Here is the file:\n<edit path="hello.txt">\nhi from relay\n</edit>\nDone.'
    result = parse(text)
    assert len(result.actions) == 1
    assert result.actions[0].content == "hi from relay"


def test_tag_inside_edit_body_is_not_parsed_as_action():
    text = '<edit path="readme.md">see <read path="x"/> for details</edit>'
    result = parse(text)
    assert [a.kind for a in result.actions] == ["edit"]
    assert '<read path="x"/>' in result.actions[0].content


def test_single_quoted_attributes_are_accepted():
    result = parse("<read path='notes/todo.txt'/>")
    assert result.actions[0].kind == "read"
    assert result.actions[0].path == "notes/todo.txt"
