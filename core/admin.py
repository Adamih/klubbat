"""Django admin for internal inspection, corrections, and retry actions."""

from django.contrib import admin

from .models import (
    AgendaItem,
    Authority,
    Body,
    Chunk,
    Document,
    DocumentAssociation,
    DocumentVersion,
    IngestionRun,
    Meeting,
)


@admin.register(Authority)
class AuthorityAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "kind")
    search_fields = ("name", "slug")
    list_filter = ("kind",)


@admin.register(Body)
class BodyAdmin(admin.ModelAdmin):
    list_display = ("name", "authority", "slug", "source_url")
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
    inlines = [DocumentAssociationInline, DocumentVersionInline]

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


@admin.register(Chunk)
class ChunkAdmin(admin.ModelAdmin):
    list_display = ("document_version", "sequence", "agenda_item", "page_start", "page_end", "title")
    list_filter = ("document_version__document__doc_type",)
    search_fields = ("text", "title")
    raw_id_fields = ("document_version", "agenda_item")


@admin.register(DocumentAssociation)
class DocumentAssociationAdmin(admin.ModelAdmin):
    list_display = ("document", "meeting", "agenda_item", "source_context")
    raw_id_fields = ("document", "meeting", "agenda_item")


@admin.register(IngestionRun)
class IngestionRunAdmin(admin.ModelAdmin):
    list_display = (
        "started_at",
        "scope",
        "status",
        "meetings_seen",
        "documents_created",
        "versions_created",
        "chunks_created",
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
        "chunks_created",
        "failures",
    )
