from django.core.management.base import BaseCommand, CommandError

from ingestion import pipeline
from .discover_meetings import _summary


class Command(BaseCommand):
    help = (
        "Retry failed work: re-download versions whose bytes never arrived, "
        "and requeue failed extractions for processing."
    )

    def add_arguments(self, parser):
        parser.add_argument("--document-id", type=int, default=None)
        parser.add_argument("--no-process", action="store_true", help="Only requeue, do not process now.")

    def handle(self, *args, **options):
        with pipeline.ingestion_lock() as acquired:
            if not acquired:
                raise CommandError("Another ingestion run is active (data/ingestion.lock).")
            run = pipeline.begin_run("retry-failed")
            try:
                stats = pipeline.retry_failed(
                    run, document_id=options["document_id"], process=not options["no_process"]
                )
            except Exception as exc:
                pipeline.finish_run(run, ok=False, error_summary=f"{type(exc).__name__}: {exc}")
                raise CommandError(f"Retry failed: {exc}")
            pipeline.finish_run(run)
        self.stdout.write(self.style.SUCCESS(f"Run {run.pk} [{run.status}]: {stats} {_summary(run)}"))