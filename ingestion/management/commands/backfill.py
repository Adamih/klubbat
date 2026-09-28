import datetime

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from ingestion import pipeline
from .discover_meetings import _adapter, _summary


class Command(BaseCommand):
    help = (
        "Backfill one complete calendar year (default: settings.BACKFILL_YEAR): "
        "discover meetings, download documents, and extract text."
    )

    def add_arguments(self, parser):
        parser.add_argument("--year", type=int, default=settings.BACKFILL_YEAR)
        parser.add_argument("--limit-meetings", type=int, default=None)

    def handle(self, *args, **options):
        year = options["year"]
        start, end = datetime.date(year, 1, 1), datetime.date(year, 12, 31)
        adapter_cls = _adapter("kommunfullmaktige")

        with pipeline.ingestion_lock() as acquired:
            if not acquired:
                raise CommandError("Another ingestion run is active (data/ingestion.lock).")
            run = pipeline.begin_run(f"backfill {year}")
            try:
                pipeline.run_discovery(
                    adapter_cls, start, end, download=True, refresh=False, run=run,
                    limit_meetings=options["limit_meetings"],
                )
                pipeline.process_versions(run=run)
            except Exception as exc:
                pipeline.finish_run(run, ok=False, error_summary=f"{type(exc).__name__}: {exc}")
                raise CommandError(f"Backfill failed: {exc}")
            pipeline.finish_run(run)
        self.stdout.write(self.style.SUCCESS(_summary(run)))