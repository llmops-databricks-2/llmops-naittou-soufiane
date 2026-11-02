"""Tests for the sources added to every answer."""

import json

from docs_curator.answer_checks import MAX_SOURCES, add_sources

DOCS_RESULT = json.dumps(
    [
        {
            "title": "Trigger jobs when new files arrive",
            "url": "https://docs.databricks.com/aws/en/jobs/file-arrival-triggers",
        },
        {"title": "Bundle resources", "url": "https://docs.databricks.com/aws/en/dev-tools/bundles/resources"},
        {
            "title": "Trigger jobs when new files arrive",
            "url": "https://docs.databricks.com/aws/en/jobs/file-arrival-triggers",
        },
    ]
)


def test_add_sources_lists_searched_pages_when_the_answer_has_no_link() -> None:
    """Citation markers go; distinct searched pages are appended, best ranked first."""
    result = add_sources("See the trigger docs 【0†L9-L19】.", [DOCS_RESULT, "not json"])

    assert result == (
        "See the trigger docs.\n\nPages searched for this answer:\n"
        "- [Trigger jobs when new files arrive](https://docs.databricks.com/aws/en/jobs/file-arrival-triggers)\n"
        "- [Bundle resources](https://docs.databricks.com/aws/en/dev-tools/bundles/resources)"
    )


def test_add_sources_turns_url_markers_into_links() -> None:
    """A marker holding a URL keeps it, as a Markdown link."""
    answer = "Use depends_on.【https://docs.databricks.com/aws/en/dev-tools/bundles/job-task-types】"

    assert add_sources(answer, [DOCS_RESULT]) == (
        "Use depends_on. ([source](https://docs.databricks.com/aws/en/dev-tools/bundles/job-task-types))"
    )


def test_add_sources_keeps_an_answer_with_links() -> None:
    """The model's own links are kept as they are."""
    answer = "See [Bundle resources](https://docs.databricks.com/aws/en/dev-tools/bundles/resources)."

    assert add_sources(answer, [DOCS_RESULT]) == answer


def test_add_sources_limits_the_list() -> None:
    """At most MAX_SOURCES pages are listed."""
    results = json.dumps([{"title": f"p{i}", "url": f"https://docs/{i}"} for i in range(10)])

    assert add_sources("text", [results]).count("- [") == MAX_SOURCES


def test_add_sources_without_search() -> None:
    """A greeting, or an answer from Genie rows only, gets no list."""
    assert add_sources("Hello!", []) == "Hello!"
