from django.core.management.base import BaseCommand, CommandError

from core.models import DocumentVersion
from ingestion import pipeline
from .discover_meetings import _summary


class Command(BaseCommand):
    help = "Render stored document versions into IR markdown with xberg (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=None)
        parser.add_argument("--document-id", type=int, default=None)
        parser.add_argument("--version-id", type=int, nargs="+", default=None)
        parser.add_argument(
            "--status",
            type=str,
            default="pending",
            choices=["pending", "processed", "failed"],
            help="Process versions currently in this status ('processed' forces reprocessing).",
        )

    def handle(self, *args, **options):
        with pipeline.ingestion_lock() as acquired:
            if not acquired:
                raise CommandError("Another ingestion run is active (data/ingestion.lock).")
            run = pipeline.begin_run(f"process status={options['status']}")
            try:
                pipeline.process_versions(
                    run=run,
                    version_ids=options["version_id"],
                    document_id=options["document_id"],
                    statuses=[options["status"]],
                    limit=options["limit"],
                    force=options["status"] == "processed",
                )
            except Exception as exc:
                pipeline.finish_run(run, ok=False, error_summary=f"{type(exc).__name__}: {exc}")
                raise CommandError(f"Processing failed: {exc}")
            pipeline.finish_run(run)
        self.stdout.write(self.style.SUCCESS(_summary(run)))