"""Topic classification (stage D): examples, generated predictions, and UI."""

import pytest
from django.urls import reverse

from core.models import Decision, Document, DocumentVersion, LabelledExample, Tag, Topic
from ingestion import classify, pipeline
from search import service

pytestmark = pytest.mark.django_db

# Fake embedding space: one dimension per topic, lit by topic keywords. Centroids
# built from the label set's own seeds land almost diagonal, so classification
# behaves like keyword matching but exercises the real centroid machinery.
KEYWORDS = {
    "skola": ["skol", "förskola", "utbildning"],
    "omsorg": ["socialtjänst", "äldre", "lss", "familjehem"],
    "trafik": ["cykelväg", "spårväg", "busslinje", "gaturum", "väghållning", "kollektivtrafik"],
    "miljo": ["klimat", "naturvård", "avfall", "miljö-"],
    "stadsbyggnad": ["detaljplan", "markanvisning", "exploatering", "översiktsplan"],
    "boende": ["bostads", "hyror"],
    "kultur": ["kulturskolan", "bibliotek", "fritid", "idrott", "förenings", "badhus", "simhall", "teater", "kulturarv"],
    "naringsliv": ["näringsliv", "besöksnäring", "arbetsmarknad"],
    "ekonomi": ["budget", "taxor", "avgifter", "bidrag", "skatter"],
    "raddning": ["räddningstjänst", "kriminal", "trygghet"],
    "organisation": ["justerare", "föredragningslista", "anmälningsärenden", "sammansättning", "protokoll"],
}


def fake_embed(texts):
    slugs = list(KEYWORDS)
    out = []
    for text in texts:
        tl = text.lower()
        vec = [1.0 if any(k in tl for k in KEYWORDS[slug]) else 0.0 for slug in slugs]
        norm = sum(v * v for v in vec) ** 0.5 or 1.0
        out.append([v / norm for v in vec])
    return out


@pytest.fixture
def classified(monkeypatch):
    monkeypatch.setattr(classify, "embed_texts", fake_embed)


def _seed_decision(version, title, text, dnr=""):
    return Decision.objects.create(
        document_version=version,
        sequence=1,
        printed_agenda_number=1,
        printed_agenda_title=title,
        printed_dnr=dnr,
        decision_text=text,
        parser_version="test",
    )


def _processed(stored_version, markdown="Spårvägen till Stenhagen behandlades vid mötet."):
    document, version = stored_version
    version.processing_status = DocumentVersion.Status.PROCESSED
    version.markdown = markdown
    version.save(update_fields=["processing_status", "markdown"])
    return document, version


def test_topics_seeded_from_label_set(stored_version, classified):
    document, version = _processed(stored_version)
    stats = pipeline.classify_content(run=pipeline.begin_run("test"))
    assert stats["topics"] == len(classify.TOPIC_LABEL_SET)
    assert Topic.objects.count() == stats["topics"]


def test_classifies_decisions_and_documents(stored_version, classified):
    document, version = _processed(stored_version)
    _seed_decision(
        version,
        "Svar på motion om skolpersonal från Erik Pelling (S)",
        "Kommunfullmäktige beslutar att anta utredningen om skolresultat och rektorsindelning.",
    )
    motion = Document.objects.create(
        title="Motion om bostäder", doc_type="motion",
        primary_source_url="https://uppsala.se/test/motion-boende",
    )
    motion_version = DocumentVersion.objects.create(
        document=motion, original_url=motion.primary_source_url,
        processing_status=DocumentVersion.Status.PROCESSED,
    )
    motion.current_version = motion_version
    motion.save(update_fields=["current_version"])
    motion_version.markdown = "Bostäder och hyror i kommunen behöver öka."
    motion_version.save(update_fields=["markdown"])

    stats = pipeline.classify_content(run=pipeline.begin_run("test"))
    assert stats["tagged"] == 2

    decision_tag = Tag.objects.get(decision__isnull=False)
    doc_tag = Tag.objects.get(document__isnull=False)
    assert decision_tag.topic.slug == "skola"
    assert doc_tag.topic.slug == "boende"
    assert decision_tag.classifier_version == classify._classification_version({})

    # Stamped targets are skipped on re-run.
    stats2 = pipeline.classify_content(run=pipeline.begin_run("test"))
    assert stats2["tagged"] == 0 and stats2["skipped"] == 2


def test_examples_guide_centroids_without_overriding_source(stored_version, classified):
    document, version = _processed(stored_version)
    decision = _seed_decision(
        version,
        "Svar på motion om skolpersonal från Erik Pelling (S)",
        "Kommunfullmäktige beslutar att anta utredningen om skolresultat.",
    )
    topic = Topic.objects.get_or_create(slug="ekonomi")[0]
    example = LabelledExample.objects.create(
        title=decision.printed_agenda_title, text=decision.decision_text, topic=topic,
    )
    before = classify._build_centroids({})["ekonomi"]
    after = classify._build_centroids(classify._example_texts())["ekonomi"]
    school_dimension = list(KEYWORDS).index("skola")
    assert after[school_dimension] > before[school_dimension]

    stats = pipeline.classify_content(run=pipeline.begin_run("test"))
    assert stats["tagged"] == 1
    assert list(decision.tags.values_list("topic__slug", flat=True)) == ["skola"]
    stats = pipeline.classify_content(run=pipeline.begin_run("test"), force=True)
    assert stats["tagged"] == 1
    example.refresh_from_db()
    assert example.topic.slug == "ekonomi"


def test_example_changes_trigger_reclassification(stored_version, classified):
    _, version = _processed(stored_version)
    _seed_decision(version, "Skolpersonal", "Kommunfullmäktige beslutar om skolresultat.")
    run = pipeline.begin_run("test")
    pipeline.classify_content(run=run)
    old_stamp = Tag.objects.get().classifier_version
    example = LabelledExample.objects.create(
        title="Skolpersonal", text="Skolresultat", topic=Topic.objects.get(slug="skola"),
    )
    assert pipeline.classify_content(run=run)["tagged"] == 1
    assert Tag.objects.get().classifier_version != old_stamp
    assert pipeline.classify_content(run=run)["skipped"] == 1
    example.topic = Topic.objects.get(slug="ekonomi")
    example.save()
    assert pipeline.classify_content(run=run)["tagged"] == 1
    example.text = "Nya avgifter"
    example.save()
    assert pipeline.classify_content(run=run)["tagged"] == 1
    example.delete()
    assert pipeline.classify_content(run=run)["tagged"] == 1
    assert Tag.objects.get().classifier_version == old_stamp


def test_example_survives_decision_reparsing(stored_version, classified):
    from ingestion import decisions
    from django.db import transaction

    _, version = _processed(stored_version)
    decision = _seed_decision(version, "Skolpersonal", "Kommunfullmäktige beslutar om skolresultat.")
    topic = classify._ensure_topics()["skola"]
    example = LabelledExample.objects.create(
        title=decision.printed_agenda_title, text=decision.decision_text, topic=topic,
    )
    pipeline.classify_content(run=pipeline.begin_run("test"))
    original_pk = decision.pk
    with transaction.atomic():
        decisions.store(version, [decisions.ParsedDecision(
            printed_agenda_number=1, printed_agenda_title="Skolpersonal",
            decision_text="Kommunfullmäktige beslutar om nya skolresultat.", outcome="annat",
        )])
    assert not Decision.objects.filter(pk=original_pk).exists()
    assert not Tag.objects.exists()
    example.refresh_from_db()
    assert example.text == "Kommunfullmäktige beslutar om skolresultat."
    assert pipeline.classify_content(run=pipeline.begin_run("test"))["tagged"] == 1
    assert Tag.objects.get().topic.slug == "skola"


def test_list_documents_skipped_and_stale_tags_removed(stored_version, classified):
    document, version = _processed(stored_version)
    motion = Document.objects.create(
        title="Inlämnade motioner till kommunfullmäktige 27 januari 2025", doc_type="motion",
        primary_source_url="https://uppsala.se/test/motioner-lista",
    )
    motion_version = DocumentVersion.objects.create(
        document=motion, original_url=motion.primary_source_url,
        processing_status=DocumentVersion.Status.PROCESSED,
    )
    motion.current_version = motion_version
    motion.save(update_fields=["current_version"])
    motion_version.markdown = "Bostäder och hyror behöver öka."
    motion_version.save(update_fields=["markdown"])
    # A stale generated tag left by a previous classifier run must go away.
    stale_topic = Topic.objects.get_or_create(slug="boende")[0]
    Tag.objects.create(document=motion, topic=stale_topic, confidence=0.5,
                       classifier_version="topic-centroid-v1")

    stats = pipeline.classify_content(run=pipeline.begin_run("test"))
    assert stats["stale_removed"] == 1
    # The list document itself is not classified.
    assert not Tag.objects.filter(document=motion).exists()


def test_dnr_prior_breaks_ties(stored_version, classified):
    document, version = _processed(stored_version)
    _seed_decision(version, "Rubrik utan nyckelord", "Inga sökord här alls.", dnr="PBN-2026-00123")
    pipeline.classify_content(run=pipeline.begin_run("test"))
    # All similarities are 0; the PBN prior lifts stadsbyggnad to the top.
    assert Tag.objects.get(decision__isnull=False).topic.slug == "stadsbyggnad"


def test_search_topic_filter(stored_version, classified):
    document, version = _processed(stored_version, "Spårvägen till Stenhagen behandlades vid mötet.")
    decision = _seed_decision(
        version, "Svar på motion om skolpersonal", "Kommunfullmäktige beslutar om skolresultat."
    )
    pipeline.classify_content(run=pipeline.begin_run("test"))

    topic = Topic.objects.get(slug="skola")
    assert service.search("Spårvägen", topic_id=topic.pk)["total"] == 1
    other = Topic.objects.exclude(slug="skola").first()
    assert service.search("Spårvägen", topic_id=other.pk)["total"] == 0


def test_decisions_page_shows_topic_chips(client, stored_version, classified):
    document, version = _processed(stored_version)
    _seed_decision(
        version, "Svar på motion om skolpersonal", "Kommunfullmäktige beslutar om skolresultat."
    )
    pipeline.classify_content(run=pipeline.begin_run("test"))

    res = client.get(reverse("website:decisions"))
    assert res.status_code == 200
    content = res.content.decode()
    assert "topic-chip" in content
    assert "amne=skola" in content
