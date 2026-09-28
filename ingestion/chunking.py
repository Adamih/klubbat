"""Chunking: page texts -> paragraph-based chunks with page references.

Agenda-item boundaries are detected conservatively: a paragraph only re-binds the
current item when its first line matches a heading whose number is a known agenda
number of the meeting. Otherwise the paragraph/page fallback applies.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass

from .extract import PageText

log = logging.getLogger(__name__)

CHUNKER_VERSION = "paragraph-v1"

# "§ 6" or "6. Stor bokstav" — the uppercase requirement keeps numbered lists out.
HEADING_RE = re.compile(r"^\s*(?:§\s*(\d{1,3})|(\d{1,3})\.\s+[A-ZÄÖÅ])")
MIN_MERGED_CHARS = 40
LONG_CHUNK_CHARS = 2400
TARGET_CHUNK_CHARS = 1200


@dataclass
class ChunkDraft:
    text: str
    page_start: int
    page_end: int
    agenda_number: int | None = None
    sequence: int = 0


def _repeated_edge_lines(pages: list[PageText]) -> set[str]:
    """Exact-match lines recurring at the top/bottom of >= half the pages are headers/footers."""
    if len(pages) < 4:
        return set()
    counts: Counter[str] = Counter()
    for page in pages:
        lines = [ln.strip() for ln in page.text.splitlines() if ln.strip()]
        edges = lines[:2] + lines[-2:]
        for line in set(edges):
            counts[line] += 1
    threshold = max(2, round(0.5 * len(pages)))
    return {line for line, count in counts.items() if count >= threshold}


def _split_long(text: str) -> list[str]:
    sentences = re.split(r"(?<=[.!?])\s+", text)
    parts: list[str] = []
    buf = ""
    for sentence in sentences:
        if buf and len(buf) + len(sentence) + 1 > TARGET_CHUNK_CHARS:
            parts.append(buf)
            buf = sentence
        else:
            buf = f"{buf} {sentence}".strip()
    if buf:
        parts.append(buf)
    return parts or [text]


def build_chunks(pages: list[PageText], agenda_numbers: set[int] | None = None) -> list[ChunkDraft]:
    repeated = _repeated_edge_lines(pages)
    drafts: list[ChunkDraft] = []
    current_item: int | None = None

    for page in pages:
        lines = page.text.splitlines()
        kept: list[str] = []
        for idx, line in enumerate(lines):
            stripped = line.strip()
            if not stripped:
                kept.append(line)
                continue
            if stripped in repeated and (idx < 2 or idx >= len(lines) - 2):
                continue
            kept.append(line)

        paragraphs: list[list[str]] = []
        buf: list[str] = []
        for line in kept:
            if not line.strip():
                if buf:
                    paragraphs.append(buf)
                    buf = []
            else:
                buf.append(line.strip())
        if buf:
            paragraphs.append(buf)
        if not paragraphs:
            continue

        # Merge very short fragments (page numbers, stray headings) into the next paragraph.
        merged: list[str] = []
        carry = ""
        for para_lines in paragraphs:
            text = " ".join(para_lines)
            if carry:
                text = f"{carry} {text}"
                carry = ""
            if len(text) < MIN_MERGED_CHARS:
                carry = text
                continue
            merged.append(text)
        if carry:
            if merged:
                merged[-1] = f"{merged[-1]} {carry}"
            else:
                merged.append(carry)

        for text in merged:
            parts = _split_long(text) if len(text) > LONG_CHUNK_CHARS else [text]
            for part in parts:
                match = HEADING_RE.match(part)
                if match and agenda_numbers:
                    num = int(match.group(1) or match.group(2))
                    if num in agenda_numbers:
                        current_item = num
                drafts.append(
                    ChunkDraft(text=part, page_start=page.number, page_end=page.number, agenda_number=current_item)
                )

    for sequence, draft in enumerate(drafts, start=1):
        draft.sequence = sequence
    log.info("Built %d chunks from %d pages", len(drafts), len(pages))
    return drafts