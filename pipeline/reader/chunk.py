"""
chunk.py - decides which pages of a long document get sent to the AI.

In plain English: the AI can only read so much at once (and every word costs money). If a
document is longer than the limits in reader.yaml (max_chars / max_pages), we send the first
3 pages plus the pages that mention important words (mortgage, lien, auction...). Short
documents are sent whole. Whenever we cut something out, we say so in a note that ends up
in review_reason, so a person knows the AI did not see everything.
"""
from __future__ import annotations

import re

KEYWORDS = ["mortgage", "borrower", "lender", "receiver", "foreclosure", "lien", "claimant",
            "parcel", "sale", "auction", "maturity", "opening", "leasing", "construction"]

_PAGE_RE = re.compile(r"^\[PAGE (\d+)\]\s*$", re.M)


def split_pages(text: str) -> list[tuple[int, str]]:
    """Cut text at its [PAGE n] markers -> [(page_number, page_text), ...]."""
    marks = list(_PAGE_RE.finditer(text))
    if not marks:
        return [(1, text.strip())]
    pages = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        pages.append((int(m.group(1)), text[m.end():end].strip()))
    return pages


def join_pages(pages: list[tuple[int, str]]) -> str:
    return "\n\n".join(f"[PAGE {n}]\n{t}" for n, t in pages)


def _keyword_score(page_text: str) -> int:
    low = page_text.lower()
    return sum(low.count(k) for k in KEYWORDS)


def _ranges(numbers: list[int]) -> str:
    """[1,2,3,7,8,12] -> '1-3, 7-8, 12'"""
    out, start = [], None
    for i, n in enumerate(numbers):
        if start is None:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            out.append((start, prev))
            start = prev = n
        if i == len(numbers) - 1:
            out.append((start, prev))
    return ", ".join(f"{a}" if a == b else f"{a}-{b}" for a, b in out)


def select_text(text: str, max_chars: int, max_pages: int, first_pages: int = 3) -> tuple[str, str | None]:
    """
    Returns (text_to_send, note). note is None when nothing was cut, otherwise a sentence like
    "truncated: sent pages 1-3, 12 of 40".
    """
    pages = split_pages(text)
    if len(text) <= max_chars and len(pages) <= max_pages:
        return text, None

    # A single huge page (or a file with no page markers): just cut it at the limit.
    if len(pages) == 1:
        return text[:max_chars], f"truncated: only the first {max_chars:,} of {len(text):,} characters were read"

    chosen: dict[int, str] = {}
    used = 0

    def try_add(num: int, body: str) -> bool:
        nonlocal used
        cost = len(body) + 20                         # a little room for the [PAGE n] marker
        if len(chosen) >= max_pages or used + cost > max_chars:
            return False
        chosen[num] = body
        used += cost
        return True

    for num, body in pages[:first_pages]:             # always the opening pages...
        try_add(num, body)
    rest = sorted(pages[first_pages:], key=lambda p: -_keyword_score(p[1]))
    for num, body in rest:                            # ...then the pages that mention key words
        if _keyword_score(body) == 0:
            break
        try_add(num, body)

    sent = sorted(chosen)
    kept = [(n, chosen[n]) for n in sent]
    # If even the first pages were too big for the limit, cut the joined text.
    result = join_pages(kept)[:max_chars]
    note = f"truncated: sent pages {_ranges(sent)} of {len(pages)}"
    return result, note
