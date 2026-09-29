"""Public website views: search, meeting browsing, document pages, coverage status."""

import datetime
import math
from collections import defaultdict
from pathlib import Path

from django.conf import settings
from django.db.models import Count, Max, Min
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, render

from core.models import (
    Authority,
    Body,
    Chunk,
    Document,
    DocumentAssociation,
    DocumentVersion,
    IngestionRun,
    Meeting,
    Vote,
)
from core.parties import party_css_class, party_label
from search import service
from search.query import build_match_query

PAGE_SIZE = 20
_TYPE_LABELS = dict(Document.DocType.choices)
_STANCE_ORDER = ("ja", "nej", "avstar", "franvarande")
_STANCE_LABELS = dict(Vote.Stance.choices)


def _shape_votes(votes):
    """Shape Vote rows for the template: party stance matrix + member lists."""
    shaped = []
    for vote in votes:
        counts: dict[str, dict[str, int]] = {}
        members: dict[str, list] = {}
        for record in vote.records.all():
            per_party = counts.setdefault(record.party_code, {})
            per_party[record.stance] = per_party.get(record.stance, 0) + 1
            members.setdefault(record.stance, []).append(record)

        parties = []
        for code in counts:
            stances = {s: counts[code][s] for s in _STANCE_ORDER if counts[code].get(s)}
            parties.append(
                {
                    "code": code,
                    "label": party_label(code),
                    "css": party_css_class(code),
                    "stances": stances,
                    "total": sum(stances.values()),
                }
            )
        parties.sort(key=lambda p: (-p["stances"].get("ja", 0), -p["total"], p["code"]))

        tally_parts = []
        if vote.tally_yes is not None:
            tally_parts.append(f"{vote.tally_yes} ja")
        if vote.tally_no is not None:
            tally_parts.append(f"{vote.tally_no} nej")
        if vote.tally_abstain:
            tally_parts.append(f"{vote.tally_abstain} avstår")
        if not tally_parts and vote.has_details:
            for stance in _STANCE_ORDER[:3]:
                named = sum(1 for record in vote.records.all() if record.stance == stance)
                if named:
                    tally_parts.append(f"{named} {dict(Vote.Stance.choices)[stance].lower()}")

        shaped.append(
            {
                "vote": vote,
                "label": vote.proposition or (vote.chunk.title if vote.chunk else "Röstning"),
                "tally_text": " · ".join(tally_parts),
                "parties": parties,
                "stance_groups": [
                    {
                        "label": _STANCE_LABELS.get(stance, stance),
                        "records": members[stance],
                    }
                    for stance in _STANCE_ORDER
                    if stance in members
                ],
                "mismatch": vote.records_match_tally is False,
            }
        )
    return shaped


def _int_param(request, name):
    raw = request.GET.get(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _date_param(request, name):
    raw = request.GET.get(name, "").strip()
    if not raw:
        return None
    try:
        return datetime.date.fromisoformat(raw).isoformat()
    except ValueError:
        return None


def _meeting_info_for_documents(doc_ids):
    """Map document id -> human-readable meeting references, in meeting order."""
    info = defaultdict(list)
    associations = (
        DocumentAssociation.objects.filter(document_id__in=doc_ids)
        .select_related("meeting", "meeting__body", "meeting__body__authority")
        .order_by("meeting__date")
    )
    for association in associations:
        meeting = association.meeting
        info[association.document_id].append(
            f"{meeting.date.isoformat()} · {meeting.body.name} ({meeting.body.authority.name})"
        )
    return info


def search_view(request):
    query = request.GET.get("q", "").strip()
    doc_type = request.GET.get("typ", "").strip() or None
    meeting_id = _int_param(request, "mote")
    authority_id = _int_param(request, "myndighet")
    body_id = _int_param(request, "organ")
    date_from = _date_param(request, "fran")
    date_to = _date_param(request, "till")
    page = max(1, _int_param(request, "sida") or 1)

    error = None
    results = None
    total = 0
    page_count = 0

    if query:
        match_expr = build_match_query(query)
        if not match_expr:
            error = "Sökfrågan innehöll inga sökbara ord."
        else:
            data = service.search(
                match_expr,
                doc_type=doc_type,
                meeting_id=meeting_id,
                authority_id=authority_id,
                body_id=body_id,
                date_from=date_from,
                date_to=date_to,
                limit=PAGE_SIZE,
                offset=(page - 1) * PAGE_SIZE,
            )
            total = data["total"]
            page_count = max(1, math.ceil(total / PAGE_SIZE)) if total else 1
            if page > page_count and total:
                page = page_count
                data = service.search(
                    match_expr,
                    doc_type=doc_type,
                    meeting_id=meeting_id,
                    authority_id=authority_id,
                    body_id=body_id,
                    date_from=date_from,
                    date_to=date_to,
                    limit=PAGE_SIZE,
                    offset=(page - 1) * PAGE_SIZE,
                )
            meeting_info = _meeting_info_for_documents([row["document_id"] for row in data["results"]])
            for row in data["results"]:
                references = meeting_info.get(row["document_id"], [])
                if len(references) > 2:
                    row["meeting_text"] = references[0] + f" +{len(references) - 1} möten"
                else:
                    row["meeting_text"] = " · ".join(references) or "okänd möteskoppling"
                row["doc_type_label"] = _TYPE_LABELS.get(row["doc_type"], row["doc_type"])
            results = data["results"]

    filter_params = request.GET.copy()
    filter_params.pop("sida", None)

    return render(
        request,
        "website/search.html",
        {
            "query": query,
            "results": results,
            "total": total,
            "page": page,
            "page_count": page_count,
            "error": error,
            "base_qs": filter_params.urlencode(),
            "doc_types": Document.DocType.choices,
            "selected": {
                "typ": doc_type,
                "mote": meeting_id,
                "myndighet": authority_id,
                "organ": body_id,
                "fran": date_from,
                "till": date_to,
            },
            "meetings": Meeting.objects.order_by("-date")[:200],
            "authorities": Authority.objects.all(),
            "bodies": Body.objects.select_related("authority").all(),
        },
    )


def meetings_view(request):
    meetings = (
        Meeting.objects.select_related("body", "body__authority")
        .order_by("-date", "id")
    )
    by_year = defaultdict(list)
    for meeting in meetings:
        by_year[meeting.date.year].append(meeting)
    years = sorted(by_year.items(), reverse=True)
    return render(request, "website/meetings.html", {"years": years})


def meeting_detail_view(request, pk):
    meeting = get_object_or_404(
        Meeting.objects.select_related("body", "body__authority"), pk=pk
    )
    agenda_items = meeting.agenda_items.all().order_by("agenda_number", "id")
    associations = (
        meeting.document_associations.select_related("document", "agenda_item", "document__current_version")
        .order_by("document__doc_type", "document__title")
    )
    grouped = defaultdict(list)
    for association in associations:
        grouped[_TYPE_LABELS.get(association.document.doc_type, association.document.doc_type)].append(association)
    return render(
        request,
        "website/meeting_detail.html",
        {"meeting": meeting, "agenda_items": agenda_items, "grouped": dict(sorted(grouped.items()))},
    )


def document_detail_view(request, pk):
    document = get_object_or_404(
        Document.objects.prefetch_related("versions", "associations"), pk=pk
    )
    versions = document.versions.order_by("-retrieved_at", "-id")
    current = document.current_version
    chunks = (
        current.chunks.order_by("sequence")
        if current is not None and current.processing_status == DocumentVersion.Status.PROCESSED
        else []
    )
    associations = document.associations.select_related(
        "meeting", "meeting__body", "meeting__body__authority", "agenda_item"
    )
    votes = []
    if current is not None and current.processing_status == DocumentVersion.Status.PROCESSED:
        votes = _shape_votes(
            Vote.objects.filter(document_version=current)
            .prefetch_related("records")
            .select_related("chunk")
            .order_by("sequence")
        )
    return render(
        request,
        "website/document_detail.html",
        {
            "document": document,
            "versions": versions,
            "current_version": current,
            "chunks": chunks,
            "associations": associations,
            "votes": votes,
            "doc_type_label": _TYPE_LABELS.get(document.doc_type, document.doc_type),
        },
    )


def version_detail_view(request, version_pk):
    version = get_object_or_404(DocumentVersion.objects.select_related("document"), pk=version_pk)
    chunks = version.chunks.order_by("sequence")
    return render(
        request,
        "website/version_detail.html",
        {"version": version, "chunks": chunks, "document": version.document},
    )


def version_file_view(request, version_pk):
    """Serve the preserved original PDF for a document version."""
    version = get_object_or_404(DocumentVersion.objects.select_related("document"), pk=version_pk)
    if not version.stored_path:
        raise Http404("Ingen sparad fil för denna version.")
    root = Path(settings.ORIGINALS_DIR).resolve()
    path = (root / version.stored_path).resolve()
    if root not in path.parents or path.suffix.lower() != ".pdf" or not path.is_file():
        raise Http404("Filen saknas i dokumentarkivet.")
    response = FileResponse(path.open("rb"), content_type="application/pdf")
    response["Content-Disposition"] = (
        f'inline; filename="dokument-{version.document_id}-v{version.pk}.pdf"'
    )
    return response


def settings_view(request):
    """AI provider settings live in the browser's localStorage; this page is just the form."""
    return render(request, "website/settings.html")


def status_view(request):
    coverage = Meeting.objects.aggregate(earliest=Min("date"), latest=Max("date"))
    version_counts = {
        row["processing_status"]: row["n"]
        for row in DocumentVersion.objects.values("processing_status").annotate(n=Count("id"))
    }
    context = {
        "coverage": coverage,
        "meeting_count": Meeting.objects.count(),
        "document_count": Document.objects.count(),
        "chunk_count": Chunk.objects.count(),
        "fts_rows": service.fts_row_count(),
        "version_counts": version_counts,
        "ocr_count": DocumentVersion.objects.filter(ocr_used=True).count(),
        "last_run": IngestionRun.objects.exclude(status=IngestionRun.Status.RUNNING).first(),
        "recent_runs": IngestionRun.objects.all()[:10],
    }
    return render(request, "website/status.html", context)