"""Case building (stage C): stitch initiatives end-to-end by diarienummer.

The dnr is printed in the initiating document, the committee report, and the
protocol's decision heading, so it identifies the case across the whole chain.
This pass scans current document versions for dnr mentions, links the
documents to cases, and pairs initiative documents that never print a dnr
(interpellations/frågor) with the agenda item of their answer via titles
("Interpellation om X" vs "Svar på interpellation om X"). Decisions and votes
reach the case through the same dnr / agenda items at view time. Re-running
rebuilds the document links; cases themselves are keyed on the dnr.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict

from core.dnrs import DNR_RE, clean_title, normalize_dnr

log = logging.getLogger(__name__)

# Document types that can belong to a case. Protocols reach cases through
# their decisions' printed dnr; kallese/föredragningslista only list items.
CASE_DOC_TYPES = ("motion", "interpellation", "fraga", "arende", "yttrande", "bilaga")

# Title precedence when several documents share a case: the initiating
# document describes the case better than later treatment documents.
_KIND_PRIORITY = {"motion": 0, "interpellation": 1, "fraga": 2, "arende": 3, "yttrande": 4, "bilaga": 5}

_MIN_PAIR_TITLE_CHARS = 10


def _normalized_title(title: str) -> str:
    return re.sub(r"\s+", " ", clean_title(title)).lower().rstrip(".")


def _pair_title(title: str) -> str:
    """The title with a leading "Svar på " removed, for question/answer pairing."""
    t = _normalized_title(title)
    return t.removeprefix("svar på ")


def _kind_from_title(title: str) -> str:
    t = title.lower()
    if "motion" in t:
        return "motion"
    if "interpellation" in t:
        return "interpellation"
    if "fråga" in t or "fraga" in t:
        return "fraga"
    return "arende"


def _document_texts(docs) -> dict[int, str]:
    """Document id -> IR markdown of the current version."""
    from core.models import DocumentVersion

    versions = [doc.current_version_id for doc in docs if doc.current_version_id]
    markdown_by_version = {
        row[0]: row[1]
        for row in DocumentVersion.objects.filter(id__in=versions).values_list("id", "markdown")
    }
    doc_by_version = {doc.current_version_id: doc.pk for doc in docs if doc.current_version_id}
    texts: dict[int, str] = {doc.pk: "" for doc in docs}
    for version_id, doc_pk in doc_by_version.items():
        texts[doc_pk] = markdown_by_version.get(version_id, "")
    return texts


def build_cases() -> dict:
    """(Re)build Case/CaseDocument rows from dnr mentions and title pairing."""
    from core.models import Case, CaseDocument, Decision, Document
    from django.db import transaction

    docs = list(
        Document.objects.exclude(current_version=None)
        .filter(doc_type__in=CASE_DOC_TYPES)
        .select_related("current_version")
    )
    texts = _document_texts(docs)
    dnrs_by_doc: dict[int, set[str]] = {
        doc.pk: {normalize_dnr(m) for m in DNR_RE.findall(texts[doc.pk])} for doc in docs
    }

    # Decisions of current protocol versions: dnrs per printed agenda-item
    # title, for pairing initiative documents that lack their own dnr.
    current_protocol_ids = list(
        Document.objects.filter(doc_type="protokoll")
        .exclude(current_version=None)
        .values_list("current_version_id", flat=True)
    )
    decision_rows = list(
        Decision.objects.filter(document_version_id__in=current_protocol_ids)
        .exclude(printed_dnr="")
        .values("pk", "printed_dnr", "printed_agenda_title")
    )
    dnr_decisions: dict[str, list[dict]] = defaultdict(list)
    dnr_by_item_title: dict[str, list[str]] = defaultdict(list)
    for row in decision_rows:
        dnr = normalize_dnr(row["printed_dnr"])
        dnr_decisions[dnr].append(row)
        if row["printed_agenda_title"]:
            dnr_by_item_title[_pair_title(row["printed_agenda_title"])].append(dnr)

    # Pair interpellations/frågor without a dnr to their answer's agenda item.
    paired = 0
    for doc in docs:
        if dnrs_by_doc[doc.pk] or doc.doc_type not in ("interpellation", "fraga"):
            continue
        title = _pair_title(doc.title)
        if len(title) < _MIN_PAIR_TITLE_CHARS:
            continue
        candidates = dnr_by_item_title.get(title)
        if not candidates:
            for item_title, item_dnrs in dnr_by_item_title.items():
                if len(item_title) >= _MIN_PAIR_TITLE_CHARS and (
                    item_title.startswith(title) or title.startswith(item_title)
                ):
                    candidates = item_dnrs
                    break
        if candidates:
            dnrs_by_doc[doc.pk] = {candidates[0]}
            paired += 1

    all_dnrs = {dnr for dnrs in dnrs_by_doc.values() for dnr in dnrs} | set(dnr_decisions)
    with transaction.atomic():
        CaseDocument.objects.all().delete()
        cases_by_dnr = {case.dnr: case for case in Case.objects.all()}
        for dnr in all_dnrs:
            if dnr not in cases_by_dnr:
                cases_by_dnr[dnr] = Case.objects.create(dnr=dnr)

        docs_by_dnr: dict[str, list] = defaultdict(list)
        for doc in docs:
            for dnr in dnrs_by_doc[doc.pk]:
                docs_by_dnr[dnr].append(doc)

        links = []
        for dnr in sorted(all_dnrs):
            case = cases_by_dnr[dnr]
            case_docs = sorted(
                docs_by_dnr.get(dnr, []),
                key=lambda d: (_KIND_PRIORITY.get(d.doc_type, 9), d.title),
            )
            item_titles = [
                row["printed_agenda_title"]
                for row in dnr_decisions.get(dnr, [])
                if row["printed_agenda_title"]
            ]
            title_source = item_titles[0] if item_titles else (
                case_docs[0].title if case_docs else ""
            )
            title = clean_title(title_source)
            kind = _kind_from_title(title)
            if kind == "arende":
                for doc in case_docs:
                    if doc.doc_type in ("motion", "interpellation", "fraga"):
                        kind = doc.doc_type
                        break
            Case.objects.filter(pk=case.pk).update(title=title[:400], kind=kind)
            links.extend(
                CaseDocument(case=case, document=doc) for doc in case_docs
            )
        CaseDocument.objects.bulk_create(links, ignore_conflicts=True)
        # Cases whose dnr no longer appears anywhere are dropped.
        Case.objects.exclude(dnr__in=all_dnrs).delete()

    return {
        "cases": Case.objects.count(),
        "links": len(links),
        "paired": paired,
    }
