"""Doc page parsing: elements of a Docusaurus page, without network access.

The fixture reproduces the markup of docs.databricks.com: minified HTML5 without </p>, </li>
and </td>, syntax-highlighted code with one div per line, admonitions and heading anchors.
"""

from pathlib import Path

import yaml

from docs_curator.doc_pages import parse_doc_page

PAGE_URL = "https://docs.databricks.com/aws/en/dev-tools/bundles/deployment-modes"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "doc_page.html"


def elements() -> list[dict]:
    """Parse the fixture page."""
    return parse_doc_page(FIXTURE.read_text(), PAGE_URL)["document"]["elements"]


def test_headings_keep_their_level_without_anchor_characters() -> None:
    """Headings of every level are found, the zero-width anchor text is dropped."""
    headings = [(e["level"], e["content"]) for e in elements() if e["type"] == "section_header"]

    assert headings == [(1, "Deployment modes"), (2, "Development mode"), (3, "Presets"), (2, "Production mode")]


def test_paragraphs_and_list_items_close_without_end_tags() -> None:
    """Omitted </p> and </li> end blocks; a <p> inside an <li> stays part of the item."""
    texts = [e["content"] for e in elements() if e["type"] == "text"]

    assert texts[:5] == [
        "Bundles support development and production modes.",
        "Choose a mode per target:",
        "Development, for iterating",
        "Production, for scheduled runs with a service principal",
        "Modes are optional.",
    ]


def test_code_block_keeps_yaml_indentation() -> None:
    """Each highlighted line is a source line, so the YAML stays valid."""
    (code,) = [e for e in elements() if e["type"] == "code"]

    assert code["language"] == "yaml"
    assert code["content"] == "targets:\n  dev:\n    mode: development"
    assert yaml.safe_load(code["content"]) == {"targets": {"dev": {"mode": "development"}}}


def test_table_becomes_markdown_without_end_tags() -> None:
    """Cells without </td> are split at the next cell or row."""
    (table,) = [e for e in elements() if e["type"] == "table"]

    assert table["content"] == (
        "| Preset | Effect |\n|---|---|\n"
        "| name_prefix | Prefixes resource names |\n"
        "| pipelines_development | Runs pipelines in development mode |"
    )


def test_image_keeps_alt_text_and_absolute_url() -> None:
    """Images become their alt text and absolute URL; images without alt text are dropped."""
    images = [e for e in elements() if e["type"] == "image"]

    assert images == [
        {
            "type": "image",
            "content": "Deploy to dev dialog",
            "url": "https://docs.databricks.com/aws/en/assets/images/deploy-dialog.png",
            "id": images[0]["id"],
        }
    ]


def test_only_the_article_body_is_parsed() -> None:
    """Navigation, the last-updated footer and the site footer are left out; ids follow the order."""
    parsed = elements()
    contents = " ".join(e["content"] for e in parsed)

    assert "Navigation text" not in contents
    assert "Last updated" not in contents
    assert "Privacy Notice" not in contents
    assert [e["id"] for e in parsed] == list(range(len(parsed)))


def test_falls_back_to_article_without_the_markdown_container() -> None:
    """If the theme-doc-markdown container disappears, the <article> is parsed instead."""
    html = "<html><body><article><h1>Title</h1><p>Body text</article></body></html>"

    parsed = parse_doc_page(html, PAGE_URL)["document"]["elements"]

    assert [(e["type"], e["content"]) for e in parsed] == [("section_header", "Title"), ("text", "Body text")]


def test_page_without_article_has_no_elements() -> None:
    """A page without an article body yields no elements, which the processor reports as a failure."""
    assert parse_doc_page("<html><body><p>Error</p></body></html>", PAGE_URL) == {"document": {"elements": []}}
