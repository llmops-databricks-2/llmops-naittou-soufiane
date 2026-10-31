"""Chunk parsed documentation pages.

parsed page elements (doc_pages.parse_doc_page)
   ├─ section_chunks: one chunk per section (h1-h3), split at element boundaries,
   │                  code blocks kept whole (default strategy)
   └─ fixed_size_chunks: fixed character windows with overlap over the whole page
                         (alternative strategy, for comparison)

Both strategies emit Markdown: code blocks are fenced with their language, so a chunk shows
an answer-ready YAML snippet.
"""

import re

# GTE Large embeds at most 512 tokens (about 2,000 characters of English text); longer input is
# truncated. Sections stay below that with room for the title and section prefix.
DEFAULT_MAX_CHARS = 1500
SECTION_LEVEL = 3  # h1-h3 start a new section; h4-h6 stay inside it

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def element_to_markdown(element: dict) -> str:
    """Render one parsed element as Markdown."""
    content = element["content"]
    if element["type"] == "section_header":
        return "#" * element.get("level", 2) + " " + content
    if element["type"] == "code":
        return f"```{element.get('language') or ''}\n{content}\n```"
    if element["type"] == "image":
        return f"[Image: {content}]({element['url']})"
    return content


def page_markdown(elements: list[dict]) -> str:
    """Render a whole page as Markdown."""
    return "\n\n".join(element_to_markdown(element) for element in elements)


def _split_oversized(element: dict, max_chars: int) -> list[str]:
    """Split one element longer than max_chars.

    Code splits at line breaks, tables between rows (each piece repeating the header), text at
    sentence ends.
    """
    if element["type"] == "code":
        fence = f"```{element.get('language') or ''}\n"
        units, joiner, wrap = element["content"].split("\n"), "\n", lambda body: f"{fence}{body}\n```"
        budget = max_chars - len(fence) - 4
    elif element["type"] == "table":
        lines = element["content"].split("\n")
        header = "\n".join(lines[:2])
        units, joiner, wrap = lines[2:], "\n", lambda body: f"{header}\n{body}"
        budget = max_chars - len(header) - 1
    else:
        units, joiner, wrap = _SENTENCE_END.split(element["content"]), " ", lambda body: body
        budget = max_chars

    pieces, current = [], ""
    for unit in units:
        candidate = f"{current}{joiner}{unit}" if current else unit
        if current and len(candidate) > budget:
            pieces.append(wrap(current))
            current = unit
        else:
            current = candidate
    if current:
        pieces.append(wrap(current))
    return pieces


def section_chunks(elements: list[dict], max_chars: int = DEFAULT_MAX_CHARS) -> list[dict]:
    """Chunk a page by section.

    A heading of level 1-3 starts a new section. Elements are added to the section's chunk until
    the next one would pass max_chars; a single element longer than max_chars is split on its own.

    Args:
        elements: Parsed page elements
        max_chars: Maximum characters of a chunk body

    Returns:
        List of {"section", "text", "has_code"}, section being the heading path ("A > B")

    """
    chunks: list[dict] = []
    headings: dict[int, str] = {}
    body: list[str] = []
    has_code = False

    def emit() -> None:
        nonlocal body, has_code
        if body:
            section = " > ".join(headings[level] for level in sorted(headings) if level > 1)
            chunks.append({"section": section, "text": "\n\n".join(body), "has_code": has_code})
        body, has_code = [], False

    for element in elements:
        if element["type"] == "section_header" and element.get("level", 2) <= SECTION_LEVEL:
            emit()
            level = element.get("level", 2)
            headings = {k: v for k, v in headings.items() if k < level}
            headings[level] = element["content"]
            continue

        markdown = element_to_markdown(element)
        pieces = [markdown] if len(markdown) <= max_chars else _split_oversized(element, max_chars)
        for piece in pieces:
            if body and len("\n\n".join([*body, piece])) > max_chars:
                emit()
            body.append(piece)
            has_code = has_code or element["type"] == "code"
    emit()
    return chunks


def fixed_size_chunks(text: str, chunk_size: int = 1000, overlap: int = 150) -> list[str]:
    """Split text into fixed-size character windows that overlap.

    Args:
        text: Text to chunk
        chunk_size: Characters per chunk
        overlap: Characters shared by consecutive chunks

    Returns:
        List of text chunks

    """
    if overlap >= chunk_size:
        raise ValueError("overlap must be smaller than chunk_size")
    step = chunk_size - overlap
    return [text[start : start + chunk_size] for start in range(0, max(len(text) - overlap, 1), step)]


def breaks_code_block(chunk: str) -> bool:
    """Whether a chunk cuts a fenced code block (an odd number of ``` fences)."""
    return chunk.count("```") % 2 == 1


def contextual_text(title: str, section: str, body: str) -> str:
    """Prefix a chunk with its page title and section, so it is retrievable on its own."""
    heading = f"{title} > {section}" if section else title
    return f"{heading}\n\n{body}"
