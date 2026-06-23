"""Network-free tests for the text action protocol parser."""

from __future__ import annotations

from relay.protocol import parse, tag_content, tag_contents


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


# --- v0.04 tags: plan / abort / blocked ------------------------------------


def test_plan_parses_steps_in_order():
    text = "Here is the plan:\n<plan><step>do A</step><step>do B</step><step>do C</step></plan>"
    result = parse(text)
    assert not result.is_parse_failure
    assert result.plan_steps == ["do A", "do B", "do C"]
    assert result.first("plan").steps == ["do A", "do B", "do C"]


def test_blocked_and_abort_parse_with_reason():
    blocked = parse("<blocked>cannot find the config file</blocked>")
    assert not blocked.is_parse_failure
    assert blocked.first("blocked").content == "cannot find the config file"

    aborted = parse("<abort>the goal contradicts itself</abort>")
    assert not aborted.is_parse_failure
    assert aborted.first("abort").content == "the goal contradicts itself"


def test_finding_parses_and_is_not_a_parse_failure():
    # v0.0.29: <finding> is a real action (the hands surfacing a bug/discovery).
    result = parse("<finding>auth.py stores the password in plaintext</finding>")
    assert not result.is_parse_failure
    assert result.first("finding").content == "auth.py stores the password in plaintext"


def test_finding_alongside_other_actions_keeps_document_order():
    text = '<read path="a.py"/>\n<finding>a.py has a SQL injection</finding>\n<done>looked</done>'
    result = parse(text)
    assert [a.kind for a in result.actions] == ["read", "finding", "done"]


def test_finding_inside_edit_body_is_not_parsed_loose():
    # A <finding> mentioned inside an <edit> file body must not surface a loose action.
    text = '<edit path="x.py">text with <finding>not real</finding> inside</edit>'
    result = parse(text)
    assert [a.kind for a in result.actions] == ["edit"]
    assert result.first("finding") is None


def test_tag_inside_plan_body_is_not_parsed_loose():
    # A step that mentions an <edit> must not surface a loose edit action.
    text = '<plan><step>create x via <edit path="x">y</edit></step></plan>'
    result = parse(text)
    assert [a.kind for a in result.actions] == ["plan"]
    assert result.first("edit") is None


def test_empty_plan_has_no_steps():
    result = parse("<plan></plan>")
    plan = result.first("plan")
    assert plan is not None
    assert plan.steps == []
    # plan_steps reflects the (empty) list, and the planner treats this as no plan.
    assert result.plan_steps == []


# --- v0.06: executor <question> tag ----------------------------------------


def test_question_parses_with_content():
    result = parse("<thinking>hmm</thinking><question>Which database should I use?</question>")
    assert not result.is_parse_failure
    q = result.first("question")
    assert q is not None and q.content == "Which database should I use?"


def test_question_distinct_from_blocked():
    result = parse("<question>need the API base url</question>")
    assert result.first("question") is not None
    assert result.first("blocked") is None  # a question is not a block


def test_question_inside_edit_body_is_not_parsed_loose():
    text = '<edit path="notes.md">TODO: ask <question>which db?</question> later</edit>'
    result = parse(text)
    assert [a.kind for a in result.actions] == ["edit"]
    assert result.first("question") is None
    assert "<question>which db?</question>" in result.actions[0].content


# --- shared tag-content helpers (v0.0.32: deduplicated from planner/conversation) ---


def test_tag_content_extracts_first_tag():
    assert tag_content("verdict", "<verdict>accept</verdict>") == "accept"
    assert tag_content("scope", "noise <scope>small</scope> tail") == "small"


def test_tag_content_returns_none_when_absent():
    assert tag_content("verdict", "no tag here") is None
    assert tag_content("verdict", "") is None


def test_tag_content_strips_whitespace():
    assert tag_content("verdict", "<verdict>  accept  </verdict>") == "accept"


def test_tag_content_handles_multiline():
    assert tag_content("reason", "<reason>line one\nline two</reason>") == "line one\nline two"


def test_tag_contents_extracts_all_non_empty():
    result = tag_contents("ask", "<ask>Q1</ask><ask></ask><ask>Q2</ask>")
    assert result == ["Q1", "Q2"]  # the empty <ask></ask> is dropped


def test_tag_contents_returns_empty_list_when_none():
    assert tag_contents("ask", "no tags") == []
