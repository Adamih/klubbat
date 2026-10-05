"""Django admin for internal inspection, corrections, and retry actions."""

from django.contrib import admin
from django.core.exceptions import PermissionDenied

from .models import (
    AgendaItem,
    Authority,
    Body,
    Case,
    CaseDocument,
    Decision,
    Document,
    DocumentAssociation,
    DocumentVersion,
    IngestionRun,
    LabelledExample,
    Meeting,
    Tag,
    Topic,
    Vote,
    VoteRecord,
    VoteReservation,
)


@admin.register(Authority)
class AuthorityAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "kind")
    search_fields = ("name", "slug")
    list_filter = ("kind",)


@admin.register(Body)
class BodyAdmin(admin.ModelAdmin):
    list_display = ("name", "authority", "slug", "seat_count", "source_url")
    list_filter = ("authority",)
    search_fields = ("name",)


class AgendaItemInline(admin.TabularInline):
    model = AgendaItem
    extra = 0
    fields = ("agenda_number", "agenda_number_end", "minutes_paragraph_number", "diarienummer", "title")
    show_change_link = True


@admin.register(Meeting)
class MeetingAdmin(admin.ModelAdmin):
    list_display = ("date", "body", "title", "source_url")
    list_filter = ("body",)
    date_hierarchy = "date"
    search_fields = ("title",)
    inlines = [AgendaItemInline]


@admin.register(AgendaItem)
class AgendaItemAdmin(admin.ModelAdmin):
    list_display = ("meeting", "agenda_number", "agenda_number_end", "minutes_paragraph_number", "diarienummer", "title")
    list_filter = ("meeting__body",)
    search_fields = ("title", "diarienummer")


class TagInline(admin.TabularInline):
    model = Tag
    extra = 0
    fields = ("topic", "confidence", "classifier_version")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class DocumentAssociationInline(admin.TabularInline):
    model = DocumentAssociation
    extra = 0
    fields = ("meeting", "agenda_item", "source_context")


class DocumentVersionInline(admin.TabularInline):
    model = DocumentVersion
    extra = 0
    fields = ("sha256", "original_url", "retrieved_at", "source_revision_label", "processing_status", "ocr_used")
    show_change_link = True


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = ("title", "doc_type", "current_version_status", "created_display")
    list_filter = ("doc_type", "current_version__processing_status")
    search_fields = ("title", "primary_source_url")
    inlines = [DocumentAssociationInline, DocumentVersionInline, TagInline]

    @admin.display(description="Status", ordering="current_version__processing_status")
    def current_version_status(self, obj):
        return obj.current_version.processing_status if obj.current_version else "—"

    @admin.display(description="Skapad", ordering="first_seen_at")
    def created_display(self, obj):
        return obj.first_seen_at


@admin.register(DocumentVersion)
class DocumentVersionAdmin(admin.ModelAdmin):
    list_display = (
        "document",
        "processing_status",
        "sha256",
        "retrieved_at",
        "source_revision_label",
        "ocr_used",
        "size_bytes",
    )
    list_filter = ("processing_status", "ocr_used", "document__doc_type")
    search_fields = ("document__title", "sha256", "original_url")
    readonly_fields = ("sha256", "stored_path", "size_bytes", "retrieved_at", "created_at")
    actions = ["requeue_for_processing", "mark_failed_for_review"]

    @admin.action(description="Köa om för extraktion (retry/reprocess)")
    def requeue_for_processing(self, request, queryset):
        count = queryset.update(processing_status="pending", processing_error="")
        self.message_user(request, f"{count} versioner köade för bearbetning.")

    @admin.action(description="Markera som misslyckad (behåll för granskning)")
    def mark_failed_for_review(self, request, queryset):
        count = queryset.update(processing_status="failed")
        self.message_user(request, f"{count} versioner markerade som misslyckade.")


@admin.register(DocumentAssociation)
class DocumentAssociationAdmin(admin.ModelAdmin):
    list_display = ("document", "meeting", "agenda_item", "source_context")
    raw_id_fields = ("document", "meeting", "agenda_item")


class VoteRecordInline(admin.TabularInline):
    model = VoteRecord
    extra = 0
    fields = ("position", "member_name", "party_code", "stance")


class VoteReservationInline(admin.TabularInline):
    model = VoteReservation
    extra = 0
    fields = ("position", "kind", "party_codes", "member_name", "text")


@admin.register(Vote)
class VoteAdmin(admin.ModelAdmin):
    list_display = (
        "document_version",
        "sequence",
        "vote_type",
        "has_details",
        "tally_yes",
        "tally_no",
        "records_match_tally",
        "parser_version",
    )
    list_filter = ("has_details", "records_match_tally", "vote_type")
    raw_id_fields = ("document_version",)
    inlines = [VoteRecordInline, VoteReservationInline]


@admin.register(Topic)
class TopicAdmin(admin.ModelAdmin):
    list_display = ("label", "slug", "description")
    search_fields = ("label", "slug")


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    list_display = ("topic", "target_display", "confidence", "classifier_version")
    list_filter = ("topic",)
    search_fields = ("decision__decision_text", "decision__printed_agenda_title", "document__title")
    raw_id_fields = ("decision", "document", "topic")
    actions = ["save_labelled_examples"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_create_example_permission(self, request):
        return request.user.has_perm("core.add_labelledexample")

    @admin.action(description="Kopiera till märkta exempel (granska ämnet efteråt)", permissions=["create_example"])
    def save_labelled_examples(self, request, queryset):
        if not self.has_create_example_permission(request):
            raise PermissionDenied
        created = 0
        for tag in queryset.select_related("decision__document_version", "document__current_version", "topic"):
            if tag.decision is not None:
                title = tag.decision.printed_agenda_title
                text = tag.decision.decision_text
                source_url = tag.decision.document_version.original_url
            elif tag.document is not None:
                title = tag.document.title
                text = tag.document.current_version.markdown if tag.document.current_version else ""
                source_url = tag.document.primary_source_url
            else:
                continue
            _, new = LabelledExample.objects.get_or_create(
                topic=tag.topic, title=title, text=text, source_url=source_url,
            )
            created += new
        self.message_user(request, f"{created} märkta exempel skapade. Granska och korrigera ämnet under Märkta exempel.")

    @admin.display(description="Mål")
    def target_display(self, obj):
        if obj.decision is not None:
            return f"beslut: {obj.decision.printed_agenda_title or obj.decision.decision_text[:60]}"
        return f"dok: {obj.document.title[:60]}" if obj.document else "—"


@admin.register(LabelledExample)
class LabelledExampleAdmin(admin.ModelAdmin):
    list_display = ("title", "topic", "updated_at")
    list_filter = ("topic",)
    search_fields = ("title", "text", "source_url")
    readonly_fields = ("created_at", "updated_at")


@admin.register(VoteReservation)
class VoteReservationAdmin(admin.ModelAdmin):
    list_display = ("vote", "position", "kind", "party_codes", "member_name")
    list_filter = ("kind",)
    search_fields = ("text", "member_name", "party_codes")


class CaseDocumentInline(admin.TabularInline):
    model = CaseDocument
    extra = 0
    raw_id_fields = ("document",)


@admin.register(Case)
class CaseAdmin(admin.ModelAdmin):
    list_display = ("dnr", "title", "kind", "created_display")
    list_filter = ("kind",)
    search_fields = ("dnr", "title")
    inlines = [CaseDocumentInline]

    @admin.display(description="Skapad", ordering="created_at")
    def created_display(self, obj):
        return obj.created_at


@admin.register(Decision)
class DecisionAdmin(admin.ModelAdmin):
    list_display = ("document_version", "sequence", "printed_agenda_number", "outcome", "parser_version")
    list_filter = ("outcome",)
    search_fields = ("decision_text", "printed_agenda_title", "printed_dnr")
    raw_id_fields = ("document_version",)
    inlines = [TagInline]


@admin.register(IngestionRun)
class IngestionRunAdmin(admin.ModelAdmin):
    list_display = (
        "started_at",
        "scope",
        "status",
        "meetings_seen",
        "documents_created",
        "versions_created",
        "failures",
    )
    list_filter = ("status", "scope")
    readonly_fields = (
        "started_at",
        "finished_at",
        "scope",
        "status",
        "error_summary",
        "meetings_seen",
        "documents_seen",
        "documents_created",
        "versions_created",
        "failures",
    )
