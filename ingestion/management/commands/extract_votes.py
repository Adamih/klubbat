from django.core.management.base import BaseCommand, CommandError

from ingestion import pipeline
from .discover_meetings import _summary


class Command(BaseCommand):
    help = (
        "Parse recorded votes (röstningar) from extracted chunk text for processed "
        "versions. Cheap stage: re-runnable, skips versions already stamped with the "
        "current parser version unless --force."
    )

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=None)
        parser.add_argument("--document-id", type=int, default=None)
        parser.add_argument("--version-id", type=int, nargs="+", default=None)
        parser.add_argument(
            "--force",
            action="store_true",
            help="Re-extract versions already stamped with the current parser version.",
        )

    def handle(self, *args, **options):
        with pipeline.ingestion_lock() as acquired:
            if not acquired:
                raise CommandError("Another ingestion run is active (data/ingestion.lock).")
            run = pipeline.begin_run("extract votes")
            try:
                stats = pipeline.extract_votes(
                    run=run,
                    version_ids=options["version_id"],
                    document_id=options["document_id"],
                    limit=options["limit"],
                    force=options["force"],
                )
            except Exception as exc:
                pipeline.finish_run(run, ok=False, error_summary=f"{type(exc).__name__}: {exc}")
                raise CommandError(f"Vote extraction failed: {exc}")
            pipeline.finish_run(run)
        self.stdout.write(self.style.SUCCESS(_summary(run)))
        self.stdout.write(
            self.style.SUCCESS(
                "Röstningar: {versions} versioner, {votes} omröstningar, "
                "{records} namngivna röster, {failures} fel.".format(**stats)
            )
        )