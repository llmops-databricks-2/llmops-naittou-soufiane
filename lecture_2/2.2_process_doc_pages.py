# Databricks notebook source
# ruff: noqa: ERA001
# Databricks MAGIC comments are executable notebook cells.
"""Download, parse, chunk and embed the documentation pages listed in docs_metadata."""

# COMMAND ----------

# MAGIC %md
# MAGIC # Lecture 2.2: Process Doc Pages
# MAGIC
# MAGIC The "Process doc pages" step of the Docs Curator. For every page of `docs_metadata` that was
# MAGIC never processed, or was updated on the docs site since, it:
# MAGIC
# MAGIC 1. saves the page HTML to the `docs_files` volume,
# MAGIC 2. parses the article into elements (`parsed_docs`), shaped like `ai_parse_document` output,
# MAGIC 3. chunks each page by section, embeds the chunks with `ai_query` (`docs_chunks`),
# MAGIC 4. records `processed` and `volume_path` in `docs_metadata`.
# MAGIC
# MAGIC ### Why not `ai_parse_document`?
# MAGIC
# MAGIC `ai_parse_document` reads PDF, image and Office files, not HTML. The page markup already
# MAGIC carries the structure, so `docs_curator.doc_pages` reads it directly:
# MAGIC
# MAGIC | Element | From the HTML | Kept as |
# MAGIC |---|---|---|
# MAGIC | `section_header` | `h1`-`h6` | text, with its level |
# MAGIC | `text` | paragraphs, list items | whitespace-normalized text |
# MAGIC | `code` | `pre` blocks, one highlighted line per source line | exact text, with its language |
# MAGIC | `table` | `table` | a Markdown table |
# MAGIC | `image` | `img` | alt text and absolute URL |
# MAGIC
# MAGIC Code blocks are kept exactly, so YAML snippets stay valid. A page that yields no heading fails
# MAGIC the run: it means the docs site markup changed and the parser needs updating.

# COMMAND ----------

import json
from collections import Counter

from databricks.sdk.runtime import dbutils
from loguru import logger
from pyspark.sql import SparkSession

from docs_curator.config import load_config
from docs_curator.docs_processor import DocsProcessor

spark = SparkSession.builder.getOrCreate()

# The environment comes from the job's base parameters; "dev" when run by hand.
dbutils.widgets.text("env", "dev")
env = dbutils.widgets.get("env")

cfg = load_config("../project_config.yml", env=env)
processor = DocsProcessor(spark=spark, config=cfg)
logger.info(f"Schema {cfg.full_schema_name}, volume {cfg.volume}, embeddings {cfg.embedding_endpoint}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Process the pages

# COMMAND ----------

processor.process_and_save()

# COMMAND ----------

# MAGIC %md
# MAGIC ## Inspect the processed documents

# COMMAND ----------

parsed_df = spark.table(processor.parsed_table)
logger.info(f"Parsed pages: {parsed_df.count()}")
parsed_df.printSchema()
parsed_df.select("doc_id", "path", "element_count", "processed").orderBy("doc_id").show(5, truncate=60)

element_types = Counter()
for row in parsed_df.select("parsed_content").collect():
    element_types.update(element["type"] for element in json.loads(row.parsed_content)["document"]["elements"])
logger.info(f"Elements by type: {dict(element_types)}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Inspect the chunks

# COMMAND ----------

chunks_df = spark.table(processor.chunks_table)
logger.info(f"Chunks: {chunks_df.count()}")
chunks_df.printSchema()
chunks_df.select("id", "section", "product_area", "has_code").orderBy("id").show(10, truncate=60)

# COMMAND ----------

spark.table(processor.metadata_table).select("doc_id", "last_updated", "processed", "volume_path").orderBy(
    "doc_id"
).show(5, truncate=70)
