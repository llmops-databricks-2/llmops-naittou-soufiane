"""The Docs Curator agent: a chat-completions tool-calling loop, inside a code-controlled frame.

question (+ session_id)
   ↓ code: load the session's last turns from Lakebase (memory.prompt_history)
system prompt + history + question (chat-completions messages)
   ↓ model: answer, or call tools (search_docs, Genie MCP) ⟲ code runs them, up to max_iterations
answer
   ↓ code: add its sources (answer_checks), save the turn's messages to Lakebase
"""

import json
from dataclasses import dataclass
from typing import Any

import backoff
import openai
from databricks.sdk import WorkspaceClient
from loguru import logger

from docs_curator.answer_checks import add_sources
from docs_curator.config import ProjectConfig
from docs_curator.mcp import ToolInfo, create_mcp_tools, genie_server_url
from docs_curator.memory import LakebaseMemory, prompt_history
from docs_curator.tools import search_docs_tool
from docs_curator.vector_search import LakebasePgVectorManager

FALLBACK_ANSWER = "I couldn't put an answer together from the documentation. Could you rephrase the question?"


def message_text(content: str | list[dict] | None) -> str:
    """Return the answer text of a chat message.

    GPT-OSS returns a list of parts, a reasoning summary and text, instead of a string (lecture 1).
    """
    if content is None or isinstance(content, str):
        return content or ""
    return "".join(part.get("text", "") for part in content if part.get("type") == "text")


def gateway_client(workspace: WorkspaceClient) -> openai.OpenAI:
    """AI Gateway client with a current token.

    Create one per call: the SDK refreshes OAuth tokens, a token fixed at start-up would expire.
    """
    token = workspace.config.authenticate()["Authorization"].removeprefix("Bearer ")
    return openai.OpenAI(base_url=f"{workspace.config.host.rstrip('/')}/ai-gateway/mlflow/v1", api_key=token)


@dataclass
class AgentTurn:
    """One answered question: the final answer and the turn's new messages (tool calls, tool results, answer)."""

    answer: str
    messages: list[dict[str, Any]]

    @property
    def tool_calls(self) -> list[dict[str, Any]]:
        """The tool calls of the turn, in order, as {"id", "type", "function": {"name", "arguments"}}."""
        return [call for message in self.messages for call in message.get("tool_calls") or []]


class DocsCuratorAgent:
    """Tool-calling agent over the Databricks docs.

    Code controls the frame (history, tools on offer, argument checks, errors, iteration limit, sources,
    saving); the model controls the decisions (whether to search, which tool, query terms and filters,
    when to stop).
    """

    def __init__(
        self,
        workspace: WorkspaceClient,
        llm_endpoint: str,
        system_prompt: str,
        tools: list[ToolInfo],
        memory: LakebaseMemory | None = None,
        max_iterations: int = 6,
        history_turns: int = 5,
        max_tokens: int = 2048,
    ) -> None:
        """Create the agent from its parts; use from_config to build them from project_config.yml.

        Args:
            workspace: Workspace client authenticating the AI Gateway calls
            llm_endpoint: AI Gateway chat model service
            system_prompt: Instructions of the agent
            tools: Tools offered to the model
            memory: Session store; without it every request is a new conversation
            max_iterations: Model calls per turn; the last one may not call tools
            history_turns: Previous turns replayed in the prompt
            max_tokens: Maximum tokens of one model response

        """
        self.workspace = workspace
        self.llm_endpoint = llm_endpoint
        self.system_prompt = system_prompt
        self.tools = {tool.name: tool for tool in tools}
        self.memory = memory
        self.max_iterations = max_iterations
        self.history_turns = history_turns
        self.max_tokens = max_tokens

    @classmethod
    def from_config(cls, config: ProjectConfig, workspace: WorkspaceClient | None = None) -> "DocsCuratorAgent":
        """Build the agent with search_docs, the Genie space tools when configured, and Lakebase memory."""
        workspace = workspace or WorkspaceClient()
        tools = [search_docs_tool(LakebasePgVectorManager(config, workspace))]
        if config.genie_space_id:
            tools += create_mcp_tools(workspace, [genie_server_url(workspace, config.genie_space_id)])
        memory = LakebaseMemory(config.lakebase_project_id, config.memory_schema, workspace)
        return cls(workspace, config.llm_endpoint, config.system_prompt, tools, memory)

    @backoff.on_exception(backoff.expo, openai.RateLimitError, max_tries=5)
    def call_llm(self, messages: list[dict[str, Any]], allow_tools: bool = True) -> dict[str, Any] | None:
        """Call the model and return its assistant message: tool calls, or the answer text.

        Returns None when the model stops with an empty answer: GPT-OSS does so on about one call in
        three after a few turns of history, a reasoning summary and no text or tool call.
        """
        response = gateway_client(self.workspace).chat.completions.create(
            model=self.llm_endpoint,
            messages=messages,
            tools=[tool.spec for tool in self.tools.values()] or openai.NOT_GIVEN,
            tool_choice=("auto" if allow_tools else "none") if self.tools else openai.NOT_GIVEN,
            reasoning_effort="low",
            max_tokens=self.max_tokens,
        )
        message = response.choices[0].message
        if message.tool_calls and allow_tools:
            # Only the fields the chat API accepts back in the history
            return {
                "role": "assistant",
                "content": message_text(message.content),
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.function.name, "arguments": call.function.arguments},
                    }
                    for call in message.tool_calls
                ],
            }
        text = message_text(message.content)
        if not text.strip():
            logger.warning(f"Empty answer from {self.llm_endpoint} (finish reason {response.choices[0].finish_reason})")
            return None
        return {"role": "assistant", "content": text}

    def execute_tool(self, name: str, arguments: str) -> str:
        """Run a tool call; a failure is returned to the model as an error it can react to."""
        try:
            if name not in self.tools:
                raise KeyError(f"unknown tool {name}; available: {sorted(self.tools)}")
            return str(self.tools[name].exec_fn(**json.loads(arguments or "{}")))
        except Exception as error:  # noqa: BLE001 - every tool error goes back to the model
            logger.warning(f"Tool {name} failed: {error!r}")
            return json.dumps({"error": f"{type(error).__name__}: {error}"})

    def run_tool_loop(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Alternate model calls and tool runs until the model answers; return the turn's new messages.

        An assistant message with tool calls is followed by one tool message per call. An empty answer is
        retried on the next iteration. The last iteration forbids tools, and if it still gives no answer
        the turn ends with FALLBACK_ANSWER. The final answer gets its sources (answer_checks.add_sources).
        """
        new_messages: list[dict[str, Any]] = []
        for iteration in range(self.max_iterations):
            message = self.call_llm(messages, allow_tools=iteration < self.max_iterations - 1)
            if message is None:
                continue
            messages.append(message)
            new_messages.append(message)
            if not message.get("tool_calls"):
                outputs = [m["content"] for m in new_messages if m["role"] == "tool"]
                message["content"] = add_sources(message["content"], outputs)
                return new_messages
            for call in message["tool_calls"]:
                logger.info(f"Calling tool: {call['function']['name']}({call['function']['arguments']})")
                result = self.execute_tool(call["function"]["name"], call["function"]["arguments"])
                tool_message = {"role": "tool", "tool_call_id": call["id"], "content": result}
                messages.append(tool_message)
                new_messages.append(tool_message)
        new_messages.append({"role": "assistant", "content": FALLBACK_ANSWER})
        return new_messages

    def load_history(self, session_id: str) -> list[dict[str, Any]]:
        """Previous turns of the session replayed in the prompt; none if memory is unavailable."""
        try:
            return prompt_history(self.memory.load_messages(session_id), self.history_turns)
        except Exception as error:  # noqa: BLE001 - answer without history rather than fail
            logger.warning(f"Could not load session {session_id}: {error!r}")
            return []

    def save_turn(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        """Append the turn to the session; the answer is still returned if saving fails."""
        try:
            self.memory.save_messages(session_id, messages)
        except Exception as error:  # noqa: BLE001
            logger.warning(f"Could not save session {session_id}: {error!r}")

    def chat(self, question: str, session_id: str | None = None) -> AgentTurn:
        """Answer one question; with a session id, the conversation continues from Lakebase memory."""
        use_memory = bool(session_id and self.memory)
        history = self.load_history(session_id) if use_memory else []
        user_message = {"role": "user", "content": question}
        messages = [{"role": "system", "content": self.system_prompt}, *history, user_message]

        new_messages = self.run_tool_loop(messages)
        if use_memory:
            self.save_turn(session_id, [user_message, *new_messages])
        return AgentTurn(answer=new_messages[-1]["content"], messages=new_messages)
