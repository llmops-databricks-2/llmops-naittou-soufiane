"""Sources on every answer: the prompt asks for links, but GPT-OSS doesn't reliably give them.

answer + the turn's tool outputs
   └─ add_sources: citation markers made links or removed; the searched pages listed if the answer has no link
"""

import json
import re

# GPT-OSS cites like its browsing tool, 【0†L9-L19】 or 【https://...】: the first means nothing to a reader
CITATION_MARKER = re.compile(r"\s*【([^】]*)】")
URL = re.compile(r"https://\S+")
MAX_SOURCES = 5


def searched_pages(tool_outputs: list[str]) -> list[tuple[str, str]]:
    """Return the distinct (title, url) pages of search_docs results, best ranked first."""
    pages: dict[str, str] = {}
    for output in tool_outputs:
        try:
            results = json.loads(output)
        except json.JSONDecodeError:
            continue
        if isinstance(results, list):
            for result in results:
                if isinstance(result, dict) and result.get("url"):
                    pages.setdefault(result["url"], result.get("title") or result["url"])
    return [(title, url) for url, title in pages.items()]


def _marker_to_link(match: re.Match) -> str:
    url = URL.search(match.group(1))
    return f" ([source]({url.group(0)}))" if url else ""


def add_sources(answer: str, tool_outputs: list[str]) -> str:
    """Add the answer's sources.

    Citation markers holding a URL become links and the others are dropped; when the answer then links to
    no page, the searched pages are listed.
    """
    answer = CITATION_MARKER.sub(_marker_to_link, answer)
    pages = searched_pages(tool_outputs)[:MAX_SOURCES]
    if "https://" in answer or not pages:
        return answer
    return answer.rstrip() + "\n\nPages searched for this answer:\n" + "\n".join(f"- [{t}]({u})" for t, u in pages)
