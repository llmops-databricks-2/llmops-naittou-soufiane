# Databricks notebook source
# ruff: noqa: ERA001
# Databricks MAGIC comments are executable notebook cells.
"""Ingest the metadata of Databricks documentation pages into a Delta table."""

# MAGIC %md
# MAGIC # Lecture 1.3: Docs Metadata Ingestion
# MAGIC
# MAGIC The "Get docs metadata → Store to delta table" step of the Docs Curator. It reads the public
# MAGIC [docs sitemap](https://docs.databricks.com/aws/en/sitemap.xml), keeps the Declarative Automation
# MAGIC Bundles and Lakeflow Jobs pages, reads each page's title, description, breadcrumb and last
# MAGIC updated date, and overwrites the table `docs_metadata` in your schema. The page bodies are
# MAGIC processed in lecture 2, which fills `processed` and `volume_path`.

# COMMAND ----------

from datetime import UTC, datetime

from databricks.sdk.runtime import dbutils
from loguru import logger
from pyspark.sql import SparkSession
from pyspark.sql.types import (
    ArrayType,
    DataType,
    DateType,
    StringType,
    StructField,
    StructType,
    TimestampType,
)

from docs_curator.config import load_config
from docs_curator.docs_metadata import fetch_docs_metadata

spark = SparkSession.builder.getOrCreate()

# The environment comes from the job's base parameters; "dev" when run by hand.
dbutils.widgets.text("env", "dev")
env = dbutils.widgets.get("env")

cfg = load_config("../project_config.yml", env=env)
TABLE_NAME = f"{cfg.full_schema_name}.docs_metadata"
MAX_PAGES = 50

spark.sql(f"CREATE SCHEMA IF NOT EXISTS {cfg.full_schema_name}")
logger.info(f"Schema {cfg.full_schema_name} ready")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Fetch the docs metadata

# COMMAND ----------

pages = fetch_docs_metadata(limit=MAX_PAGES)
logger.info(f"Fetched {len(pages)} pages")
logger.info(f"Sample page: {pages[0]['title']} ({pages[0]['url']})")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Store to a Delta table
# MAGIC
# MAGIC Column comments describe the table for the Genie space that exposes it later.

# COMMAND ----------


def column(name: str, data_type: DataType, nullable: bool, comment: str) -> StructField:
    """Define a column with its Unity Catalog comment."""
    return StructField(name, data_type, nullable, metadata={"comment": comment})


schema = StructType(
    [
        column("doc_id", StringType(), False, "Page path under docs.databricks.com/aws/en/, such as jobs/git"),
        column("url", StringType(), False, "Page URL, used as the source link in answers"),
        column("title", StringType(), False, "Page title"),
        column("description", StringType(), True, "Page summary from its meta description"),
        column("product_area", StringType(), False, "bundles (Declarative Automation Bundles) or jobs (Lakeflow Jobs)"),
        column("breadcrumb", ArrayType(StringType()), True, "Navigation path of the page in the docs"),
        column("last_updated", DateType(), True, "Date the page was last updated on the docs site"),
        column("ingestion_timestamp", TimestampType(), False, "When this row was ingested"),
        column("processed", TimestampType(), True, "When the page body was parsed (lecture 2)"),
        column("volume_path", StringType(), True, "Raw page HTML in the volume (lecture 2)"),
    ]
)

ingested_at = datetime.now(UTC)
rows = [
    {
        "doc_id": page["doc_id"],
        "url": page["url"],
        "title": page["title"] or page["doc_id"],
        "description": page["description"],
        "product_area": page["product_area"],
        "breadcrumb": page["breadcrumb"],
        "last_updated": page["last_updated"],
        "ingestion_timestamp": ingested_at,
        "processed": None,
        "volume_path": None,
    }
    for page in pages
]

df = spark.createDataFrame(rows, schema=schema)
df.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(TABLE_NAME)
spark.sql(
    f"COMMENT ON TABLE {TABLE_NAME} IS 'Metadata of Databricks documentation pages on bundles and jobs, "
    "one row per page, from the docs.databricks.com sitemap'"
)
logger.info(f"Wrote {df.count()} rows to {TABLE_NAME}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Verify the data

# COMMAND ----------

docs_df = spark.table(TABLE_NAME)
logger.info(f"Total pages: {docs_df.count()}")
docs_df.printSchema()
docs_df.select("doc_id", "title", "product_area", "last_updated").show(5, truncate=60)

# COMMAND ----------

logger.info("Pages by product area:")
docs_df.groupBy("product_area").count().orderBy("count", ascending=False).show()

logger.info("Most recently updated pages:")
docs_df.select("title", "last_updated", "doc_id").orderBy("last_updated", ascending=False).show(5, truncate=60)
