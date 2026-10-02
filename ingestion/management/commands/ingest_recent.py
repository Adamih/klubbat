import datetime

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from ingestion import pipeline
from .discover_meetings import _adapter, _summary


class Command(BaseCommand):
    help = (
        "Ongoing ingestion for the scheduler: revisit meetings from the last --days, "
        "re-download their documents to catch late publications and corrections, then extract."
    )

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=settings.RECENT_WINDOW_DAYS)
        parser.add_argument("--adapter", type=str, default="kommunfullmaktige")

    def handle(self, *args, **options):
        end = datetime.date.today()
        start = end - datetime.timedelta(days=options["days"])
        adapter_cls = _adapter(options["adapter"])

        with pipeline.ingestion_lock() as acquired:
            if not acquired:
                raise CommandError("Another ingestion run is active (data/ingestion.lock).")
            run = pipeline.begin_run(f"recent {start}..{end} refresh")
            try:
                pipeline.run_discovery(adapter_cls, start, end, download=True, refresh=True, run=run)
                pipeline.process_versions(run=run)
                pipeline.extract_votes(run=run)
                pipeline.extract_decisions(run=run)
                pipeline.build_cases()
            except Exception as exc:
                pipeline.finish_run(run, ok=False, error_summary=f"{type(exc).__name__}: {exc}")
                raise CommandError(f"Ingestion failed: {exc}")
            pipeline.finish_run(run)
        self.stdout.write(self.style.SUCCESS(_summary(run)))