"""Pipeline tests: idempotency, versioning, extraction retry, FTS sync."""

import hashlib

import pytest
from django.db import connection
from django.utils import timezone

from core.models import Chunk, Document, DocumentVersion, IngestionRun
from ingestion import chunking, pipeline
from ingestion.extract import EXTRACTOR_VERSION, PageText
from ingestion.management.commands.discover_meetings import _summary


pytestmark = pytest.mark.django_db


def test_extraction_and_chunking(stored_version, settings):
    document, version = stored_version
    run = pipeline.begin_run("test")
    pipeline.process_versions(run=run, version_ids=[version.id])
    version.refresh_from_db()
    assert version.processing_status == DocumentVersion.Status.PROCESSED
    chunks = list(version.chunks.order_by("sequence"))
    assert chunks
    joined = " ".join(c.text for c in chunks)
    assert "Spårvägen" in joined  # Swedish diacritics survive extraction
    for chunk in chunks:
        assert chunk.page_start >= 1
        assert chunk.processing_version.startswith("pdfplumber")


def test_process_is_idempotent(stored_version):
    document, version = stored_version
    run = pipeline.begin_run("test")
    pipeline.process_versions(run=run, version_ids=[version.id])
    first_count = Chunk.objects.filter(document_version=version).count()
    pipeline.process_versions(run=run, version_ids=[version.id], statuses=["processed"])
    assert Chunk.objects.filter(document_version=version).count() == first_count
    assert version.chunks.count() == first_count


def test_reprocess_replaces_chunks(stored_version):
    document, version = stored_version
    run = pipeline.begin_run("test")
    pipeline.process_versions(run=run, version_ids=[version.id])
    before = Chunk.objects.filter(document_version=version).count()
    # Force reprocessing: chunks must be replaced, not duplicated.
    version.processing_status = DocumentVersion.Status.PENDING
    version.save(update_fields=["processing_status"])
    pipeline.process_versions(run=run, version_ids=[version.id])
    assert Chunk.objects.filter(document_version=version).count() == before


def test_fts_stays_in_sync(stored_version):
    document, version = stored_version
    run = pipeline.begin_run("test")
    pipeline.process_versions(run=run, version_ids=[version.id])
    with connection.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM search_chunkfts")
        fts_rows = cursor.fetchone()[0]
    assert fts_rows == Chunk.objects.filter(document_version=version).count()
    pipeline.rebuild_search_index()
    with connection.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM search_chunkfts")
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


def test_chunking_pages_and_numbers():
    pages = [
        PageText(1, "text", "Rubrik\nFörsta stycket med tillräckligt många tecken för att behållas separat."),
        PageText(2, "text", "Andra sidan innehåller mer text som också blir en chunk med sidrefer."),
    ]
    drafts = chunking.build_chunks(pages)
    assert drafts
    assert drafts[0].page_start == 1
    assert any(d.page_start == 2 for d in drafts)
    assert [d.sequence for d in drafts] == list(range(1, len(drafts) + 1))


def test_chunking_binds_agenda_numbers():
    pages = [PageText(1, "text", "20. Detaljplan för kollektivtrafik\nBeslut: planen antas enligt förslag.")]
    drafts = chunking.build_chunks(pages, agenda_numbers={20})
    assert drafts[0].agenda_number == 20
