"""Vote extraction (stage C): parser, pipeline wiring, and document page UI."""

import pytest
from django.urls import reverse

from core.models import AgendaItem, Chunk, DocumentAssociation, DocumentVersion, Vote, VoteRecord
from ingestion import pipeline, votes
from search import service

pytestmark = pytest.mark.django_db


JA_LIST = (
    "Ja-röst avger:\n"
    "Erik Pelling (S), Tobias Smedberg (V), Caitlin Mc Evoy (S), Asal\n"
    "Gohari (S), Karin Karelius (V) och Susanne Engström (MP)."
)
NEJ_LIST = "Nej-röster avger:\nTherez Almerfors (M) och Kent Kumpula (SD)."


def _chunk(text, pk=1, agenda_item=None):
    return {"pk": pk, "agenda_item": agenda_item, "text": text}


def test_parses_labeled_lists():
    parsed = votes.parse_version([_chunk(f"Reservationer Moderaterna (M) reserverar sig.\nOmröstningsdetaljer\n{JA_LIST}\n{NEJ_LIST}")])
    assert len(parsed) == 1
    vote = parsed[0]
    assert vote.has_details
    assert [r.stance for r in vote.records] == ["ja"] * 6 + ["nej"] * 2
    assert vote.records[0].member_name == "Erik Pelling"
    assert vote.records[0].party_code == "S"
    assert vote.records[2].member_name == "Caitlin Mc Evoy"  # line break inside list
    assert vote.records[3].member_name == "Asal Gohari"  # name split by line break
    assert vote.records[5].member_name == "Susanne Engström"  # " och" lead-in
    assert vote.records[6].member_name == "Therez Almerfors"
    assert vote.records[7].party_code == "SD"
    # Narrative before/after the lists must not leak in as voters.
    assert "Moderaterna" not in [r.member_name for r in vote.records]


def test_unknown_party_code_and_absent_list():
    text = f"Omröstningsdetaljer\n{JA_LIST}\nFrånvarande:\nStefan Hanna (UP) och Lina Hamberg (UP)."
    parsed = votes.parse_version([_chunk(text)])
    assert len(parsed) == 1
    assert parsed[0].has_details
    assert [r.party_code for r in parsed[0].records][-2:] == ["UP", "UP"]
    assert [r.stance for r in parsed[0].records][-2:] == ["franvarande", "franvarande"]


def test_block_straddling_chunks_with_page_noise():
    first = _chunk("Omröstningsdetaljer\nJa-röst avger:\nErik Pelling (S), Kent", pk=1)
    second = _chunk("Sida 27 (73) Protokoll 2025-02-24\nKumpula (SD) och Annika Jonsson (KD).", pk=2)
    parsed = votes.parse_version([first, second])
    assert len(parsed) == 1
    ja = [r for r in parsed[0].records if r.stance == "ja"]
    assert [(r.member_name, r.party_code) for r in ja] == [
        ("Erik Pelling", "S"),
        ("Kent Kumpula", "SD"),
        ("Annika Jonsson", "KD"),
    ]
    assert parsed[0].chunk_index == 0  # attributed to the chunk where the block starts


def test_merged_page_header_does_not_eat_the_block():
    # The chunker can merge a short page header into the paragraph line that
    # carries the marker; stripping the header must not eat the block too.
    text = (
        "Sida 26 (73) Protokoll 2025-02-24 Ordföranden finner att motionen avslås. "
        "Omröstningsdetaljer\nJa-röst avger:\nErik Pelling (S) och Tobias Smedberg (V)."
    )
    parsed = votes.parse_version([_chunk(text)])
    assert len(parsed) == 1
    assert [r.member_name for r in parsed[0].records] == ["Erik Pelling", "Tobias Smedberg"]


def test_tally_only_vote_without_name_lists():
    text = (
        "Omröstning begärs och genomförs.\n"
        "Kommunfullmäktige godkänner följande omröstningsproposition:\n"
        "Rösta Ja för att avslå tilläggsyrkandet, rösta Nej för att bifalla tilläggsyrkandet.\n"
        "Med 39 ja-röster mot 40 nej-röster beslutades avslag."
    )
    parsed = votes.parse_version([_chunk(text)])
    assert len(parsed) == 1
    vote = parsed[0]
    assert not vote.has_details
    assert vote.tally_yes == 39 and vote.tally_no == 40
    assert "Rösta Ja för att avslå" in vote.proposition
    assert vote.outcome_text.startswith("Med 39 ja-röster")
    assert vote.records_match_tally is None


def test_unmatched_gap_tally_stands_alone():
    # The tally (2/1) does not match the named lists (6 ja / 2 nej), so it is
    # not attached to the block: two honest rows instead of a false pairing.
    text = (
        "Rösta Ja för avslag, rösta Nej för bifall.\n"
        "Med 2 ja-röster mot 1 nej-röster beslutades avslag.\n"
        "Omröstningsdetaljer\n"
        f"{JA_LIST}\n"
    )
    parsed = votes.parse_version([_chunk(text)])
    assert len(parsed) == 2
    standalone, block = parsed
    assert block.has_details and standalone.tally_yes == 2
    assert block.tally_yes is None
    assert block.records_match_tally is None


def test_matching_tally_verifies():
    text = (
        "Omröstningsdetaljer\n"
        "Ja-röst avger:\nErik Pelling (S) och Tobias Smedberg (V).\n"
        "Nej-röst avger:\nTherez Almerfors (M).\n"
    )
    parsed = votes.parse_version([_chunk("Med 2 ja-röster mot 1 nej-röster beslutades avslag.\n" + text)])
    vote = parsed[0]
    assert vote.has_details
    assert vote.records_match_tally is True


def test_multiple_blocks_and_standalone_between():
    first = f"Omröstningsdetaljer\nJa-röst avger:\nErik Pelling (S).\n"
    middle = "Med 1 ja-röster mot 0 nej-röster beslutades bifall.\n"
    second = f"Omröstningsdetaljer\nNej-röst avger:\nKent Kumpula (SD).\n"
    parsed = votes.parse_version([_chunk(first + middle + second)])
    # The middle tally matches neither block's lists, so it stands alone
    # between the two block votes.
    assert len(parsed) == 3
    assert parsed[0].records[0].party_code == "S"
    assert parsed[1].has_details is False and parsed[1].tally_yes == 1
    assert parsed[2].records[0].party_code == "SD"


def test_rounds_inside_one_block():
    # One "Omröstningsdetaljer" section can carry several numbered rounds, each
    # with its own lists; round 2's lists must not merge into round 1's vote.
    text = (
        "Omröstningsdetaljer Omröstning nr 1\n"
        "Ja-röst avger:\nErik Pelling (S).\nNej-röst avger:\nTherez Almerfors (M).\n"
        "Omröstning nr 2 Nej-röst avger:\nKent Kumpula (SD).\nAvstår:\nStefan Hanna (UP).\n"
    )
    parsed = votes.parse_version([_chunk(text)])
    assert len(parsed) == 2
    assert [(r.stance, r.party_code) for r in parsed[0].records] == [("ja", "S"), ("nej", "M")]
    assert [(r.stance, r.party_code) for r in parsed[1].records] == [("nej", "SD"), ("avstar", "UP")]


def test_tail_tally_becomes_standalone_vote():
    parsed = votes.parse_version([_chunk(f"Omröstningsdetaljer\nJa-röst avger:\nErik Pelling (S).\nMed 1 ja-röster mot 0 nej-röster beslutades bifall.")])
    assert len(parsed) == 2  # the block vote + the tail tally as a standalone vote
    assert parsed[1].has_details is False
    assert parsed[1].tally_yes == 1


def test_vote_type_classification():
    assert votes._classify_vote("", "Med 2 ja-röster mot 1 nej-röster beslutades avslag.") == "avslag"
    # The outcome sentence decides, even when the proposition mentions several types.
    assert (
        votes._classify_vote(
            "Rösta JA för bifall, rösta NEJ för att återremittera ärendet.",
            "Med 3 ja-röster mot 2 nej-röster finner ordföranden att beslutat att återremittera ärendet.",
        )
        == "ateremittera"
    )
    assert votes._classify_vote("", "Kommunfullmäktige valde Anna för ett år framåt.") == "val"
    assert votes._classify_vote("", "Något helt annat.") == "annat"


def test_decision_text_extraction():
    assert (
        votes._decision_text(
            "Med 44 ja-röster mot 36 nej-röster finner ordföranden att kommunfullmäktige "
            "beslutar att bifalla kommunstyrelsens förslag."
        )
        == "bifalla kommunstyrelsens förslag"
    )
    assert votes._decision_text("Med 39 ja-röster mot 40 nej-röster beslutades avslag.") == "avslag"
    assert votes._decision_text("Ingen beslutsmening här.") == ""


def test_reservations_and_yttranden_parsed():
    text = (
        "Omröstning begärs och genomförs.\n"
        "Rösta Ja för bifall till kommunstyrelsens förslag, rösta Nej för bifall till motionen.\n"
        "Med 6 ja-röster mot 2 nej-röster beslutades bifall.\n"
        "Reservationer Närvarande ledamöter (M), (KD) och (SD) reserverar sig mot beslutet.\n"
        "Närvarande ledamöter (C) reserverar sig mot beslutet med följande motivering: "
        "Centerpartiet anser att frågan borde ha utretts mer.\n"
        "Särskilda yttranden Lina Hamberg (UP) inkommer med särskilt yttrande: "
        "Utvecklingspartiet anser att projektet ska skrotas.\n"
        f"Omröstningsdetaljer\n{JA_LIST}\n{NEJ_LIST}"
    )
    parsed = votes.parse_version([_chunk(text)])
    assert len(parsed) == 1
    reservations = parsed[0].reservations
    assert [(r.kind, r.party_codes) for r in reservations] == [
        ("reservation", ["M", "KD", "SD"]),
        ("reservation", ["C"]),
        ("yttrande", ["UP"]),
    ]
    assert reservations[2].member_name == "Lina Hamberg"
    # Heading words must not leak into entry text or names.
    assert not reservations[0].text.startswith("Reservationer")
    assert reservations[0].text.endswith("reserverar sig mot beslutet.")
    assert reservations[1].text.startswith("Närvarande ledamöter (C)")
    assert reservations[2].text.startswith("Lina Hamberg (UP) inkommer")


def test_agenda_item_resolved_from_printed_heading(source_tree, stored_version):
    _, _, meeting = source_tree
    item = AgendaItem.objects.create(
        meeting=meeting,
        agenda_number=18,
        title="Svar på motion om att utreda en breddning av Kungsängsbron från Therez Almerfors (M)",
    )
    document, version = _processed_version(stored_version)
    text = (
        "§ 18 Svar på motion om att utreda en breddning av Kungsängsbron från Therez Almerfors (M) "
        "KSN-2024-03238 Beslut Kommunfullmäktige beslutar att bifalla kommunstyrelsens förslag.\n"
        "Omröstning begärs och genomförs.\n"
        "Med 6 ja-röster mot 2 nej-röster finner ordföranden att kommunfullmäktige beslutar att "
        "bifalla kommunstyrelsens förslag.\n"
        f"Omröstningsdetaljer\n{JA_LIST}\n{NEJ_LIST}"
    )
    Chunk.objects.create(
        document_version=version,
        sequence=999,
        title=document.title,
        text=text,
        page_start=39,
        page_end=40,
        processing_version="test",
    )
    pipeline.extract_votes(run=pipeline.begin_run("test"), version_ids=[version.id])

    vote = Vote.objects.get()
    assert vote.agenda_item_id == item.pk
    assert vote.vote_type == "bifall"
    assert "Kungsängsbron" in vote.agenda_title
    assert vote.decision_text == "bifalla kommunstyrelsens förslag"


def _processed_version(stored_version):
    document, version = stored_version
    version.processing_status = DocumentVersion.Status.PROCESSED
    version.save(update_fields=["processing_status"])
    return document, version


def test_extract_votes_pipeline_and_idempotency(stored_version):
    document, version = _processed_version(stored_version)
    Chunk.objects.create(
        document_version=version,
        sequence=1,
        title=document.title,
        text=f"Omröstningsdetaljer\n{JA_LIST}\n{NEJ_LIST}",
        page_start=1,
        page_end=1,
        processing_version="test",
    )
    run = pipeline.begin_run("test")
    stats = pipeline.extract_votes(run=run)
    assert stats["versions"] == 1
    assert stats["votes"] == 1
    assert stats["records"] == 8
    version.refresh_from_db()
    assert version.vote_parser_version == votes.VOTE_PARSER_VERSION

    # Stamped versions are skipped; re-running creates nothing new.
    stats2 = pipeline.extract_votes(run=pipeline.begin_run("test"))
    assert stats2["versions"] == 0
    assert Vote.objects.count() == 1
    assert VoteRecord.objects.count() == 8

    # --force replaces rows instead of duplicating them.
    stats3 = pipeline.extract_votes(run=pipeline.begin_run("test"), force=True)
    assert stats3["votes"] == 1
    assert Vote.objects.count() == 1
    assert VoteRecord.objects.count() == 8


def test_document_detail_shows_votes_section(client, stored_version):
    document, version = _processed_version(stored_version)
    chunk = Chunk.objects.create(
        document_version=version,
        sequence=1,
        title=f"{document.title} — §12 Trafikfrågan",
        text=f"Omröstningsdetaljer\n{JA_LIST}",
        page_start=23,
        page_end=23,
        processing_version="test",
    )
    pipeline.extract_votes(run=pipeline.begin_run("test"), version_ids=[version.id])
    res = client.get(reverse("website:document-detail", args=[document.pk]))
    assert res.status_code == 200
    content = res.content.decode()
    assert "Röstningar" in content
    assert 'title="Socialdemokraterna"' in content  # party chip with full name
    assert 'class="party-position"' in content  # party-line chips replace the counts table
    assert "Erik Pelling" in content
    assert reverse("website:member", args=["erik-pelling"]) in content  # name links to member page
    assert f'id="chunk-{chunk.pk}"' in content
    assert f'href="#chunk-{chunk.pk}"' in content
    assert "ai-vote-button" in content  # per-vote AI explainer


def test_document_detail_without_votes_hides_section(client, stored_version):
    document, version = _processed_version(stored_version)
    res = client.get(reverse("website:document-detail", args=[document.pk]))
    assert res.status_code == 200
    assert '<section class="votes">' not in res.content.decode()


def _seed_vote_chunks(document, version, with_agenda_item=None):
    Chunk.objects.create(
        document_version=version,
        sequence=999,  # past any chunks created by a real extraction
        title=f"{document.title} — §12 Trafikfrågan",
        text=f"Omröstningsdetaljer\n{JA_LIST}\n{NEJ_LIST}",
        page_start=23,
        page_end=23,
        processing_version="test",
        agenda_item=with_agenda_item,
    )


def test_votes_page_lists_votes_and_filters(client, stored_version):
    document, version = _processed_version(stored_version)
    _seed_vote_chunks(document, version)
    pipeline.extract_votes(run=pipeline.begin_run("test"), version_ids=[version.id])

    res = client.get(reverse("website:votes"))
    assert res.status_code == 200
    content = res.content.decode()
    assert document.title in content
    assert "Socialdemokraterna" in content  # party options + strip labels
    assert 'href="/dokument/' in content

    # Party filter: the vote has both S and M records; C has none.
    res_s = client.get(reverse("website:votes"), {"parti": "S"})
    assert document.title in res_s.content.decode()
    res_c = client.get(reverse("website:votes"), {"parti": "C"})
    assert "Inga röstningar matchar filtret" in res_c.content.decode()

    # Year filter follows the meeting date (2025-01-27 in the fixture).
    res_year = client.get(reverse("website:votes"), {"ar": "2025"})
    assert document.title in res_year.content.decode()
    res_old = client.get(reverse("website:votes"), {"ar": "2024"})
    assert "Inga röstningar matchar filtret" in res_old.content.decode()


def test_meeting_detail_shows_vote_chip(client, stored_version, source_tree):
    _, _, meeting = source_tree
    document, version = _processed_version(stored_version)
    agenda_item = AgendaItem.objects.create(meeting=meeting, agenda_number=12, title="Trafikfrågan")
    association = DocumentAssociation.objects.get(document=document, meeting=meeting)
    association.agenda_item = agenda_item
    association.save(update_fields=["agenda_item"])
    _seed_vote_chunks(document, version)
    pipeline.extract_votes(run=pipeline.begin_run("test"), version_ids=[version.id])

    res = client.get(reverse("website:meeting-detail", args=[meeting.pk]))
    assert res.status_code == 200
    content = res.content.decode()
    assert "vote-chip" in content
    assert f"#vote-{Vote.objects.get().pk}" in content
    assert "Röstningar (1)" in content  # chip on the document entry too


def test_meeting_detail_without_votes_has_no_chip(client, stored_version, source_tree):
    _, _, meeting = source_tree
    document, version = _processed_version(stored_version)
    AgendaItem.objects.create(meeting=meeting, agenda_number=12, title="Trafikfrågan")
    res = client.get(reverse("website:meeting-detail", args=[meeting.pk]))
    assert res.status_code == 200
    assert "vote-chip" not in res.content.decode()


def test_search_service_has_votes_filter(stored_version):
    document, version = _processed_version(stored_version)
    run = pipeline.begin_run("test")
    pipeline.process_versions(run=run, version_ids=[version.id], statuses=[DocumentVersion.Status.PROCESSED])

    assert service.search("Spårvägen", has_votes=None)["total"] == 1
    assert service.search("Spårvägen", has_votes=True)["total"] == 0

    _seed_vote_chunks(document, version)
    pipeline.extract_votes(run=pipeline.begin_run("test"), version_ids=[version.id])
    assert service.search("Spårvägen", has_votes=True)["total"] == 1
    assert service.search("Spårvägen", has_votes=False)["total"] == 1


def test_search_view_vote_filter_checkbox(client, stored_version):
    document, version = _processed_version(stored_version)
    run = pipeline.begin_run("test")
    pipeline.process_versions(run=run, version_ids=[version.id], statuses=[DocumentVersion.Status.PROCESSED])

    res = client.get(reverse("website:search"), {"q": "Spårvägen", "rostning": "1"})
    assert res.status_code == 200
    content = res.content.decode()
    assert "Har röstning" in content
    assert 'name="rostning" value="1" checked' in content

    _seed_vote_chunks(document, version)
    pipeline.extract_votes(run=pipeline.begin_run("test"), version_ids=[version.id])
    res = client.get(reverse("website:search"), {"q": "Spårvägen", "rostning": "1"})
    assert document.title in res.content.decode()


def test_votes_page_filters_on_vote_type(client, stored_version):
    document, version = _processed_version(stored_version)
    Chunk.objects.create(
        document_version=version,
        sequence=999,
        title=document.title,
        text=f"Med 6 ja-röster mot 2 nej-röster beslutades bifall.\nOmröstningsdetaljer\n{JA_LIST}\n{NEJ_LIST}",
        page_start=23,
        page_end=23,
        processing_version="test",
    )
    pipeline.extract_votes(run=pipeline.begin_run("test"), version_ids=[version.id])

    res = client.get(reverse("website:votes"), {"typ": "bifall"})
    content = res.content.decode()
    assert document.title in content
    assert "vote-type-bifall" in content
    res = client.get(reverse("website:votes"), {"typ": "avslag"})
    assert "Inga röstningar matchar filtret" in res.content.decode()


def test_member_page_shows_voting_history(client, stored_version):
    document, version = _processed_version(stored_version)
    _seed_vote_chunks(document, version)
    pipeline.extract_votes(run=pipeline.begin_run("test"), version_ids=[version.id])

    res = client.get(reverse("website:member", args=["erik-pelling"]))
    assert res.status_code == 200
    content = res.content.decode()
    assert "Erik Pelling" in content
    assert "1 ja" in content
    assert reverse("website:document-detail", args=[document.pk]) in content
    assert "stance-badge" in content

    res = client.get(reverse("website:member", args=["finns-inte"]))
    assert res.status_code == 404


def test_seat_count_sanity_warning(client, stored_version, source_tree):
    _, body, _ = source_tree
    body.seat_count = 3
    body.save(update_fields=["seat_count"])
    document, version = _processed_version(stored_version)
    _seed_vote_chunks(document, version)  # 8 named voters > 3 seats
    pipeline.extract_votes(run=pipeline.begin_run("test"), version_ids=[version.id])

    res = client.get(reverse("website:document-detail", args=[document.pk]))
    assert "överstiger antalet ledamöter" in res.content.decode()