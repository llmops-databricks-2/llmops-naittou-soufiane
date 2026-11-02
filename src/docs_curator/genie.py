"""Genie space over docs_metadata, exposed to the agent as a managed MCP server.

docs_metadata (lecture 1: one row per page, with column comments)
   ↓ (ensure_genie_space: created once per catalog, found again by title)
Genie space → /api/2.0/mcp/genie/{space_id} (managed MCP: the agent's metadata tool)
"""

import json

from databricks.sdk import WorkspaceClient
from loguru import logger

from docs_curator.config import ProjectConfig

# Genie expects 32-hex ids, sorted, on every sample question and instruction
INSTRUCTIONS = [
    "docs_metadata has one row per documentation page of Declarative Automation Bundles (product_area = 'bundles') "
    "and Lakeflow Jobs (product_area = 'jobs').",
    "Always return url and title with page results, so the answer can link to the pages.",
    "'Recently updated' or 'changed' means ordered by last_updated, newest first.",
    "breadcrumb is the navigation path of the page in the docs, such as ['Jobs', 'Configure', 'Triggers']; "
    "search it with array_contains or by exploding it.",
    "The table holds page metadata only, not page content: questions on how to configure something are "
    "answered by the documentation search, not by this space.",
]
SAMPLE_QUESTIONS = [
    "Which bundles pages were updated most recently?",
    "How many pages are there per product area?",
    "List the jobs pages about triggers",
]
EXAMPLE_SQLS = [
    (
        "Which pages were updated most recently?",
        "SELECT title, url, product_area, last_updated FROM {table} ORDER BY last_updated DESC LIMIT 10",
    ),
]


def _id(n: int) -> str:
    return f"{n:032x}"


def genie_space_payload(table: str) -> dict:
    """Build the serialized_space of the Genie space over the docs_metadata table."""
    return {
        "version": 2,
        "config": {
            "sample_questions": [
                {"id": _id(0x100 + i), "question": [question]} for i, question in enumerate(SAMPLE_QUESTIONS)
            ]
        },
        "data_sources": {
            "tables": [
                {
                    "identifier": table,
                    "column_configs": [
                        {"column_name": "ingestion_timestamp", "exclude": True},
                        {
                            "column_name": "product_area",
                            "enable_format_assistance": True,
                            "enable_entity_matching": True,
                        },
                        {"column_name": "title", "enable_format_assistance": True, "enable_entity_matching": True},
                        {"column_name": "volume_path", "exclude": True},
                    ],
                }
            ]
        },
        "instructions": {
            "example_question_sqls": [
                {"id": _id(0x200 + i), "question": [question], "sql": [sql.format(table=table)]}
                for i, (question, sql) in enumerate(EXAMPLE_SQLS)
            ],
            "text_instructions": [{"id": _id(0x300), "content": INSTRUCTIONS}],
        },
    }


def ensure_genie_space(workspace: WorkspaceClient, config: ProjectConfig) -> str:
    """Return the id of the docs_metadata Genie space of this catalog, creating it the first time.

    Spaces are found by title, so a rerun reuses the space instead of creating another one.
    """
    table = f"{config.full_schema_name}.docs_metadata"
    title = f"docs-curator {config.full_schema_name}"
    for space in workspace.genie.list_spaces().spaces or []:
        if space.title == title:
            logger.info(f"Genie space exists: {space.space_id} ({title})")
            return space.space_id

    space = workspace.genie.create_space(
        warehouse_id=config.warehouse_id,
        serialized_space=json.dumps(genie_space_payload(table)),
        title=title,
        description="Metadata of the Databricks docs pages on bundles and jobs known to the Docs Curator.",
    )
    logger.info(f"Created Genie space: {space.space_id} ({title})")
    return space.space_id
