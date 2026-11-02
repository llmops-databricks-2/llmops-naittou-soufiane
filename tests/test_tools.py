"""Tests for the search_docs tool and the compaction of Genie MCP responses."""

import json

import pytest

from docs_curator.mcp import compact_genie_response
from docs_curator.tools import MAX_RESULTS, search_docs_tool

ROW = {
    "id": "jobs/git#3",
    "doc_id": "jobs/git",
    "title": "Use Git with jobs",
    "section": "Configure",
    "product_area": "jobs",
    "url": "https://docs.databricks.com/aws/en/jobs/git",
    "has_code": False,
    "text": "Use Git with jobs > Configure\n...",
    "score": 0.81234,
    "vector_score": 0.8,
    "text_score": 0.1,
}


class FakeSearcher:
    """Records the search arguments and returns one row."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def hybrid_search(
        self, query: str, num_results: int = 5, product_area: str | None = None, has_code: bool | None = None
    ) -> list[dict]:
        """Return ROW."""
        self.calls.append(
            {"query": query, "num_results": num_results, "product_area": product_area, "has_code": has_code}
        )
        return [ROW]


def test_search_docs_returns_citable_fields_only() -> None:
    """Results carry title, section, URL and text, but no chunk id the model would cite instead of the URL."""
    searcher = FakeSearcher()
    result = json.loads(search_docs_tool(searcher).exec_fn(query="git", product_area="jobs", has_code=False))

    assert result == [
        {
            "title": "Use Git with jobs",
            "section": "Configure",
            "url": "https://docs.databricks.com/aws/en/jobs/git",
            "has_code": False,
            "score": 0.812,
            "text": "Use Git with jobs > Configure\n...",
        }
    ]
    assert searcher.calls == [{"query": "git", "num_results": 5, "product_area": "jobs", "has_code": False}]


def test_search_docs_clamps_num_results() -> None:
    """The model can't ask for more than MAX_RESULTS sections, or fewer than one."""
    searcher = FakeSearcher()
    tool = search_docs_tool(searcher)
    tool.exec_fn(query="git", num_results=100)
    tool.exec_fn(query="git", num_results=0)

    assert [call["num_results"] for call in searcher.calls] == [MAX_RESULTS, 1]


@pytest.mark.parametrize("arguments", [{"query": " "}, {"query": "git", "product_area": "pipelines"}])
def test_search_docs_rejects_invalid_arguments(arguments: dict) -> None:
    """An empty query or an unknown product area is an error for the model, not a search."""
    searcher = FakeSearcher()
    with pytest.raises(ValueError):
        search_docs_tool(searcher).exec_fn(**arguments)
    assert searcher.calls == []


GENIE_RESPONSE = {
    "content": {
        "queryAttachments": [
            {
                "query": "SELECT product_area, count(*) AS pages FROM docs_metadata GROUP BY product_area",
                "description": "Pages per product area",
                "statement_response": {
                    "statement_id": "01f1",
                    "status": {"state": "SUCCEEDED"},
                    "manifest": {
                        "format": "JSON_ARRAY",
                        "schema": {
                            "column_count": 2,
                            "columns": [
                                {"name": "product_area", "type_name": "STRING", "position": 0},
                                {"name": "pages", "type_name": "LONG", "position": 1},
                            ],
                        },
                        "total_chunk_count": 1,
                        "total_row_count": 2,
                    },
                    "result": {
                        "chunk_index": 0,
                        "data_array": [
                            {"values": [{"string_value": "bundles"}, {"string_value": "36"}]},
                            {"values": [{"string_value": "jobs"}, {"string_value": "14"}]},
                        ],
                    },
                },
            }
        ],
        "textAttachments": ["There are 36 bundles pages and 14 jobs pages."],
        "suggestedQuestions": ["Which pages were updated most recently?"],
    },
    "conversationId": "conv",
    "messageId": "msg",
    "status": "COMPLETED",
}


def test_compact_genie_response() -> None:
    """Keep status, ids, text, SQL and rows; drop the manifest, typed values and suggestions."""
    compact = json.loads(compact_genie_response(json.dumps(GENIE_RESPONSE)))

    assert compact == {
        "status": "COMPLETED",
        "conversation_id": "conv",
        "message_id": "msg",
        "answer": "There are 36 bundles pages and 14 jobs pages.",
        "sql": "SELECT product_area, count(*) AS pages FROM docs_metadata GROUP BY product_area",
        "columns": ["product_area", "pages"],
        "rows": [["bundles", "36"], ["jobs", "14"]],
        "total_rows": 2,
    }


@pytest.mark.parametrize("text", ["not json", '["a list"]', '{"other": "tool"}'])
def test_compact_genie_response_passes_other_text_through(text: str) -> None:
    """Anything that isn't a Genie message reaches the model unchanged."""
    assert compact_genie_response(text) == text
