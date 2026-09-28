"""Ingestion orchestration: discovery, downloading, extraction.

Rules enforced here:
- Downloads, extraction, and OCR never run inside database write transactions.
- Write transactions are short (one meeting / one version / one document at a time).
- Re-running on unchanged sources creates nothing new (idempotent).
- A hash identifies file content, never the logical document.
- A disappeared link never deletes previously collected material.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from core.models import (
    AgendaItem,
    Authority,
    Body,
    Chunk,
    Document,
    DocumentAssociation,
    DocumentVersion,
    IngestionRun,
    Meeting,
)

from . import chunking, extract
from .adapters.base import SourceAdapter
from .adapters.uppsala import get_adapter_class
from .fetch import Fetcher, FetchError, looks_like_pdf
from .locking import IngestionLock

log = logging.getLogger(__name__)

LOCK_PATH = settings.DATA_DIR / "ingestion.lock"


def ingestion_lock() -> IngestionLock:
    settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
    return IngestionLock(LOCK_PATH)


def begin_run(scope: str) -> IngestionRun:
    # A run that died without closing is recorded as failed, never silently overwritten.
    IngestionRun.objects.filter(status=IngestionRun.Status.RUNNING).update(
        status=IngestionRun.Status.FAILED,
        error_summary="Processen avslutades utan att stänga run (troligen krasch).",
        finished_at=timezone.now(),
    )
    return IngestionRun.objects.create(scope=scope, status=IngestionRun.Status.RUNNING)


def finish_run(run: IngestionRun, *, ok: bool = True, error_summary: str = "") -> IngestionRun:
    run.finished_at = timezone.now()
    if not ok:
        run.status = IngestionRun.Status.FAILED
    elif run.failures:
        run.status = IngestionRun.Status.PARTIAL
    else:
        run.status = IngestionRun.Status.SUCCESS
    run.error_summary = error_summary[:2000]
    run.save(update_fields=["finished_at", "status", "error_summary"])
    return run


def ensure_source(adapter_cls) -> Body:
    authority, _ = Authority.objects.get_or_create(
        slug=adapter_cls.authority["slug"],
        defaults={
            "name": adapter_cls.authority["name"],
            "kind": adapter_cls.authority["kind"],
        },
    )
    body, _ = Body.objects.get_or_create(
        authority=authority,
        slug=adapter_cls.slug,
        defaults={"name": adapter_cls.body_name, "source_url": getattr(adapter_cls, "body_source_url", "")},
    )
    return body


def _resolve_item(items: list[AgendaItem], number: int | None) -> AgendaItem | None:
    if number is None:
        return None
    for item in items:
        if item.agenda_number == number:
            return item
    for item in items:  # ranged items ("7-12.")
        if item.agenda_number is not None and item.agenda_number_end is not None:
            if item.agenda_number <= number <= item.agenda_number_end:
                return item
    return None


def _persist_meeting(body: Body, snapshot, run: IngestionRun):
    """Upsert one meeting snapshot. Returns (meeting, items, {url: Document})."""
    with transaction.atomic():
        meeting, created = Meeting.objects.get_or_create(
            body=body,
            source_url=snapshot.source_url,
            defaults={"date": snapshot.date, "title": snapshot.title},
        )
        if not created:
            meeting.date = snapshot.date
            meeting.title = snapshot.title
            meeting.save(update_fields=["date", "title", "last_seen_at"])

        items: list[AgendaItem] = []
        for ref in snapshot.agenda_items:
            if ref.agenda_number is not None:
                item, _ = AgendaItem.objects.get_or_create(
                    meeting=meeting,
                    agenda_number=ref.agenda_number,
                    defaults={"title": ref.title, "agenda_number_end": ref.agenda_number_end},
                )
            else:
                item, _ = AgendaItem.objects.get_or_create(
                    meeting=meeting,
                    agenda_number=None,
                    title=ref.title,
                    defaults={"agenda_number_end": None},
                )
            items.append(item)

        docs_by_url: dict[str, Document] = {}
        for ref in snapshot.documents:
            document, doc_created = Document.objects.get_or_create(
                primary_source_url=ref.url,
                defaults={"title": ref.title, "doc_type": ref.doc_type},
            )
            item = _resolve_item(items, ref.agenda_number)
            DocumentAssociation.objects.get_or_create(
                document=document,
                meeting=meeting,
                agenda_item=item,
                defaults={"source_context": ref.source_context},
            )
            docs_by_url[ref.url] = document
            run.documents_seen += 1
            if doc_created:
                run.documents_created += 1
        run.meetings_seen += 1
        run.save(update_fields=["meetings_seen", "documents_seen", "documents_created"])

    # Snapshot files are written outside the transaction; nothing is deleted on re-crawl.
    _write_snapshot(meeting, snapshot)
    return meeting, items, docs_by_url


def _write_snapshot(meeting: Meeting, snapshot) -> None:
    directory = settings.SNAPSHOTS_DIR / str(meeting.pk)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = timezone.now().strftime("%Y%m%dT%H%M%S")
    (directory / f"{stamp}.html").write_bytes(snapshot.html)
    meta = {
        "url": snapshot.source_url,
        "fetched_at": timezone.now().isoformat(),
        "title": snapshot.title,
        "date": snapshot.date.isoformat() if snapshot.date else None,
        "documents_found": len(snapshot.documents),
    }
    (directory / f"{stamp}.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8"
    )


def _download_one(url: str, fetcher: Fetcher) -> dict:
    """Worker: download + validate + hash + content-addressed store. No DB access."""
    try:
        result = fetcher.get(url)
        if len(result.content) > settings.MAX_DOWNLOAD_BYTES:
            return {"ok": False, "url": url, "error": f"file exceeds MAX_DOWNLOAD_BYTES ({len(result.content)} bytes)"}
        if not looks_like_pdf(result.content):
            return {"ok": False, "url": url, "error": "content is not a valid PDF"}
        sha = hashlib.sha256(result.content).hexdigest()
        stored_path = f"{sha[:2]}/{sha}.pdf"
        destination = settings.ORIGINALS_DIR / stored_path
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            tmp = destination.with_suffix(".part")
            tmp.write_bytes(result.content)
            os.replace(tmp, destination)
        return {"ok": True, "url": url, "sha256": sha, "stored_path": stored_path, "size": len(result.content)}
    except FetchError as exc:
        return {"ok": False, "url": url, "error": str(exc)[:300]}
    except OSError as exc:
        return {"ok": False, "url": url, "error": f"storage error: {exc!r}"[:300]}


def download_documents(docs_with_labels: list[tuple[Document, str, str | None]], fetcher: Fetcher, refresh: bool, run: IngestionRun):
    """Download pending/refresh document URLs concurrently; persist versions in short transactions."""
    tasks: list[tuple[Document, str, str | None]] = []
    for document, url, revision_label in docs_with_labels:
        has_bytes = document.versions.filter(sha256__isnull=False).exists()
        if has_bytes and not refresh:
            continue
        tasks.append((document, url, revision_label))
    if not tasks:
        return

    with ThreadPoolExecutor(max_workers=settings.DOWNLOAD_CONCURRENCY) as pool:
        futures = {pool.submit(_download_one, url, fetcher): (document, url, label) for document, url, label in tasks}
        for future in as_completed(futures):
            document, url, revision_label = futures[future]
            outcome = future.result()
            with transaction.atomic():
                if not outcome["ok"]:
                    failed_row, _ = DocumentVersion.objects.get_or_create(
                        document=document,
                        original_url=url,
                        sha256__isnull=True,
                        defaults={
                            "processing_status": DocumentVersion.Status.FAILED,
                            "processing_error": outcome["error"],
                        },
                    )
                    if failed_row.processing_status != DocumentVersion.Status.FAILED:
                        failed_row.processing_status = DocumentVersion.Status.FAILED
                    failed_row.processing_error = outcome["error"]
                    failed_row.save(update_fields=["processing_status", "processing_error"])
                    run.failures += 1
                    log.error("Download failed: %s: %s", url, outcome["error"])
                    continue

                sha = outcome["sha256"]
                version = DocumentVersion.objects.filter(document=document, sha256=sha).first()
                created = version is None
                if created:
                    # Reuse a failed-download row for the same URL if one exists.
                    version = DocumentVersion.objects.filter(
                        document=document, original_url=url, sha256__isnull=True
                    ).first() or DocumentVersion(document=document, sha256=sha)
                version.original_url = url
                version.sha256 = sha
                version.stored_path = outcome["stored_path"]
                version.size_bytes = outcome["size"]
                version.retrieved_at = timezone.now()
                version.source_revision_label = revision_label
                version.processing_status = DocumentVersion.Status.PENDING
                version.save()
                if created:
                    run.versions_created += 1
                current = document.current_version
                if current is None or (current.retrieved_at or current.created_at) <= version.retrieved_at:
                    document.current_version = version
                    document.save(update_fields=["current_version", "last_seen_at"])
                run.save(update_fields=["versions_created", "failures"])


def run_discovery(
    adapter_cls,
    start: datetime.date,
    end: datetime.date,
    *,
    download: bool = True,
    refresh: bool = False,
    run: IngestionRun,
    limit_meetings: int | None = None,
) -> None:
    body = ensure_source(adapter_cls)
    with Fetcher() as fetcher:
        adapter: SourceAdapter = adapter_cls(fetcher)
        try:
            urls = adapter.discover_meeting_urls(start, end)
        except FetchError as exc:
            log.error("Meeting discovery failed: %s", exc)
            run.failures += 1
            run.error_summary = f"discovery failed: {exc!r}"[:2000]
            return
        log.info("Discovered %d meetings in %s..%s", len(urls), start, end)
        if limit_meetings:
            urls = urls[:limit_meetings]
        for url in urls:
            try:
                snapshot = adapter.fetch_meeting(url)
            except FetchError as exc:
                log.error("Meeting page fetch failed (%s): %s", url, exc)
                run.failures += 1
                continue
            if snapshot.date is None:
                log.error("Could not parse meeting date from %s; skipping", url)
                run.failures += 1
                continue
            try:
                _, _, docs_by_url = _persist_meeting(body, snapshot, run)
            except Exception:
                log.exception("Persisting meeting failed: %s", url)
                run.failures += 1
                continue
            if download:
                docs_with_labels = [
                    (document, ref_url, _label_for(snapshot, ref_url)) for ref_url, document in docs_by_url.items()
                ]
                download_documents(docs_with_labels, fetcher, refresh=refresh, run=run)


def _label_for(snapshot, url: str) -> str | None:
    for ref in snapshot.documents:
        if ref.url == url:
            return ref.revision_label
    return None


def process_versions(
    *,
    run: IngestionRun,
    version_ids: list[int] | None = None,
    document_id: int | None = None,
    statuses: list[str] | None = None,
    limit: int | None = None,
) -> None:
    """Extract + chunk stored versions; one failure is recorded and does not abort the run."""
    statuses = statuses or [DocumentVersion.Status.PENDING]
    queryset = DocumentVersion.objects.filter(
        processing_status__in=statuses
    ).exclude(stored_path="")
    if version_ids:
        queryset = queryset.filter(id__in=version_ids)
    if document_id:
        queryset = queryset.filter(document_id=document_id)
    queryset = queryset.select_related("document").order_by("id")
    if limit:
        queryset = queryset[:limit]

    for version in queryset:
        document = version.document
        source_path = settings.ORIGINALS_DIR / version.stored_path
        if not source_path.exists():
            _mark_failed(version, f"stored file missing: {version.stored_path}", run)
            continue
        try:
            result = extract.extract_pdf(source_path, settings.DERIVED_DIR, version.sha256 or "unknown")
            drafts = chunking.build_chunks(result.pages)
            if drafts:
                _store_chunks(version, drafts, run, ocr_used=result.ocr_used)
                if result.ocr_error:
                    _note_ocr_error(version, result.ocr_error)
            else:
                _mark_failed(version, "no text could be extracted (all pages empty or OCR unavailable)", run)
        except Exception as exc:  # noqa: BLE001 - recorded per version, run continues
            _mark_failed(version, f"{type(exc).__name__}: {exc}", run)
            log.error("Processing failed for version %s: %s", version.pk, exc)


def _primary_items_for(document: Document) -> list[AgendaItem]:
    association = document.associations.select_related("meeting").order_by("id").first()
    if association is None:
        return []
    return list(association.meeting.agenda_items.all())


def _store_chunks(version: DocumentVersion, drafts: list[chunking.ChunkDraft], run: IngestionRun, *, ocr_used: bool) -> None:
    with transaction.atomic():
        items = _primary_items_for(version.document)
        title_base = version.document.title
        Chunk.objects.filter(document_version=version).delete()
        objects = []
        for draft in drafts:
            item = _resolve_item(items, draft.agenda_number)
            title = title_base if item is None else f"{title_base} — {item.title}"
            objects.append(
                Chunk(
                    document_version=version,
                    sequence=draft.sequence,
                    agenda_item=item,
                    title=title[:500],
                    text=draft.text,
                    page_start=draft.page_start,
                    page_end=draft.page_end,
                    processing_version=f"{extract.EXTRACTOR_VERSION}/{chunking.CHUNKER_VERSION}",
                )
            )
        Chunk.objects.bulk_create(objects)
        version.processing_status = DocumentVersion.Status.PROCESSED
        version.ocr_used = ocr_used
        version.processing_error = ""
        version.extracted_at = timezone.now()
        version.save(update_fields=["processing_status", "processing_error", "ocr_used", "extracted_at"])
        run.chunks_created += len(objects)
        run.save(update_fields=["chunks_created"])


def _note_ocr_error(version: DocumentVersion, error: str) -> None:
    """Version stays processed/partially searchable, but the OCR gap is recorded."""
    with transaction.atomic():
        version.processing_error = f"OCR ofullständig: {error}"[:1000]
        version.save(update_fields=["processing_error"])
    log.warning("Version %s stored with incomplete OCR: %s", version.pk, error)


def _mark_failed(version: DocumentVersion, error: str, run: IngestionRun) -> None:
    with transaction.atomic():
        version.processing_status = DocumentVersion.Status.FAILED
        version.processing_error = error[:1000]
        version.save(update_fields=["processing_status", "processing_error"])
        run.failures += 1
        run.save(update_fields=["failures"])
    log.error("Version %s failed: %s", version.pk, error)


def retry_failed(run: IngestionRun, *, document_id: int | None = None, process: bool = True) -> dict:
    """Re-attempt failed downloads (null hash) and requeue failed extractions."""
    stats = {"redownloaded": 0, "requeued": 0}
    failed_rows = DocumentVersion.objects.filter(processing_status=DocumentVersion.Status.FAILED)
    if document_id:
        failed_rows = failed_rows.filter(document_id=document_id)
    null_hash_rows = list(failed_rows.filter(sha256__isnull=True).select_related("document"))
    if null_hash_rows:
        with Fetcher() as fetcher:
            download_documents(
                [(row.document, row.original_url, row.source_revision_label) for row in null_hash_rows],
                fetcher,
                refresh=False,
                run=run,
            )
        stats["redownloaded"] = len(null_hash_rows)
    requeued = failed_rows.filter(sha256__isnull=False).update(
        processing_status=DocumentVersion.Status.PENDING, processing_error=""
    )
    stats["requeued"] = requeued
    if process and requeued:
        process_versions(run=run, statuses=[DocumentVersion.Status.PENDING], document_id=document_id)
    return stats


def rebuild_search_index() -> dict:
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute("INSERT INTO search_chunkfts(search_chunkfts) VALUES('rebuild')")
        cursor.execute("SELECT COUNT(*) FROM search_chunkfts")
        fts_rows = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM core_chunk")
        chunk_rows = cursor.fetchone()[0]
    return {"fts_rows": fts_rows, "chunk_rows": chunk_rows}


def backup_database(dest_dir: Path, include_originals: bool = False) -> dict:
    """SQLite online backup (WAL-safe) plus an optional copy of original documents."""
    import sqlite3

    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = timezone.now().strftime("%Y%m%d-%H%M%S")
    db_dest = dest_dir / f"db-{stamp}.sqlite3"
    source = sqlite3.connect(str(settings.DATA_DIR / "db.sqlite3"))
    target = sqlite3.connect(str(db_dest))
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    result = {"database": str(db_dest), "originals": None}
    if include_originals:
        originals_dest = dest_dir / f"originals-{stamp}"
        shutil.copytree(settings.ORIGINALS_DIR, originals_dest, dirs_exist_ok=True)
        result["originals"] = str(originals_dest)
    return result