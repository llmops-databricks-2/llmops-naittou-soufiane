# Databricks notebook source
# ruff: noqa: ERA001
# Databricks MAGIC comments are executable notebook cells.
"""Review the chunk sizes of the section strategy and compare a fixed-size alternative."""

# COMMAND ----------

# MAGIC %md
# MAGIC # Lecture 2.3: Chunking Strategies
# MAGIC
# MAGIC `docs_chunks` (lecture 2.2) uses **section chunking**: a level 1-3 heading starts a new chunk,
# MAGIC elements are added up to 1,500 characters, and a code block is never split unless it alone is
# MAGIC longer. Each chunk starts with its page title and section path, so it is retrievable on its own.
# MAGIC
# MAGIC The alternative tried here is **fixed-size chunking**: 1,000-character windows with 150 characters of
# MAGIC overlap over the whole page, the simplest strategy. Both are compared on the same parsed pages:
# MAGIC
# MAGIC | Question | Why it matters for the Docs Curator |
# MAGIC |---|---|
# MAGIC | How long are the chunks? | GTE Large embeds at most 512 tokens (about 2,000 characters); longer text is truncated |
# MAGIC | How many chunks cut a code block? | A cut YAML snippet is invalid, and answers must quote valid YAML |
# MAGIC | Does a chunk know where it comes from? | A chunk without its page and section is ambiguous ("Development mode" of what?) |

# COMMAND ----------

import json
from statistics import mean, median

from databricks.sdk.runtime import dbutils
from loguru import logger
from pyspark.sql import SparkSession

from docs_curator.chunking import breaks_code_block, fixed_size_chunks, page_markdown
from docs_curator.config import load_config

spark = SparkSession.builder.getOrCreate()

dbutils.widgets.text("env", "dev")
cfg = load_config("../project_config.yml", env=dbutils.widgets.get("env"))

parsed_table = f"{cfg.full_schema_name}.parsed_docs"
chunks_table = f"{cfg.full_schema_name}.docs_chunks"

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Inspect the processed documents and chunk tables

# COMMAND ----------

parsed_df = spark.table(parsed_table)
chunks_df = spark.table(chunks_table)
logger.info(f"{parsed_table}: {parsed_df.count()} pages")
logger.info(f"{chunks_table}: {chunks_df.count()} chunks")
chunks_df.printSchema()

sample = chunks_df.where("has_code").orderBy("id").first()
logger.info(f"Sample chunk {sample.id} ({len(sample.text)} characters):\n{sample.text}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Chunk sizes of both strategies

# COMMAND ----------


def size_stats(name: str, texts: list[str]) -> dict:
    """Summarize the size of a set of chunks, in characters (about 4 per token)."""
    lengths = sorted(len(text) for text in texts)
    return {
        "strategy": name,
        "chunks": len(lengths),
        "avg_chars": round(mean(lengths)),
        "median_chars": round(median(lengths)),
        "min_chars": lengths[0],
        "max_chars": lengths[-1],
        "over_2000_chars": sum(length > 2000 for length in lengths),
        "cut_code_blocks": sum(breaks_code_block(text) for text in texts),
    }


section_texts = [row.text for row in chunks_df.select("text").collect()]

fixed_texts = []
for row in parsed_df.select("parsed_content").collect():
    elements = json.loads(row.parsed_content)["document"]["elements"]
    fixed_texts.extend(fixed_size_chunks(page_markdown(elements), chunk_size=1000, overlap=150))

comparison = [size_stats("section (default)", section_texts), size_stats("fixed 1000/150", fixed_texts)]
spark.createDataFrame(comparison).select(*comparison[0].keys()).show(truncate=False)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Size distribution

# COMMAND ----------

bucket_edges = [250, 500, 1000, 1500, 2000]


def bucket(length: int) -> str:
    """Name the size bucket of a chunk."""
    lower = 0
    for edge in bucket_edges:
        if length <= edge:
            return f"{lower + 1}-{edge}"
        lower = edge
    return f">{bucket_edges[-1]}"


for name, texts in (("section (default)", section_texts), ("fixed 1000/150", fixed_texts)):
    counts = {}
    for text in texts:
        counts[bucket(len(text))] = counts.get(bucket(len(text)), 0) + 1
    logger.info(f"{name}: {dict(sorted(counts.items(), key=lambda item: int(item[0].strip('>').split('-')[0])))}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. The same YAML snippet in both strategies
# MAGIC
# MAGIC A fixed-size window starts and ends anywhere, so it can cut a YAML block and lose the page and
# MAGIC section the snippet belongs to.

# COMMAND ----------

cut = next(text for text in fixed_texts if breaks_code_block(text))
logger.info(f"Fixed-size chunk that cuts a code block:\n{cut}")
