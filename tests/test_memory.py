"""Tests for the history replayed from session memory."""

from docs_curator.memory import prompt_history


def user(text: str) -> dict:
    """Build a user message."""
    return {"role": "user", "content": text}


def answer(text: str) -> dict:
    """Build an assistant answer."""
    return {"role": "assistant", "content": text}


def call(call_id: str) -> dict:
    """Build an assistant message calling search_docs."""
    function = {"name": "search_docs", "arguments": "{}"}
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": call_id, "type": "function", "function": function}],
    }


def output(call_id: str) -> dict:
    """Build the tool message answering a call."""
    return {"role": "tool", "tool_call_id": call_id, "content": "[...]"}


def test_replays_questions_and_answers_without_tool_messages() -> None:
    """Tool calls and tool results are stored but not replayed."""
    items = [user("q1"), call("a"), output("a"), answer("a1")]

    assert prompt_history(items) == [user("q1"), answer("a1")]


def test_keeps_only_the_last_turns() -> None:
    """A turn starts at a user message; older turns are dropped whole."""
    items = [user("q1"), answer("a1"), user("q2"), call("b"), output("b"), answer("a2"), user("q3"), answer("a3")]

    assert prompt_history(items, max_turns=2) == [user("q2"), answer("a2"), user("q3"), answer("a3")]


def test_drops_messages_before_the_first_user_message() -> None:
    """A load cut by its row limit can start mid-turn; the partial turn is skipped."""
    items = [output("a"), answer("a0"), user("q1"), answer("a1")]

    assert prompt_history(items) == [user("q1"), answer("a1")]


def test_empty_session() -> None:
    """A new session replays nothing."""
    assert prompt_history([]) == []
