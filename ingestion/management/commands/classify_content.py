from django.core.management.base import BaseCommand, CommandError

from ingestion import pipeline
from .discover_meetings import _summary


class Command(BaseCommand):
    help = (
        "Tag decisions and initiative documents with policy-area topics "
        "(nearest-centroid over Swedish sentence embeddings). Labelled examples "
        "in admin guide the classifier; generated tags can all be replaced."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Re-classify targets already tagged with the current classifier version.",
        )

    def handle(self, *args, **options):
        with pipeline.ingestion_lock() as acquired:
            if not acquired:
                raise CommandError("Another ingestion run is active (data/ingestion.lock).")
            run = pipeline.begin_run("classify content")
            try:
                stats = pipeline.classify_content(run=run, force=options["force"])
            except Exception as exc:
                pipeline.finish_run(run, ok=False, error_summary=f"{type(exc).__name__}: {exc}")
                raise CommandError(f"Classification failed: {exc}")
            pipeline.finish_run(run)
        self.stdout.write(self.style.SUCCESS(_summary(run)))
        self.stdout.write(
            self.style.SUCCESS(
                "Ämnestaggar: {topics} ämnen, {tagged} taggade, "
                "{skipped} redan aktuella, {failures} fel.".format(**stats)
            )
        )
