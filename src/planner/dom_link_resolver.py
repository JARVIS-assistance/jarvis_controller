from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse


@dataclass(frozen=True)
class ResolvedLink:
    href: str
    title: str
    score: int


def resolve_link_from_dom_output(
    output: dict[str, Any],
    *,
    query: str,
) -> ResolvedLink | None:
    """Choose the best link from a client-provided DOM/link snapshot."""
    query_tokens = _tokens(query)
    if not query_tokens:
        return None

    links = _extract_links(output)
    best: ResolvedLink | None = None
    for link in links:
        href = str(link.get("href") or link.get("url") or "").strip()
        if not href.startswith(("http://", "https://")):
            continue
        title = _link_title(link)
        if not title and _is_internal_or_low_value(href):
            continue
        score = _score_link(query_tokens=query_tokens, title=title, href=href)
        if score <= 0:
            continue
        candidate = ResolvedLink(href=href, title=title, score=score)
        if best is None or candidate.score > best.score:
            best = candidate
    return best


def _extract_links(output: dict[str, Any]) -> list[dict[str, Any]]:
    raw = output.get("links")
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    dom = output.get("dom")
    if isinstance(dom, dict):
        raw = dom.get("links")
        if isinstance(raw, list):
            return [item for item in raw if isinstance(item, dict)]
    page = output.get("page")
    if isinstance(page, dict):
        raw = page.get("links")
        if isinstance(raw, list):
            return [item for item in raw if isinstance(item, dict)]
    return []


def _link_title(link: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("text", "title", "ariaLabel", "aria_label", "label"):
        value = link.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    return " ".join(parts)


def _score_link(*, query_tokens: set[str], title: str, href: str) -> int:
    haystack = f"{title} {href}".lower()
    haystack_tokens = _tokens(haystack)
    score = 0
    score += 10 * len(query_tokens & haystack_tokens)
    if title and all(token in haystack for token in query_tokens):
        score += 20
    if _is_internal_or_low_value(href):
        score -= 12
    if title:
        score += 3
    if _looks_like_primary_result(href):
        score += 2
    return score


def _tokens(text: str) -> set[str]:
    normalized = re.sub(r"[^\w가-힣]+", " ", text.lower())
    return {token for token in normalized.split() if len(token) >= 2}


def _is_internal_or_low_value(href: str) -> bool:
    parsed = urlparse(href)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    if "google." in host and path in {"/search", "/preferences", "/settings"}:
        return True
    if "google." in host and path.startswith(("/imgres", "/maps", "/shopping")):
        return True
    if "webcache.googleusercontent.com" in host:
        return True
    return False


def _looks_like_primary_result(href: str) -> bool:
    parsed = urlparse(href)
    return bool(parsed.netloc and parsed.path and parsed.path != "/")
