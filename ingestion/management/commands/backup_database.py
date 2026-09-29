from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from ingestion import pipeline


class Command(BaseCommand):
    help = (
        "Back up the SQLite database using the SQLite backup API (WAL-safe). "
        "Optionally copy the original document store. The derived extraction "
        "cache (data/derived/) is included by default; skip it with --no-derived."
    )

    def add_arguments(self, parser):
        parser.add_argument("--dest", type=str, default=str(settings.BASE_DIR / "backups"))
        parser.add_argument("--originals", action="store_true", help="Also copy original PDFs.")
        parser.add_argument(
            "--no-derived", action="store_true", help="Skip the derived extraction cache."
        )

    def handle(self, *args, **options):
        result = pipeline.backup_database(
            Path(options["dest"]),
            include_originals=options["originals"],
            include_derived=not options["no_derived"],
        )
        self.stdout.write(self.style.SUCCESS(f"Database backup: {result['database']}"))
        if result["originals"]:
            self.stdout.write(self.style.SUCCESS(f"Originals backup: {result['originals']}"))
        if result["derived"]:
            self.stdout.write(self.style.SUCCESS(f"Derived cache backup: {result['derived']}"))