"""Vote extraction (stage C): recorded votes parsed from chunk text.

Stage B (chunking) output is the only input — no PDF or OCR access, so a
re-run is a fast text pass. Parsing is driven by the protocol's own printed
structure: an "Omröstningsdetaljer" block of labeled name lists, plus optional
tally/proposition sentences elsewhere in the text. Results are stamped with
VOTE_PARSER_VERSION per document version; re-extraction deletes and recreates
the rows (latest state only, no history).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

VOTE_PARSER_VERSION = "omrostningsdetaljer-v3"

# "Omröstningsdetaljer" introduces a block of named voter lists.
MARKER_RE = re.compile(r"Omröstningsdetaljer", re.IGNORECASE)

# Labeled sections inside a block (variants observed in the 2025 protocols:
# "Ja-röst avger:" / "Ja-röster avger:" / "Ja-röst avgör:", likewise Nej,
# plus "Avstår:" and "Frånvarande:").
STANCE_LABELS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ja", re.compile(r"Ja-röst(?:er)?\s+avg[öaoe]r[^:]{0,20}:", re.IGNORECASE)),
    ("nej", re.compile(r"Nej-röst(?:er)?\s+avg[öaoe]r[^:]{0,20}:", re.IGNORECASE)),
    ("avstar", re.compile(r"Avst(?:å|a)r[^:]{0,20}:", re.IGNORECASE)),
    ("franvarande", re.compile(r"Frånvarande[^:]{0,20}:", re.IGNORECASE)),
)

# "Omröstning nr 2" — a details section can hold several numbered rounds, each
# with its own labeled lists.
ROUND_RE = re.compile(r"Omröstning nr\s+(\d{1,3})\b", re.IGNORECASE)

# "Med 39 ja-röster mot 40 nej-röster" — printed when a vote is demanded.
TALLY_RE = re.compile(
    r"Med\s+(\d+)\s+ja-röster\s+mot\s+(\d+)\s+nej-röster(?:\s+med\s+(\d+)\s+avst(?:å|a)r)?",
    re.IGNORECASE,
)
PROPOSITION_RE = re.compile(r"omröstningsproposition(?:en)?\s*:\s*", re.IGNORECASE)

# Page furniture that can split a name list across a chunk boundary. The
# chunker merges short header fragments into paragraph lines ("Sida 26 (73)
# Protokoll 2025-02-24 Ordföranden finner ..."), so only the noise itself may
# be removed — never the whole line around it.
PAGE_NOISE_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bSida\s+\d+(?:\s*\(\d+\)|\s+av\s+\d+)?", re.IGNORECASE),
    re.compile(r"\bProtokoll\s+\d{4}-\d{2}-\d{2}", re.IGNORECASE),
    re.compile(r"(?<=\s)\d+\s*\(\d+\)(?=\s)"),  # leftover "27 (73)" page refs
    re.compile(r"(?<=\s)\(\d+\)(?=\s)"),  # standalone "(73)" page refs
)

# One list item: "Name (P)" followed by ",", " och", "." or end of list.
ITEM_RE = re.compile(
    r"\s*(?:och\s+)?"
    r"(?P<name>[^,.;:()\n]+?)"
    r"\s*\((?P<party>[^()\d]{1,10})\)"
    r"(?P<sep>\s*,|\s+och\b|\s*\.)?"
)

MAX_LIST_CHARS = 4000
MAX_PROPOSITION_CHARS = 600
# The result sentence ("Med N ja-röster ...") is reprinted at the top of every
# page a details section spans; identical tallies closer than this are repeats.
PAGE_REPEAT_CHARS = 5000


@dataclass
class ParsedRecord:
    position: int
    member_name: str
    party_code: str
    stance: str


@dataclass
class ParsedVote:
    chunk_index: int | None
    agenda_item_id: int | None
    proposition: str = ""
    outcome_text: str = ""
    tally_yes: int | None = None
    tally_no: int | None = None
    tally_abstain: int | None = None
    records: list[ParsedRecord] = field(default_factory=list)
    has_details: bool = False
    records_match_tally: bool | None = None


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _scan_list(text: str, start: int, limit: int) -> list[tuple[str, str]]:
    """Consume 'Name (P), Name (P) och Name (P).' from start while list syntax holds."""
    records: list[tuple[str, str]] = []
    pos = start
    while pos < limit:
        match = ITEM_RE.match(text, pos)
        if match is None:
            break
        name = _normalize(match.group("name"))
        party = match.group("party").strip().upper()
        if not name or not party:
            break
        records.append((name, party))
        pos = match.end()
        sep = (match.group("sep") or "").strip()
        if sep != "," and sep != "och":
            break  # "." or nothing -> the list ended; trailing narrative must not be scanned
    return records


def _stance_sections(text: str, start: int, end: int) -> list[tuple[int, int, str]]:
    """Labeled list sections as (label_start, label_end, stance), in order."""
    sections: list[tuple[int, int, str]] = []
    for stance, label_re in STANCE_LABELS:
        for match in label_re.finditer(text, start, end):
            sections.append((match.start(), match.end(), stance))
    sections.sort()
    return sections


def _parse_lists(text: str, start: int, end: int) -> list[ParsedRecord]:
    records: list[ParsedRecord] = []
    sections = _stance_sections(text, start, end)
    for i, (label_start, label_end, stance) in enumerate(sections):
        limit = sections[i + 1][0] if i + 1 < len(sections) else end
        limit = min(limit, label_end + MAX_LIST_CHARS)
        for position, (name, party) in enumerate(_scan_list(text, label_end, limit), start=1):
            records.append(ParsedRecord(position, name, party, stance))
    return records


def _vote_boundaries(window: str, seg_start: int, seg_end: int) -> list[int]:
    """Vote starts within one round segment.

    Within one vote the labeled sections always appear in ja → nej → avstår →
    frånvarande order; a stance that repeats or steps back ("Ja-röst avger"
    again after "Avstår", or twice in a row) therefore opens a second vote
    group, which the protocol prints without a new "Omröstningsdetaljer".
    """
    rank = {stance: i for i, (stance, _) in enumerate(STANCE_LABELS)}
    boundaries = [seg_start]
    previous_rank = None
    for label_start, _, stance in _stance_sections(window, seg_start, seg_end):
        if previous_rank is not None and rank[stance] <= previous_rank:
            boundaries.append(label_start)
        previous_rank = rank[stance]
    return boundaries


def _sentence_at(text: str, pos: int) -> str:
    segment = _normalize(text[pos:pos + 300])
    end = segment.find(". ")
    return segment[: end + 1] if end != -1 else segment


def _segments_and_trailing(gap: str) -> tuple[list[dict], str]:
    """Split a between-blocks gap into vote segments keyed by tally sentences.

    The last segment belongs to the vote whose details block follows the gap;
    earlier segments (contested votes printed without name lists) and a
    trailing proposition without a tally are handled by the caller.
    """
    tallies = [
        {
            "pos": match.start(),
            "yes": int(match.group(1)),
            "no": int(match.group(2)),
            "abstain": int(match.group(3)) if match.group(3) else None,
            "sentence": _sentence_at(gap, match.start()),
        }
        for match in TALLY_RE.finditer(gap)
    ]
    props = [match for match in PROPOSITION_RE.finditer(gap)]
    segments = []
    for entry in tallies:
        proposition = ""
        for prop in reversed(props):
            if prop.end() < entry["pos"] and entry["pos"] - prop.end() < 800:
                proposition = _normalize(gap[prop.end():entry["pos"]])[:MAX_PROPOSITION_CHARS]
                break
        segments.append(entry | {"proposition": proposition})
    trailing_proposition = ""
    last_tally_end = max((t["pos"] for t in tallies), default=-1)
    for prop in reversed(props):
        if prop.end() > last_tally_end:
            trailing_proposition = _normalize(gap[prop.end():prop.end() + MAX_PROPOSITION_CHARS])
            break
    return segments, trailing_proposition


def _round_starts(window: str) -> list[int]:
    """Start offsets of the vote rounds inside a details-block window.

    A round reference is a boundary only when labeled lists follow soon after
    (narrative mentions of "omröstning nr" must not split anything).
    """
    starts = [0]
    for match in ROUND_RE.finditer(window):
        if any(
            label_re.search(window, match.end(), min(len(window), match.end() + 120))
            for _, label_re in STANCE_LABELS
        ):
            starts.append(match.start())
    return starts


def _parse_window(window: str) -> list[tuple[int, ParsedVote]]:
    """(round_start_offset, ParsedVote) pairs for one details-block window."""
    parsed: list[tuple[int, ParsedVote]] = []
    round_starts = _round_starts(window)
    for i, seg_start in enumerate(round_starts):
        seg_end = round_starts[i + 1] if i + 1 < len(round_starts) else len(window)
        boundaries = _vote_boundaries(window, seg_start, seg_end)
        for j, group_start in enumerate(boundaries):
            group_end = boundaries[j + 1] if j + 1 < len(boundaries) else seg_end
            vote = ParsedVote(chunk_index=None, agenda_item_id=None)
            vote.records = _parse_lists(window, group_start, group_end)
            vote.has_details = bool(vote.records)
            if vote.records:
                parsed.append((group_start, vote))
    return parsed


def _tally_matches(vote: ParsedVote, segment: dict) -> bool:
    counts = {"ja": 0, "nej": 0, "avstar": 0}
    for record in vote.records:
        if record.stance in counts:
            counts[record.stance] += 1
    match = counts["ja"] == segment["yes"] and counts["nej"] == segment["no"]
    if match and segment["abstain"] is not None:
        match = counts["avstar"] == segment["abstain"]
    return match


def _finalize(vote: ParsedVote) -> None:
    if vote.tally_yes is None or not vote.records:
        return
    counts = {"ja": 0, "nej": 0, "avstar": 0}
    for record in vote.records:
        if record.stance in counts:
            counts[record.stance] += 1
    match = counts["ja"] == vote.tally_yes and counts["nej"] == vote.tally_no
    if match and vote.tally_abstain is not None:
        match = counts["avstar"] == vote.tally_abstain
    vote.records_match_tally = match


def _clean_page_noise(text: str) -> str:
    for pattern in PAGE_NOISE_RES:
        text = pattern.sub(" ", text)
    return text


def _stitch(chunks: list[dict]) -> tuple[str, list[tuple[int, int, int]]]:
    """Concatenate chunk texts (whitespace-normalized, page furniture dropped).

    Returns the stitched text and (start, end, chunk_index) spans so parsed
    positions can be attributed back to the chunk they started in.
    """
    parts: list[str] = []
    spans: list[tuple[int, int, int]] = []
    offset = 0
    for idx, chunk in enumerate(chunks):
        cleaned = _normalize(_clean_page_noise(chunk["text"]))
        if parts:
            parts.append(" ")
            offset += 1
        spans.append((offset, offset + len(cleaned), idx))
        parts.append(cleaned)
        offset += len(cleaned)
    return "".join(parts), spans


def _chunk_index_at(spans: list[tuple[int, int, int]], pos: int) -> int | None:
    owner = None
    for start, end, idx in spans:
        if start <= pos < end:
            return idx
        if start <= pos:
            owner = idx  # position in a join space: attribute to the previous chunk
    return owner


def _vote_from_segment(segment: dict, spans, chunks, abs_pos: int) -> ParsedVote:
    idx = _chunk_index_at(spans, abs_pos)
    agenda_item = chunks[idx]["agenda_item"] if idx is not None else None
    return ParsedVote(
        chunk_index=idx,
        agenda_item_id=agenda_item,
        proposition=segment["proposition"],
        outcome_text=segment["sentence"],
        tally_yes=segment["yes"],
        tally_no=segment["no"],
        tally_abstain=segment["abstain"],
    )


def parse_version(chunks: list[dict]) -> list[ParsedVote]:
    """Parse recorded votes from one version's chunk texts (dicts: pk/agenda_item/text)."""
    if not chunks:
        return []
    stitched, spans = _stitch(chunks)
    markers = [match.start() for match in MARKER_RE.finditer(stitched)]
    found: list[tuple[int, ParsedVote]] = []

    emitted: tuple[tuple[int, int, int | None], int] | None = None

    def emit_standalone(segment: dict, base: int) -> None:
        """Record a tally-only vote, deduplicating page-top repeats of the
        same result sentence (the protocol reprints it on every page)."""
        nonlocal emitted
        numbers = (segment["yes"], segment["no"], segment["abstain"])
        pos = base + segment["pos"]
        if emitted and emitted[0] == numbers and pos - emitted[1] < PAGE_REPEAT_CHARS:
            return
        emitted = (numbers, pos)
        found.append((pos, _vote_from_segment(segment, spans, chunks, pos)))

    for k, marker in enumerate(markers):
        gap_start = markers[k - 1] if k else 0
        segments, trailing_prop = _segments_and_trailing(stitched[gap_start:marker])
        block_end = markers[k + 1] if k + 1 < len(markers) else len(stitched)
        rounds = _parse_window(stitched[marker:block_end])

        # Gap tallies attach to the round whose named counts verify them; the
        # rest stand alone (contested votes printed without name lists).
        attached: dict[int, dict] = {}
        for segment in segments:
            target = next(
                (i for i, (_, vote) in enumerate(rounds) if i not in attached and _tally_matches(vote, segment)),
                None,
            )
            if target is None:
                emit_standalone(segment, gap_start)
            else:
                attached[target] = segment

        for i, (seg_start, round_vote) in enumerate(rounds):
            segment = attached.get(i)
            if segment:
                round_vote.proposition = segment["proposition"]
                round_vote.tally_yes = segment["yes"]
                round_vote.tally_no = segment["no"]
                round_vote.tally_abstain = segment["abstain"]
                round_vote.outcome_text = segment["sentence"]
            elif i == 0 and trailing_prop:
                round_vote.proposition = trailing_prop
            abs_pos = marker + seg_start
            round_vote.chunk_index = _chunk_index_at(spans, abs_pos)
            round_vote.agenda_item_id = (
                chunks[round_vote.chunk_index]["agenda_item"]
                if round_vote.chunk_index is not None
                else None
            )
            _finalize(round_vote)
            if round_vote.records or round_vote.tally_yes is not None:
                found.append((abs_pos, round_vote))

    # Tallies after the last block (or in a version without blocks) are votes
    # printed without named lists; each pairs with its nearest preceding
    # proposition.
    tail_start = markers[-1] if markers else 0
    tail_segments, _ = _segments_and_trailing(stitched[tail_start:])
    for segment in tail_segments:
        emit_standalone(segment, tail_start)

    found.sort(key=lambda pair: pair[0])
    return [vote for _, vote in found]


def store(version, chunks: list[dict], parsed: list[ParsedVote]) -> tuple[int, int]:
    """Replace the version's Vote/VoteRecord rows. Call inside a transaction."""
    from core.models import Vote, VoteRecord  # local import: parser stays import-light

    Vote.objects.filter(document_version=version).delete()
    votes_created = 0
    records_created = 0
    for sequence, parsed_vote in enumerate(parsed, start=1):
        chunk_pk = (
            chunks[parsed_vote.chunk_index]["pk"] if parsed_vote.chunk_index is not None else None
        )
        vote = Vote.objects.create(
            document_version=version,
            chunk_id=chunk_pk,
            agenda_item_id=parsed_vote.agenda_item_id,
            sequence=sequence,
            proposition=parsed_vote.proposition[:2000],
            outcome_text=parsed_vote.outcome_text[:1000],
            tally_yes=parsed_vote.tally_yes,
            tally_no=parsed_vote.tally_no,
            tally_abstain=parsed_vote.tally_abstain,
            has_details=parsed_vote.has_details,
            records_match_tally=parsed_vote.records_match_tally,
            parser_version=VOTE_PARSER_VERSION,
        )
        votes_created += 1
        if parsed_vote.records:
            VoteRecord.objects.bulk_create(
                VoteRecord(
                    vote=vote,
                    position=record.position,
                    member_name=record.member_name[:200],
                    party_code=record.party_code[:12],
                    stance=record.stance,
                )
                for record in parsed_vote.records
            )
            records_created += len(parsed_vote.records)
    return votes_created, records_created