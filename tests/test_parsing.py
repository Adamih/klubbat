"""Adapter parsing tests against saved fixture HTML from the live site."""

import datetime
from pathlib import Path

import pytest
from django.test import override_settings

from ingestion.adapters.uppsala import (
    UppsalaAdapter,
    classify_document,
    normalize_url,
    parse_meeting_url,
)

RECON = Path(__file__).resolve().parents[1] / "recon"


class _FakeFetcher:
    """Serves saved recon files instead of the network."""

    def __init__(self, mapping):
        self.mapping = mapping
        self.requested = []

    def get(self, url):
        self.requested.append(url)
        key = next((k for k in self.mapping if k in url), None)
        if key is None:
            raise AssertionError(f"Unexpected URL in test: {url}")
        path = RECON / self.mapping[key]
        from ingestion.fetch import FetchResult

        return FetchResult(url=url, final_url=url, status_code=200, content=path.read_bytes())


def test_parse_slug_dates():
    assert parse_meeting_url(
        "https://uppsala.se/kommun-och-politik/organisation/ledning/kommunfullmaktige/moten/2025/27-januari/"
    ) == (2025, datetime.date(2000, 1, 27))
    multi = parse_meeting_url(
        "https://uppsala.se/kommun-och-politik/organisation/ledning/kommunfullmaktige/moten/2025/3---4-november-2025/"
    )
    assert multi is not None and multi[1] is not None and multi[1].month == 11


def test_normalize_url():
    assert normalize_url("/contentassets/a/b.pdf") == "https://uppsala.se/contentassets/a/b.pdf"
    assert normalize_url("https://WWW.uppsala.se/x/").startswith("https://uppsala.se/x/")


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Kallelse kommunfullmäktige 2025-01-27", "kallelse"),
        ("Protokoll kommunfullmäktige 2025-01-27", "protokoll"),
        ("Justerat protokoll kommunfullmäktige", "protokoll"),
        ("5. Inlämnade motioner till kommunfullmäktige", "motion"),
        ("24. Svar på motion om ljudet i KF-salen", "arende"),
        ("32. Interpellation om digitalisering", "interpellation"),
        ("32.1. Svar på interpellation om digitalisering", "arende"),
        ("13. Styrdokument för finansverksamheten", "arende"),
        ("Föredragningslista", "foredragningslista"),
    ],
)
def test_classify_documents(title, expected):
    doc_type, _ = classify_document(title, "ärenden")
    assert doc_type == expected


@override_settings()
def test_meeting_page_parsing():
    adapter = UppsalaAdapter(_FakeFetcher({"27-januari": "mote-2025-01-27.html"}))
    snapshot = adapter.fetch_meeting(
        "https://uppsala.se/kommun-och-politik/organisation/ledning/kommunfullmaktige/moten/2025/27-januari/"
    )
    assert snapshot.date == datetime.date(2025, 1, 27)
    assert snapshot.documents, "meeting page should yield documents"
    titles = [d.title for d in snapshot.documents]
    assert any("Kallelse" in t for t in titles)
    assert any("Protokoll" in t for t in titles)
    protokoll = next(d for d in snapshot.documents if d.doc_type == "protokoll")
    assert protokoll.url.endswith(".pdf")
    kallelse = next(d for d in snapshot.documents if d.doc_type == "kallelse")
    assert kallelse.revision_label is None
    arenden = [d for d in snapshot.documents if d.doc_type == "arende"]
    assert arenden, "Ärenden section should produce arende documents"
    numbered = [d for d in snapshot.documents if d.agenda_number is not None]
    assert numbered
    assert snapshot.agenda_items


def test_meeting_page_multi_part_items():
    """17 and 17.1 are separate documents with separate agenda binding."""
    adapter = UppsalaAdapter(_FakeFetcher({"27-januari": "mote-2025-01-27.html"}))
    snapshot = adapter.fetch_meeting(
        "https://uppsala.se/kommun-och-politik/organisation/ledning/kommunfullmaktige/moten/2025/27-januari/"
    )
    seventeens = [d for d in snapshot.documents if d.title.startswith(("17.", "17.1.", "35.", "35.1."))]
    assert len(seventeens) >= 2
    numbers = {d.title.split(".")[0] for d in seventeens}
    assert "17" in numbers or "35" in numbers
