# Databricks notebook source
# ruff: noqa: ERA001
# Databricks MAGIC comments are executable notebook cells.
"""The search_docs custom tool, and the same questions answered by a fixed RAG workflow and by the agent."""

# COMMAND ----------

# MAGIC %md
# MAGIC # Lecture 3.1: Custom function tools
# MAGIC
# MAGIC Lecture 2 built hybrid search over `docs_chunks_synced` (Lakebase pgvector). Here it becomes a tool,
# MAGIC `search_docs`, with a narrow contract: the model supplies a query and optional filters, and gets back
# MAGIC sections with their page title, URL and text. It never sees SQL or credentials.
# MAGIC
# MAGIC ```text
# MAGIC Workflow (code decides):  question → hybrid_search(question) → prompt with chunks → model → answer
# MAGIC Agent (model decides):    question → model → search_docs(query, product_area?, has_code?) ⟲ → answer
# MAGIC ```
# MAGIC
# MAGIC The same four questions go through both, to see what handing the decision to the model changes.

# COMMAND ----------

import json

from databricks.sdk import WorkspaceClient
from databricks.sdk.runtime import dbutils
from loguru import logger
from pyspark.sql import SparkSession

from docs_curator.agent import DocsCuratorAgent, gateway_client, message_text
from docs_curator.config import load_config
from docs_curator.tools import SEARCH_DOCS_SPEC, search_docs_tool
from docs_curator.vector_search import LakebasePgVectorManager

spark = SparkSession.builder.getOrCreate()
w = WorkspaceClient()

dbutils.widgets.text("env", "dev")
cfg = load_config("../project_config.yml", env=dbutils.widgets.get("env"))

pgvector = LakebasePgVectorManager(config=cfg, workspace=w)
search_docs = search_docs_tool(pgvector)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. The tool contract
# MAGIC
# MAGIC The description tells the model what the tool knows; the parameter descriptions carry the lessons of
# MAGIC lecture 2: name the configuration keys (`depends_on`) rather than paraphrase them, and filter on
# MAGIC `has_code` to reach YAML examples.

# COMMAND ----------

logger.info(json.dumps(SEARCH_DOCS_SPEC, indent=2))

result = json.loads(search_docs.exec_fn(query="depends_on task_key", product_area="bundles", has_code=True))
for row in result[:3]:
    logger.info(f"{row['score']} | {row['title']} > {row['section']} | {row['url']}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Workflow: retrieval always runs
# MAGIC
# MAGIC The code retrieves five chunks for the user's words as they are, builds the prompt and calls the
# MAGIC model once. Predictable: one search, one model call, every time.

# COMMAND ----------


def rag_workflow(question: str) -> tuple[str, int]:
    """Answer with one hybrid search on the question itself; return the answer and the prompt size."""
    chunks = pgvector.hybrid_search(question, num_results=5)
    context = "\n\n---\n\n".join(f"[{row['title']}]({row['url']})\n{row['text']}" for row in chunks)
    messages = [
        {"role": "system", "content": "Answer from the documentation excerpts only, citing their links."},
        {"role": "user", "content": f"Documentation excerpts:\n\n{context}\n\nQuestion: {question}"},
    ]
    response = gateway_client(w).chat.completions.create(
        model=cfg.llm_endpoint, messages=messages, reasoning_effort="low", max_tokens=2048
    )
    return message_text(response.choices[0].message.content), len(messages[1]["content"])


questions = [
    "Hi! What can you help me with?",
    "How do I make the second task of a job depend on the first one in a bundle? Show the YAML.",
    "How do I run a job when new files arrive in a volume?",
    "What does mode: development change in a bundle deployment?",
]

workflow_answers = {}
for question in questions:
    answer, prompt_chars = rag_workflow(question)
    workflow_answers[question] = answer
    logger.info(f"Q: {question}\n(1 search, {prompt_chars} prompt characters)\n{answer}\n")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Agent: the model decides
# MAGIC
# MAGIC `DocsCuratorAgent` is a chat-completions tool loop (each tool call answered by a `tool` message),
# MAGIC offering only `search_docs` here. The model decides whether to search, rewrites the question into
# MAGIC search terms, picks filters and searches again if the results fall short. Code keeps the frame: argument checks, tool errors returned to the model, six model calls at
# MAGIC most (the last without tools), a retry of empty answers, and the sources of the answer.

# COMMAND ----------

agent = DocsCuratorAgent(w, cfg.llm_endpoint, cfg.system_prompt, tools=[search_docs])

comparison = []
for question in questions:
    logger.info(f"Q: {question}")
    turn = agent.chat(question)
    answer = turn.answer
    logger.info(f"{answer}\n")
    comparison.append(
        {
            "question": question[:60],
            "workflow_searches": 1,
            "agent_searches": len(turn.tool_calls),
            "agent_filters": "; ".join(
                ", ".join(
                    f"{key}={value}"
                    for key, value in json.loads(call["function"]["arguments"]).items()
                    if key != "query"
                )
                for call in turn.tool_calls
            ),
            "workflow_links": workflow_answers[question].count("https://"),
            "agent_links": answer.count("https://"),
        }
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. What changed
# MAGIC
# MAGIC The workflow searches even for a greeting and always with the user's words; the agent skips the
# MAGIC search when none is needed and spends more calls when the first results fall short.

# COMMAND ----------

spark.createDataFrame(comparison).select(*comparison[0].keys()).show(truncate=False)
