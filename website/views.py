"""Public website views: search, meeting browsing, document pages, coverage status."""

import datetime
import math
from collections import Counter, defaultdict
from pathlib import Path

import mistune
from django.conf import settings
from django.db.models import Count, Exists, Max, Min, OuterRef, Q
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, render
from django.utils.text import slugify

from core.models import (
    AgendaItem,
    Authority,
    Body,
    Case,
    Decision,
    Document,
    DocumentAssociation,
    DocumentVersion,
    IngestionRun,
    Meeting,
    Vote,
    VoteRecord,
)
from core.dnrs import clean_title, normalize_dnr
from core.parties import party_css_class, party_label
from search import service
from search.query import build_match_query

PAGE_SIZE = 20
_TYPE_LABELS = dict(Document.DocType.choices)
_STANCE_ORDER = ("ja", "nej", "avstar", "franvarande")
_STANCE_LABELS = dict(Vote.Stance.choices)
_STANCE_PART_LABELS = {"ja": "ja", "nej": "nej", "avstar": "avstår", "franvarande": "frånvarande"}

_markdown = mistune.create_markdown(plugins=["table", "strikethrough", "footnotes"])


def _shape_votes(votes, seat_count=None):
    """Shape Vote rows for the template: subject, outcome summary, party
    positions, member lists, reservations, and data-quality warnings."""
    shaped = []
    for vote in votes:
        counts: dict[str, dict[str, int]] = {}
        members: dict[str, list] = {}
        for record in vote.records.all():
            per_party = counts.setdefault(record.party_code, {})
            per_party[record.stance] = per_party.get(record.stance, 0) + 1
            members.setdefault(record.stance, []).append(
                {
                    "name": record.member_name,
                    "party": record.party_code,
                    "slug": slugify(record.member_name),
                }
            )

        parties = []
        for code in counts:
            stances = {s: counts[code][s] for s in _STANCE_ORDER if counts[code].get(s)}
            parts = [
                f"{counts[code][s]} {_STANCE_PART_LABELS[s]}"
                for s in _STANCE_ORDER
                if counts[code].get(s)
            ]
            parties.append(
                {
                    "code": code,
                    "label": party_label(code),
                    "css": party_css_class(code),
                    "stances": stances,
                    "parts": parts,
                    "total": sum(stances.values()),
                }
            )
        parties.sort(key=lambda p: (-p["stances"].get("ja", 0), -p["total"], p["code"]))

        # Compact per-stance party strip for list views (vote index page).
        strip = []
        for stance in _STANCE_ORDER[:3]:
            entries = [
                {"code": code, "css": party_css_class(code), "count": counts[code][stance]}
                for code in counts
                if counts[code].get(stance)
            ]
            entries.sort(key=lambda e: -e["count"])
            if entries:
                strip.append({"label": _STANCE_LABELS[stance], "entries": entries})

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

        agenda_title = vote.agenda_title
        agenda_number = vote.printed_agenda_number
        margin = (
            abs(vote.tally_yes - vote.tally_no)
            if vote.tally_yes is not None and vote.tally_no is not None
            else None
        )

        reservations = [
            {
                "kind_label": reservation.get_kind_display(),
                "party_codes": [
                    code.strip() for code in reservation.party_codes.split(",") if code.strip()
                ],
                "party_chips": [
                    {"code": code.strip(), "css": party_css_class(code.strip())}
                    for code in reservation.party_codes.split(",")
                    if code.strip()
                ],
                "member_name": reservation.member_name,
                "text": reservation.text,
            }
            for reservation in vote.reservations.all()
        ]

        named_total = sum(len(records) for records in members.values())
        sanity = []
        if vote.records_match_tally is False:
            sanity.append("Namnlistan stämmer inte med angivet röstantal.")
        if seat_count:
            cast = sum(n for n in (vote.tally_yes, vote.tally_no, vote.tally_abstain) if n)
            if cast > seat_count:
                sanity.append(f"Rösterna ({cast}) överstiger antalet ledamöter ({seat_count}).")
            if named_total > seat_count:
                sanity.append(
                    f"Namnlistan ({named_total} röster) överstiger antalet ledamöter ({seat_count})."
                )

        shaped.append(
            {
                "vote": vote,
                "agenda": {"number": agenda_number, "title": agenda_title} if agenda_title else None,
                "subject": agenda_title,
                "type": (
                    {"code": vote.vote_type, "label": vote.get_vote_type_display()}
                    if vote.vote_type != Vote.VoteType.ANNAT
                    else None
                ),
                "decision": vote.decision_text,
                "margin": margin,
                "label": agenda_title or vote.proposition or "Röstning",
                "tally_text": " · ".join(tally_parts),
                "parties": parties,
                "strip": strip,
                "reservations": reservations,
                "stance_groups": [
                    {
                        "label": _STANCE_LABELS.get(stance, stance),
                        "records": members[stance],
                    }
                    for stance in _STANCE_ORDER
                    if stance in members
                ],
                "sanity": sanity,
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
    has_votes = request.GET.get("rostning", "").strip() == "1"
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
                has_votes=has_votes,
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
                    has_votes=has_votes,
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
                "rostning": has_votes,
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
    agenda_items = list(meeting.agenda_items.all().order_by("agenda_number", "id"))
    associations = (
        meeting.document_associations.select_related("document", "agenda_item", "document__current_version")
        .order_by("document__doc_type", "document__title")
    )
    grouped = defaultdict(list)
    for association in associations:
        grouped[_TYPE_LABELS.get(association.document.doc_type, association.document.doc_type)].append(association)

    # Recorded votes for this meeting's documents' current versions. Votes
    # reach agenda items through each document's association; protocols (no
    # item binding) get their chip on the document entry instead.
    current_version_ids = {
        association.document.current_version_id
        for association in associations
        if association.document.current_version_id
    }
    votes = (
        Vote.objects.filter(document_version_id__in=current_version_ids)
        .select_related("document_version")
        .order_by("sequence")
    )
    votes_by_document: dict[int, list[Vote]] = defaultdict(list)
    for vote in votes:
        votes_by_document[vote.document_version.document_id].append(vote)

    votes_by_item: dict[int, list[Vote]] = defaultdict(list)
    for association in associations:
        if association.agenda_item_id:
            votes_by_item[association.agenda_item_id].extend(
                votes_by_document.get(association.document_id, [])
            )

    # Parsed decisions mapped onto agenda items via the printed § number
    # (document-level attribution: no decision FK).
    number_to_item = {item.agenda_number: item.pk for item in agenda_items if item.agenda_number is not None}
    decision_by_item: dict[int, Decision] = {}
    if current_version_ids:
        for decision in (
            Decision.objects.filter(document_version_id__in=current_version_ids)
            .select_related("document_version__document")
            .order_by("sequence")
        ):
            target = number_to_item.get(decision.printed_agenda_number)
            if target:
                decision_by_item.setdefault(target, decision)

    agenda_rows = [
        {
            "item": item,
            "votes": votes_by_item.get(item.pk, []),
            "decision": decision_by_item.get(item.pk),
        }
        for item in agenda_items
    ]
    for assoc_list in grouped.values():
        for association in assoc_list:
            doc_votes = votes_by_document.get(association.document_id, [])
            association.vote_count = len(doc_votes)
            association.first_vote = doc_votes[0] if doc_votes else None

    return render(
        request,
        "website/meeting_detail.html",
        {
            "meeting": meeting,
            "agenda_rows": agenda_rows,
            "grouped": dict(sorted(grouped.items())),
        },
    )


def votes_view(request):
    """Chronological index of recorded votes in current document versions."""
    current_ids = list(
        Document.objects.exclude(current_version=None).values_list("current_version_id", flat=True)
    )
    base_votes = Vote.objects.filter(document_version_id__in=current_ids)
    party = request.GET.get("parti", "").strip().upper() or None
    vote_type = request.GET.get("typ", "").strip() or None
    year = _int_param(request, "ar")

    votes = (
        base_votes.select_related("document_version__document")
        .prefetch_related("records", "reservations", "document_version__document__associations__meeting")
        .annotate(meeting_date=Min("document_version__document__associations__meeting__date"))
        .order_by("-meeting_date", "document_version_id", "sequence")
    )
    if party:
        votes = votes.filter(
            Exists(VoteRecord.objects.filter(vote_id=OuterRef("pk"), party_code=party))
        )
    if vote_type:
        votes = votes.filter(vote_type=vote_type)
    if year:
        votes = votes.filter(meeting_date__year=year)

    shaped = _shape_votes(votes)
    for row, vote in zip(shaped, votes):
        document = vote.document_version.document
        row["document"] = document
        association = document.associations.first()
        row["meeting"] = association.meeting if association else None

    party_codes = (
        VoteRecord.objects.filter(vote__document_version_id__in=current_ids)
        .values_list("party_code", flat=True)
        .distinct()
    )
    party_options = sorted(
        {(code, party_label(code)) for code in party_codes}, key=lambda pair: pair[1]
    )
    year_options = [d.year for d in Meeting.objects.dates("date", "year", order="DESC")]

    return render(
        request,
        "website/votes.html",
        {
            "votes": shaped,
            "total_votes": base_votes.count(),
            "total_records": VoteRecord.objects.filter(vote__document_version_id__in=current_ids).count(),
            "party_options": party_options,
            "year_options": year_options,
            "type_options": Vote.VoteType.choices,
            "selected": {"parti": party, "typ": vote_type, "ar": year},
        },
    )


def member_view(request, slug):
    """A member's voting history, matched by name across current versions.

    Protocols name members in plain text, so the match is by normalized name;
    same-named members cannot be told apart and name variants are grouped.
    """
    variants = sorted(
        {
            name
            for name in VoteRecord.objects.values_list("member_name", flat=True).distinct()
            if slugify(name) == slug
        }
    )
    if not variants:
        raise Http404("Ingen ledamot med det namnet.")
    current_ids = list(
        Document.objects.exclude(current_version=None).values_list("current_version_id", flat=True)
    )
    records = list(
        VoteRecord.objects.filter(
            member_name__in=variants, vote__document_version_id__in=current_ids
        ).select_related("vote")
    )
    votes = (
        Vote.objects.filter(records__member_name__in=variants, document_version_id__in=current_ids)
        .annotate(meeting_date=Min("document_version__document__associations__meeting__date"))
        .select_related("document_version__document")
        .prefetch_related("reservations")
        .order_by("-meeting_date", "document_version_id", "sequence")
    )
    record_by_vote = {record.vote_id: record for record in records}

    stance_counts = {stance: 0 for stance in _STANCE_ORDER}
    party_codes: dict[str, int] = {}
    rows = []
    for vote in votes:
        record = record_by_vote.get(vote.pk)
        if record is None:
            continue
        stance_counts[record.stance] = stance_counts.get(record.stance, 0) + 1
        party_codes[record.party_code] = party_codes.get(record.party_code, 0) + 1
        rows.append(
            {
                "vote": vote,
                "record": record,
                "document": vote.document_version.document,
                "subject": vote.agenda_title or vote.proposition or "Röstning",
            }
        )
    party_options = sorted(party_codes, key=party_codes.get, reverse=True)

    # Majority alignment and party-line deviations across the named votes.
    # The party line is the party's own ja/nej majority in each vote; a
    # deviation is a member voting against it when the line is not a tie.
    vote_records: dict[int, list[dict]] = defaultdict(list)
    for row in VoteRecord.objects.filter(vote_id__in=[vote.pk for vote in votes]).values(
        "vote_id", "party_code", "stance"
    ):
        vote_records[row["vote_id"]].append(row)

    decided = 0
    won = 0
    deviations = []
    subject_by_vote = {row["vote"].pk: row["subject"] for row in rows}
    for vote in votes:
        record = record_by_vote.get(vote.pk)
        if record is None or record.stance not in ("ja", "nej"):
            continue
        others = vote_records.get(vote.pk, [])
        yes = vote.tally_yes if vote.tally_yes is not None else sum(1 for r in others if r["stance"] == "ja")
        no = vote.tally_no if vote.tally_no is not None else sum(1 for r in others if r["stance"] == "nej")
        if yes != no:
            decided += 1
            if (record.stance == "ja") == (yes > no):
                won += 1
        party_stances = [
            r["stance"]
            for r in others
            if r["party_code"] == record.party_code and r["stance"] in ("ja", "nej")
        ]
        if len(party_stances) >= 2:
            majority_stance, majority_count = Counter(party_stances).most_common(1)[0]
            if majority_count > len(party_stances) - majority_count and record.stance != majority_stance:
                deviations.append(
                    {
                        "vote": vote,
                        "stance": record.stance,
                        "party_stance": majority_stance,
                        "subject": subject_by_vote.get(vote.pk, "Röstning"),
                    }
                )

    return render(
        request,
        "website/member.html",
        {
            "slug": slug,
            "name": variants[0],
            "variants": variants,
            "parties": [
                {"code": code, "label": party_label(code), "css": party_css_class(code)}
                for code in party_options
            ],
            "primary_party": party_options[0] if party_options else None,
            "stance_counts": [
                {"label": _STANCE_LABELS[stance], "count": stance_counts[stance]}
                for stance in _STANCE_ORDER
            ],
            "rows": rows,
            "alignment_pct": round(100 * won / decided) if decided else None,
            "decided_count": decided,
            "deviations": deviations,
        },
    )


def decisions_view(request):
    """Chronological feed of parsed agenda-item decisions in current versions."""
    current_ids = list(
        Document.objects.exclude(current_version=None).values_list("current_version_id", flat=True)
    )
    base = Decision.objects.filter(document_version_id__in=current_ids)
    outcome = request.GET.get("typ", "").strip() or None
    year = _int_param(request, "ar")

    decisions = (
        base.select_related("document_version__document")
        .annotate(meeting_date=Min("document_version__document__associations__meeting__date"))
        .order_by("-meeting_date", "document_version_id", "sequence")
    )
    if outcome:
        decisions = decisions.filter(outcome=outcome)
    if year:
        decisions = decisions.filter(meeting_date__year=year)

    # Votes are matched to decisions within the same protocol version by the
    # printed agenda number (document-level attribution: no FK).
    votes_by_key: dict[tuple[int, int], list[Vote]] = defaultdict(list)
    for vote in Vote.objects.filter(document_version_id__in=current_ids, printed_agenda_number__isnull=False):
        votes_by_key[(vote.document_version_id, vote.printed_agenda_number)].append(vote)

    cases = _cases_by_dnr(decision.printed_dnr for decision in decisions)
    rows = []
    for decision in decisions:
        document = decision.document_version.document
        association = document.associations.first()
        rows.append(
            {
                "decision": decision,
                "document": document,
                "meeting": association.meeting if association else None,
                "subject": decision.printed_agenda_title,
                "votes": votes_by_key.get((decision.document_version_id, decision.printed_agenda_number), []),
                "case": cases.get(normalize_dnr(decision.printed_dnr)),
            }
        )

    year_options = [d.year for d in Meeting.objects.dates("date", "year", order="DESC")]
    return render(
        request,
        "website/decisions.html",
        {
            "rows": rows,
            "total_decisions": base.count(),
            "outcome_options": Decision.Outcome.choices,
            "year_options": year_options,
            "selected": {"typ": outcome, "ar": year},
        },
    )


def _cases_by_dnr(dnrs):
    """Map normalized dnr -> Case for the given raw dnr strings."""
    wanted = {normalize_dnr(dnr) for dnr in dnrs if dnr}
    if not wanted:
        return {}
    return {normalize_dnr(case.dnr): case for case in Case.objects.filter(dnr__in=wanted)}


def _doc_type_label(doc):
    return _TYPE_LABELS.get(doc.doc_type, doc.doc_type)


def case_view(request, dnr):
    """One initiative end-to-end: its documents, decisions, and votes by dnr."""
    dnr = normalize_dnr(dnr)
    case = get_object_or_404(Case, dnr=dnr)
    current_protocol_ids = list(
        Document.objects.filter(doc_type="protokoll")
        .exclude(current_version=None)
        .values_list("current_version_id", flat=True)
    )
    decisions = sorted(
        (
            decision
            for decision in Decision.objects.filter(document_version_id__in=current_protocol_ids)
            .exclude(printed_dnr="")
            .select_related("document_version__document")
            .annotate(meeting_date=Min("document_version__document__associations__meeting__date"))
            if normalize_dnr(decision.printed_dnr) == dnr
        ),
        key=lambda decision: (decision.meeting_date or datetime.date.min, decision.sequence),
    )

    # Votes matched to decisions by printed agenda number within the same
    # protocol version (document-level attribution: no FK).
    votes_by_key: dict[tuple[int, int], list[Vote]] = defaultdict(list)
    protocol_votes = (
        Vote.objects.filter(document_version_id__in=current_protocol_ids, printed_agenda_number__isnull=False)
        .select_related("document_version__document")
        .prefetch_related("records", "reservations")
        .order_by("document_version_id", "sequence")
    )
    for vote in protocol_votes:
        votes_by_key[(vote.document_version_id, vote.printed_agenda_number)].append(vote)

    decision_rows = []
    for decision in decisions:
        decision_rows.append(
            {
                "decision": decision,
                "document": decision.document_version.document,
                "meeting": (
                    decision.document_version.document.associations.select_related("meeting").first().meeting
                    if decision.document_version.document.associations.exists()
                    else None
                ),
                "votes": votes_by_key.get((decision.document_version_id, decision.printed_agenda_number), []),
            }
        )

    doc_rows = [
        {
            "association": link,
            "type_label": _doc_type_label(link.document),
        }
        for link in case.case_documents.select_related(
            "document", "document__current_version"
        ).order_by("document__doc_type", "document__title")
    ]

    seen_votes: set[int] = set()
    flat_votes = []
    for row in decision_rows:
        for vote in row["votes"]:
            if vote.pk not in seen_votes:
                seen_votes.add(vote.pk)
                flat_votes.append(vote)

    return render(
        request,
        "website/case.html",
        {
            "case": case,
            "doc_rows": doc_rows,
            "decision_rows": decision_rows,
            "votes": _shape_votes(flat_votes),
        },
    )


def agenda_item_view(request, pk):
    """One agenda item: its decision, recorded votes, and attached documents."""
    item = get_object_or_404(
        AgendaItem.objects.select_related("meeting", "meeting__body", "meeting__body__authority"),
        pk=pk,
    )
    meeting = item.meeting
    current_ids = list(
        meeting.document_associations.exclude(document__current_version=None).values_list(
            "document__current_version_id", flat=True
        )
    )
    # Decisions match by printed § number only (document-level attribution).
    decisions = []
    if item.agenda_number is not None:
        decisions = list(
            Decision.objects.filter(document_version_id__in=current_ids, printed_agenda_number=item.agenda_number)
            .select_related("document_version__document")
            .order_by("document_version_id", "sequence")
        )

    # Votes of the meeting's documents bound to this agenda item; votes carry
    # no agenda-item FK, so binding follows each document's association.
    bound_doc_ids = list(
        DocumentAssociation.objects.filter(agenda_item=item).values_list("document_id", flat=True)
    )
    votes = (
        Vote.objects.filter(document_version__document_id__in=bound_doc_ids, document_version_id__in=current_ids)
        .select_related("document_version__document")
        .prefetch_related("records", "reservations")
        .order_by("document_version_id", "sequence")
    )
    shaped_votes = _shape_votes(votes, seat_count=meeting.body.seat_count)
    documents = (
        DocumentAssociation.objects.filter(agenda_item=item)
        .select_related("document", "document__current_version")
    )
    document_rows = [
        {
            "association": association,
            "type_label": _doc_type_label(association.document),
        }
        for association in documents
    ]
    cases = _cases_by_dnr(decision.printed_dnr for decision in decisions)
    decision_rows = [
        {
            "decision": decision,
            "document": decision.document_version.document,
            "case": cases.get(normalize_dnr(decision.printed_dnr)),
        }
        for decision in decisions
    ]

    return render(
        request,
        "website/agenda_item.html",
        {
            "item": item,
            "meeting": meeting,
            "decision_rows": decision_rows,
            "votes": shaped_votes,
            "document_rows": document_rows,
        },
    )


def party_view(request, code):
    """Voting statistics for one party across current document versions."""
    code = code.strip().upper()
    current_ids = list(
        Document.objects.exclude(current_version=None).values_list("current_version_id", flat=True)
    )
    records = list(
        VoteRecord.objects.filter(party_code=code, vote__document_version_id__in=current_ids)
        .select_related("vote", "vote__document_version__document")
    )
    if not records:
        raise Http404("Ingen röstning registrerad för partiet.")

    stance_counts = {stance: 0 for stance in _STANCE_ORDER}
    members: dict[str, int] = {}
    stances_by_vote: dict[int, Counter] = defaultdict(Counter)
    for record in records:
        stance_counts[record.stance] += 1
        members[record.member_name] = members.get(record.member_name, 0) + 1
        stances_by_vote[record.vote_id][record.stance] += 1

    # Overall ja/nej per vote (tallies when printed, else the named records).
    vote_ids = list(stances_by_vote)
    overall: dict[int, Counter] = defaultdict(Counter)
    for row in VoteRecord.objects.filter(vote_id__in=vote_ids, stance__in=("ja", "nej")).values(
        "vote_id", "stance"
    ):
        overall[row["vote_id"]][row["stance"]] += 1

    unanimous = 0
    decided = 0
    won = 0
    for vote_id, positions in stances_by_vote.items():
        own = {s: n for s, n in positions.items() if s in ("ja", "nej")}
        if not own:
            continue
        decided += 1
        if len(own) == 1:
            unanimous += 1
        party_side = max(own, key=own.get)
        vote = next(v for v in records if v.vote_id == vote_id).vote
        yes = vote.tally_yes if vote.tally_yes is not None else overall[vote_id]["ja"]
        no = vote.tally_no if vote.tally_no is not None else overall[vote_id]["nej"]
        if yes != no and party_side == ("ja" if yes > no else "nej"):
            won += 1

    votes = (
        Vote.objects.filter(pk__in=vote_ids)
        .annotate(meeting_date=Min("document_version__document__associations__meeting__date"))
        .select_related("document_version__document")
        .order_by("-meeting_date", "document_version_id", "sequence")
    )
    rows = []
    for vote in votes[:15]:
        positions = stances_by_vote[vote.pk]
        parts = [
            f"{positions[stance]} {_STANCE_PART_LABELS[stance]}"
            for stance in _STANCE_ORDER
            if positions.get(stance)
        ]
        rows.append(
            {
                "vote": vote,
                "document": vote.document_version.document,
                "subject": vote.agenda_title or vote.proposition or "Röstning",
                "position": " · ".join(parts),
            }
        )

    member_rows = [
        {"name": name, "slug": slugify(name), "count": count}
        for name, count in sorted(members.items(), key=lambda pair: (-pair[1], pair[0]))
    ]
    return render(
        request,
        "website/party.html",
        {
            "code": code,
            "label": party_label(code),
            "css": party_css_class(code),
            "stance_counts": [
                {"label": _STANCE_LABELS[stance], "count": stance_counts[stance]}
                for stance in _STANCE_ORDER
            ],
            "vote_count": len(vote_ids),
            "unanimity_pct": round(100 * unanimous / decided) if decided else None,
            "win_pct": round(100 * won / decided) if decided else None,
            "decided_count": decided,
            "members": member_rows,
            "rows": rows,
        },
    )


def document_detail_view(request, pk):
    document = get_object_or_404(
        Document.objects.prefetch_related("versions", "associations"), pk=pk
    )
    versions = document.versions.order_by("-retrieved_at", "-id")
    current = document.current_version
    has_text = (
        current is not None
        and current.processing_status == DocumentVersion.Status.PROCESSED
        and bool(current.markdown)
    )
    markdown_html = _markdown(current.markdown) if has_text else ""
    associations = document.associations.select_related(
        "meeting", "meeting__body", "meeting__body__authority", "agenda_item"
    )
    cases = list(
        Case.objects.filter(case_documents__document=document).order_by("dnr")
    )
    votes = []
    if has_text:
        seat_count = next(
            (
                association.meeting.body.seat_count
                for association in associations
                if association.meeting.body.seat_count
            ),
            None,
        )
        votes = _shape_votes(
            Vote.objects.filter(document_version=current)
            .prefetch_related("records", "reservations")
            .order_by("sequence"),
            seat_count=seat_count,
        )
    return render(
        request,
        "website/document_detail.html",
        {
            "document": document,
            "versions": versions,
            "current_version": current,
            "markdown_html": markdown_html,
            "associations": associations,
            "votes": votes,
            "cases": cases,
            "doc_type_label": _doc_type_label(document),
        },
    )


def version_detail_view(request, version_pk):
    version = get_object_or_404(DocumentVersion.objects.select_related("document"), pk=version_pk)
    markdown_html = _markdown(version.markdown) if version.markdown else ""
    return render(
        request,
        "website/version_detail.html",
        {"version": version, "markdown_html": markdown_html, "document": version.document},
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
        "indexed_version_count": DocumentVersion.objects.exclude(markdown="").count(),
        "fts_rows": service.fts_row_count(),
        "version_counts": version_counts,
        "ocr_count": DocumentVersion.objects.filter(ocr_used=True).count(),
        "last_run": IngestionRun.objects.exclude(status=IngestionRun.Status.RUNNING).first(),
        "recent_runs": IngestionRun.objects.all()[:10],
    }
    return render(request, "website/status.html", context)