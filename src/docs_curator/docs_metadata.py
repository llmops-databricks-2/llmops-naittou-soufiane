"""Fetch metadata of Databricks documentation pages.

docs.databricks.com sitemap.xml
   ↓ (parse_sitemap: keep pages under the configured path prefixes)
page URLs
   ↓ (parse_page_head: title, description, breadcrumb, last updated)
docs_metadata table (written by the ingestion notebook)
"""

import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import date
from html.parser import HTMLParser

import backoff
import requests
from loguru import logger

SITEMAP_URL = "https://docs.databricks.com/aws/en/sitemap.xml"
DOCS_ROOT = "https://docs.databricks.com/aws/en/"

# Path prefix (relative to DOCS_ROOT) -> product area, in priority order for the page limit.
PRODUCT_AREAS = {
    "dev-tools/bundles/": "bundles",
    "jobs/": "jobs",
}

_SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
_TITLE_SUFFIX = re.compile(r"\s*\|\s*Databricks on AWS\s*$")


def parse_sitemap(sitemap_xml: str, prefixes: dict[str, str] = PRODUCT_AREAS, limit: int = 50) -> list[dict]:
    """Select documentation pages from a sitemap.

    Args:
        sitemap_xml: Content of sitemap.xml
        prefixes: Path prefix relative to DOCS_ROOT mapped to its product area, in priority order
        limit: Maximum number of pages to return

    Returns:
        List of {"url", "doc_id", "product_area"} dictionaries, pages of earlier prefixes first

    """
    root = ET.fromstring(sitemap_xml)
    urls = sorted(loc.text.strip() for loc in root.iterfind("sm:url/sm:loc", _SITEMAP_NS) if loc.text)

    pages = []
    for prefix, product_area in prefixes.items():
        prefix_url = DOCS_ROOT + prefix
        for url in urls:
            if url.startswith(prefix_url):
                if len(pages) >= limit:
                    return pages
                doc_id = url.removeprefix(DOCS_ROOT).strip("/")
                pages.append({"url": url, "doc_id": doc_id, "product_area": product_area})
    return pages


class _PageHeadParser(HTMLParser):
    """Collect the metadata of a Docusaurus documentation page.

    HTMLParser calls the handle_* methods from inside feed(), once per tag and text run, so the
    attributes are filled in when feed() and close() return.
    """

    def __init__(self) -> None:
        super().__init__()
        self.title: str | None = None
        self.description: str | None = None
        self.canonical_url: str | None = None
        self.breadcrumb: list[str] = []
        self.last_updated: str | None = None
        self._in_title = False
        self._in_json_ld = False
        self._json_ld = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "title" and self.title is None:
            self._in_title = True
            self.title = ""
        elif tag == "meta" and attributes.get("name") == "description":
            self.description = attributes.get("content")
        elif tag == "link" and attributes.get("rel") == "canonical":
            self.canonical_url = attributes.get("href")
        elif tag == "script" and attributes.get("type") == "application/ld+json":
            self._in_json_ld = True
            self._json_ld = ""
        elif tag == "time" and self.last_updated is None:
            self.last_updated = attributes.get("datetime")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag == "script" and self._in_json_ld:
            self._in_json_ld = False
            self._read_breadcrumb(self._json_ld)

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif self._in_json_ld:
            self._json_ld += data

    def _read_breadcrumb(self, json_ld: str) -> None:
        try:
            data = json.loads(json_ld)
        except json.JSONDecodeError:
            return
        if isinstance(data, dict) and data.get("@type") == "BreadcrumbList":
            items = sorted(data.get("itemListElement", []), key=lambda item: item.get("position", 0))
            self.breadcrumb = [item["name"] for item in items if "name" in item]


def parse_page_head(html: str) -> dict:
    """Extract the metadata of a documentation page.

    Args:
        html: HTML of the page

    Returns:
        Dictionary with title, description, canonical_url, breadcrumb and last_updated (date or None)

    """
    parser = _PageHeadParser()
    parser.feed(html)
    parser.close()
    title = _TITLE_SUFFIX.sub("", parser.title or "").strip() or None
    last_updated = date.fromisoformat(parser.last_updated[:10]) if parser.last_updated else None
    return {
        "title": title,
        "description": parser.description,
        "canonical_url": parser.canonical_url,
        "breadcrumb": parser.breadcrumb,
        "last_updated": last_updated,
    }


@backoff.on_exception(backoff.expo, requests.RequestException, max_tries=3)
def _get(session: requests.Session, url: str) -> str:
    response = session.get(url, timeout=30)
    response.raise_for_status()
    return response.text


def fetch_docs_metadata(
    prefixes: dict[str, str] = PRODUCT_AREAS, limit: int = 50, delay_seconds: float = 0.5
) -> list[dict]:
    """Fetch the metadata of the documentation pages under the given prefixes.

    Args:
        prefixes: Path prefix relative to DOCS_ROOT mapped to its product area, in priority order
        limit: Maximum number of pages to fetch
        delay_seconds: Pause between page requests, to be polite to the docs site

    Returns:
        List of page metadata dictionaries

    """
    with requests.Session() as session:
        pages = parse_sitemap(_get(session, SITEMAP_URL), prefixes, limit)
        logger.info(f"Selected {len(pages)} pages from the sitemap")

        records = []
        for page in pages:
            records.append({**page, **parse_page_head(_get(session, page["url"]))})
            time.sleep(delay_seconds)
    return records
