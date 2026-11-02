"""Lakebase pgvector search over the docs chunks Synced Table."""

from __future__ import annotations

import os
import urllib.parse
from typing import Any

import psycopg
from databricks.sdk import WorkspaceClient
from loguru import logger

from docs_curator.config import ProjectConfig

RESULT_COLUMNS = "id, doc_id, title, section, product_area, url, has_code, text"


class LakebasePgVectorManager:
    """Search chunk embeddings in a Lakebase Synced Table with pgvector.

    The Synced Table is a read-only copy of docs_chunks, created with a type override so that
    `embedding` is a pgvector vector column. Full-text search uses an expression index, built on
    the same expression the queries use.
    """

    synced_table_name = "docs_chunks_synced"

    def __init__(self, config: ProjectConfig, workspace: WorkspaceClient | None = None) -> None:
        self.config = config
        self.lakebase_project_id = config.lakebase_project_id
        self.embedding_model = config.embedding_endpoint
        # Synced Tables land in a Postgres schema named after the Unity Catalog schema
        self.table_name = f'"{config.schema}"."{self.synced_table_name}"'
        self.workspace = workspace

        # Shared by the GIN index and the queries, so Postgres can use the index.
        # `text` already starts with the page title and section (chunking.contextual_text).
        self.text_expr = "to_tsvector('english', text)"

    def _get_connection_string(self) -> str:
        """Create a short-lived Lakebase connection string."""
        workspace = self.workspace or WorkspaceClient()
        postgres_api = workspace.postgres
        endpoint_name = os.environ.get("LAKEBASE_ENDPOINT")
        if endpoint_name:
            host = os.environ.get("PGHOST")
            database = os.environ.get("PGDATABASE", "databricks_postgres")
        else:
            project_name = f"projects/{self.lakebase_project_id}"
            branch = next(iter(postgres_api.list_branches(parent=project_name)))
            endpoint = next(iter(postgres_api.list_endpoints(parent=branch.name)))
            endpoint_name = endpoint.name
            host = endpoint.status.hosts.host
            database = "databricks_postgres"
        credential = postgres_api.generate_database_credential(endpoint=endpoint_name)
        client_id = os.environ.get("DATABRICKS_CLIENT_ID")
        username = urllib.parse.quote_plus(client_id or workspace.current_user.me().user_name)
        return f"postgresql://{username}:{credential.token}@{host}:5432/{database}?sslmode=require"

    def connection(self) -> psycopg.Connection:
        """Open a Lakebase connection using a fresh workspace credential."""
        return psycopg.connect(self._get_connection_string())

    def ensure_extension(self) -> None:
        """Enable pgvector.

        Must run before the Synced Table with a vector column is created: creating a synced table
        does not install extensions.
        """
        with self.connection() as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        logger.info("pgvector extension ready")

    def ensure_indexes(self) -> None:
        """Create the vector and full-text indexes on the Synced Table.

        Synced Tables allow CREATE INDEX (but no writes). Idempotent: rerun it if the indexes are
        missing, for example after the Synced Table was recreated.
        """
        with self.connection() as conn:
            conn.execute(
                f"""
                CREATE INDEX IF NOT EXISTS {self.synced_table_name}_embedding_hnsw
                ON {self.table_name} USING hnsw (embedding vector_cosine_ops)
                """
            )
            conn.execute(
                f"""
                CREATE INDEX IF NOT EXISTS {self.synced_table_name}_search_gin
                ON {self.table_name} USING gin (({self.text_expr}))
                """
            )
        logger.info(f"pgvector indexes ready on {self.table_name}")

    @staticmethod
    def _vector_literal(vector: list[float] | tuple[float, ...]) -> str:
        """Format a Python vector for PostgreSQL's vector input syntax."""
        return "[" + ",".join(str(float(value)) for value in vector) + "]"

    def embed_query(self, query: str) -> list[float]:
        """Generate a query embedding with the same model used for the chunks."""
        workspace = self.workspace or WorkspaceClient()
        client = workspace.serving_endpoints.get_open_ai_client()
        response = client.embeddings.create(model=self.embedding_model, input=query)
        if not response.data or not response.data[0].embedding:
            raise ValueError("The embedding endpoint returned no vector")
        return list(response.data[0].embedding)

    def _fetch(self, sql: str, params: list[Any]) -> list[dict[str, Any]]:
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute(sql, params)
            rows = cursor.fetchall()
            columns = [description.name for description in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in rows]

    @staticmethod
    def _filters(product_area: str | None, has_code: bool | None) -> tuple[str, list[Any]]:
        """Build the WHERE clause of the metadata filters."""
        where, params = "WHERE embedding IS NOT NULL", []
        if product_area is not None:
            where += " AND product_area = %s"
            params.append(product_area)
        if has_code is not None:
            where += " AND has_code = %s"
            params.append(has_code)
        return where, params

    def search(
        self,
        query: str,
        num_results: int = 5,
        product_area: str | None = None,
        has_code: bool | None = None,
    ) -> list[dict[str, Any]]:
        """Run cosine-similarity search, optionally filtered by product area and code chunks."""
        query_vector = self._vector_literal(self.embed_query(query))
        where, filter_params = self._filters(product_area, has_code)
        sql = f"""
            SELECT {RESULT_COLUMNS}, 1 - (embedding <=> %s::vector) AS score
            FROM {self.table_name}
            {where}
            ORDER BY embedding <=> %s::vector
            LIMIT %s
        """
        return self._fetch(sql, [query_vector, *filter_params, query_vector, num_results])

    def hybrid_search(
        self,
        query: str,
        num_results: int = 5,
        product_area: str | None = None,
        has_code: bool | None = None,
        vector_weight: float = 0.7,
    ) -> list[dict[str, Any]]:
        """Combine cosine similarity and PostgreSQL full-text ranking.

        plainto_tsquery requires every term of the question (AND), which a whole question almost
        never matches, so its terms are combined with OR instead: ts_rank_cd still ranks chunks
        matching more terms, closer together, higher. Normalization 32 scales the rank to 0-1,
        comparable with the cosine similarity.
        """
        query_vector = self._vector_literal(self.embed_query(query))
        where, filter_params = self._filters(product_area, has_code)
        sql = f"""
            WITH ranked AS (
                SELECT {RESULT_COLUMNS},
                       1 - (embedding <=> %s::vector) AS vector_score,
                       ts_rank_cd(
                           {self.text_expr},
                           replace(plainto_tsquery('english', %s)::text, '&', '|')::tsquery,
                           32
                       ) AS text_score
                FROM {self.table_name}
                {where}
            )
            SELECT *, (%s * vector_score + %s * text_score) AS score
            FROM ranked
            ORDER BY score DESC
            LIMIT %s
        """
        params = [query_vector, query, *filter_params, vector_weight, 1.0 - vector_weight, num_results]
        return self._fetch(sql, params)
