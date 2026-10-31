"""Parse Databricks documentation pages into structured elements.

docs page HTML (Docusaurus)
   ↓ (parse_doc_page)
{"document": {"elements": [...]}}, shaped like ai_parse_document output

ai_parse_document reads PDF, image and Office files, not HTML. The page markup already carries
the structure, so it is read directly: headings, paragraphs and list items, code blocks with their
language (one source line per highlighted line, so YAML indentation is kept), tables, and images
as their alt text and URL.
"""

from html.parser import HTMLParser
from urllib.parse import urljoin

_TEXT_TAGS = {"p", "li", "dt", "dd", "blockquote"}
_HEADING_TAGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
# The pages are minified HTML5, which leaves out optional end tags (</p>, </li>, </td>). These
# start tags close an open paragraph, and these end tags close an open paragraph or list item.
_CLOSES_P = {"p", "div", "ul", "ol", "table", "pre", "dl", "blockquote", *_HEADING_TAGS}
_ENDS_CLOSING_P = {"div", "li", "ul", "ol", "dd", "blockquote", "td", "th"}
_ENDS_CLOSING_LI = {"ul", "ol"}
_ZERO_WIDTH = "\u200b"
# Containers of hidden copies: the "expand table" dialog repeats every table of the page.
_SKIPPED_CLASSES = {"table-expand-card", "table-expand-backdrop"}


def _normalize(text: str) -> str:
    """Collapse whitespace and drop the zero-width spaces of heading anchors."""
    return " ".join(text.replace(_ZERO_WIDTH, "").split())


class _ArticleParser(HTMLParser):
    """Collect the elements of the article body of a documentation page.

    HTMLParser calls the handle_* methods from inside feed(), once per tag and text run, so the
    elements are complete when feed() and close() return. Only the content inside the container
    (a div with the class theme-doc-markdown, or <article> as a fallback) is kept.
    """

    def __init__(self, page_url: str, container: tuple[str, str | None]) -> None:
        super().__init__(convert_charrefs=True)
        self.page_url = page_url
        self.container_tag, self.container_class = container
        self.elements: list[dict] = []
        self._depth = 0  # nesting depth of container_tag inside the container; 0 = outside
        self._block: tuple[str, list[str]] | None = None  # open text block: tag, text
        self._resume: str | None = None  # text block to reopen after a code block inside it
        self._code: tuple[str | None, list[str]] | None = None  # language, text
        self._table: list[list[str]] | None = None
        self._cell: list[str] | None = None
        self._skip = 0  # nesting depth of <div> inside a skipped container; 0 = not skipping

    def _add(self, element: dict) -> None:
        element["id"] = len(self.elements)
        self.elements.append(element)

    def _close_block(self) -> None:
        if self._block is None:
            return
        tag, parts = self._block
        content = _normalize("".join(parts))
        if content:
            if tag in _HEADING_TAGS:
                self._add({"type": "section_header", "level": _HEADING_TAGS[tag], "content": content})
            else:
                self._add({"type": "text", "content": content})
        self._block = None

    def _close_cell(self) -> None:
        if self._cell is not None and self._table:
            self._table[-1].append(_normalize("".join(self._cell)))
        self._cell = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = (attributes.get("class") or "").split()
        if self._depth == 0:
            if tag == self.container_tag and (self.container_class is None or self.container_class in classes):
                self._depth = 1
            return
        if tag == self.container_tag:
            self._depth += 1
        if self._skip:
            self._skip += tag == "div"
            return
        if tag == "div" and _SKIPPED_CLASSES.intersection(classes):
            self._skip = 1
            return

        if self._code is not None:
            if tag == "br":
                self._code[1].append("\n")
            return

        if self._block is not None:
            open_tag = self._block[0]
            if (open_tag in _HEADING_TAGS or open_tag == "p") and tag in _CLOSES_P:
                self._close_block()
            elif open_tag == "li" and tag in ("li", "pre", "table", *_HEADING_TAGS):
                if tag == "pre":
                    self._resume = "li"
                self._close_block()

        if tag == "pre":
            language = next((c.removeprefix("language-") for c in classes if c.startswith("language-")), None)
            self._code = (language, [])
        elif tag == "table":
            self._table = []
        elif self._table is not None:
            if tag == "tr":
                self._close_cell()
                self._table.append([])
            elif tag in ("td", "th"):
                self._close_cell()
                self._cell = []
        elif tag == "img":
            alt = _normalize(attributes.get("alt") or "")
            src = attributes.get("src")
            if alt and src:
                self._add({"type": "image", "content": alt, "url": urljoin(self.page_url, src)})
        elif self._block is None and (tag in _TEXT_TAGS or tag in _HEADING_TAGS):
            self._block = (tag, [])
        elif self._block is not None and (tag in _TEXT_TAGS or tag in ("br", "div")):
            # A <p> inside an <li> stays part of the list item, separated by a space.
            self._block[1].append(" ")

    def handle_endtag(self, tag: str) -> None:
        if self._depth == 0:
            return
        if tag == self.container_tag:
            self._depth -= 1
            if self._depth == 0:
                self._close_block()
                return
        if self._skip:
            self._skip -= tag == "div"
            return

        if self._code is not None:
            if tag == "pre":
                language, parts = self._code
                content = "".join(parts).replace(_ZERO_WIDTH, "").strip("\n")
                if content.strip():
                    self._add({"type": "code", "language": language, "content": content})
                self._code = None
                if self._resume:
                    self._block, self._resume = (self._resume, []), None
            return

        if self._table is not None:
            if tag in ("td", "th", "tr"):
                self._close_cell()
            elif tag == "table":
                self._close_cell()
                rows = [row for row in self._table if any(row)]
                if rows:
                    self._add({"type": "table", "content": _table_to_markdown(rows)})
                self._table = None
            return

        if self._block is not None:
            open_tag = self._block[0]
            if (
                tag == open_tag
                or (open_tag == "p" and tag in _ENDS_CLOSING_P)
                or (open_tag == "li" and tag in _ENDS_CLOSING_LI)
            ):
                self._close_block()

    def handle_data(self, data: str) -> None:
        if self._depth == 0 or self._skip:
            return
        if self._code is not None:
            self._code[1].append(data)
        elif self._cell is not None:
            self._cell.append(data)
        elif self._block is not None:
            self._block[1].append(data)


def _table_to_markdown(rows: list[list[str]]) -> str:
    """Render table rows as a Markdown table, the first row as the header."""
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
    lines += ["| " + " | ".join(row) + " |" for row in rows[1:]]
    return "\n".join(lines)


def parse_doc_page(html: str, page_url: str) -> dict:
    """Parse the article body of a documentation page into elements.

    Args:
        html: HTML of the page
        page_url: URL of the page, to resolve relative image links

    Returns:
        {"document": {"elements": [...]}}, each element with an id, a type (section_header, text,
        code, table or image) and content; headings carry a level, code a language, images a url.
        The elements are empty when the page has no article body.

    """
    for container in (("div", "theme-doc-markdown"), ("article", None)):
        parser = _ArticleParser(page_url, container)
        parser.feed(html)
        parser.close()
        if parser.elements:
            break
    return {"document": {"elements": parser.elements}}
