from django.core.management.base import BaseCommand

from ingestion import pipeline


class Command(BaseCommand):
    help = "Rebuild the FTS5 index from stored chunks."

    def handle(self, *args, **options):
        result = pipeline.rebuild_search_index()
        self.stdout.write(
            self.style.SUCCESS(
                f"Rebuilt index: fts_rows={result['fts_rows']} chunk_rows={result['chunk_rows']}"
            )
        )
        if result["fts_rows"] != result["chunk_rows"]:
            self.stdout.write(self.style.WARNING("fts_rows != chunk_rows — investigate."))