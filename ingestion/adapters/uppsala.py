"""Adapter for uppsala.se meeting pages (Uppsala kommun, kommunfullmäktige).

Structure (verified 2026-09 against the live site):
- Body page: /kommun-och-politik/organisation/ledning/kommunfullmaktige/
  Year filter: GET ?SelectedYear=<year> lists that year's meetings.
- Meeting page: .../moten/<year>/<date-slug>/ with sections:
  h2 "Kallelse och protokoll" and h2 "Ärenden"; each document block is an
  h3 heading (carrying the document title) followed by a PDF link
  (href to /contentassets/.../*.pdf, link text is just "Ladda ner dokument").
- Sub-items use numbers like "17.1." and agenda ranges like "7-12.".
- Older documents (2003-2010) live in a separate archive and are out of scope.
"""

from __future__ import annotations

import datetime
import re
from urllib.parse import urljoin, urlsplit

from selectolax.parser import HTMLParser

from .base import AgendaItemRef, DocumentRef, MeetingSnapshot, SourceAdapter

BASE_URL = "https://uppsala.se"
BODY_PATH = "/kommun-och-politik/organisation/ledning/kommunfullmaktige/"
MEETING_PATH_RE = re.compile(
    r"^/kommun-och-politik/organisation/ledning/kommunfullmaktige/moten/(\d{4})/([^/]+)/?$"
)

MONTHS = {
    "januari": 1, "februari": 2, "mars": 3, "april": 4, "maj": 5, "juni": 6,
    "juli": 7, "augusti": 8, "september": 9, "oktober": 10, "november": 11, "december": 12,
}
# "26-januari", "3---4-november-2025", "14–15-december"
SLUG_DATE_RE = re.compile(
    r"(\d{1,2})(?:[-–]{1,3}(\d{1,2}))?[-–](januari|februari|mars|april|maj|juni|juli|"
    r"augusti|september|oktober|november|december)\b"
)

ITEM_NUMBER_RE = re.compile(r"^(\d{1,2})(?:[-–](\d{1,2}))?\.\s+")
SUBITEM_NUMBER_RE = re.compile(r"^\d{1,2}\.\d{1,2}\.\s+")

_SVAR_PREFIXES = ("svar på ", "svar pa ", "svarpå ")


def _norm_text(node) -> str:
    return " ".join(node.text(deep=True, separator=" ").split())


def parse_slug_date(slug: str) -> datetime.date | None:
    match = SLUG_DATE_RE.search(slug)
    if not match:
        return None
    day = int(match.group(1))
    month = MONTHS[match.group(3)]
    return datetime.date(2000, month, day)  # year patched by caller


def parse_meeting_url(url: str) -> tuple[int, datetime.date | None] | None:
    """Return (year, date) parsed from a meeting page URL, or None."""
    match = MEETING_PATH_RE.match(urlsplit(url).path)
    if not match:
        return None
    year = int(match.group(1))
    return year, parse_slug_date(match.group(2))


_NUMBER_PREFIX_RE = re.compile(r"^\s*\d{1,2}(?:\.\d{1,2})?(?:[-–]\d{1,2}(?:\.\d{1,2})?)?\.\s+")


def _strip_number_prefix(title_lower: str) -> str:
    return _NUMBER_PREFIX_RE.sub("", title_lower, count=1)


def classify_document(title: str, section: str) -> tuple[str, str | None]:
    """Map a document heading to a Document.DocType value (+ revision label)."""
    t = title.lower()
    revision = "justerat" if "justerat" in t else None
    stripped = _strip_number_prefix(t)
    if "kallelse" in stripped:
        return "kallelse", revision
    if "föredragningslista" in stripped or "foredragningslista" in stripped:
        return "foredragningslista", revision
    if "protokoll" in stripped:
        return "protokoll", revision
    if "bilaga" in stripped:
        return "bilaga", revision
    if "yttrande" in stripped:
        return "yttrande", revision
    # "Svar på …" documents are the board's response within a case: always ärende,
    # even when the subject line contains "motion" or "interpellation".
    if stripped.startswith(_SVAR_PREFIXES):
        return "arende", revision
    if "interpellation" in stripped:
        return "interpellation", revision
    if "motion" in stripped:
        return "motion", revision
    if "fråga" in stripped or "fraga" in stripped:
        return "fraga", revision
    if section == "ärenden":
        return "arende", revision
    return "annat", revision


def normalize_url(href: str) -> str:
    absolute = urljoin(BASE_URL + "/", href)
    parts = urlsplit(absolute)
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if host != "uppsala.se":
        return absolute
    path = parts.path.rstrip("/")
    return f"{BASE_URL}{path}/" if parts.path.endswith("/") else f"{BASE_URL}{parts.path}"


def _is_source_pdf(href: str) -> bool:
    if ".pdf" not in href.lower():
        return False
    if href.startswith("docreader") or "docreader.readspeaker.com" in href:
        return False
    parts = urlsplit(urljoin(BASE_URL + "/", href))
    host = parts.netloc.lower()
    return host in ("uppsala.se", "www.uppsala.se", "")


class UppsalaAdapter(SourceAdapter):
    slug = "kommunfullmaktige"
    body_name = "Kommunfullmäktige"
    authority = {"slug": "uppsala-kommun", "name": "Uppsala kommun", "kind": "municipality"}
    body_source_url = BASE_URL + BODY_PATH

    def discover_meeting_urls(self, start: datetime.date, end: datetime.date) -> list[str]:
        years = sorted({start.year, end.year} | set(range(start.year, end.year + 1)))
        urls: list[str] = []
        for year in sorted(years):
            response = self.fetcher.get(f"{BASE_URL}{BODY_PATH}?SelectedYear={year}")
            tree = HTMLParser(response.content.decode("utf-8", errors="replace"))
            for anchor in tree.css("a[href]"):
                href = anchor.attributes.get("href") or ""
                absolute = normalize_url(href)
                parsed = parse_meeting_url(absolute)
                if not parsed or parsed[1] is None:
                    continue
                year_, date = parsed
                if start <= date.replace(year=year_) <= end:
                    urls.append(absolute)
        return sorted(set(urls))

    def fetch_meeting(self, url: str) -> MeetingSnapshot:
        response = self.fetcher.get(url)
        html = response.content.decode("utf-8", errors="replace")
        tree = HTMLParser(html)

        title = "Möte"
        for h1 in tree.css("h1"):
            title = _norm_text(h1) or title
            break

        parsed = parse_meeting_url(url)
        date = parsed[1].replace(year=parsed[0]) if parsed and parsed[1] else None

        section = ""
        current_heading: str | None = None
        heading_docs = 0
        agenda_items: list[AgendaItemRef] = []
        documents: list[DocumentRef] = []
        seen_urls: set[str] = set()

        # traverse() yields nodes in document order; tree.css("h2, h3, a[href]")
        # groups by selector and loses the heading->link adjacency we rely on.
        for node in tree.root.traverse():
            if node.tag not in ("h2", "h3", "a"):
                continue
            if node.tag == "a" and not (node.attributes.get("href") or ""):
                continue
            if node.tag == "h2":
                text = _norm_text(node)
                section = "ärenden" if text.lower().startswith("ärenden") else text.lower()
                current_heading = None
            elif node.tag == "h3":
                text = _norm_text(node)
                if not text:
                    continue
                current_heading = text
                heading_docs = 0
                if section == "ärenden":
                    sub = SUBITEM_NUMBER_RE.match(text)
                    if sub:
                        agenda_items.append(AgendaItemRef(None, None, text))
                    else:
                        num_match = ITEM_NUMBER_RE.match(text)
                        number = int(num_match.group(1)) if num_match else None
                        end = int(num_match.group(2)) if num_match and num_match.group(2) else None
                        stripped = ITEM_NUMBER_RE.sub("", text, count=1) if num_match else text
                        agenda_items.append(AgendaItemRef(number, end, stripped))
            elif node.tag == "a":
                href = node.attributes.get("href") or ""
                if not _is_source_pdf(href):
                    continue
                absolute = normalize_url(href)
                if absolute in seen_urls:
                    continue
                seen_urls.add(absolute)
                heading_docs += 1
                doc_title = current_heading or "Dokument"
                if heading_docs > 1:
                    doc_title = f"{doc_title} (del {heading_docs})"
                doc_type, revision = classify_document(doc_title, section)
                agenda_number = None
                if section == "ärenden" and current_heading:
                    num_match = ITEM_NUMBER_RE.match(current_heading)
                    if num_match and not SUBITEM_NUMBER_RE.match(current_heading):
                        agenda_number = int(num_match.group(1))
                documents.append(
                    DocumentRef(
                        url=absolute,
                        title=doc_title,
                        doc_type=doc_type,
                        source_context=f"meeting page: {section}",
                        agenda_number=agenda_number,
                        revision_label=revision,
                    )
                )

        return MeetingSnapshot(
            source_url=url,
            date=date,
            title=title,
            html=response.content,
            agenda_items=agenda_items,
            documents=documents,
        )


def get_adapter_class(slug: str):
    adapters = {cls.slug: cls for cls in (UppsalaAdapter,)}
    if slug not in adapters:
        raise ValueError(f"Unknown source adapter: {slug!r}. Available: {sorted(adapters)}")
    return adapters[slug]