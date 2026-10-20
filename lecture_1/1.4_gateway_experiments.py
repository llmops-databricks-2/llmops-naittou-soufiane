# Databricks notebook source
# ruff: noqa: ERA001
# Databricks MAGIC comments are executable notebook cells.
"""Try the course chat model service on Docs Curator questions."""

# COMMAND ----------

# MAGIC %md
# MAGIC # Lecture 1.4: Model Service Experiments
# MAGIC
# MAGIC Calls the course's chat model service (`llm_endpoint` in `project_config.yml`) through the AI
# MAGIC Gateway, `/ai-gateway/mlflow/v1`, with prompts the Docs Curator must handle. The workspace
# MAGIC client authenticates you; its token is passed to the client and never printed. For each prompt
# MAGIC the notebook records the answer, the token usage and the latency.

# COMMAND ----------

import time

from databricks.sdk import WorkspaceClient
from databricks.sdk.runtime import dbutils
from loguru import logger
from openai import OpenAI
from pyspark.sql import SparkSession

from docs_curator.config import load_config

dbutils.widgets.text("env", "dev")
cfg = load_config("../project_config.yml", env=dbutils.widgets.get("env"))
spark = SparkSession.builder.getOrCreate()

workspace = WorkspaceClient()
client = OpenAI(
    base_url=f"{workspace.config.host}/ai-gateway/mlflow/v1",
    api_key=workspace.config.authenticate()["Authorization"].removeprefix("Bearer "),
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Prompts
# MAGIC
# MAGIC Two plain questions, one grounded in a row of `docs_metadata` with a source link to cite, and
# MAGIC one about a recent change, to see whether the model admits what it does not know.

# COMMAND ----------

page = (
    spark.table(f"{cfg.full_schema_name}.docs_metadata")
    .filter("doc_id = 'dev-tools/bundles/deployment-modes'")
    .select("title", "description", "url")
    .first()
)

prompts = {
    "multi_task_job": "How do I structure a Databricks Asset Bundle configuration file for a multi-task job "
    "where the second task depends on the first? Show the YAML.",
    "zorder": "Show me an example of optimizing a Delta table with Z-ordering, and say when liquid clustering "
    "is the better choice.",
    "grounded": f"Using only this documentation page, explain when to use development mode.\n"
    f"Title: {page.title}\nSummary: {page.description}\nURL: {page.url}\n"
    "Cite the URL in your answer.",
    "recent_change": "Databricks Asset Bundles were renamed recently. What is their new name, and since when?",
}

# COMMAND ----------


def split_content(content: str | list[dict]) -> tuple[str, str]:
    """Split a message content into its answer text and its reasoning summary.

    GPT-OSS returns a list of parts, a "reasoning" part with a summary and "text" parts, instead of
    a plain string.
    """
    if isinstance(content, str):
        return content, ""
    text = "".join(part["text"] for part in content if part.get("type") == "text")
    reasoning = " ".join(
        summary["text"] for part in content if part.get("type") == "reasoning" for summary in part.get("summary", [])
    )
    return text, reasoning


results = []
for name, prompt in prompts.items():
    start = time.perf_counter()
    response = client.chat.completions.create(
        model=cfg.llm_endpoint,
        messages=[
            {"role": "system", "content": cfg.system_prompt},
            {"role": "user", "content": prompt},
        ],
        reasoning_effort="low",
        max_tokens=1024,
    )
    latency = time.perf_counter() - start
    answer, reasoning = split_content(response.choices[0].message.content)
    results.append(
        {
            "prompt": name,
            "model": response.model,
            "finish_reason": response.choices[0].finish_reason,
            "prompt_tokens": response.usage.prompt_tokens,
            "completion_tokens": response.usage.completion_tokens,
            "total_tokens": response.usage.total_tokens,
            "latency_s": round(latency, 2),
            "reasoning": reasoning,
            "answer": answer,
        }
    )
    logger.info(f"--- {name} ({response.usage.total_tokens} tokens, {latency:.1f}s)\nReasoning: {reasoning}\n{answer}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Token usage and latency

# COMMAND ----------

spark.createDataFrame(results).drop("answer").show(truncate=False)
