# Databricks notebook source
# ruff: noqa: ERA001
# Databricks MAGIC comments are executable notebook cells.
"""Save the agent's conversation in Lakebase and load it on the next turn."""

# COMMAND ----------

# MAGIC %md
# MAGIC # Lecture 3.3: Session memory in Lakebase
# MAGIC
# MAGIC A serving endpoint or app keeps no state between requests: each question is a new call. The caller
# MAGIC passes a `session_id`; the agent loads that session's history from Lakebase before calling the model
# MAGIC and appends the new turn after.
# MAGIC
# MAGIC ```text
# MAGIC agent.chat(question, session_id)
# MAGIC    ├─ load: "<catalog>_memory".session_messages WHERE session_id → last 5 turns, questions and answers
# MAGIC    ├─ model ⟲ tools
# MAGIC    └─ save: the question, every tool call and result, the answer (one JSONB row per message, one transaction)
# MAGIC ```
# MAGIC
# MAGIC Everything is stored, so the conversation can be inspected later. Only questions and answers are
# MAGIC replayed: tool results are up to 8 KB of chunks per search, and the answers already carry the links.

# COMMAND ----------

import json
from uuid import uuid4

from databricks.sdk import WorkspaceClient
from databricks.sdk.runtime import dbutils
from loguru import logger
from pyspark.sql import SparkSession

from docs_curator import lakebase
from docs_curator.agent import DocsCuratorAgent
from docs_curator.config import load_config
from docs_curator.memory import prompt_history

spark = SparkSession.builder.getOrCreate()
w = WorkspaceClient()

dbutils.widgets.text("env", "dev")
cfg = load_config("../project_config.yml", env=dbutils.widgets.get("env"))

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. The session table
# MAGIC
# MAGIC dev, acc and prd share one Lakebase project, so each catalog gets its own Postgres schema
# MAGIC (`llmops_dev_memory` in dev). Rows are ordered by `id`: the messages of one turn are inserted in one
# MAGIC transaction and share `created_at`.

# COMMAND ----------

agent = DocsCuratorAgent.from_config(cfg, w)
agent.memory.ensure_schema()
logger.info(f"Memory table: {agent.memory.table_name}; tools: {list(agent.tools)}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. A conversation over three requests
# MAGIC
# MAGIC The second and third questions only make sense with the first: "that job", "its tasks". Each
# MAGIC request builds a new agent, as a new serving replica would, so the context can only come from
# MAGIC Lakebase.

# COMMAND ----------

session_id = f"lecture-3-3-{uuid4()}"
turns = [
    "How do I make the second task of a job depend on the first one in a bundle?",
    "And how do I run that job only when new files arrive in a volume?",
    "What happens to its tasks when I deploy it with mode: development?",
]

for question in turns:
    agent = DocsCuratorAgent.from_config(cfg, w)
    logger.info(f"Q: {question}")
    logger.info(f"{agent.chat(question, session_id=session_id).answer}\n")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. What was stored, and what the next turn replays

# COMMAND ----------

with lakebase.connect(cfg.lakebase_project_id, w) as conn:
    rows = conn.execute(
        f"""
        SELECT id, created_at, message_data->>'role' AS role,
               coalesce(message_data->'tool_calls'->0->'function'->>'name', '') AS tool_call,
               length(message_data::text) AS bytes
        FROM {agent.memory.table_name} WHERE session_id = %s ORDER BY id
        """,
        (session_id,),
    ).fetchall()

stored = spark.createDataFrame(
    [dict(zip(["id", "created_at", "role", "tool_call", "bytes"], row, strict=True)) for row in rows]
)
stored.show(truncate=False)

replayed = prompt_history(agent.memory.load_messages(session_id), agent.history_turns)
logger.info(
    f"Stored {len(rows)} messages ({sum(row[4] for row in rows)} bytes); the next turn replays {len(replayed)} "
    f"({sum(len(json.dumps(message)) for message in replayed)} bytes)"
)
