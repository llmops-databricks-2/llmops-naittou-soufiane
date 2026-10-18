"""Docs metadata parsing: sitemap selection and page head extraction, without network access."""

from datetime import date
from pathlib import Path

from docs_curator.docs_metadata import parse_page_head, parse_sitemap

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_parse_sitemap_keeps_prefixes_in_priority_order() -> None:
    """Bundles pages come before jobs pages, and other sections are dropped."""
    pages = parse_sitemap((FIXTURES / "sitemap.xml").read_text())

    assert [page["doc_id"] for page in pages] == [
        "dev-tools/bundles",
        "dev-tools/bundles/settings",
        "jobs",
        "jobs/configure-task",
    ]
    assert [page["product_area"] for page in pages] == ["bundles", "bundles", "jobs", "jobs"]


def test_parse_sitemap_applies_limit() -> None:
    """The limit cuts the lower-priority pages first."""
    pages = parse_sitemap((FIXTURES / "sitemap.xml").read_text(), limit=3)

    assert [page["product_area"] for page in pages] == ["bundles", "bundles", "jobs"]


def test_parse_page_head_extracts_metadata() -> None:
    """Title loses the site suffix; breadcrumb is ordered by position."""
    metadata = parse_page_head((FIXTURES / "bundles_page.html").read_text())

    assert metadata["title"] == "What are Declarative Automation Bundles?"
    assert metadata["description"].startswith("Learn about Declarative Automation Bundles")
    assert metadata["canonical_url"] == "https://docs.databricks.com/aws/en/dev-tools/bundles/"
    assert metadata["breadcrumb"] == ["CI/CD", "Declarative Automation Bundles"]
    assert metadata["last_updated"] == date(2026, 9, 11)


def test_parse_page_head_tolerates_missing_metadata() -> None:
    """A page without metadata yields empty values instead of failing."""
    metadata = parse_page_head("<html><head></head><body></body></html>")

    assert metadata == {
        "title": None,
        "description": None,
        "canonical_url": None,
        "breadcrumb": [],
        "last_updated": None,
    }
