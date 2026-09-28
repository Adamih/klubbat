"""FTS query construction and search service tests."""

import hashlib

import pytest
from django.db import connection
from django.utils import timezone

from core.models import Chunk, Document, DocumentAssociation, DocumentVersion, Meeting
from ingestion import pipeline as pl
from search import service
from search.query import build_match_query

pytestmark = pytest.mark.django_db


def test_plain_words():
    assert build_match_query("spårväg Stenhagen") == "spårväg Stenhagen"


def test_prefix_wildcard():
    assert build_match_query("spårv*") == "spårv*"


def test_quoted_phrase():
    assert build_match_query('"plan B för spårvägen"') == '"plan B för spårvägen"'


def test_fts_syntax_is_neutralized():
    # Operators and FTS keywords must not reach the MATCH expression.
    assert build_match_query("text: NEAR((a,b)) AND") == "text a b"
    assert build_match_query("cola* OR \"x\"*") == '"x" cola*'


def test_diacritics_preserved():
    assert "ä" in build_match_query("kommunfullmäktige")
    assert build_match_query("GRÄNBY") == "GRÄNBY"


def test_search_groups_by_document_and_respects_filters(stored_version, source_tree):
    document, version = stored_version
    run = pl.begin_run("test")
    pl.process_versions(run=run, version_ids=[version.id])

    match_expr = build_match_query("Stenhagen")
    data = service.search(match_expr, limit=10)
    assert data["total"] >= 1
    row = data["results"][0]
    assert row["document_id"] == document.id
    assert row["page_start"] >= 1
    assert "<mark>" in row["snippet_html"]
    assert "Spårvägen" in row["snippet_html"] or "Stenhagen" in row["snippet_html"]

    # Filters that do not match must return zero.
    empty = service.search(match_expr, doc_type="kallelse")
    assert empty["total"] == 0
    empty = service.search(match_expr, date_from="2030-01-01")
    assert empty["total"] == 0


def test_old_versions_are_not_searchable(source_tree, pdf_bytes, settings):
    """A superseded version's chunks must not surface in default search."""
    _, _, meeting = source_tree
    document = Document.objects.create(
        title="Ärende med två versioner", doc_type="arende",
        primary_source_url="https://uppsala.se/contentassets/two/ver.pdf",
    )
    DocumentAssociation.objects.create(document=document, meeting=meeting)
    contents = {"old": pdf_bytes, "new": pdf_bytes + b"\n% v2"}
    versions = {}
    for label, content in contents.items():
        sha = hashlib.sha256(content).hexdigest()
        stored = f"{sha[:2]}/{sha}.pdf"
        destination = settings.ORIGINALS_DIR / stored
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        version = DocumentVersion.objects.create(
            document=document, sha256=sha, original_url=document.primary_source_url,
            stored_path=stored, processing_status=DocumentVersion.Status.PENDING,
            retrieved_at=timezone.now(),
        )
        versions[label] = version
    document.current_version = versions["new"]
    document.save(update_fields=["current_version"])

    run = pl.begin_run("test")
    pl.process_versions(run=run, version_ids=[v.id for v in versions.values()])

    # Both versions have chunks, but only the current version's count.
    all_chunks = Chunk.objects.filter(document_version__document=document).count()
    current_chunks = versions["new"].chunks.count()
    assert all_chunks > current_chunks >= 1

    data = service.search(build_match_query("Spårvägen"))
    doc_rows = [r for r in data["results"] if r["document_id"] == document.id]
    for row in doc_rows:
        assert row["version_id"] == versions["new"].id
