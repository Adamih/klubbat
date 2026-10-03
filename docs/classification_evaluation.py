"""Classifier evaluation: measure topic-tag precision against manual review.

Two steps:

1.  uv run python docs/classification_evaluation.py sample
    Picks a stratified, reproducible sample of tagged targets (per topic: the
    lowest-confidence items plus random ones) and writes
    docs/classification_sample.json for review.

2.  uv run python docs/classification_evaluation.py report
    Reads docs/classification_review.json (verdicts: correct/wrong/uncertain,
    with the suggested topic for wrong ones) and writes
    docs/classification_evaluation.md with overall and per-topic precision,
    confusion pairs, and confidence calibration.
"""

import json
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from core.models import Tag

DOCS = Path(__file__).resolve().parent
SAMPLE_PATH = DOCS / "classification_sample.json"
REVIEW_PATH = DOCS / "classification_review.json"

TEXT_CAP = 400
PER_TOPIC = 5  # 2 lowest-confidence + 3 random
SEED = 2026


def _target(tag: Tag) -> dict:
    if tag.decision is not None:
        return {
            "key": f"decision:{tag.decision_id}",
            "kind": "decision",
            "topic": tag.topic.slug,
            "confidence": round(tag.confidence, 2),
            "title": tag.decision.printed_agenda_title,
            "text": (tag.decision.decision_text or "")[:TEXT_CAP],
        }
    version = tag.document.current_version
    return {
        "key": f"document:{tag.document_id}",
        "kind": "document",
        "topic": tag.topic.slug,
        "confidence": round(tag.confidence, 2),
        "title": tag.document.title,
        "text": (version.markdown if version else "")[:TEXT_CAP],
    }


def sample() -> None:
    tagged = list(Tag.objects.select_related("topic", "decision", "document", "document__current_version"))
    targets = [_target(tag) for tag in tagged]
    by_topic: dict[str, list[dict]] = defaultdict(list)
    for target in targets:
        by_topic[target["topic"]].append(target)

    rng = random.Random(SEED)
    chosen = []
    for topic in sorted(by_topic):
        items = sorted(by_topic[topic], key=lambda t: t["confidence"])
        low = items[:2]
        rest = items[2:]
        picked = low + rng.sample(rest, min(3, len(rest)))
        for item in picked:
            item["stratum"] = "low" if item in low else "random"
        chosen.extend(picked)

    SAMPLE_PATH.write_text(
        json.dumps({"seed": SEED, "sample_size": len(chosen), "targets": chosen}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print(f"wrote {len(chosen)} sampled targets to {SAMPLE_PATH.name} "
          f"({Counter(t['topic'] for t in chosen)})")


def report() -> None:
    review = json.loads(REVIEW_PATH.read_text(encoding="utf-8"))
    sample = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
    by_key = {t["key"]: t for t in sample["targets"]}

    judged = []
    missing = []
    for key, entry in review.items():
        target = by_key.get(key)
        if target is None:
            continue
        judged.append({**target, **entry})
    for key in by_key:
        if key not in review:
            missing.append(key)

    correct = [t for t in judged if t["verdict"] == "correct"]
    wrong = [t for t in judged if t["verdict"] == "wrong"]
    uncertain = [t for t in judged if t["verdict"] == "uncertain"]
    n_scored = len(correct) + len(wrong)
    precision = len(correct) / n_scored if n_scored else 0.0

    lines = [
        "# Klassificeringsutvärdering (stage D)",
        "",
        f"Grund: {len(judged)} manuell granskade taggar av 520 (urval: per ämne de "
        "2 sämsta konfidenserna + 3 slumpmässiga, seed 2026). "
        f"Bedömning per post: rätt/fel/osäkert med förslag på rätt ämne vid fel.",
        "",
        f"- Total precision: **{precision:.0%}** ({len(correct)}/{n_scored} rätt, "
        f"{len(uncertain)} osäkra)",
        "",
        "## Precision per ämne",
        "",
        "| Ämne | Rätt | Fel | Precision |",
        "|---|---|---|---|",
    ]
    by_topic = defaultdict(lambda: {"correct": 0, "wrong": 0})
    for t in judged:
        if t["verdict"] in ("correct", "wrong"):
            by_topic[t["topic"]][t["verdict"]] += 1
    for topic in sorted(by_topic, key=lambda s: -by_topic[s]["correct"] - by_topic[s]["wrong"]):
        c, w = by_topic[topic]["correct"], by_topic[topic]["wrong"]
        total = c + w
        lines.append(f"| {topic} | {c} | {w} | {c / total:.0%} |" if total else f"| {topic} | 0 | 0 | – |")

    if wrong:
        lines += ["", "## Förväxlingar (tilldelat → föreslaget)", ""]
        confusions = Counter((t["topic"], t.get("suggested") or "?") for t in wrong)
        for (assigned, suggested), n in confusions.most_common():
            lines.append(f"- {assigned} → **{suggested}** ({n})")

    buckets = [("<0.45", [t for t in judged if t["confidence"] < 0.45]),
               (">=0.45", [t for t in judged if t["confidence"] >= 0.45])]
    lines += ["", "## Konfidenskalibrering", "", "| Konfidens | Rätt | Fel | Precision |", "|---|---|---|---|"]
    for label, items in buckets:
        c = sum(1 for t in items if t["verdict"] == "correct")
        w = sum(1 for t in items if t["verdict"] == "wrong")
        total = c + w
        lines.append(f"| {label} | {c} | {w} | {c / total:.0%} |" if total else f"| {label} | 0 | 0 | – |")

    if wrong:
        lines += ["", "## Felbedömda poster", ""]
        for t in wrong:
            lines.append(f"- **{t['topic']} → {t.get('suggested')}** ({t['confidence']:.2f}) {t['title'][:80]} — {t.get('note', '')}")

    if uncertain:
        lines += ["", "## Osäkra bedömningar (räknas ej i precisionen)", ""]
        for t in uncertain:
            lines.append(f"- {t['topic']} ({t['confidence']:.2f}) {t['title'][:80]}")

    if missing:
        lines += ["", f"*(ej granskade: {len(missing)})*"]

    out = DOCS / "classification_evaluation.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {out.name}: precision={precision:.0%} ({len(correct)}/{n_scored}), "
          f"wrong={len(wrong)}, uncertain={len(uncertain)}, unreviewed={len(missing)}")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "sample"
    if mode == "sample":
        sample()
    elif mode == "report":
        report()
    else:
        print("usage: classification_evaluation.py sample|report")
        sys.exit(1)
