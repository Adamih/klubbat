"""Source adapter interface.

Each publishing system (municipality website) gets one adapter that knows how to
discover meeting pages and parse documents out of them. Source-specific selectors
live inside the adapter, never in the pipeline.
"""

from __future__ import annotations

import abc
import datetime
from dataclasses import dataclass, field


@dataclass
class AgendaItemRef:
    agenda_number: int | None
    agenda_number_end: int | None
    title: str


@dataclass
class DocumentRef:
    url: str
    title: str
    doc_type: str  # core.Document.DocumentType value
    source_context: str  # e.g. "meeting page: arenden"
    agenda_number: int | None = None
    revision_label: str | None = None


@dataclass
class MeetingSnapshot:
    source_url: str
    date: datetime.date | None
    title: str
    html: bytes
    agenda_items: list[AgendaItemRef] = field(default_factory=list)
    documents: list[DocumentRef] = field(default_factory=list)


class SourceAdapter(abc.ABC):
    slug: str  # body slug, e.g. "kommunfullmaktige"
    body_name: str
    authority: dict  # {"slug": ..., "name": ..., "kind": ...}

    def __init__(self, fetcher):
        self.fetcher = fetcher

    @abc.abstractmethod
    def discover_meeting_urls(self, start: datetime.date, end: datetime.date) -> list[str]:
        """Return meeting page URLs whose meeting date falls in [start, end]."""

    @abc.abstractmethod
    def fetch_meeting(self, url: str) -> MeetingSnapshot:
        """Fetch a meeting page and parse its agenda items and documents."""
