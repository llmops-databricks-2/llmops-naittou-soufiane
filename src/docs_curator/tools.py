"""Custom function tools of the agent.

search_docs(query, product_area?, has_code?, num_results?)
   ↓ (LakebasePgVectorManager.hybrid_search: 0.7 cosine + 0.3 full-text rank)
docs_chunks_synced (Lakebase pgvector)
   ↓
JSON list of chunks with title, section, URL and full text, for grounded answers with links
"""

import json
from typing import Any, Protocol

from docs_curator.docs_metadata import PRODUCT_AREAS
from docs_curator.mcp import ToolInfo

MAX_RESULTS = 10

SEARCH_DOCS_SPEC = {
    "type": "function",
    "function": {
        "name": "search_docs",
        "description": (
            "Search the content of the Databricks documentation on Declarative Automation Bundles and Lakeflow "
            "Jobs (hybrid semantic and keyword search). Returns the best matching sections, each with its page "
            "title, section, URL and full text, including YAML and code."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "What to look for, in the words the docs would use. Include the configuration keys you "
                        "expect, such as depends_on or job_clusters, rather than paraphrases."
                    ),
                },
                "product_area": {
                    "type": "string",
                    "enum": sorted(set(PRODUCT_AREAS.values())),
                    "description": "Only search bundles (Declarative Automation Bundles) or jobs (Lakeflow Jobs) pages.",
                },
                "has_code": {
                    "type": "boolean",
                    "description": (
                        "true to only return sections containing a code block, such as YAML examples. The "
                        "reference tables listing which keys exist are not code: leave it unset to find a key."
                    ),
                },
                "num_results": {
                    "type": "integer",
                    "description": f"Number of sections to return, 1 to {MAX_RESULTS}. Default 5.",
                },
            },
            "required": ["query"],
        },
    },
}


class Searcher(Protocol):
    """What search_docs needs from LakebasePgVectorManager."""

    def hybrid_search(
        self, query: str, num_results: int = 5, product_area: str | None = None, has_code: bool | None = None
    ) -> list[dict[str, Any]]:
        """Return ranked chunk rows."""
        ...


def search_docs_tool(searcher: Searcher) -> ToolInfo:
    """Expose hybrid search over the docs chunks as the search_docs tool."""

    def search_docs(
        query: str, product_area: str | None = None, has_code: bool | None = None, num_results: int = 5
    ) -> str:
        if not query.strip():
            raise ValueError("query is empty")
        if product_area is not None and product_area not in PRODUCT_AREAS.values():
            raise ValueError(f"product_area must be one of {sorted(set(PRODUCT_AREAS.values()))}")
        num_results = max(1, min(int(num_results), MAX_RESULTS))
        rows = searcher.hybrid_search(query, num_results=num_results, product_area=product_area, has_code=has_code)
        # No chunk id: GPT-OSS cites any id it is given (【jobs/git#3】) instead of the page URL
        return json.dumps(
            [
                {
                    "title": row["title"],
                    "section": row["section"],
                    "url": row["url"],
                    "has_code": row["has_code"],
                    "score": round(float(row["score"]), 3),
                    "text": row["text"],
                }
                for row in rows
            ]
        )

    return ToolInfo(name="search_docs", spec=SEARCH_DOCS_SPEC, exec_fn=search_docs)
