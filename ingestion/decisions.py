"""Decision extraction (stage C): the "Beslut" block of every agenda item.

The decision parser runs over the same flattened IR markdown as the vote
parser and is driven by the printed protocol structure: each agenda item opens
with a "§ N <title> [dnr] Beslut" heading, and the decision text that follows
ends at a section marker ("Beslutsgång", "Ärendet", "Sammanfattning", …) or at
the next § heading. Most decisions are unanimous and never reach a Vote; this
is what makes them visible. Results are stamped with DECISION_PARSER_VERSION
per document version; re-extraction deletes and recreates the rows. Only
protocols are parsed — committee reports (ärende) print the same "§ N … Beslut"
structure, but as the board's proposal, not the council's decision, and mixing
them would double every agenda item. Attribution is document-level for now.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from ingestion.votes import (
    AGENDA_HEADING_RE,
    _clean_page_noise,
    _markdown_plain,
    _normalize,
)

log = logging.getLogger(__name__)

DECISION_PARSER_VERSION = "beslut-v6"

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


def parse_markdown(markdown: str) -> list[ParsedDecision]:
    """Parse decision blocks from one version's IR markdown."""
    text = _normalize(_clean_page_noise(_markdown_plain(markdown)))
    if not text:
        return []
    headings = [
        (match.start(), match.end(), int(match.group("number")), _normalize(match.group("title")), match.group("dnr") or "")
        for match in AGENDA_HEADING_RE.finditer(text)
    ]
    parsed: list[ParsedDecision] = []
    for i, (start, text_start, number, title, dnr) in enumerate(headings):
        next_start = headings[i + 1][0] if i + 1 < len(headings) else len(text)
        block = text[text_start:next_start]
        marker = SECTION_MARKER_RE.search(block)
        decision = _normalize(block[: marker.start() if marker else len(block)])[:MAX_DECISION_CHARS]
        if not decision:
            continue
        parsed.append(
            ParsedDecision(
                printed_agenda_number=number,
                printed_agenda_title=title[:300],
                printed_dnr=dnr,
                decision_text=decision,
                outcome=_classify_decision(decision),
            )
        )
    return parsed


def store(version, parsed: list[ParsedDecision]) -> int:
    """Replace the version's Decision rows. Call inside a transaction."""
    from core.models import Decision
    from core.textmatch import find_page

    Decision.objects.filter(document_version=version).delete()
    for sequence, parsed_decision in enumerate(parsed, start=1):
        # Best-effort page reference: the decision text, else the title.
        page = find_page(version.pk, parsed_decision.decision_text)
        if page is None:
            page = find_page(version.pk, parsed_decision.printed_agenda_title)
        Decision.objects.create(
            document_version=version,
            sequence=sequence,
            printed_agenda_number=parsed_decision.printed_agenda_number,
            printed_agenda_title=parsed_decision.printed_agenda_title,
            printed_dnr=parsed_decision.printed_dnr,
            decision_text=parsed_decision.decision_text,
            outcome=parsed_decision.outcome,
            page=page,
            parser_version=DECISION_PARSER_VERSION,
        )
    return len(parsed)
