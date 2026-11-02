"""Conversation history of the agent in Lakebase Postgres.

agent turn (chat-completions messages: question, assistant tool calls, tool results, answer)
   ↓ (save_messages: one row per message, in order)
"<catalog>_memory".session_messages (JSONB, Lakebase)
   ↓ (load_messages → prompt_history: last turns, user questions and answers only)
next turn's prompt
"""

import json
from typing import Any

from databricks.sdk import WorkspaceClient
from loguru import logger

from docs_curator import lakebase

# Rows read per load. A turn is usually 2-8 messages, so this covers far more turns than are replayed.
MAX_LOADED_MESSAGES = 200


def is_answer(message: dict[str, Any]) -> bool:
    """Whether a message is a question or an answer, rather than a tool call or a tool result."""
    return message.get("role") == "user" or (message.get("role") == "assistant" and not message.get("tool_calls"))


def prompt_history(messages: list[dict[str, Any]], max_turns: int = 5) -> list[dict[str, Any]]:
    """Select the stored messages replayed in the next prompt.

    Keeps the last `max_turns` turns, a turn starting at a user message, and within them only questions
    and answers. Tool calls and results are stored, so the conversation can be inspected later, but not
    replayed: up to 8 KB of chunks per search would crowd the prompt, the answers already carry the page
    links, and a follow-up that needs the content searches again.
    """
    starts = [index for index, message in enumerate(messages) if message.get("role") == "user"]
    if not starts:
        return []
    recent = messages[starts[-max_turns] if len(starts) >= max_turns else starts[0] :]
    return [message for message in recent if is_answer(message)]


class LakebaseMemory:
    """Append-only store of a session's chat messages in Lakebase.

    The table lives in its own Postgres schema per catalog (dev, acc and prd share the Lakebase project),
    next to the schema holding the docs_chunks Synced Table.
    """

    table = "session_messages"

    def __init__(self, project_id: str, schema: str, workspace: WorkspaceClient | None = None) -> None:
        self.project_id = project_id
        self.schema = schema
        self.workspace = workspace
        self.table_name = f'"{schema}"."{self.table}"'

    def ensure_schema(self) -> None:
        """Create the schema, table and index if they do not exist."""
        with lakebase.connect(self.project_id, self.workspace) as conn:
            conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{self.schema}"')
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self.table_name} (
                    id BIGSERIAL PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    message_data JSONB NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            conn.execute(f"CREATE INDEX IF NOT EXISTS {self.table}_session_idx ON {self.table_name} (session_id, id)")
        logger.info(f"Session memory ready: {self.table_name}")

    def load_messages(self, session_id: str, limit: int = MAX_LOADED_MESSAGES) -> list[dict[str, Any]]:
        """Load the last `limit` messages of a session, oldest first.

        Ordered by id: every message of a turn is inserted in one transaction, so they share created_at.
        """
        with lakebase.connect(self.project_id, self.workspace) as conn:
            rows = conn.execute(
                f"SELECT message_data FROM {self.table_name} WHERE session_id = %s ORDER BY id DESC LIMIT %s",
                (session_id, limit),
            ).fetchall()
        return [row[0] for row in reversed(rows)]

    def save_messages(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        """Append the messages of one turn to a session, in one transaction."""
        if not messages:
            return
        with lakebase.connect(self.project_id, self.workspace) as conn, conn.cursor() as cursor:
            cursor.executemany(
                f"INSERT INTO {self.table_name} (session_id, message_data) VALUES (%s, %s)",
                [(session_id, json.dumps(message)) for message in messages],
            )
