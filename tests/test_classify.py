"""Topic classification (stage D): centroid classifier, protection, and UI."""

import pytest
from django.urls import reverse

from core.models import Decision, Document, DocumentVersion, Tag, Topic
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
    "kultur": ["kulturskolan", "bibliotek", "fritid", "idrott", "förenings"],
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
    assert decision_tag.classifier_version == classify.CLASSIFIER_VERSION

    # Stamped targets are skipped on re-run.
    stats2 = pipeline.classify_content(run=pipeline.begin_run("test"))
    assert stats2["tagged"] == 0 and stats2["skipped"] == 2


def test_manual_tag_protected_and_used_as_seed(stored_version, classified):
    document, version = _processed(stored_version)
    decision = _seed_decision(
        version,
        "Svar på motion om skolpersonal från Erik Pelling (S)",
        "Kommunfullmäktige beslutar att anta utredningen om skolresultat.",
    )
    topic = Topic.objects.get_or_create(slug="ekonomi")[0]
    Tag.objects.create(decision=decision, topic=topic, confidence=1.0,
                       classifier_version="manual", is_manual=True)

    stats = pipeline.classify_content(run=pipeline.begin_run("test"))
    assert stats["protected"] == 1
    assert list(decision.tags.values_list("topic__slug", flat=True)) == ["ekonomi"]


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