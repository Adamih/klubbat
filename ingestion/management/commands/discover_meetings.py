import datetime

from django.core.management.base import BaseCommand, CommandError
from django.conf import settings

from ingestion import pipeline
from ingestion.adapters.uppsala import UppsalaAdapter


class Command(BaseCommand):
    help = (
        "Discover meetings and documents for a date range, optionally downloading new PDFs. "
        "Discovery is idempotent; unchanged sources create nothing new."
    )

    def add_arguments(self, parser):
        parser.add_argument("--year", type=int, help="Calendar year to cover (overrides start/end).")
        parser.add_argument("--start", type=str, help="Range start, YYYY-MM-DD.")
        parser.add_argument("--end", type=str, help="Range end, YYYY-MM-DD.")
        parser.add_argument("--no-download", action="store_true", help="Only parse pages, do not download PDFs.")
        parser.add_argument(
            "--refresh",
            action="store_true",
            help="Re-download documents whose bytes were already fetched, to detect corrections.",
        )
        parser.add_argument("--limit-meetings", type=int, default=None)
        parser.add_argument("--adapter", type=str, default="kommunfullmaktige")

    def handle(self, *args, **options):
        today = datetime.date.today()
        if options["year"]:
            start, end = datetime.date(options["year"], 1, 1), datetime.date(options["year"], 12, 31)
        elif options["start"] and options["end"]:
            start, end = _parse_date(options["start"]), _parse_date(options["end"])
        else:
            start = datetime.date(today.year, 1, 1)
            end = today
        adapter_cls = _adapter(options["adapter"])

        with pipeline.ingestion_lock() as acquired:
            if not acquired:
                raise CommandError("Another ingestion run is active (data/ingestion.lock).")
            run = pipeline.begin_run(f"{adapter_cls.slug} discover {start}..{end}{' refresh' if options['refresh'] else ''}")
            try:
                pipeline.run_discovery(
                    adapter_cls,
                    start,
                    end,
                    download=not options["no_download"],
                    refresh=options["refresh"],
                    run=run,
                    limit_meetings=options["limit_meetings"],
                )
            except Exception as exc:
                pipeline.finish_run(run, ok=False, error_summary=f"{type(exc).__name__}: {exc}")
                raise CommandError(f"Ingestion failed: {exc}")
            pipeline.finish_run(run)
        self.stdout.write(self.style.SUCCESS(_summary(run)))


def _parse_date(value: str) -> datetime.date:
    try:
        return datetime.date.fromisoformat(value)
    except ValueError as exc:
        raise CommandError(f"Invalid date {value!r}: {exc}")

def _adapter(slug: str):
    from ingestion.adapters.uppsala import get_adapter_class

    return get_adapter_class(slug)


def _summary(run) -> str:
    return (
        f"Run {run.pk} [{run.status}]: meetings={run.meetings_seen} docs seen={run.documents_seen} "
        f"created={run.documents_created} versions={run.versions_created} failures={run.failures}"
    )