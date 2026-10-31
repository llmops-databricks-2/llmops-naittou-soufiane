# Databricks notebook source
# ruff: noqa: ERA001
# Databricks MAGIC comments are executable notebook cells.
"""Sync docs_chunks to Lakebase and search it with pgvector: semantic, filtered and hybrid."""

# COMMAND ----------

# MAGIC %md
# MAGIC # Lecture 2.5: Synced Tables and pgvector on Lakebase
# MAGIC
# MAGIC `docs_chunks` (lecture 2.2) holds the chunks and their `embedding`. A Lakebase Synced Table
# MAGIC copies it into Postgres, and we search it with pgvector.
# MAGIC
# MAGIC ```text
# MAGIC docs_chunks (Unity Catalog Delta: text + metadata + embedding)
# MAGIC          │  Synced Table (managed, read-only copy, TRIGGERED)
# MAGIC          ▼
# MAGIC Lakebase Postgres: "<schema>".docs_chunks_synced
# MAGIC          ├── HNSW cosine index on embedding        → semantic search
# MAGIC          ├── WHERE product_area / has_code          → filtered search
# MAGIC          └── GIN index on to_tsvector(text)         → hybrid search (with full-text ranking)
# MAGIC ```
# MAGIC
# MAGIC No embeddings are generated or written here: Databricks owns the copy, we own the indexes and
# MAGIC the queries.

# COMMAND ----------

import time

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound
from databricks.sdk.runtime import dbutils
from databricks.sdk.service.pipelines import UpdateInfoState
from databricks.sdk.service.postgres import (
    NewPipelineSpec,
    SyncedTable,
    SyncedTableState,
    SyncedTableSyncedTableSpec,
    SyncedTableSyncedTableSpecPgSpecificType,
    SyncedTableSyncedTableSpecSyncedTableSchedulingPolicy,
    SyncedTableSyncedTableSpecTypeOverride,
)
from loguru import logger
from pyspark.sql import SparkSession

from docs_curator.config import load_config
from docs_curator.vector_search import LakebasePgVectorManager

spark = SparkSession.builder.getOrCreate()
w = WorkspaceClient()

dbutils.widgets.text("env", "dev")
cfg = load_config("../project_config.yml", env=dbutils.widgets.get("env"))

source_table = f"{cfg.full_schema_name}.docs_chunks"
synced_table = f"{cfg.full_schema_name}.{LakebasePgVectorManager.synced_table_name}"
synced_table_name = f"synced_tables/{synced_table}"
postgres_branch = next(iter(w.postgres.list_branches(parent=f"projects/{cfg.lakebase_project_id}"))).name

if not cfg.usage_policy_id:
    raise ValueError("usage_policy_id is missing in project_config.yml")

logger.info(f"Source table: {source_table}")
logger.info(f"Synced table: {synced_table} on {postgres_branch}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Validate the Delta source
# MAGIC
# MAGIC Synced Tables need a stable, non-null primary key (`id` = `<doc_id>#<chunk_index>`), and
# MAGIC triggered synchronization needs Change Data Feed, which lecture 2.2 enables.

# COMMAND ----------

source_columns = {field.name for field in spark.table(source_table).schema.fields}
missing = {"id", "embedding"} - source_columns
if missing:
    raise ValueError(f"{source_table} has no {missing} column. Run lecture 2.2 first.")
spark.sql(f"ALTER TABLE {source_table} SET TBLPROPERTIES (delta.enableChangeDataFeed = true)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Create or sync the Synced Table
# MAGIC
# MAGIC - `type_overrides` syncs `embedding` as a pgvector `vector(1024)` column (GTE Large has 1,024
# MAGIC   dimensions). Without it, Delta `ARRAY` columns land in Postgres as `JSONB`.
# MAGIC - `new_pipeline_spec` sets the usage policy on the sync pipeline and where it keeps its state.
# MAGIC - `TRIGGERED` syncs when asked: the first run creates and fills the table, later runs start a
# MAGIC   sync so new, updated and deleted chunks reach Lakebase.
# MAGIC
# MAGIC Creating a Synced Table does not install Postgres extensions, so pgvector is enabled first.

# COMMAND ----------

pgvector = LakebasePgVectorManager(config=cfg, workspace=w)
pgvector.ensure_extension()


def synced_table_exists() -> bool:
    """Whether the Synced Table already exists."""
    try:
        w.postgres.get_synced_table(name=synced_table_name)
        return True
    except NotFound:
        return False


if not synced_table_exists():
    w.postgres.create_synced_table(
        synced_table=SyncedTable(
            spec=SyncedTableSyncedTableSpec(
                source_table_full_name=source_table,
                branch=postgres_branch,
                postgres_database="databricks_postgres",
                primary_key_columns=["id"],
                scheduling_policy=SyncedTableSyncedTableSpecSyncedTableSchedulingPolicy.TRIGGERED,
                create_database_objects_if_missing=True,
                new_pipeline_spec=NewPipelineSpec(
                    budget_policy_id=cfg.usage_policy_id,
                    storage_catalog=cfg.catalog,
                    storage_schema=cfg.schema,
                ),
                type_overrides=[
                    SyncedTableSyncedTableSpecTypeOverride(
                        column_name="embedding",
                        pg_type=SyncedTableSyncedTableSpecPgSpecificType.PG_SPECIFIC_TYPE_VECTOR,
                        size=1024,
                    )
                ],
            )
        ),
        synced_table_id=synced_table,
    ).wait()
    logger.info(f"Created Synced Table: {synced_table}")
    update_id = None
else:
    # A Synced Table runs on a Lakeflow pipeline; starting a pipeline update runs a sync, like
    # "Sync now" in the UI.
    pipeline_id = w.postgres.get_synced_table(name=synced_table_name).status.pipeline_id
    update_id = w.pipelines.start_update(pipeline_id=pipeline_id).update_id
    logger.info(f"Sync started: pipeline {pipeline_id}, update {update_id}")

# COMMAND ----------

online_states = {SyncedTableState.SYNCED_TABLE_ONLINE, SyncedTableState.SYNCED_TABLE_ONLINE_NO_PENDING_UPDATE}
failed_states = {SyncedTableState.SYNCED_TABLE_OFFLINE_FAILED, SyncedTableState.SYNCED_TABLE_ONLINE_PIPELINE_FAILED}

while True:
    status = w.postgres.get_synced_table(name=synced_table_name).status
    if update_id:
        # Wait for the sync we started, not just for the table to be online
        update = w.pipelines.get_update(pipeline_id=status.pipeline_id, update_id=update_id).update
        if update.state == UpdateInfoState.COMPLETED:
            break
        if update.state in {UpdateInfoState.FAILED, UpdateInfoState.CANCELED}:
            raise RuntimeError(f"Sync {update_id} ended in state {update.state}")
    elif status.detailed_state in online_states:
        break
    if status.detailed_state in failed_states:
        raise RuntimeError(f"Synced Table failed: {status.detailed_state} | {status.message}")
    logger.info(f"Waiting for sync: {status.detailed_state}")
    time.sleep(30)

logger.info(f"Synced Table is up to date: {status.detailed_state}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Index the Synced Table and check the copy

# COMMAND ----------

pgvector.ensure_indexes()

with pgvector.connection() as conn:
    synced_rows = conn.execute(f"SELECT count(*) FROM {pgvector.table_name}").fetchone()[0]
    embedding_type = conn.execute(f"SELECT pg_typeof(embedding)::text FROM {pgvector.table_name} LIMIT 1").fetchone()[0]
    indexes = conn.execute(
        "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = %s AND tablename = %s ORDER BY indexname",
        (cfg.schema, pgvector.synced_table_name),
    ).fetchall()

delta_rows = spark.table(source_table).count()
logger.info(f"Rows: Delta {delta_rows}, Lakebase {synced_rows}; embedding type: {embedding_type}")
for index_name, definition in indexes:
    logger.info(f"{index_name}: {definition}")
if synced_rows != delta_rows:
    raise RuntimeError("The Synced Table does not match its Delta source")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. Semantic, filtered and hybrid search
# MAGIC
# MAGIC Each question runs three ways, five results each:
# MAGIC
# MAGIC | Mode | What it does |
# MAGIC |---|---|
# MAGIC | Semantic | cosine similarity between the question and chunk embeddings |
# MAGIC | Filtered | the same, restricted with `WHERE` on `product_area` and/or `has_code` |
# MAGIC | Hybrid | 0.7 × cosine similarity + 0.3 × full-text rank of the question's terms (OR) |
# MAGIC
# MAGIC Each question names the term an answer needs (`expect`, such as the `depends_on` key); a result
# MAGIC counts as a hit when its chunk contains it, ignoring case.

# COMMAND ----------

questions = [
    {
        "question": "How do I make the second task of a job depend on the first one in a bundle?",
        "expect": "depends_on",
        "filters": {"product_area": "bundles", "has_code": True},
    },
    {
        "question": "How do I run a job when new files arrive in storage?",
        "expect": "file arrival",
        "filters": {"product_area": "jobs"},
    },
    {
        "question": "What does mode: development change, and what is the [dev ${workspace.current_user.short_name}] prefix?",
        "expect": "mode: development",
        "filters": {"product_area": "bundles"},
    },
]


def show(mode: str, rows: list[dict], expect: str) -> tuple[list[str], int]:
    """Log the ranked results of one search; return their chunk ids and the number of hits."""
    logger.info(f"  {mode}")
    hits = [expect.lower() in row["text"].lower() for row in rows]
    for position, row in enumerate(rows, 1):
        extra = f" (vector {row['vector_score']:.3f}, text {row['text_score']:.3f})" if "text_score" in row else ""
        logger.info(
            f"    {position}. {row['score']:.3f}{extra} | hit={hits[position - 1]} | code={row['has_code']} | "
            f"{row['title'][:45]} > {(row['section'] or '-')[:35]}"
        )
    return [row["id"] for row in rows], sum(hits)


comparison = []
for item in questions:
    question, expect = item["question"], item["expect"]
    logger.info(f"Q: {question}")
    semantic, semantic_hits = show("semantic", pgvector.search(question), expect)
    filtered, filtered_hits = show(f"filtered {item['filters']}", pgvector.search(question, **item["filters"]), expect)
    hybrid, hybrid_hits = show("hybrid", pgvector.hybrid_search(question), expect)
    comparison.append(
        {
            "expect": expect,
            "semantic_hits": semantic_hits,
            "filtered_hits": filtered_hits,
            "hybrid_hits": hybrid_hits,
            "semantic_top1": semantic[0],
            "hybrid_top1": hybrid[0],
            "filtered_overlap": len(set(semantic) & set(filtered)),
            "hybrid_overlap": len(set(semantic) & set(hybrid)),
        }
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5. What changed between the modes
# MAGIC
# MAGIC Hits count the top-five results containing the expected term; overlap counts the semantic
# MAGIC results the other mode also returned.

# COMMAND ----------

spark.createDataFrame(comparison).select(*comparison[0].keys()).show(truncate=False)
