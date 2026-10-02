"""Vote extraction (stage C): recorded votes parsed from a version's IR markdown.

The markdown (stage A output, cached per sha256) is flattened to plain text and
parsed by the protocol's own printed structure: an "Omröstningsdetaljer" block
of labeled name lists, plus optional tally/proposition sentences elsewhere in
the text. Results are stamped with VOTE_PARSER_VERSION per document version;
re-extraction deletes and recreates the rows (latest state only, no history).
Attribution is document-level for now: votes carry the printed agenda item, not
page or chunk references.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

VOTE_PARSER_VERSION = "omrostningsdetaljer-v7"

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

# Page furniture that survives the layout pass on some documents. Only the
# noise itself may be removed — never the whole line around it.
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

# A reservation entry: "(M), (KD) och (SD) reserverar sig ..." optionally
# preceded by words like "Närvarande ledamöter" or the party's name. The
# lead part must not cross sentence punctuation, so a match always starts
# at (or near) the beginning of the entry's own sentence.
RESERVATION_ENTRY_RE = re.compile(
    r"(?P<lead>[^.:;()\n]{0,70}?)"
    r"(?P<parties>(?:\([^()\d]{1,10}\)\s*(?:,\s*|\s+och\s+)?)+)"
    r"reserverar",
    re.IGNORECASE,
)
# A särskilt yttrande entry: "Lina Hamberg (UP) inkommer med särskilt yttrande: ..."
YTTRANDE_ENTRY_RE = re.compile(
    r"(?P<name>[A-ZÅÄÖ][^():.;\n]{2,60}?)\s*\((?P<party>[^()\d]{1,10})\)\s+"
    r"inkommer\s+med\s+särskilt\s+yttrande",
    re.IGNORECASE,
)
# Leading section headings that the entry matcher can start inside; stripped
# from entry text and names.
RESERVATION_HEADING_STRIP_RE = re.compile(
    r"^(?:Särskilda yttranden|Reservationer)\s+", re.IGNORECASE
)

# Agenda item heading as printed in protocols: "§ 123 Svar på motion om att
# utreda en breddning av Kungsängsbron från Therez Almerfors (M)
# KSN-2024-03238 Beslut". The title runs to the optional diarienummer or the
# "Beslut" keyword that starts the decision text. Shared by the decision
# parser (ingestion/decisions.py).
AGENDA_HEADING_RE = re.compile(
    r"§\s*(?P<number>\d{1,3})\s+"
    r"(?P<title>[^§\n]{3,300}?)"
    r"(?:\s+(?P<dnr>[A-ZÅÄÖ]{2,6}-\d{4}-\d{3,6}))?"
    r"\s+Beslut\b"
)

# Vote-type classification, applied to the outcome sentence first and the
# proposition second; first matching type wins.
_VOTE_TYPE_RES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ateremittera", re.compile(r"återremitter", re.IGNORECASE)),
    ("bifall", re.compile(r"\bbifall(?:a|s)?\b|biföll", re.IGNORECASE)),
    ("avslag", re.compile(r"\bavslag\b|avslår|avslås\b", re.IGNORECASE)),
    ("avvisning", re.compile(r"\bavvis(?:a|ad|as|ning)\b", re.IGNORECASE)),
    ("bordlaggning", re.compile(r"bordlägg", re.IGNORECASE)),
    (
        "val",
        re.compile(r"\bval(?:s|et|des|de)?\b|tjänsteförordnande|\bförordnand|utsågs|omval", re.IGNORECASE),
    ),
    ("procedur", re.compile(r"avgöra (?:om )?ärendet|ärendet idag|avgöras (?:idag|genast)", re.IGNORECASE)),
)

# The decided clause inside the outcome sentence, e.g.
# "… beslutar att bifalla kommunstyrelsens förslag" -> "bifalla kommunstyrelsens förslag".
_DECISION_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"beslutar(?:t)?\s+att\s+(.+)", re.IGNORECASE),
    re.compile(r"beslut(?:ades|ats)\s+att\s+(.+)", re.IGNORECASE),
    re.compile(
        r"beslut(?:ades|ats)\s+(avslag|bifall|avvisning|bordläggning|återremittering)", re.IGNORECASE
    ),
)

MAX_PROPOSITION_CHARS = 600
# The result sentence ("Med N ja-röster ...") is reprinted at the top of every
# page a details section spans; identical tallies closer than this are repeats.
PAGE_REPEAT_CHARS = 5000
# Reservations/särskilda yttranden are searched for in the text between a
# vote's outcome sentence and the next vote/section.
MAX_RESERVATION_WINDOW_CHARS = 6000
MAX_RESERVATION_ENTRIES = 10
MAX_RESERVATION_CHARS = 3000


@dataclass
class ParsedRecord:
    position: int
    member_name: str
    party_code: str
    stance: str


@dataclass
class ParsedReservation:
    kind: str  # "reservation" | "yttrande"
    party_codes: list[str] = field(default_factory=list)
    member_name: str = ""
    text: str = ""


@dataclass
class ParsedVote:
    proposition: str = ""
    outcome_text: str = ""
    decision_text: str = ""
    vote_type: str = "annat"
    printed_agenda_number: int | None = None
    printed_agenda_title: str = ""
    tally_yes: int | None = None
    tally_no: int | None = None
    tally_abstain: int | None = None
    records: list[ParsedRecord] = field(default_factory=list)
    reservations: list[ParsedReservation] = field(default_factory=list)
    has_details: bool = False
    records_match_tally: bool | None = None


def _markdown_plain(md: str) -> str:
    """Flatten xberg markdown to plain text: drop heading markers and emphasis."""
    lines = []
    for line in md.splitlines():
        line = line.strip()
        line = re.sub(r"^#{1,6}\s+", "", line)
        line = line.replace("**", "").replace("*", "")
        lines.append(line)
    return "\n".join(lines)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _clean_page_noise(text: str) -> str:
    for pattern in PAGE_NOISE_RES:
        text = pattern.sub(" ", text)
    return text


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
        limit = min(limit, label_end + 4000)
        for position, (name, party) in enumerate(_scan_list(text, label_end, limit), start=1):
            records.append(ParsedRecord(position, name, party, stance))
    return records


def _vote_boundaries(text: str, seg_start: int, seg_end: int) -> list[int]:
    """Vote starts within one round segment.

    Within one vote the labeled sections always appear in ja → nej → avstår →
    frånvarande order; a stance that repeats or steps back ("Ja-röst avger"
    again after "Avstår", or twice in a row) therefore opens a second vote
    group, which the protocol prints without a new "Omröstningsdetaljer".
    """
    rank = {stance: i for i, (stance, _) in enumerate(STANCE_LABELS)}
    boundaries = [seg_start]
    previous_rank = None
    for label_start, _, stance in _stance_sections(text, seg_start, seg_end):
        if previous_rank is not None and rank[stance] <= previous_rank:
            boundaries.append(label_start)
        previous_rank = rank[stance]
    return boundaries


def _sentence_at(text: str, pos: int) -> str:
    segment = _normalize(text[pos:pos + 300])
    end = segment.find(". ")
    return segment[: end + 1] if end != -1 else segment


def _classify_vote(proposition: str, outcome_text: str) -> str:
    """Vote type from the outcome sentence first, the proposition second."""
    for source in (outcome_text, proposition):
        if not source:
            continue
        for type_code, pattern in _VOTE_TYPE_RES:
            if pattern.search(source):
                return type_code
    return "annat"


def _decision_text(outcome_text: str) -> str:
    """The decided clause of the outcome sentence, without the vote-count preamble."""
    for pattern in _DECISION_RES:
        match = pattern.search(outcome_text)
        if match:
            return _normalize(match.group(1).strip().rstrip("."))[:200]
    return ""


def _annotate(vote: ParsedVote) -> None:
    vote.vote_type = _classify_vote(vote.proposition, vote.outcome_text)
    vote.decision_text = _decision_text(vote.outcome_text)


def _reservation_windows(
    text: str, base: int, segments: list[dict], next_boundary: int
) -> dict[int, str]:
    """Per-segment text between a vote's outcome sentence and the next thing
    printed (the next tally, the next vote's proposition lead-in, or the
    boundary that ends this stretch): where reservations/yttranden live."""
    windows: dict[int, str] = {}
    for i, segment in enumerate(segments):
        sentence_end = base + segment["pos"] + len(segment["sentence"])
        candidates = [next_boundary]
        if i + 1 < len(segments):
            candidates.append(base + segments[i + 1]["pos"])
        following_prop = PROPOSITION_RE.search(text, sentence_end, next_boundary)
        if following_prop:
            candidates.append(following_prop.start())
        end = min(candidates)
        if end <= sentence_end:
            continue
        window = text[sentence_end:min(end, sentence_end + MAX_RESERVATION_WINDOW_CHARS)]
        if _normalize(window):
            windows[i] = window
    return windows


def _parse_reservations(window: str) -> list[ParsedReservation]:
    """Entries that follow a vote's decision: reservations by one or more
    parties, and särskilda yttranden by named members."""
    entries: list[tuple[int, ParsedReservation]] = []
    for match in RESERVATION_ENTRY_RE.finditer(window):
        codes = [code.strip().upper() for code in re.findall(r"\(([^()\d]{1,10})\)", match.group("parties"))]
        entries.append((match.start(), ParsedReservation(kind="reservation", party_codes=codes)))
    for match in YTTRANDE_ENTRY_RE.finditer(window):
        entries.append(
            (
                match.start(),
                ParsedReservation(
                    kind="yttrande",
                    party_codes=[match.group("party").strip().upper()],
                    member_name=_normalize(match.group("name")),
                ),
            )
        )
    entries.sort(key=lambda entry: entry[0])

    parsed: list[ParsedReservation] = []
    for i, (start, reservation) in enumerate(entries[:MAX_RESERVATION_ENTRIES]):
        end = entries[i + 1][0] if i + 1 < len(entries) else len(window)
        text = RESERVATION_HEADING_STRIP_RE.sub("", _normalize(window[start:end]))
        if not text:
            continue
        reservation.text = text[:MAX_RESERVATION_CHARS]
        reservation.member_name = RESERVATION_HEADING_STRIP_RE.sub("", reservation.member_name)
        parsed.append(reservation)
    return parsed


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
            vote = ParsedVote()
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


def parse_markdown(markdown: str) -> list[ParsedVote]:
    """Parse recorded votes from one version's IR markdown."""
    text = _normalize(_clean_page_noise(_markdown_plain(markdown)))
    if not text:
        return []
    markers = [match.start() for match in MARKER_RE.finditer(text)]
    # "§ N <title> ... Beslut" headings, for resolving each vote's agenda item.
    headings = [
        (match.start(), int(match.group("number")), _normalize(match.group("title")))
        for match in AGENDA_HEADING_RE.finditer(text)
    ]

    def agenda_at(pos: int) -> tuple[int | None, str]:
        number = title = None
        for heading_pos, heading_number, heading_title in headings:
            if heading_pos > pos:
                break
            number, title = heading_number, heading_title
        return number, (title or "")

    found: list[tuple[int, ParsedVote]] = []

    emitted: tuple[tuple[int, int, int | None], int] | None = None

    def emit_standalone(segment: dict, base: int, reservations: list[ParsedReservation]) -> None:
        """Record a tally-only vote, deduplicating page-top repeats of the
        same result sentence (the protocol reprints it on every page)."""
        nonlocal emitted
        numbers = (segment["yes"], segment["no"], segment["abstain"])
        pos = base + segment["pos"]
        if emitted and emitted[0] == numbers and pos - emitted[1] < PAGE_REPEAT_CHARS:
            return
        emitted = (numbers, pos)
        vote = ParsedVote(
            proposition=segment["proposition"],
            outcome_text=segment["sentence"],
            tally_yes=segment["yes"],
            tally_no=segment["no"],
            tally_abstain=segment["abstain"],
        )
        vote.reservations = reservations
        vote.printed_agenda_number, vote.printed_agenda_title = agenda_at(pos)
        _annotate(vote)
        found.append((pos, vote))

    for k, marker in enumerate(markers):
        gap_start = markers[k - 1] if k else 0
        segments, trailing_prop = _segments_and_trailing(text[gap_start:marker])
        block_end = markers[k + 1] if k + 1 < len(markers) else len(text)
        rounds = _parse_window(text[marker:block_end])
        reservation_windows = _reservation_windows(text, gap_start, segments, marker)
        parsed_windows = {i: _parse_reservations(window) for i, window in reservation_windows.items()}

        # Gap tallies attach to the round whose named counts verify them; the
        # rest stand alone (contested votes printed without name lists).
        attached: dict[int, int] = {}
        for seg_index, segment in enumerate(segments):
            target = next(
                (i for i, (_, vote) in enumerate(rounds) if i not in attached and _tally_matches(vote, segment)),
                None,
            )
            if target is None:
                emit_standalone(segment, gap_start, parsed_windows.get(seg_index, []))
            else:
                attached[target] = seg_index

        for i, (seg_start, round_vote) in enumerate(rounds):
            seg_index = attached.get(i)
            if seg_index is not None:
                segment = segments[seg_index]
                round_vote.proposition = segment["proposition"]
                round_vote.tally_yes = segment["yes"]
                round_vote.tally_no = segment["no"]
                round_vote.tally_abstain = segment["abstain"]
                round_vote.outcome_text = segment["sentence"]
                round_vote.reservations = parsed_windows.get(seg_index, [])
            elif i == 0 and trailing_prop:
                round_vote.proposition = trailing_prop
            abs_pos = marker + seg_start
            round_vote.printed_agenda_number, round_vote.printed_agenda_title = agenda_at(abs_pos)
            _finalize(round_vote)
            _annotate(round_vote)
            if round_vote.records or round_vote.tally_yes is not None:
                found.append((abs_pos, round_vote))

    # Tallies after the last block (or in a version without blocks) are votes
    # printed without named lists; each pairs with its nearest preceding
    # proposition.
    tail_start = markers[-1] if markers else 0
    tail_segments, _ = _segments_and_trailing(text[tail_start:])
    tail_windows = _reservation_windows(text, tail_start, tail_segments, len(text))
    for seg_index, segment in enumerate(tail_segments):
        emit_standalone(
            segment,
            tail_start,
            _parse_reservations(tail_windows[seg_index]) if seg_index in tail_windows else [],
        )

    found.sort(key=lambda pair: pair[0])
    return [vote for _, vote in found]


def store(version, parsed: list[ParsedVote]) -> tuple[int, int]:
    """Replace the version's Vote/VoteRecord/VoteReservation rows. Call inside a transaction."""
    from core.models import Vote, VoteRecord, VoteReservation  # local import: parser stays import-light

    Vote.objects.filter(document_version=version).delete()
    votes_created = 0
    records_created = 0
    for sequence, parsed_vote in enumerate(parsed, start=1):
        vote = Vote.objects.create(
            document_version=version,
            sequence=sequence,
            proposition=parsed_vote.proposition[:2000],
            outcome_text=parsed_vote.outcome_text[:1000],
            decision_text=parsed_vote.decision_text[:200],
            vote_type=parsed_vote.vote_type,
            printed_agenda_number=parsed_vote.printed_agenda_number,
            agenda_title=parsed_vote.printed_agenda_title[:300],
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
        if parsed_vote.reservations:
            VoteReservation.objects.bulk_create(
                VoteReservation(
                    vote=vote,
                    position=position,
                    kind=reservation.kind,
                    party_codes=", ".join(reservation.party_codes)[:100],
                    member_name=reservation.member_name[:200],
                    text=reservation.text[:MAX_RESERVATION_CHARS],
                )
                for position, reservation in enumerate(parsed_vote.reservations, start=1)
            )
    return votes_created, records_created
