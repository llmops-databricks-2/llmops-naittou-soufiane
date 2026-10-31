"""Process documentation pages into embedded chunks.

docs_metadata (lecture 1: one row per page)
   ↓ (download_pages: pages never processed, or updated since)
raw page HTML in the volume: /Volumes/{catalog}/{schema}/{volume}/{run}/{doc_id}.html
   ↓ (parse_pages: doc_pages.parse_doc_page)
parsed_docs (JSON elements, shaped like ai_parse_document output)
   ↓ (chunk_pages: chunking.section_chunks + ai_query embeddings)
docs_chunks (contextual text + metadata + embedding, Change Data Feed on)
   └─ (Lakebase Synced Table + LakebasePgVectorManager search) (lecture 2.5 notebook)
"""

import json
import os
import time
from datetime import UTC, datetime

import requests
from loguru import logger
from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import types as T
from pyspark.sql.functions import expr

from docs_curator.chunking import DEFAULT_MAX_CHARS, contextual_text, section_chunks
from docs_curator.config import ProjectConfig
from docs_curator.doc_pages import parse_doc_page
from docs_curator.docs_metadata import fetch_text

PARSED_SCHEMA = T.StructType(
    [
        T.StructField("doc_id", T.StringType(), False),
        T.StructField("path", T.StringType(), False),
        T.StructField("parsed_content", T.StringType(), False),
        T.StructField("element_count", T.IntegerType(), False),
        T.StructField("processed", T.TimestampType(), False),
    ]
)

CHUNK_SCHEMA = T.StructType(
    [
        T.StructField("id", T.StringType(), False),
        T.StructField("doc_id", T.StringType(), False),
        T.StructField("chunk_index", T.IntegerType(), False),
        T.StructField("section", T.StringType(), True),
        T.StructField("text", T.StringType(), False),
        T.StructField("has_code", T.BooleanType(), False),
        T.StructField("product_area", T.StringType(), True),
        T.StructField("title", T.StringType(), True),
        T.StructField("url", T.StringType(), True),
        T.StructField("last_updated", T.DateType(), True),
        T.StructField("processed", T.TimestampType(), False),
    ]
)


def _quote_list(values: list[str]) -> str:
    """Render strings as a SQL list of literals."""
    return ", ".join("'" + value.replace("'", "''") + "'" for value in values)


class DocsProcessor:
    """Download, parse, chunk and embed the pages listed in docs_metadata."""

    def __init__(self, spark: SparkSession, config: ProjectConfig, max_chars: int = DEFAULT_MAX_CHARS) -> None:
        """Initialize the processor for one run.

        Args:
            spark: SparkSession instance
            config: ProjectConfig with the catalog, schema, volume and embedding endpoint
            max_chars: Maximum characters of a chunk body

        """
        self.spark = spark
        self.cfg = config
        self.max_chars = max_chars
        self.run_ts = datetime.now(UTC).replace(microsecond=0)
        self.raw_dir = f"/Volumes/{config.catalog}/{config.schema}/{config.volume}/{self.run_ts:%Y%m%d%H%M}"
        self.metadata_table = f"{config.full_schema_name}.docs_metadata"
        self.parsed_table = f"{config.full_schema_name}.parsed_docs"
        self.chunks_table = f"{config.full_schema_name}.docs_chunks"

    def pages_to_process(self) -> list[dict]:
        """Pages never processed, or updated on the docs site since they were processed."""
        rows = self.spark.sql(f"""
            SELECT doc_id, url, title, product_area, last_updated
            FROM {self.metadata_table}
            WHERE processed IS NULL OR last_updated > CAST(processed AS DATE)
            ORDER BY doc_id
        """).collect()
        return [row.asDict() for row in rows]

    def download_pages(self, pages: list[dict], delay_seconds: float = 0.5) -> list[dict]:
        """Save each page's HTML to the volume and record its path."""
        self.spark.sql(f"CREATE VOLUME IF NOT EXISTS {self.cfg.full_volume_path}")
        os.makedirs(self.raw_dir, exist_ok=True)
        with requests.Session() as session:
            for page in pages:
                page["volume_path"] = f"{self.raw_dir}/{page['doc_id'].replace('/', '__')}.html"
                with open(page["volume_path"], "w", encoding="utf-8") as file:
                    file.write(fetch_text(session, page["url"]))
                time.sleep(delay_seconds)
        logger.info(f"Downloaded {len(pages)} pages to {self.raw_dir}")
        return pages

    def parse_pages(self, pages: list[dict]) -> None:
        """Parse each downloaded page and store its elements in parsed_docs.

        Raises:
            RuntimeError: if a page yields no elements or no heading, which means the docs site
                markup changed and the parser needs updating

        """
        rows, failures = [], []
        for page in pages:
            with open(page["volume_path"], encoding="utf-8") as file:
                parsed = parse_doc_page(file.read(), page["url"])
            elements = parsed["document"]["elements"]
            if not any(element["type"] == "section_header" for element in elements):
                failures.append(page["doc_id"])
            page["elements"] = elements
            rows.append((page["doc_id"], page["volume_path"], json.dumps(parsed), len(elements), self.run_ts))
        if failures:
            raise RuntimeError(f"No article content parsed for {len(failures)} pages: {failures}")

        self._replace_rows(self.parsed_table, self.spark.createDataFrame(rows, schema=PARSED_SCHEMA), pages)
        logger.info(f"Parsed {len(rows)} pages into {self.parsed_table}")

    def chunk_pages(self, pages: list[dict]) -> None:
        """Chunk the parsed pages, embed the chunks and store them in docs_chunks."""
        rows = []
        for page in pages:
            for index, chunk in enumerate(section_chunks(page["elements"], self.max_chars)):
                rows.append(
                    (
                        f"{page['doc_id']}#{index}",
                        page["doc_id"],
                        index,
                        chunk["section"],
                        contextual_text(page["title"], chunk["section"], chunk["text"]),
                        chunk["has_code"],
                        page["product_area"],
                        page["title"],
                        page["url"],
                        page["last_updated"],
                        self.run_ts,
                    )
                )
        endpoint = self.cfg.embedding_endpoint.replace("'", "''")
        chunks_df = self.spark.createDataFrame(rows, schema=CHUNK_SCHEMA).withColumn(
            # ai_query via expr: not every runtime exposes it in pyspark.sql.functions
            "embedding",
            expr(f"CAST(ai_query('{endpoint}', text) AS ARRAY<FLOAT>)"),
        )
        self._replace_rows(self.chunks_table, chunks_df, pages)
        # Change Data Feed lets the Lakebase Synced Table sync incrementally, deletes included
        self.spark.sql(f"ALTER TABLE {self.chunks_table} SET TBLPROPERTIES (delta.enableChangeDataFeed = true)")
        logger.info(f"Saved {len(rows)} chunks to {self.chunks_table}")

    def _replace_rows(self, table: str, df: DataFrame, pages: list[dict]) -> None:
        """Replace the rows of the given pages, and drop pages no longer in docs_metadata."""
        if self.spark.catalog.tableExists(table):
            self.spark.sql(f"""
                DELETE FROM {table}
                WHERE doc_id IN ({_quote_list([page["doc_id"] for page in pages])})
                   OR doc_id NOT IN (SELECT doc_id FROM {self.metadata_table})
            """)
        df.write.mode("append").option("mergeSchema", "true").saveAsTable(table)

    def mark_processed(self, pages: list[dict]) -> None:
        """Record the run time and raw HTML path of each processed page in docs_metadata."""
        updates = self.spark.createDataFrame(
            [(page["doc_id"], page["volume_path"]) for page in pages], "doc_id STRING, volume_path STRING"
        )
        updates.createOrReplaceTempView("processed_pages")
        self.spark.sql(f"""
            MERGE INTO {self.metadata_table} AS target
            USING processed_pages AS source
            ON target.doc_id = source.doc_id
            WHEN MATCHED THEN UPDATE SET
                processed = TIMESTAMP '{self.run_ts:%Y-%m-%d %H:%M:%S}',
                volume_path = source.volume_path
        """)

    def process_and_save(self) -> None:
        """Complete workflow: download, parse and chunk the pages to process."""
        pages = self.pages_to_process()
        if not pages:
            logger.info("No new or updated pages to process.")
            return
        logger.info(f"Processing {len(pages)} pages")
        self.download_pages(pages)
        self.parse_pages(pages)
        self.chunk_pages(pages)
        self.mark_processed(pages)
        logger.info("Processing complete!")
