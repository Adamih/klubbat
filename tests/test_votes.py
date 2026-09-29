"""Vote extraction (stage C): parser, pipeline wiring, and document page UI."""

import pytest
from django.urls import reverse

from core.models import Chunk, DocumentVersion, Vote, VoteRecord
from ingestion import pipeline, votes

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
    assert "Socialdemokraterna" in content
    assert "Erik Pelling (S)" in content
    assert f'id="chunk-{chunk.pk}"' in content
    assert f'href="#chunk-{chunk.pk}"' in content


def test_document_detail_without_votes_hides_section(client, stored_version):
    document, version = _processed_version(stored_version)
    res = client.get(reverse("website:document-detail", args=[document.pk]))
    assert res.status_code == 200
    assert "Röstningar" not in res.content.decode()