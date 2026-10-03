"""Ingestion orchestration: discovery, downloading, IR extraction, parsing.

Rules enforced here:
- Extraction and OCR never run inside database write transactions.
- Write transactions are short (one meeting / one version / one document at a time).
- Re-running on unchanged sources creates nothing new (idempotent).
- A hash identifies file content, never the logical document.
- A disappeared link never deletes previously collected material.

Stage A renders each stored PDF into structured markdown with xberg (cached in
a sidecar keyed on sha256 and stamped with IR_VERSION). Stage C parses votes
and decisions from the markdown, then stitches cases by diarienummer. Parsed
rows reference the document version only — page/part-level association is
deferred; the IR sidecar keeps per-page text so it can be reintroduced later
without re-paying extraction.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from core.models import (
    AgendaItem,
    Authority,
    Body,
    Document,
    DocumentAssociation,
    DocumentPage,
    DocumentVersion,
    IngestionRun,
    Meeting,
)

from . import cases, decisions, ir, votes
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
    force: bool = False,
) -> None:
    """Stage A: render stored versions into IR markdown (xberg).

    Selection is by processing status; a version already stamped with the
    current IR version is skipped unless force=True (or the status was reset
    to pending). One failure is recorded and does not abort the run.
    """
    statuses = statuses or [DocumentVersion.Status.PENDING]
    queryset = DocumentVersion.objects.filter(
        processing_status__in=statuses
    ).exclude(stored_path="")
    if not force:
        queryset = queryset.filter(
            Q(ir_version__isnull=True) | ~Q(ir_version=ir.IR_VERSION)
        )
    if version_ids:
        queryset = queryset.filter(id__in=version_ids)
    if document_id:
        queryset = queryset.filter(document_id=document_id)
    queryset = queryset.order_by("id")
    if limit:
        queryset = queryset[:limit]

    for version in queryset:
        source_path = settings.ORIGINALS_DIR / version.stored_path
        if not source_path.exists():
            _mark_failed(version, f"stored file missing: {version.stored_path}", run)
            continue
        try:
            cached = ir._cached_ir(version)
            if cached is not None:
                log.info("Version %s: reusing cached IR %s", version.pk, (version.sha256 or "")[:12])
                result = cached
            else:
                result = ir.extract_ir(source_path, settings.DERIVED_DIR, version.sha256 or "unknown")
        except Exception as exc:  # noqa: BLE001 - recorded per version, run continues
            _mark_failed(version, f"{type(exc).__name__}: {exc}", run)
            log.error("IR extraction failed for version %s: %s", version.pk, exc)
            continue
        with transaction.atomic():
            version.markdown = result.markdown
            version.ir_version = ir.IR_VERSION
            version.ocr_used = result.ocr_used
            version.processing_status = DocumentVersion.Status.PROCESSED
            version.processing_error = ""
            version.extracted_at = timezone.now()
            version.save(
                update_fields=[
                    "markdown", "ir_version", "ocr_used",
                    "processing_status", "processing_error", "extracted_at",
                ]
            )
            # Mirror per-page text for page references (search hits, votes,
            # decisions). Sidecars keep the same data; the DB copy makes it
            # queryable without file access.
            DocumentPage.objects.filter(document_version=version).delete()
            DocumentPage.objects.bulk_create(
                DocumentPage(document_version=version, number=p["number"], text=p["text"])
                for p in result.pages
            )


def extract_votes(
    *,
    run: IngestionRun,
    version_ids: list[int] | None = None,
    document_id: int | None = None,
    limit: int | None = None,
    force: bool = False,
) -> dict:
    """Stage C: parse recorded votes from IR markdown for processed versions.

    Versions stamped with the current vote-parser version are skipped unless
    force=True. One version's failure is recorded and does not abort the run.
    """
    queryset = DocumentVersion.objects.filter(
        processing_status=DocumentVersion.Status.PROCESSED
    ).exclude(markdown="")
    if not force:
        queryset = queryset.filter(
            Q(vote_parser_version__isnull=True) | ~Q(vote_parser_version=votes.VOTE_PARSER_VERSION)
        )
    if version_ids:
        queryset = queryset.filter(id__in=version_ids)
    if document_id:
        queryset = queryset.filter(document_id=document_id)
    queryset = queryset.order_by("id")
    if limit:
        queryset = queryset[:limit]

    stats = {"versions": 0, "votes": 0, "records": 0, "failures": 0}
    for version in queryset:
        try:
            parsed = votes.parse_markdown(version.markdown)
        except Exception as exc:  # noqa: BLE001 - recorded per version, run continues
            log.error("Vote extraction failed for version %s: %s", version.pk, exc)
            run.failures += 1
            stats["failures"] += 1
            continue
        with transaction.atomic():
            created_votes, created_records = votes.store(version, parsed)
            version.vote_parser_version = votes.VOTE_PARSER_VERSION
            version.save(update_fields=["vote_parser_version"])
        stats["versions"] += 1
        stats["votes"] += created_votes
        stats["records"] += created_records
    if stats["failures"]:
        run.save(update_fields=["failures"])
    return stats


def extract_decisions(
    *,
    run: IngestionRun,
    version_ids: list[int] | None = None,
    document_id: int | None = None,
    limit: int | None = None,
    force: bool = False,
) -> dict:
    """Stage C: parse agenda-item decision blocks from IR markdown (protocols only).

    Mirrors extract_votes: versions stamped with the current decision-parser
    version are skipped unless force=True; one failure never aborts the run.
    """
    queryset = DocumentVersion.objects.filter(
        processing_status=DocumentVersion.Status.PROCESSED,
        markdown__gt="",
        document__doc_type=Document.DocType.PROTOCOL,
    )
    if not force:
        queryset = queryset.filter(
            Q(decision_parser_version__isnull=True) | ~Q(decision_parser_version=decisions.DECISION_PARSER_VERSION)
        )
    if version_ids:
        queryset = queryset.filter(id__in=version_ids)
    if document_id:
        queryset = queryset.filter(document_id=document_id)
    queryset = queryset.order_by("id")
    if limit:
        queryset = queryset[:limit]

    stats = {"versions": 0, "decisions": 0, "failures": 0}
    for version in queryset:
        try:
            parsed = decisions.parse_markdown(version.markdown)
        except Exception as exc:  # noqa: BLE001 - recorded per version, run continues
            log.error("Decision extraction failed for version %s: %s", version.pk, exc)
            run.failures += 1
            stats["failures"] += 1
            continue
        with transaction.atomic():
            created = decisions.store(version, parsed)
            version.decision_parser_version = decisions.DECISION_PARSER_VERSION
            version.save(update_fields=["decision_parser_version"])
        stats["versions"] += 1
        stats["decisions"] += created
    if stats["failures"]:
        run.save(update_fields=["failures"])
    return stats


def build_cases() -> dict:
    """Stage C: stitch initiatives end-to-end by diarienummer (see cases.py)."""
    return cases.build_cases()


def classify_content(*, run, force: bool = False) -> dict:
    """Stage D: policy-area tags for decisions/initiatives (see classify.py)."""
    from . import classify

    return classify.classify_content(run=run, force=force)


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
    """Rebuild the document-level FTS index from stored version markdown."""
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM search_documentfts")
        cursor.execute(
            """
            INSERT INTO search_documentfts(rowid, title, text, document_id, version_id)
            SELECT dv.id, d.title, dv.markdown, d.id, dv.id
            FROM core_documentversion dv
            JOIN core_document d ON d.id = dv.document_id
            WHERE dv.markdown != ''
            """
        )
        cursor.execute("SELECT COUNT(*) FROM search_documentfts")
        fts_rows = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM core_documentversion WHERE markdown != ''")
        version_rows = cursor.fetchone()[0]
    return {"fts_rows": fts_rows, "indexed_versions": version_rows}


def backup_database(dest_dir: Path, include_originals: bool = False, include_derived: bool = True) -> dict:
    """SQLite online backup (WAL-safe) plus copies of the original documents and
    the derived extraction cache (IR sidecars: expensive to regenerate)."""
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
    result = {"database": str(db_dest), "originals": None, "derived": None}
    if include_originals:
        originals_dest = dest_dir / f"originals-{stamp}"
        shutil.copytree(settings.ORIGINALS_DIR, originals_dest, dirs_exist_ok=True)
        result["originals"] = str(originals_dest)
    if include_derived:
        derived_dest = dest_dir / f"derived-{stamp}"
        shutil.copytree(settings.DERIVED_DIR, derived_dest, dirs_exist_ok=True)
        result["derived"] = str(derived_dest)
    return result
