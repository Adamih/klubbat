from django.core.management.base import BaseCommand

from ingestion import pipeline
from ingestion.adapters.uppsala import UppsalaAdapter


class Command(BaseCommand):
    help = "Create the Authority/Body rows for configured sources (idempotent)."

    def handle(self, *args, **options):
        body = pipeline.ensure_source(UppsalaAdapter)
        self.stdout.write(self.style.SUCCESS(f"Source ready: {body} (id={body.pk})"))