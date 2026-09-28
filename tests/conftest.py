"""Shared fixtures: a small synthetic meeting + document with a real tiny PDF."""

import hashlib
from pathlib import Path

import pytest
from django.core.management import call_command

from core.models import Authority, Body, Document, DocumentAssociation, DocumentVersion, Meeting

FIXTURES = Path(__file__).parent / "fixtures"


def _make_pdf_bytes() -> bytes:
    """Minimal but valid single-page PDF with extractable Swedish text."""
    # Written as a raw PDF so the test suite has no PDF-generation dependency.
    # Text must exceed the "usable text" threshold so no OCR is triggered.
    # WinAnsiEncoding maps bytes 0xE5 -> å, 0xE4 -> ä and 0xF6 -> ö.
    stream = (
        b"BT /F1 14 Tf 72 720 Td "
        b"(Sp\\345rv\\344gen till Stenhagen och Gr\\344nby behandlades vid m\\366tet) Tj ET"
    )
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
    xref_pos = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += (
        b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
        % (len(objects) + 1, xref_pos)
    )
    return bytes(out)


@pytest.fixture
def source_tree(db):
    """Uppsala authority + body + one meeting, seeded."""
    authority = Authority.objects.create(slug="uppsala-kommun", name="Uppsala kommun", kind="municipality")
    body = Body.objects.create(
        authority=authority,
        slug="kommunfullmaktige",
        name="Kommunfullmäktige",
        source_url="https://uppsala.se/kommun-och-politik/organisation/ledning/kommunfullmaktige/",
    )
    meeting = Meeting.objects.create(
        body=body,
        date="2025-01-27",
        title="Kommunfullmäktige 27 januari",
        source_url="https://uppsala.se/kommun-och-politik/organisation/ledning/kommunfullmaktige/moten/2025/27-januari/",
    )
    return authority, body, meeting


@pytest.fixture
def pdf_bytes():
    return _make_pdf_bytes()


@pytest.fixture
def stored_version(source_tree, pdf_bytes, settings):
    """A DocumentVersion with real bytes stored under ORIGINALS_DIR."""
    _, _, meeting = source_tree
    content = pdf_bytes
    sha = hashlib.sha256(content).hexdigest()
    stored = f"{sha[:2]}/{sha}.pdf"
    destination = settings.ORIGINALS_DIR / stored
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)

    document = Document.objects.create(
        title="Ärende 20: Detaljplan för kollektivtrafik",
        doc_type="arende",
        primary_source_url="https://uppsala.se/contentassets/abc/testarende.pdf",
        current_version=None,
    )
    DocumentAssociation.objects.create(
        document=document,
        meeting=meeting,
        source_context="meeting page: ärenden",
    )
    version = DocumentVersion.objects.create(
        document=document,
        sha256=sha,
        original_url=document.primary_source_url,
        stored_path=stored,
        size_bytes=len(content),
        processing_status=DocumentVersion.Status.PENDING,
    )
    document.current_version = version
    document.save(update_fields=["current_version"])
    return document, version


@pytest.fixture
def seed_command(db):
    def run():
        call_command("seed_sources", verbosity=0)

    return run