"""Decision extraction (stage C): the "Beslut" block of every agenda item.

The decision parser runs over the same stitched chunk text as the vote parser
and is driven by the printed protocol structure: each agenda item opens with a
"§ N <title> [dnr] Beslut" heading, and the decision text that follows ends at
a section marker ("Beslutsgång", "Ärendet", "Sammanfattning", …) or at the
next § heading. Most decisions are unanimous and never reach a Vote; this is
what makes them visible. Results are stamped with DECISION_PARSER_VERSION per
document version; re-extraction deletes and recreates the rows.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from ingestion.votes import (
    AGENDA_HEADING_RE,
    _chunk_index_at,
    _normalize,
    _stitch,
)

log = logging.getLogger(__name__)

DECISION_PARSER_VERSION = "beslut-v3"

# Section headings that end the decision text. Order in the protocol varies;
# the first match inside the block ends it. Case-sensitive on purpose: the
# lowercase forms appear inside narrative sentences ("i ärendet").
SECTION_MARKER_RE = re.compile(
    r"\s(?:Beslutsgång|Ärendet|Sammanfattning|Övrigt|Beslutsunderlag|Yrkanden|"
    r"Huvudinnehåll|Tjänsteskrivelse|Bakgrund|Bedömning|Rekommendation|Närvarande)\b"
)

# Decision phrases specific to Uppsala protocols that the vote classifier
# does not cover: answering a motion/interpellation "med föredragningen" is
# how a motion is declined; fastställa/godkänna/bevilja are adoptions.
_DECISION_EXTRA_RES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("avslag", re.compile(r"besvara (?:motionen|interpellationen|frågan) med föredragningen")),
    ("bifall", re.compile(r"\b(?:fastställ(?:er|a)|godkänn(?:er|a)|bevilj(?:ar|a))\b")),
)

MAX_DECISION_CHARS = 1500


@dataclass
class ParsedDecision:
    chunk_index: int | None
    agenda_item_id: int | None
    printed_agenda_number: int | None
    printed_agenda_title: str
    printed_dnr: str = ""
    decision_text: str = ""
    outcome: str = "annat"


def _classify_decision(decision_text: str) -> str:
    from ingestion.votes import _classify_vote

    outcome = _classify_vote(decision_text, "")
    if outcome != "annat":
        return outcome
    for outcome_code, pattern in _DECISION_EXTRA_RES:
        if pattern.search(decision_text):
            return outcome_code
    return "annat"


def parse_decisions(chunks: list[dict]) -> list[ParsedDecision]:
    """Parse decision blocks from one version's chunk texts (dicts: pk/agenda_item/text)."""
    if not chunks:
        return []
    stitched, spans = _stitch(chunks)
    headings = [
        (match.start(), match.end(), int(match.group("number")), _normalize(match.group("title")), match.group("dnr") or "")
        for match in AGENDA_HEADING_RE.finditer(stitched)
    ]
    parsed: list[ParsedDecision] = []
    for i, (start, text_start, number, title, dnr) in enumerate(headings):
        next_start = headings[i + 1][0] if i + 1 < len(headings) else len(stitched)
        block = stitched[text_start:next_start]
        marker = SECTION_MARKER_RE.search(block)
        text = _normalize(block[: marker.start() if marker else len(block)])[:MAX_DECISION_CHARS]
        if not text:
            continue
        chunk_index = _chunk_index_at(spans, start)
        parsed.append(
            ParsedDecision(
                chunk_index=chunk_index,
                agenda_item_id=chunks[chunk_index]["agenda_item"] if chunk_index is not None else None,
                printed_agenda_number=number,
                printed_agenda_title=title[:300],
                printed_dnr=dnr,
                decision_text=text,
                outcome=_classify_decision(text),
            )
        )
    return parsed


def store(version, chunks: list[dict], parsed: list[ParsedDecision]) -> int:
    """Replace the version's Decision rows. Call inside a transaction."""
    from core.models import Decision
    from ingestion.votes import _resolve_agenda_items

    agenda_by_number = _resolve_agenda_items(version, parsed)
    Decision.objects.filter(document_version=version).delete()
    for sequence, parsed_decision in enumerate(parsed, start=1):
        chunk_pk = (
            chunks[parsed_decision.chunk_index]["pk"]
            if parsed_decision.chunk_index is not None
            else None
        )
        agenda_item_id = parsed_decision.agenda_item_id
        if agenda_item_id is None and parsed_decision.printed_agenda_number is not None:
            agenda_item_id = agenda_by_number.get(parsed_decision.printed_agenda_number)
        Decision.objects.create(
            document_version=version,
            chunk_id=chunk_pk,
            agenda_item_id=agenda_item_id,
            sequence=sequence,
            printed_agenda_number=parsed_decision.printed_agenda_number,
            printed_agenda_title=parsed_decision.printed_agenda_title,
            printed_dnr=parsed_decision.printed_dnr,
            decision_text=parsed_decision.decision_text,
            outcome=parsed_decision.outcome,
            parser_version=DECISION_PARSER_VERSION,
        )
    return len(parsed)
