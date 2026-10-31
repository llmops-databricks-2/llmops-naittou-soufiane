"""Chunking strategies: section chunks keep code blocks whole, fixed-size windows overlap."""

import pytest

from docs_curator.chunking import (
    breaks_code_block,
    contextual_text,
    element_to_markdown,
    fixed_size_chunks,
    section_chunks,
)

YAML = "targets:\n  dev:\n    mode: development"
ELEMENTS = [
    {"type": "section_header", "level": 1, "content": "Deployment modes"},
    {"type": "text", "content": "Bundles support two modes."},
    {"type": "section_header", "level": 2, "content": "Development mode"},
    {"type": "text", "content": "Set the mode on the target:"},
    {"type": "code", "language": "yaml", "content": YAML},
    {"type": "section_header", "level": 4, "content": "Notes"},
    {"type": "text", "content": "A level 4 heading stays inside its section."},
    {"type": "section_header", "level": 3, "content": "Presets"},
    {"type": "image", "content": "Deploy to dev dialog", "url": "https://docs/img.png"},
]


def test_sections_start_at_level_1_to_3_headings() -> None:
    """Each h1-h3 starts a chunk named by its heading path below the page title."""
    chunks = section_chunks(ELEMENTS)

    assert [chunk["section"] for chunk in chunks] == ["", "Development mode", "Development mode > Presets"]
    assert [chunk["has_code"] for chunk in chunks] == [False, True, False]
    assert "#### Notes" in chunks[1]["text"]
    assert chunks[2]["text"] == "[Image: Deploy to dev dialog](https://docs/img.png)"


def test_code_block_is_fenced_with_its_language() -> None:
    """Code renders as a fenced block, so a chunk shows answer-ready YAML."""
    assert element_to_markdown(ELEMENTS[4]) == f"```yaml\n{YAML}\n```"


def test_long_section_splits_at_element_boundaries() -> None:
    """A section longer than max_chars becomes several chunks, none cutting the code block."""
    elements = [{"type": "section_header", "level": 2, "content": "Long"}]
    elements += [{"type": "text", "content": "x" * 60}] * 3 + [ELEMENTS[4]]

    chunks = section_chunks(elements, max_chars=150)

    assert len(chunks) > 1
    assert all(len(chunk["text"]) <= 150 for chunk in chunks)
    assert not any(breaks_code_block(chunk["text"]) for chunk in chunks)
    assert {chunk["section"] for chunk in chunks} == {"Long"}


def test_oversized_code_block_splits_at_line_breaks_into_fenced_pieces() -> None:
    """A code block longer than max_chars is split between lines, each piece fenced."""
    code = {"type": "code", "language": "yaml", "content": "\n".join(f"key_{i}: value_{i}" for i in range(30))}

    chunks = section_chunks([code], max_chars=120)

    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk["text"].startswith("```yaml\n") and chunk["text"].endswith("\n```")
        assert not breaks_code_block(chunk["text"])


def test_oversized_table_splits_between_rows_repeating_the_header() -> None:
    """A table longer than max_chars is split between rows, each piece a table with the header."""
    header = "| Resource | Description |\n|---|---|"
    rows = [f"| resource_{i} | {'d' * 30} |" for i in range(20)]
    table = {"type": "table", "content": "\n".join([header, *rows])}

    chunks = section_chunks([table], max_chars=300)

    assert len(chunks) > 1
    assert all(chunk["text"].startswith(header + "\n") and len(chunk["text"]) <= 300 for chunk in chunks)
    assert sum(chunk["text"].count("| resource_") for chunk in chunks) == len(rows)


def test_fixed_size_chunks_overlap_and_cover_the_text() -> None:
    """Windows of chunk_size share overlap characters and together cover the text."""
    text = "".join(str(i % 10) for i in range(250))

    chunks = fixed_size_chunks(text, chunk_size=100, overlap=20)

    assert [len(chunk) for chunk in chunks] == [100, 100, 90]
    assert chunks[0][-20:] == chunks[1][:20]
    assert chunks[-1].endswith(text[-10:])


def test_fixed_size_chunks_reject_overlap_not_below_size() -> None:
    """An overlap as large as the chunk would never advance."""
    with pytest.raises(ValueError, match="overlap"):
        fixed_size_chunks("text", chunk_size=10, overlap=10)


def test_contextual_text_prefixes_title_and_section() -> None:
    """A chunk carries its page title and section path."""
    assert (
        contextual_text("Deployment modes", "Development mode", "Body") == "Deployment modes > Development mode\n\nBody"
    )
    assert contextual_text("Deployment modes", "", "Body") == "Deployment modes\n\nBody"
