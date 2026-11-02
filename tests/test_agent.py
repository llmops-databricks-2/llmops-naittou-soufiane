"""Tests for the agent's code-controlled orchestration, with a scripted model instead of the AI Gateway."""

import json

import pytest

from docs_curator.agent import FALLBACK_ANSWER, DocsCuratorAgent, message_text
from docs_curator.mcp import ToolInfo


def text(answer: str) -> dict:
    """Build an assistant answer."""
    return {"role": "assistant", "content": answer}


def calls(*tool_calls: tuple[str, str, str]) -> dict:
    """Build an assistant message calling tools, from (id, name, arguments) triples."""
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}
            for call_id, name, arguments in tool_calls
        ],
    }


def echo(call_id: str, value: str = "") -> tuple[str, str, str]:
    """Build a call of the echo tool."""
    return call_id, "echo", json.dumps({"value": value})


class ScriptedAgent(DocsCuratorAgent):
    """Replays model messages in order and records what each model call saw."""

    def __init__(self, script: list[dict | None], **kwargs: object) -> None:
        echo_tool = ToolInfo(name="echo", spec={"type": "function"}, exec_fn=lambda value="": f"echo:{value}")

        def fail() -> str:
            raise RuntimeError("Lakebase is down")

        broken = ToolInfo(name="broken", spec={"type": "function"}, exec_fn=fail)
        super().__init__(None, "chat", "system prompt", [echo_tool, broken], **kwargs)
        self.script = script
        self.seen: list[tuple[list[dict], bool]] = []

    def call_llm(self, messages: list[dict], allow_tools: bool = True) -> dict | None:
        """Return the next scripted message."""
        self.seen.append(([dict(message) for message in messages], allow_tools))
        return self.script.pop(0)


class FakeMemory:
    """In-memory stand-in for LakebaseMemory."""

    def __init__(self, stored: list[dict] | None = None) -> None:
        self.stored = list(stored or [])

    def load_messages(self, session_id: str) -> list[dict]:
        """Return everything stored."""
        return list(self.stored)

    def save_messages(self, session_id: str, messages: list[dict]) -> None:
        """Append the turn."""
        self.stored += messages


def roles(messages: list[dict]) -> list[str]:
    """List the roles, marking tool-calling assistant messages, for comparing sequences."""
    return ["assistant+tools" if message.get("tool_calls") else message["role"] for message in messages]


def test_tool_call_then_answer() -> None:
    """The tool result follows its call, and the model sees it before answering."""
    agent = ScriptedAgent([calls(echo("1", "hi")), text("done")])
    new = agent.run_tool_loop([{"role": "user", "content": "q"}])

    assert roles(new) == ["assistant+tools", "tool", "assistant"]
    assert new[1] == {"role": "tool", "tool_call_id": "1", "content": "echo:hi"}
    assert agent.seen[1][0][-1]["content"] == "echo:hi"


def test_parallel_calls_each_get_a_result() -> None:
    """Several calls in one message are all run, each answered by a tool message with its id."""
    agent = ScriptedAgent([calls(echo("1", "a"), echo("2", "b")), text("done")])
    new = agent.run_tool_loop([])

    assert roles(new) == ["assistant+tools", "tool", "tool", "assistant"]
    assert [message["tool_call_id"] for message in new[1:3]] == ["1", "2"]


def test_tool_errors_go_back_to_the_model() -> None:
    """A failing tool, an unknown tool or bad JSON become an error result instead of failing the turn."""
    agent = ScriptedAgent(
        [calls(("1", "broken", "{}"), ("2", "missing", "{}"), ("3", "echo", "{not json")), text("sorry")]
    )
    new = agent.run_tool_loop([])

    errors = [json.loads(message["content"])["error"] for message in new if message["role"] == "tool"]
    assert errors[0] == "RuntimeError: Lakebase is down"
    assert errors[1].startswith("KeyError")
    assert errors[2].startswith("JSONDecodeError")
    assert new[-1]["content"] == "sorry"


def test_empty_answer_is_retried() -> None:
    """An empty answer (None) doesn't end the turn."""
    agent = ScriptedAgent([None, text("done")])

    assert roles(agent.run_tool_loop([])) == ["assistant"]
    assert len(agent.seen) == 2


def test_last_iteration_forbids_tools_and_falls_back() -> None:
    """The last model call may not call tools; without an answer the turn ends with the fallback."""
    agent = ScriptedAgent([calls(echo("1")), calls(echo("2")), None], max_iterations=3)
    new = agent.run_tool_loop([])

    assert [allow_tools for _, allow_tools in agent.seen] == [True, True, False]
    assert new[-1] == {"role": "assistant", "content": FALLBACK_ANSWER}


def test_answer_gets_the_searched_pages() -> None:
    """An answer without links lists the pages the tools returned."""
    result = json.dumps([{"title": "Bundle resources", "url": "https://docs/resources"}])
    agent = ScriptedAgent([calls(echo("1", "x")), text("Use depends_on.")])
    agent.tools["echo"] = ToolInfo(name="echo", spec={"type": "function"}, exec_fn=lambda value="": result)

    assert agent.run_tool_loop([])[-1]["content"].endswith("- [Bundle resources](https://docs/resources)")


def test_chat_loads_history_and_saves_the_turn() -> None:
    """The prompt is system + replayed history + question; the whole turn is appended to the session."""
    previous = [
        {"role": "user", "content": "q1"},
        calls(echo("0")),
        {"role": "tool", "tool_call_id": "0", "content": "chunks"},
        text("a1"),
    ]
    memory = FakeMemory(previous)
    agent = ScriptedAgent([calls(echo("1", "x")), text("a2")], memory=memory)
    turn = agent.chat("q2", session_id="s")

    prompt = agent.seen[0][0]
    assert roles(prompt) == ["system", "user", "assistant", "user"]
    assert prompt[-1]["content"] == "q2"
    assert roles(memory.stored[len(previous) :]) == ["user", "assistant+tools", "tool", "assistant"]
    assert turn.answer == "a2"
    assert [call["id"] for call in turn.tool_calls] == ["1"]


def test_chat_without_session_id_keeps_no_memory() -> None:
    """Without a session id nothing is loaded or saved."""
    memory = FakeMemory()
    agent = ScriptedAgent([text("hello")], memory=memory)

    assert agent.chat("hi").answer == "hello"
    assert memory.stored == []


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("plain", "plain"),
        (None, ""),
        ([{"type": "reasoning", "summary": [{"text": "thinking"}]}, {"type": "text", "text": "answer"}], "answer"),
    ],
)
def test_message_text(content: str | list | None, expected: str) -> None:
    """GPT-OSS content parts are reduced to the answer text."""
    assert message_text(content) == expected
