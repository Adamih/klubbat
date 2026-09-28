"""Authoritative data model: authorities, bodies, meetings, documents, versions, chunks, runs."""

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
    created_at = models.DateTimeField(auto_now_add=True)

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


class Chunk(models.Model):
    """A searchable text chunk of one document version, with page references."""

    document_version = models.ForeignKey(DocumentVersion, on_delete=models.CASCADE, related_name="chunks")
    sequence = models.IntegerField()
    agenda_item = models.ForeignKey(AgendaItem, null=True, blank=True, on_delete=models.SET_NULL, related_name="chunks")
    title = models.CharField(max_length=500)
    text = models.TextField()
    page_start = models.IntegerField()
    page_end = models.IntegerField()
    processing_version = models.CharField(max_length=100)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["document_version", "sequence"], name="uniq_chunk_version_seq"),
        ]
        indexes = [models.Index(fields=["document_version", "sequence"])]
        ordering = ["document_version_id", "sequence"]

    def __str__(self) -> str:
        return f"chunk {self.pk} seq {self.sequence} p{self.page_start}-{self.page_end}"


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
    chunks_created = models.IntegerField(default=0)
    failures = models.IntegerField(default=0)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self) -> str:
        return f"run {self.pk} [{self.scope}] {self.status}"
