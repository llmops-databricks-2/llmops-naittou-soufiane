# Databricks notebook source
# ruff: noqa: ERA001
# Databricks MAGIC comments are executable notebook cells.
"""A Genie space over docs_metadata, used by the agent through its managed MCP server."""

# COMMAND ----------

# MAGIC %md
# MAGIC # Lecture 3.2: Genie through managed MCP
# MAGIC
# MAGIC `search_docs` knows what the pages say. Questions about the page catalog itself ("which bundles pages
# MAGIC changed this month?", "how many jobs pages are there?") are SQL over `docs_metadata` (lecture 1). A
# MAGIC Genie space writes and runs that SQL; Databricks serves it as a managed MCP server, so the agent gets
# MAGIC its tools without code of ours behind them.
# MAGIC
# MAGIC ```text
# MAGIC docs_metadata (Delta, column comments) → Genie space → /api/2.0/mcp/genie/{space_id}
# MAGIC    → query_space_<id>, poll_response_<id> (MCP tools) → agent, next to search_docs
# MAGIC ```

# COMMAND ----------

import json

import nest_asyncio
from databricks.sdk import WorkspaceClient
from databricks.sdk.runtime import dbutils
from databricks_mcp import DatabricksMCPClient
from loguru import logger

from docs_curator.agent import DocsCuratorAgent
from docs_curator.config import load_config
from docs_curator.genie import ensure_genie_space
from docs_curator.mcp import compact_genie_response, genie_server_url

# The MCP client calls asyncio.run, which needs nesting in the notebook's running event loop
nest_asyncio.apply()
w = WorkspaceClient()

dbutils.widgets.text("env", "dev")
cfg = load_config("../project_config.yml", env=dbutils.widgets.get("env"))

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. The Genie space
# MAGIC
# MAGIC Created once per catalog and found again by its title (`docs-curator <catalog>.<schema>`): the
# MAGIC `docs_metadata` table with its column comments, instructions (`url` with every page result, "recently
# MAGIC updated" means by `last_updated`, the table has no page content), sample questions and one example SQL.
# MAGIC The id goes into `genie_space_id` in `project_config.yml`, which the agent reads.

# COMMAND ----------

space_id = ensure_genie_space(w, cfg)
if cfg.genie_space_id != space_id:
    logger.warning(f"Set genie_space_id: {space_id} in project_config.yml (now {cfg.genie_space_id})")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Its MCP tools
# MAGIC
# MAGIC The server exposes two tools: `query_space_<id>` asks a question, `poll_response_<id>` fetches an
# MAGIC answer that wasn't ready yet. Their descriptions come from the server.

# COMMAND ----------

server_url = genie_server_url(w, space_id)
mcp_client = DatabricksMCPClient(server_url=server_url, workspace_client=w)
for tool in mcp_client.list_tools():
    logger.info(f"{tool.name}: {json.dumps(tool.inputSchema)}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Raw and compacted answers
# MAGIC
# MAGIC Genie answers with text, the SQL it ran and the full statement result, typed values and chunk
# MAGIC manifest included. `compact_genie_response` keeps the status, ids, text, SQL, columns and rows: the
# MAGIC same information for fewer tokens of every later model call in the turn.

# COMMAND ----------

query_tool = next(tool.name for tool in mcp_client.list_tools() if tool.name.startswith("query_space"))
raw = "".join(
    c.text for c in mcp_client.call_tool(query_tool, {"query": "How many pages are there per product area?"}).content
)
compact = compact_genie_response(raw)
logger.info(f"Raw: {len(raw)} characters, compact: {len(compact)} characters")
logger.info(json.dumps(json.loads(compact), indent=2))

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. The agent with both tools
# MAGIC
# MAGIC The system prompt tells the model which tool knows what: content questions go to `search_docs`,
# MAGIC catalog questions to Genie. The routing itself is the model's decision.

# COMMAND ----------

agent = DocsCuratorAgent.from_config(cfg, w)
agent.memory = None  # one-off questions here; memory is lecture 3.3
logger.info(f"Tools: {list(agent.tools)}")

for question in [
    "Which bundles pages were updated most recently?",
    "Which jobs pages talk about triggers, and how do I set a file arrival trigger?",
]:
    logger.info(f"Q: {question}")
    logger.info(f"{agent.chat(question).answer}\n")
