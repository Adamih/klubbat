"""Pipeline tests: IR extraction, idempotency, versioning, FTS sync."""

import hashlib

import pytest
from django.db import connection
from django.utils import timezone

from core.models import Document, DocumentPage, DocumentVersion, IngestionRun
from ingestion import pipeline
from ingestion.ir import IR_VERSION, _clean_markdown
from ingestion.management.commands.discover_meetings import _summary


pytestmark = pytest.mark.django_db


def test_clean_markdown_strips_xberg_annotations():
    raw = (
        "Kommunfullmäktiges protokoll måndagen (xberg:internal:font-size-pt: 18.96)\n\n"
        "Plats och tid (xberg:internal:font-size-pt: 15)\nStadshuset, klockan 13:15."
    )
    clean = _clean_markdown(raw)
    assert "xberg:internal" not in clean
    assert "Kommunfullmäktiges protokoll måndagen" in clean
    assert "Plats och tid\nStadshuset, klockan 13:15." in clean


def test_extraction_stores_markdown(stored_version):
    document, version = stored_version
    run = pipeline.begin_run("test")
    pipeline.process_versions(run=run, version_ids=[version.id])
    version.refresh_from_db()
    assert version.processing_status == DocumentVersion.Status.PROCESSED
    assert version.markdown.strip()
    assert version.ir_version == IR_VERSION
    assert "Spårvägen" in version.markdown  # Swedish diacritics survive extraction


def test_extraction_stores_page_text(stored_version):
    document, version = stored_version
    pipeline.process_versions(run=pipeline.begin_run("test"), version_ids=[version.id])
    pages = list(version.pages.order_by("number"))
    assert pages, "per-page text must be mirrored from the IR sidecar"
    assert pages[0].number == 1
    assert "Spårvägen" in pages[0].text


def test_find_page_matches_and_misses(stored_version):
    from core.textmatch import find_page

    document, version = stored_version
    pipeline.process_versions(run=pipeline.begin_run("test"), version_ids=[version.id])
    assert find_page(version.id, "Spårvägen till Stenhagen och Gränby behandlades vid mötet") == 1
    assert find_page(version.id, "Någon mening som inte finns i dokumentet alls.") is None
    assert find_page(version.id, "kort") is None  # needle below the minimum length


def test_process_is_idempotent(stored_version):
    document, version = stored_version
    run = pipeline.begin_run("test")
    pipeline.process_versions(run=run, version_ids=[version.id])
    version.refresh_from_db()
    first_markdown = version.markdown
    # Already-stamped versions are skipped entirely.
    stats_run = pipeline.begin_run("test")
    pipeline.process_versions(run=stats_run, version_ids=[])
    pipeline.process_versions(run=stats_run, statuses=[DocumentVersion.Status.PROCESSED])
    version.refresh_from_db()
    assert version.markdown == first_markdown


def test_sidecar_cache_reuses_extraction(stored_version, settings):
    document, version = stored_version
    run = pipeline.begin_run("test")
    pipeline.process_versions(run=run, version_ids=[version.id])
    # Requeue + force: the sidecar cache must satisfy reprocessing without re-extraction.
    version.processing_status = DocumentVersion.Status.PENDING
    version.save(update_fields=["processing_status"])
    pipeline.process_versions(run=run, version_ids=[version.id], force=True)
    version.refresh_from_db()
    assert version.processing_status == DocumentVersion.Status.PROCESSED
    assert version.markdown.strip()
    assert (settings.DERIVED_DIR / f"{version.sha256}.ir.json").is_file()


def test_fts_stays_in_sync(stored_version):
    document, version = stored_version
    run = pipeline.begin_run("test")
    pipeline.process_versions(run=run, version_ids=[version.id])
    with connection.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM search_documentfts")
        fts_rows = cursor.fetchone()[0]
    assert fts_rows == 1
    pipeline.rebuild_search_index()
    with connection.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM search_documentfts")
        assert cursor.fetchone()[0] == fts_rows


def test_changed_bytes_create_new_version(source_tree, pdf_bytes, settings):
    _, _, meeting = source_tree
    document = Document.objects.create(
        title="Protokoll kommunfullmäktige",
        doc_type="protokoll",
        primary_source_url="https://uppsala.se/contentassets/x/protokoll.pdf",
    )
    content_a = pdf_bytes
    content_b = pdf_bytes + b"\n% appended page"

    for content in (content_a, content_b):
        sha = hashlib.sha256(content).hexdigest()
        stored = f"{sha[:2]}/{sha}.pdf"
        destination = settings.ORIGINALS_DIR / stored
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        version, created = DocumentVersion.objects.get_or_create(
            document=document,
            sha256=sha,
            defaults={
                "original_url": document.primary_source_url,
                "stored_path": stored,
                "size_bytes": len(content),
                "retrieved_at": timezone.now(),
            },
        )
        document.current_version = version
        document.save(update_fields=["current_version"])

    assert document.versions.count() == 2  # old version preserved
    assert document.current_version.sha256 == hashlib.sha256(content_b).hexdigest()


def test_run_summary_and_finish():
    run = pipeline.begin_run("test-scope")
    run.failures = 1
    pipeline.finish_run(run)
    assert run.status == IngestionRun.Status.PARTIAL
    run2 = pipeline.begin_run("test-scope")
    pipeline.finish_run(run2)
    assert run2.status == IngestionRun.Status.SUCCESS
    assert "meetings=" in _summary(run2)
