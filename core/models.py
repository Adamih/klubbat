"""Authoritative data model: authorities, bodies, meetings, documents, versions, runs.

Parsed content (votes, decisions) references the document version only; the
IR sidecar on disk keeps the structured markdown and per-page text, so page- or
part-level association can be reintroduced later without re-paying extraction.
"""

from django.db import models
from django.db.models import Q


class Authority(models.Model):
    """A municipality or region. Stable internal identifier is the slug."""

    class Kind(models.TextChoices):
        MUNICIPALITY = "municipality", "Kommun"
        REGION = "region", "Region"

    slug = models.SlugField(max_length=120, unique=True)
    name = models.CharField(max_length=200)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Body(models.Model):
    """A decision-making body within an authority (e.g. kommunfullmäktige)."""

    authority = models.ForeignKey(Authority, on_delete=models.CASCADE, related_name="bodies")
    slug = models.SlugField(max_length=120)
    name = models.CharField(max_length=200)
    source_url = models.URLField(blank=True)
    # Number of elected members; used as a sanity reference for vote tallies.
    seat_count = models.IntegerField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["authority", "slug"], name="uniq_body_authority_slug"),
        ]
        ordering = ["authority__name", "name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.authority.name})"


class Meeting(models.Model):
    body = models.ForeignKey(Body, on_delete=models.CASCADE, related_name="meetings")
    date = models.DateField()
    title = models.CharField(max_length=300)
    source_url = models.URLField()
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["body", "source_url"], name="uniq_meeting_body_url"),
        ]
        ordering = ["-date"]

    def __str__(self) -> str:
        return f"{self.body.name} {self.date.isoformat()}"


class AgendaItem(models.Model):
    meeting = models.ForeignKey(Meeting, on_delete=models.CASCADE, related_name="agenda_items")
    agenda_number = models.IntegerField(null=True, blank=True)
    # End of an agenda-number range as published (e.g. "7-12." -> 7..12). Null unless ranged.
    agenda_number_end = models.IntegerField(null=True, blank=True)
    minutes_paragraph_number = models.IntegerField(null=True, blank=True)
    diarienummer = models.CharField(max_length=100, null=True, blank=True)
    title = models.CharField(max_length=500)
    source_url = models.URLField(blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["meeting", "agenda_number"],
                condition=Q(agenda_number__isnull=False),
                name="uniq_agendaitem_meeting_number",
            ),
        ]
        ordering = ["meeting__date", "agenda_number", "id"]

    def __str__(self) -> str:
        num = self.agenda_number if self.agenda_number is not None else "–"
        return f"§{num} {self.title[:60]}"


class Document(models.Model):
    """A logical document, identified by its first-seen source URL."""

    class DocType(models.TextChoices):
        KALLELSE = "kallelse", "Kallelse"
        AGENDA_LIST = "foredragningslista", "Föredragningslista"
        PROTOCOL = "protokoll", "Protokoll"
        CASE = "arende", "Ärende"
        ATTACHMENT = "bilaga", "Bilaga"
        MOTION = "motion", "Motion"
        INTERPELLATION = "interpellation", "Interpellation"
        QUESTION = "fraga", "Fråga"
        STATEMENT = "yttrande", "Yttrande"
        OTHER = "annat", "Övrigt"

    title = models.CharField(max_length=500)
    doc_type = models.CharField(max_length=30, choices=DocType.choices, default=DocType.OTHER)
    primary_source_url = models.URLField(unique=True)
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)
    # Latest retrieved version (may be superseded; older versions remain).
    current_version = models.ForeignKey(
        "core.DocumentVersion",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    class Meta:
        ordering = ["title", "id"]

    def __str__(self) -> str:
        return f"{self.title[:80]} [{self.doc_type}]"


class DocumentAssociation(models.Model):
    """Links a logical document to the meeting (and optional agenda item) it belongs to."""

    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="associations")
    meeting = models.ForeignKey(Meeting, on_delete=models.CASCADE, related_name="document_associations")
    agenda_item = models.ForeignKey(
        AgendaItem, null=True, blank=True, on_delete=models.SET_NULL, related_name="document_associations"
    )
    source_context = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["document", "meeting"])]
        ordering = ["meeting__date", "id"]

    def __str__(self) -> str:
        return f"{self.document_id} @ meeting {self.meeting_id}"


class DocumentVersion(models.Model):
    """Stored bytes of one revision of a logical document. SHA-256 identifies file content."""

    class Status(models.TextChoices):
        PENDING = "pending", "Ej bearbetad"
        PROCESSED = "processed", "Bearbetad"
        FAILED = "failed", "Misslyckad"

    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="versions")
    sha256 = models.CharField(max_length=64, null=True, blank=True)
    original_url = models.URLField()
    stored_path = models.CharField(max_length=500, blank=True)  # relative to ORIGINALS_DIR
    size_bytes = models.IntegerField(null=True, blank=True)
    retrieved_at = models.DateTimeField(null=True, blank=True)
    source_revision_label = models.CharField(max_length=100, null=True, blank=True)  # e.g. "justerat"
    processing_status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    processing_error = models.TextField(blank=True)
    ocr_used = models.BooleanField(default=False)
    extracted_at = models.DateTimeField(null=True, blank=True)
    # Stage A output: the version's rendered markdown (xberg IR). The full
    # structured IR lives in the derived sidecar; this column is what search
    # and the document page read.
    markdown = models.TextField(blank=True)
    # IR format that produced the markdown (mirrors ingestion.ir.IR_VERSION).
    ir_version = models.CharField(max_length=100, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    # Latest vote-parser stage (stage C) that produced this version's Vote rows.
    vote_parser_version = models.CharField(max_length=100, null=True, blank=True)
    # Latest decision-parser stage that produced this version's Decision rows.
    decision_parser_version = models.CharField(max_length=100, null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["document", "sha256"],
                condition=Q(sha256__isnull=False),
                name="uniq_docversion_doc_hash",
            ),
        ]
        indexes = [
            models.Index(fields=["original_url"]),
            models.Index(fields=["processing_status"]),
        ]
        ordering = ["-retrieved_at", "-id"]

    def __str__(self) -> str:
        return f"v{self.pk} {self.document.title[:60]} ({self.processing_status})"


class Vote(models.Model):
    """One recorded vote (stage C output), parsed from a version's markdown.

    Only the latest parse is kept: re-extraction deletes and recreates the rows.
    Attribution is document-level for now: the printed agenda item is carried
    as printed metadata, without page or part references.
    """

    class Stance(models.TextChoices):
        YES = "ja", "Ja"
        NO = "nej", "Nej"
        ABSTAIN = "avstar", "Avstår"
        ABSENT = "franvarande", "Frånvarande"

    class VoteType(models.TextChoices):
        BIFALL = "bifall", "Bifall"
        AVSLAG = "avslag", "Avslag"
        AVVISNING = "avvisning", "Avvisning"
        ATEREMITTERA = "ateremittera", "Återremittering"
        BORDLAGGNING = "bordlaggning", "Bordläggning"
        VAL = "val", "Val/förordnande"
        PROCEDUR = "procedur", "Procedurell röstning"
        ANNAT = "annat", "Annat"

    document_version = models.ForeignKey(DocumentVersion, on_delete=models.CASCADE, related_name="votes")
    sequence = models.IntegerField()  # order of the vote within the version
    proposition = models.TextField(blank=True)  # "Rösta Ja för ..., rösta Nej för ..."
    outcome_text = models.TextField(blank=True)  # "Med 39 ja-röster mot 40 nej-röster ..." sentence
    decision_text = models.TextField(blank=True)  # decided clause, e.g. "bifalla kommunstyrelsens förslag"
    vote_type = models.CharField(
        max_length=20, choices=VoteType.choices, default=VoteType.ANNAT
    )
    # Agenda item as printed in the protocol ("Svar på motion om att utreda
    # ..."), used as the vote's subject.
    printed_agenda_number = models.IntegerField(null=True, blank=True)
    agenda_title = models.CharField(max_length=300, blank=True)
    tally_yes = models.IntegerField(null=True, blank=True)
    tally_no = models.IntegerField(null=True, blank=True)
    tally_abstain = models.IntegerField(null=True, blank=True)
    has_details = models.BooleanField(default=False)  # named voter lists were present
    # None when there is no stated tally to compare the named voters against.
    records_match_tally = models.BooleanField(null=True, blank=True)
    parser_version = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["document_version", "sequence"], name="uniq_vote_version_seq"),
        ]
        ordering = ["document_version_id", "sequence"]

    def __str__(self) -> str:
        return f"vote v{self.document_version_id} #{self.sequence}"


class Case(models.Model):
    """An initiative (motion, interpellation, fråga) or committee case, keyed
    on its diarienummer. The dnr is printed in the initiating document, the
    committee report, and the protocol's decision heading, so it stitches the
    whole chain together. Built by the build_cases pass."""

    class Kind(models.TextChoices):
        MOTION = "motion", "Motion"
        INTERPELLATION = "interpellation", "Interpellation"
        FRAGA = "fraga", "Fråga"
        ARENDE = "arende", "Ärende"
        ANNAT = "annat", "Annat"

    dnr = models.CharField(max_length=100, unique=True)
    title = models.CharField(max_length=400, blank=True)
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.ARENDE)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["dnr"]

    def __str__(self) -> str:
        return f"{self.dnr} {self.title[:60]}"


class CaseDocument(models.Model):
    """A document that belongs to a case: it mentions the case's dnr."""

    case = models.ForeignKey(Case, on_delete=models.CASCADE, related_name="case_documents")
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="case_links")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["case", "document"], name="uniq_case_document"),
        ]
        ordering = ["case_id", "id"]

    def __str__(self) -> str:
        return f"{self.case_id} <- {self.document_id}"


class Decision(models.Model):
    """One recorded decision (stage C output), parsed from a protocol's
    "§ N <title> … Beslut" block. Most decisions are unanimous and never reach
    a Vote; the printed decision text is the content. Attribution is
    document-level for now: the printed agenda item/dnr are carried as printed
    metadata, without page or part references."""

    class Outcome(models.TextChoices):
        BIFALL = "bifall", "Bifall"
        AVSLAG = "avslag", "Avslag"
        AVVISNING = "avvisning", "Avvisning"
        ATEREMITTERA = "ateremittera", "Återremittering"
        BORDLAGGNING = "bordlaggning", "Bordläggning"
        VAL = "val", "Val/förordnande"
        PROCEDUR = "procedur", "Procedurellt"
        ANNAT = "annat", "Annat"

    document_version = models.ForeignKey(DocumentVersion, on_delete=models.CASCADE, related_name="decisions")
    sequence = models.IntegerField()  # order of the decision within the version
    # Agenda item as printed in the protocol heading, used as the subject when
    # no AgendaItem row matches.
    printed_agenda_number = models.IntegerField(null=True, blank=True)
    printed_agenda_title = models.CharField(max_length=300, blank=True)
    printed_dnr = models.CharField(max_length=100, blank=True)
    decision_text = models.TextField()
    outcome = models.CharField(max_length=20, choices=Outcome.choices, default=Outcome.ANNAT)
    parser_version = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["document_version", "sequence"], name="uniq_decision_version_seq"),
        ]
        ordering = ["document_version_id", "sequence"]

    def __str__(self) -> str:
        return f"decision v{self.document_version_id} #{self.sequence}"


class VoteReservation(models.Model):
    """A reservation (reservation) or special opinion (särskilt yttrande)
    printed together with a vote's decision, with the parties or member behind it."""

    class Kind(models.TextChoices):
        RESERVATION = "reservation", "Reservation"
        YTTRANDE = "yttrande", "Särskilt yttrande"

    vote = models.ForeignKey(Vote, on_delete=models.CASCADE, related_name="reservations")
    position = models.IntegerField()
    kind = models.CharField(max_length=12, choices=Kind.choices)
    # Comma-separated party codes as printed ("M, KD, SD"); empty when the
    # entry belongs to a single named member.
    party_codes = models.CharField(max_length=100, blank=True)
    member_name = models.CharField(max_length=200, blank=True)
    text = models.TextField()

    class Meta:
        ordering = ["vote_id", "position"]

    def __str__(self) -> str:
        who = self.member_name or self.party_codes or "?"
        return f"{self.get_kind_display()} ({who})"


class VoteRecord(models.Model):
    """One named member's stance in a Vote, as printed in the protocol."""

    vote = models.ForeignKey(Vote, on_delete=models.CASCADE, related_name="records")
    position = models.IntegerField()
    member_name = models.CharField(max_length=200)
    party_code = models.CharField(max_length=12)
    stance = models.CharField(max_length=12, choices=Vote.Stance.choices)

    class Meta:
        ordering = ["vote_id", "position"]

    def __str__(self) -> str:
        return f"{self.member_name} ({self.party_code}) {self.stance}"


class Topic(models.Model):
    """A policy-area label in the fixed classifier taxonomy (defined in
    ingestion/classify.py). Rows are seeded from the constant; removed labels
    are retired manually in admin."""

    slug = models.SlugField(max_length=100, unique=True)
    label = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["label"]

    def __str__(self) -> str:
        return self.label


class Tag(models.Model):
    """One topic classification of a decision or initiative document (stage D).

    One tag per target, stamped with classifier_version and a confidence in
    [0, 1]. Rows tagged by hand (is_manual) are the training seed for the
    centroid classifier and are never overwritten by re-classification.
    """

    decision = models.ForeignKey(Decision, null=True, blank=True, on_delete=models.CASCADE, related_name="tags")
    document = models.ForeignKey(Document, null=True, blank=True, on_delete=models.CASCADE, related_name="tags")
    topic = models.ForeignKey(Topic, on_delete=models.CASCADE, related_name="tags")
    confidence = models.FloatField(default=0.0)
    classifier_version = models.CharField(max_length=100)
    is_manual = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["decision", "topic"],
                condition=Q(decision__isnull=False),
                name="uniq_tag_decision_topic",
            ),
            models.UniqueConstraint(
                fields=["document", "topic"],
                condition=Q(document__isnull=False),
                name="uniq_tag_document_topic",
            ),
        ]
        indexes = [models.Index(fields=["topic", "decision"]), models.Index(fields=["topic", "document"])]

    def __str__(self) -> str:
        target = self.decision or self.document
        return f"{self.topic} ({self.confidence:.2f}) on {target}"


class IngestionRun(models.Model):
    class Status(models.TextChoices):
        RUNNING = "running", "Pågående"
        SUCCESS = "success", "Klar"
        PARTIAL = "partial", "Klar med fel"
        FAILED = "failed", "Misslyckad"

    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    scope = models.CharField(max_length=200)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.RUNNING)
    error_summary = models.TextField(blank=True)
    meetings_seen = models.IntegerField(default=0)
    documents_seen = models.IntegerField(default=0)
    documents_created = models.IntegerField(default=0)
    versions_created = models.IntegerField(default=0)
    failures = models.IntegerField(default=0)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self) -> str:
        return f"run {self.pk} [{self.scope}] {self.status}"
