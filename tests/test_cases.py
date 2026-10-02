"""Case building: dnr stitching, title pairing, and the case page UI."""

import pytest
from django.urls import reverse

from core.models import (
    AgendaItem,
    Case,
    CaseDocument,
    Decision,
    Document,
    DocumentVersion,
)
from ingestion import pipeline

pytestmark = pytest.mark.django_db

JA_LIST = (
    "Ja-röst avger:\nErik Pelling (S), Eva Christiernin (S), Caitlin Mc Evoy (S), Therez Almerfors (M)."
)
NEJ_LIST = "Nej-röster avger:\nSofia Andersson (M) och Marta Obminska (M)."

URL_COUNTER = {"n": 0}


def _seed_markdown(document, version, text):
    version.processing_status = DocumentVersion.Status.PROCESSED
    version.markdown = text
    version.save(update_fields=["processing_status", "markdown"])


def _make_document(title, doc_type):
    URL_COUNTER["n"] += 1
    document = Document.objects.create(
        title=title,
        doc_type=doc_type,
        primary_source_url=f"https://uppsala.se/test/{URL_COUNTER['n']}",
    )
    version = DocumentVersion.objects.create(
        document=document,
        original_url=document.primary_source_url,
        processing_status=DocumentVersion.Status.PROCESSED,
    )
    document.current_version = version
    document.save(update_fields=["current_version"])
    return document, version


def test_build_cases_links_documents_by_dnr(stored_version):
    document, version = stored_version
    _seed_markdown(document, version, "Utredning enligt motion KSN-2024-03238, bilaga 1.")
    motion, motion_version = _make_document("5. Inlämnade motioner till kommunfullmäktige", "motion")
    _seed_markdown(motion, motion_version, "Motion KSN-2024-03238 om att utreda en breddning.")

    stats = pipeline.build_cases()
    assert stats["cases"] == 1
    case = Case.objects.get()
    assert case.dnr == "KSN-2024-03238"
    assert case.kind == "motion"  # title mentions motioner; motion doc linked
    assert case.title.endswith("Inlämnade motioner till kommunfullmäktige")
    assert set(CaseDocument.objects.values_list("document__pk", flat=True)) == {document.pk, motion.pk}

    # Re-running rebuilds links without duplicating.
    pipeline.build_cases()
    assert Case.objects.count() == 1 and CaseDocument.objects.count() == 2


def test_build_cases_pairs_interpellation_by_title(source_tree, stored_version):
    _, _, meeting = source_tree
    document, version = stored_version
    document.doc_type = "protokoll"
    document.save(update_fields=["doc_type"])
    AgendaItem.objects.create(
        meeting=meeting,
        agenda_number=12,
        title="Svar på interpellation om Saturnus från Emil Eneblad (SD)",
    )
    _seed_markdown(
        document,
        version,
        "§ 12 Svar på interpellation om Saturnus från Emil Eneblad (SD) KSN-2026-00999 Beslut "
        "Kommunfullmäktige beslutar 1. att besvara interpellationen med föredragningen. "
        "Ärendet Interpellationen gällde Saturnus. Beslutsgång Ordföranden finner att det "
        "finns ett förslag till beslut.",
    )
    pipeline.extract_decisions(run=pipeline.begin_run("test"), version_ids=[version.id])
    assert Decision.objects.get().printed_agenda_number == 12

    interp, interp_version = _make_document("11. Interpellation om Saturnus", "interpellation")
    _seed_markdown(interp, interp_version, "Interpellation om Saturnus, ställd av Emil Eneblad.")

    stats = pipeline.build_cases()
    assert stats["paired"] == 1
    case = Case.objects.get(dnr="KSN-2026-00999")
    assert case.title.startswith("Svar på interpellation om Saturnus")
    assert case.kind == "interpellation"
    assert list(CaseDocument.objects.values_list("document__pk", flat=True)) == [interp.pk]


_PROTOCOL_TEXT = (
    "§ 12 Svar på interpellation om Saturnus från Emil Eneblad (SD) KSN-2026-00999 Beslut "
    "Kommunfullmäktige beslutar 1. att besvara interpellationen med föredragningen. "
    "Ärendet Interpellationen gällde Saturnus. Beslutsgång Ordföranden finner att det finns "
    "ett förslag till beslut. Omröstning begärs och genomförs. "
    "Med 4 ja-röster mot 2 nej-röster beslutades bifall. "
    f"Omröstningsdetaljer\n{JA_LIST}\n{NEJ_LIST}"
)


def test_case_page_shows_documents_decisions_and_votes(client, source_tree, stored_version):
    _, _, meeting = source_tree
    document, version = stored_version
    document.doc_type = "protokoll"
    document.save(update_fields=["doc_type"])
    AgendaItem.objects.create(
        meeting=meeting,
        agenda_number=12,
        title="Svar på interpellation om Saturnus från Emil Eneblad (SD)",
    )
    _seed_markdown(document, version, _PROTOCOL_TEXT)
    interp, interp_version = _make_document("11. Interpellation om Saturnus", "interpellation")
    _seed_markdown(interp, interp_version, "Interpellation om Saturnus, ställd av Emil Eneblad.")
    pipeline.extract_decisions(run=pipeline.begin_run("test"), version_ids=[version.id])
    pipeline.extract_votes(run=pipeline.begin_run("test"), version_ids=[version.id])
    pipeline.build_cases()

    res = client.get(reverse("website:case", args=["KSN-2026-00999"]))
    assert res.status_code == 200
    content = res.content.decode()
    assert "KSN-2026-00999" in content
    assert "besvara interpellationen med föredragningen" in content
    assert "§12" in content  # printed agenda number shown per decision entry
    assert "Röstningar" in content and 'id="vote-' in content
    assert reverse("website:document-detail", args=[document.pk]) in content

    # dnr normalization: lowercase with odd spacing resolves to the same case
    res = client.get(reverse("website:case", args=[" ksn-2026-00999 "]))
    assert res.status_code == 200
    res = client.get(reverse("website:case", args=["KSN-0000-00000"]))
    assert res.status_code == 404


def test_case_links_on_agenda_item_document_and_feed_pages(client, source_tree, stored_version):
    _, _, meeting = source_tree
    document, version = stored_version
    document.doc_type = "protokoll"
    document.save(update_fields=["doc_type"])
    item = AgendaItem.objects.create(
        meeting=meeting,
        agenda_number=12,
        title="Svar på interpellation om Saturnus från Emil Eneblad (SD)",
    )
    _seed_markdown(document, version, _PROTOCOL_TEXT)
    report, report_version = _make_document(
        "12. Svar på interpellation om Saturnus från Emil Eneblad (SD)", "arende"
    )
    _seed_markdown(report, report_version, "Förslag: besvara interpellationen KSN-2026-00999 med föredragningen.")
    pipeline.extract_decisions(run=pipeline.begin_run("test"), version_ids=[version.id])
    pipeline.build_cases()
    case = Case.objects.get()

    res = client.get(reverse("website:agenda-item", args=[item.pk]))
    content = res.content.decode()
    assert "Ärendet:" in content
    assert reverse("website:case", args=[case.dnr]) in content

    res = client.get(reverse("website:document-detail", args=[report.pk]))
    content = res.content.decode()
    assert "Ärende:" in content
    assert reverse("website:case", args=[case.dnr]) in content

    res = client.get(reverse("website:decisions"))
    content = res.content.decode()
    assert reverse("website:case", args=[case.dnr]) in content