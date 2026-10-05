"""Topical classification (stage D): policy-area tags for decisions and
initiative documents.

The classifier is a nearest-centroid over Swedish sentence embeddings — no
gradient training. Each topic's centroid is anchored on its label, description
and seed phrases (below), then refined by independent labelled examples.
Assignments are stamped with CLASSIFIER_VERSION and the example-set fingerprint:
unchanged assignments are skipped, while example edits re-tag all targets on
the next run. The diarienummer
prefix (e.g. PBN = plan-och-byggnadsnämnden) is used as a small prior.
"""

from __future__ import annotations

import logging
import re
import hashlib
import json

log = logging.getLogger(__name__)

CLASSIFIER_VERSION = "topic-centroid-v3"

# KBLab's Swedish sentence-BERT (National Library of Sweden).
EMBEDDING_MODEL = "KBLab/sentence-bert-swedish-cased"

# Confidence boost for items whose diarienummer prefix names the originating
# nämnd; extended as prefixes are confirmed against real data.
DNR_TOPIC_PRIOR = {"PBN": "stadsbyggnad"}
DNR_PREFIX_RE = re.compile(r"\b([A-ZÅÄÖ]{2,4})-\d{4}-")

# The fixed taxonomy. Seeds are representative decision-title phrases that
# anchor each centroid; descriptions carry the label's meaning.
TOPIC_LABEL_SET: list[dict] = [
    {
        "slug": "skola",
        "label": "Skola och förskola",
        "description": "Skolverksamhet, förskola, utbildning och skolresultat.",
        "seeds": [
            "Svar på motion om skolpersonal",
            "Förskoleplatser och barnomsorg",
            "Rektorsindelning och skolans organisation",
            "Skolresultat och kunskapskrav",
            "Entreprenörskap i undervisningen och skolans kontakt med näringslivet",
        ],
    },
    {
        "slug": "omsorg",
        "label": "Socialtjänst och äldreomsorg",
        "description": "Socialtjänst, äldreboenden, familjeinsatser och funktionsnödsinsatser.",
        "seeds": [
            "Socialtjänstens organisation",
            "Äldreboende och trygghetsboende",
            "LSS-insatser och funktionsnöd",
            "Familjehem och barn och unga",
        ],
    },
    {
        "slug": "trafik",
        "label": "Trafik och kollektivtrafik",
        "description": "Vägar, gator, cykelvägar, spårväg och kollektivtrafik.",
        "seeds": [
            "Cykelvägar och gångbanor",
            "Väghållning och gaturum",
            "Spårväg i Uppsala",
            "Busslinjer och kollektivtrafik",
        ],
    },
    {
        "slug": "miljo",
        "label": "Miljö och klimat",
        "description": "Klimatmål, naturvård, avfall, vatten och miljöskydd.",
        "seeds": [
            "Klimatmål och utsläppsminskning",
            "Naturvård och gröna ytor",
            "Avfallshantering och kretslopp",
            "Miljö- och hälsoskydd",
        ],
    },
    {
        "slug": "stadsbyggnad",
        "label": "Detaljplaner och stadsutveckling",
        "description": "Detaljplaner, markanvisning, exploatering och översiktsplan.",
        "seeds": [
            "Antagande av detaljplan",
            "Markanvisning och exploatering",
            "Översiktsplan för Uppsala kommun",
        ],
    },
    {
        "slug": "boende",
        "label": "Bostäder",
        "description": "Bostadsbyggande, hyror och bostadsförsörjning.",
        "seeds": [
            "Bostadsbyggande och hyror",
            "Allmännyttiga bostäder",
            "Bostadsförsörjning och bostadsbrist",
        ],
    },
    {
        "slug": "kultur",
        "label": "Kultur och fritid",
        "description": "Kulturskola, bibliotek, idrott, fritid, föreningsliv och kulturanläggningar.",
        "seeds": [
            "Kulturskolan och bibliotek",
            "Fritidsgårdar och idrottsanläggningar",
            "Föreningsstöd och evenemang",
            "Badhus och simhallar",
            "Teater, scenkonst och stadsteatern",
            "Kulturarv och stadsbildens historia",
        ],
    },
    {
        "slug": "naringsliv",
        "label": "Näringsliv och arbetsmarknad",
        "description": "Företagande, besöksnäring och arbetsmarknadsåtgärder.",
        "seeds": [
            "Näringslivsutveckling och företagande",
            "Besöksnäring och evenemang",
            "Arbetsmarknadsåtgärder",
        ],
    },
    {
        "slug": "ekonomi",
        "label": "Kommunekonomi och budget",
        "description": "Mål och budget, taxor, avgifter, bidrag och kommunekonomi.",
        "seeds": [
            "Mål och budget",
            "Taxor och avgifter",
            "Kommunbidrag per verksamhet",
            "Fastighetsavgifter och skatter",
        ],
    },
    {
        "slug": "raddning",
        "label": "Räddningstjänst och trygghet",
        "description": "Räddningstjänst, kriminalförebyggande arbete och trygghet.",
        "seeds": [
            "Räddningstjänstens organisation",
            "Kriminalförebyggande arbete",
            "Trygghet och säkerhet i stadsdelen",
        ],
    },
    {
        "slug": "organisation",
        "label": "Organisation och procedur",
        "description": "Kommunorganisation, protokoll, val och procedurfrågor på sammanträdet.",
        "seeds": [
            "Val av justerare samt justeringsdag",
            "Fastställande av föredragningslista",
            "Anmälningsärenden till kommunfullmäktige",
            "Sammansättning av nämnder och utskott",
            "Politikernas arvoden och ersättningar",
        ],
    },
]

TEXT_CAP = 1200  # characters of decision/document text fed to the classifier

# Compilations of several initiatives ("Inlämnade motioner/interpellationer
# till kommunfullmäktige <datum>") span many topics: no single tag is honest.
LIST_DOCUMENT_RE = re.compile(
    r"Inlämnade (?:motioner|interpellationer|frågor) till kommunfullmäktige", re.IGNORECASE
)

_model = None


def embed_texts(texts: list[str]) -> "list[list[float]]":
    """Embed texts with the Swedish sentence-BERT model (lazy-loaded)."""
    global _model
    if not texts:
        return []
    if _model is None:
        from sentence_transformers import SentenceTransformer

        log.info("Loading embedding model %s (first run downloads it)", EMBEDDING_MODEL)
        _model = SentenceTransformer(EMBEDDING_MODEL)
    vectors = _model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
    return [v.tolist() for v in vectors]


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _ensure_topics() -> dict[str, "Topic"]:
    """Seed Topic rows from the constant; returns slug -> Topic."""
    from core.models import Topic

    by_slug = {}
    for topic in TOPIC_LABEL_SET:
        obj, _ = Topic.objects.get_or_create(
            slug=topic["slug"],
            defaults={"label": topic["label"], "description": topic["description"]},
        )
        by_slug[topic["slug"]] = obj
    return by_slug


def _example_texts() -> dict[str, list[str]]:
    """Saved example text per topic, independent of the source's lifecycle."""
    from core.models import LabelledExample

    texts: dict[str, list[str]] = {}
    slugs = [topic["slug"] for topic in TOPIC_LABEL_SET]
    for example in LabelledExample.objects.filter(topic__slug__in=slugs).select_related("topic"):
        body = f"{example.title}\n{example.text}"
        texts.setdefault(example.topic.slug, []).append(body[:TEXT_CAP])
    return texts


def _classification_version(example_texts: dict[str, list[str]]) -> str:
    payload = json.dumps(example_texts, sort_keys=True, ensure_ascii=False)
    fingerprint = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"{CLASSIFIER_VERSION}:{fingerprint}"


def _build_centroids(example_texts: dict[str, list[str]]) -> dict[str, list[float]]:
    """Normalized centroid per topic: label + description + seeds + examples."""
    texts: list[str] = []
    owners: list[str] = []
    for topic in TOPIC_LABEL_SET:
        for phrase in [topic["label"], topic["description"], *topic["seeds"]]:
            texts.append(phrase)
            owners.append(topic["slug"])
    for slug, items in example_texts.items():
        for item in items:
            texts.append(item)
            owners.append(slug)

    vectors = embed_texts(texts)
    centroids: dict[str, list[float]] = {}
    for slug in dict.fromkeys(owners):
        own = [vectors[i] for i, o in enumerate(owners) if o == slug]
        dim = len(own[0])
        mean = [sum(v[i] for v in own) / len(own) for i in range(dim)]
        norm = sum(x * x for x in mean) ** 0.5 or 1.0
        centroids[slug] = [x / norm for x in mean]
    return centroids


def _targets() -> list[dict]:
    """Decisions of current versions + initiative documents with text."""
    from core.models import Decision, Document

    current_ids = list(
        Document.objects.exclude(current_version=None).values_list("current_version_id", flat=True)
    )
    targets: list[dict] = []
    for decision in (
        Decision.objects.filter(document_version_id__in=current_ids).select_related("document_version")
    ):
        text = f"{decision.printed_agenda_title}\n{decision.decision_text}"[:TEXT_CAP]
        targets.append({"decision": decision, "document": None, "text": text, "dnr": decision.printed_dnr})
    for document in (
        Document.objects.filter(doc_type__in=("motion", "interpellation", "fraga"))
        .exclude(current_version=None)
        .exclude(current_version__markdown="")
        .select_related("current_version")
    ):
        if LIST_DOCUMENT_RE.search(document.title):
            continue
        text = f"{document.title}\n{document.current_version.markdown}"[:TEXT_CAP]
        targets.append({"decision": None, "document": document, "text": text, "dnr": ""})
    return targets


def classify_content(*, run, force: bool = False) -> dict:
    """Tag untagged decisions/initiatives with their top policy area.

    Independent examples seed the centroids. Targets already carrying the
    current classifier/example-set stamp are skipped unless force=True.
    Embedding is one batch; the DB writes are short transactions.
    """
    from django.db import transaction

    from core.models import Q, Tag

    topics = _ensure_topics()
    slugs = set(topics)
    example_texts = _example_texts()
    classifier_version = _classification_version(example_texts)
    stats = {"topics": len(slugs), "tagged": 0, "skipped": 0, "failures": 0}
    try:
        centroids = _build_centroids(example_texts)
    except Exception as exc:  # noqa: BLE001 - a failed model load must not abort the run
        log.error("Classifier setup failed: %s", exc)
        run.failures += 1
        stats["failures"] += 1
        return stats

    tagged_by_decision: dict[int, list[Tag]] = {}
    tagged_by_document: dict[int, list[Tag]] = {}
    for tag in Tag.objects.filter(topic__slug__in=slugs).select_related("topic"):
        if tag.decision_id is not None:
            tagged_by_decision.setdefault(tag.decision_id, []).append(tag)
        if tag.document_id is not None:
            tagged_by_document.setdefault(tag.document_id, []).append(tag)

    targets = _targets()
    # Targets that are no longer classified (e.g. multi-topic initiative
    # lists) must not keep their old classifier tags.
    decision_ids = {t["decision"].pk for t in targets if t["decision"]}
    document_ids = {t["document"].pk for t in targets if t["document"]}
    stale = (
        (Q(decision__isnull=False) & ~Q(decision_id__in=decision_ids))
        | (Q(document__isnull=False) & ~Q(document_id__in=document_ids))
    )
    removed, _ = Tag.objects.filter(stale).delete()
    stats["stale_removed"] = removed

    pending: list[dict] = []
    for target in targets:
        key = target["decision"].pk if target["decision"] else target["document"].pk
        existing = (
            tagged_by_decision.get(key) if target["decision"] else tagged_by_document.get(key)
        ) or []
        if not force and any(tag.classifier_version == classifier_version for tag in existing):
            stats["skipped"] += 1
            continue
        pending.append(target)

    if pending:
        vectors = embed_texts([t["text"] for t in pending])
        for target, vector in zip(pending, vectors):
            scores = {slug: _dot(vector, centroid) for slug, centroid in centroids.items()}
            prefix_match = DNR_PREFIX_RE.search(target["dnr"] or "")
            if prefix_match and prefix_match.group(1) in DNR_TOPIC_PRIOR:
                prior = DNR_TOPIC_PRIOR[prefix_match.group(1)]
                scores[prior] = scores.get(prior, 0.0) + 0.1
            top_slug = max(scores, key=scores.get)
            confidence = min(1.0, max(0.0, scores[top_slug]))
            with transaction.atomic():
                if target["decision"] is not None:
                    Tag.objects.filter(decision=target["decision"]).delete()
                    Tag.objects.create(
                        decision=target["decision"], topic=topics[top_slug],
                        confidence=confidence, classifier_version=classifier_version,
                    )
                else:
                    Tag.objects.filter(document=target["document"]).delete()
                    Tag.objects.create(
                        document=target["document"], topic=topics[top_slug],
                        confidence=confidence, classifier_version=classifier_version,
                    )
            stats["tagged"] += 1
    if stats["failures"]:
        run.save(update_fields=["failures"])
    log.info("Classified %d targets (%d skipped)", stats["tagged"], stats["skipped"])
    return stats
