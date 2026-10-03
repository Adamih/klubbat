"""Text-to-page matching over DocumentPage rows.

Anchors (vote outcome sentences, decision texts, search snippets) are located
on their version's pages by normalized containment: markdown decoration is
stripped from both sides and whitespace is collapsed, since the whole-document
markdown and the per-page IR text format text runs slightly differently.
"""

from __future__ import annotations

import re

DECO_RE = re.compile(r"[*`#]+")
WS_RE = re.compile(r"\s+")

NEEDLE_CHARS = 80


def normalize_for_match(text: str) -> str:
    return WS_RE.sub(" ", DECO_RE.sub(" ", text)).strip()


def find_page(version_id: int, needle: str) -> int | None:
    """The page number whose text contains the needle, or None.

    Matches on the needle's leading characters; page order makes the first
    occurrence win, which is the page where the anchor starts.
    """
    from core.models import DocumentPage

    target = normalize_for_match(needle)[:NEEDLE_CHARS]
    if len(target) < 20:
        return None
    for page in DocumentPage.objects.filter(document_version_id=version_id).order_by("number"):
        if target in normalize_for_match(page.text):
            return page.number
    return None
