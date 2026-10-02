from django.core.management.base import BaseCommand

from ingestion import pipeline


class Command(BaseCommand):
    help = "Rebuild the FTS5 index from stored version markdown."

    def handle(self, *args, **options):
        result = pipeline.rebuild_search_index()
        self.stdout.write(
            self.style.SUCCESS(
                f"Rebuilt index: fts_rows={result['fts_rows']} indexed_versions={result['indexed_versions']}"
            )
        )
        if result["fts_rows"] != result["indexed_versions"]:
            self.stdout.write(self.style.WARNING("fts_rows != indexed_versions — investigate."))