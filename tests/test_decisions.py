"""Decision extraction (stage C): parser, pipeline wiring, and decision UI."""

import pytest
from django.urls import reverse

from core.models import AgendaItem, Chunk, Decision, DocumentVersion, Meeting, VoteRecord
from ingestion import decisions, pipeline

pytestmark = pytest.mark.django_db


JA_LIST = (
    "Ja-röst avger:\n"
    "Erik Pelling (S), Eva Christiernin (S), Caitlin Mc Evoy (S), Therez Almerfors (M)."
)
NEJ_LIST = "Nej-röster avger:\nSofia Andersson (M) och Marta Obminska (M)."


def _chunk(text, pk=1, agenda_item=None):
    return {"pk": pk, "agenda_item": agenda_item, "text": text}


def test_parse_decision_blocks():
    text = (
        "§ 1 Val av justerare samt justeringsdag Beslut Kommunfullmäktige beslutar 1. att utse "
        "Hilde Klasson (S) till justerare. Beslutsgång Ordföranden finner att det finns ett förslag. "
        "§ 2 Interpellation om Sommarteatern Beslut Kommunfullmäktige beslutar 1. att bordlägga ärendet. "
        "§ 3 Svar på motion om utredning KSN-2026-00123 Beslut Kommunfullmäktige beslutar 1. att besvara "
        "motionen med föredragningen i ärendet. Ärendet Motionen yrkar om utredning. Beslutsgång "
        "Ordföranden finner att det finns ett förslag till beslut."
    )
    parsed = decisions.parse_decisions([_chunk(text)])
    assert len(parsed) == 3
    first, second, third = parsed
    assert first.printed_agenda_number == 1
    assert first.decision_text.startswith("Kommunfullmäktige beslutar 1. att utse")
    assert first.decision_text.endswith("till justerare.")  # cut at Beslutsgång
    assert first.outcome == "annat"
    assert second.outcome == "bordlaggning"
    assert third.printed_dnr == "KSN-2026-00123"
    assert third.outcome == "avslag"  # "besvara motionen med föredragningen"
    assert third.decision_text.endswith("i ärendet.")  # cut at Ärendet


def test_parse_decisions_skips_versions_without_headings():
    assert decisions.parse_decisions([_chunk("Inga beslut här.")]) == []


def _seed_protocol(document, version, text):
    document.doc_type = "protokoll"
    document.save(update_fields=["doc_type"])
    version.processing_status = DocumentVersion.Status.PROCESSED
    version.save(update_fields=["processing_status"])
    Chunk.objects.create(
        document_version=version,
        sequence=999,
        title=document.title,
        text=text,
        page_start=1,
        page_end=2,
        processing_version="test",
    )


def test_extract_decisions_pipeline_and_idempotency(source_tree, stored_version):
    _, _, meeting = source_tree
    document, version = stored_version
    item = AgendaItem.objects.create(
        meeting=meeting,
        agenda_number=3,
        title="Svar på motion om utredning",
    )
    text = (
        "§ 3 Svar på motion om utredning KSN-2026-00123 Beslut Kommunfullmäktige beslutar "
        "1. att besvara motionen med föredragningen i ärendet. Ärendet Motionen yrkar. "
        "Beslutsgång Ordföranden finner att det finns ett förslag till beslut."
    )
    _seed_protocol(document, version, text)
    run = pipeline.begin_run("test")
    stats = pipeline.extract_decisions(run=run)
    assert stats["versions"] == 1 and stats["decisions"] == 1
    decision = Decision.objects.get()
    assert decision.agenda_item_id == item.pk
    assert decision.printed_dnr == "KSN-2026-00123"
    assert decision.outcome == "avslag"
    version.refresh_from_db()
    assert version.decision_parser_version == decisions.DECISION_PARSER_VERSION

    stats2 = pipeline.extract_decisions(run=pipeline.begin_run("test"))
    assert stats2["versions"] == 0 and Decision.objects.count() == 1
    stats3 = pipeline.extract_decisions(run=pipeline.begin_run("test"), force=True)
    assert stats3["decisions"] == 1 and Decision.objects.count() == 1


def test_extract_decisions_skips_non_protocol_documents(source_tree, stored_version):
    """Committee reports print the same §-headings, but as proposals — the
    decision pipeline covers protocols only."""
    document, version = stored_version
    _seed_protocol(
        document,
        version,
        "§ 3 Förslag till beslut Beslut Kommunstyrelsen föreslår kommunfullmäktige besluta "
        "1. att besvara motionen med föredragningen. Beslutsgång Ordföranden finner att det "
        "finns ett förslag till beslut.",
    )
    document.doc_type = "arende"
    document.save(update_fields=["doc_type"])
    stats = pipeline.extract_decisions(run=pipeline.begin_run("test"), force=True)
    assert stats["versions"] == 0 and stats["decisions"] == 0
    assert Decision.objects.count() == 0


def test_agenda_item_page_shows_decision_and_votes(client, source_tree, stored_version):
    _, _, meeting = source_tree
    document, version = stored_version
    item = AgendaItem.objects.create(
        meeting=meeting,
        agenda_number=18,
        title="Svar på motion om utredning",
    )
    text = (
        "§ 18 Svar på motion om utredning KSN-2026-00123 Beslut Kommunfullmäktige beslutar "
        "1. att bifalla kommunstyrelsens förslag. Beslutsgång Ordföranden finner att det finns "
        "ett förslag till beslut. Omröstning begärs och genomförs. "
        "Med 4 ja-röster mot 2 nej-röster beslutades bifall. "
        f"Omröstningsdetaljer\n{JA_LIST}\n{NEJ_LIST}"
    )
    _seed_protocol(document, version, text)
    pipeline.extract_decisions(run=pipeline.begin_run("test"), version_ids=[version.id])
    pipeline.extract_votes(run=pipeline.begin_run("test"), version_ids=[version.id])

    res = client.get(reverse("website:agenda-item", args=[item.pk]))
    assert res.status_code == 200
    content = res.content.decode()
    assert "besvara" not in content and "bifalla kommunstyrelsens förslag" in content
    assert "vote-type-bifall" in content
    assert "Röstningar" in content and 'id="vote-' in content
    assert reverse("website:document-detail", args=[document.pk]) in content

    res = client.get(reverse("website:agenda-item", args=[item.pk + 9999]))
    assert res.status_code == 404


def test_decisions_feed_and_filters(client, stored_version):
    document, version = stored_version
    text = (
        "§ 5 Svar på motion om utredning Beslut Kommunfullmäktige beslutar 1. att besvara "
        "motionen med föredragningen i ärendet. Ärendet Motionen yrkar. Beslutsgång Ordföranden "
        "finner att det finns ett förslag till beslut."
    )
    _seed_protocol(document, version, text)
    pipeline.extract_decisions(run=pipeline.begin_run("test"), version_ids=[version.id])

    res = client.get(reverse("website:decisions"))
    content = res.content.decode()
    assert "besvara motionen med föredragningen" in content
    assert "vote-type-avslag" in content

    res = client.get(reverse("website:decisions"), {"typ": "bifall"})
    assert "Inga beslut matchar filtret" in res.content.decode()

    res = client.get(reverse("website:decisions"), {"ar": "1999"})
    assert "Inga beslut matchar filtret" in res.content.decode()


def test_meeting_page_shows_decision_digest(client, source_tree, stored_version):
    _, _, meeting = source_tree
    document, version = stored_version
    AgendaItem.objects.create(meeting=meeting, agenda_number=5, title="Svar på motion om utredning")
    text = (
        "§ 5 Svar på motion om utredning Beslut Kommunfullmäktige beslutar 1. att besvara "
        "motionen med föredragningen i ärendet. Ärendet Motionen yrkar. Beslutsgång Ordföranden "
        "finner att det finns ett förslag till beslut."
    )
    _seed_protocol(document, version, text)
    pipeline.extract_decisions(run=pipeline.begin_run("test"), version_ids=[version.id])

    res = client.get(reverse("website:meeting-detail", args=[meeting.pk]))
    content = res.content.decode()
    assert "agenda-decision" in content
    assert "besvara motionen med föredragningen" in content
    assert reverse("website:agenda-item", args=[999999]) not in content  # no unrelated links


def test_member_stats_alignment_and_deviations(client, stored_version):
    document, version = stored_version
    text = (
        "§ 9 Ärende för diskussion Beslut Kommunfullmäktige beslutar 1. att anteckna "
        "diskussionen. Beslutsgång Ordföranden finner att det finns ett förslag till beslut. "
        f"Omröstningsdetaljer\n{JA_LIST}\n{NEJ_LIST}"
    )
    _seed_protocol(document, version, text)
    pipeline.extract_votes(run=pipeline.begin_run("test"), version_ids=[version.id])

    res = client.get(reverse("website:member", args=["therez-almerfors"]))
    content = res.content.decode()
    # Therez (M) voted ja while her party's majority (2 of 3 M members) voted nej.
    assert "avvikelser från partilinjen" in content
    assert "på vinnande sida i" in content

    res = client.get(reverse("website:member", args=["erik-pelling"]))
    assert "avvikelser från partilinjen" not in res.content.decode()  # voted with the S line


def test_party_page_stats(client, stored_version):
    document, version = stored_version
    text = (
        "§ 9 Ärende för diskussion Beslut Kommunfullmäktige beslutar 1. att anteckna "
        "diskussionen. Beslutsgång Ordföranden finner att det finns ett förslag till beslut. "
        f"Omröstningsdetaljer\n{JA_LIST}\n{NEJ_LIST}"
    )
    _seed_protocol(document, version, text)
    pipeline.extract_votes(run=pipeline.begin_run("test"), version_ids=[version.id])

    res = client.get(reverse("website:party", args=["s"]))
    assert res.status_code == 200
    content = res.content.decode()
    assert "Socialdemokraterna" in content
    assert "3 ja" in content
    assert "enighet i 100 %" in content  # all three S members voted ja
    assert "på vinnande sida i 100 %" in content  # ja (3) beat nej (2)
    assert reverse("website:member", args=["erik-pelling"]) in content

    res = client.get(reverse("website:party", args=["xx"]))
    assert res.status_code == 404
