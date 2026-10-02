from django.core.management.base import BaseCommand, CommandError

from ingestion import pipeline


class Command(BaseCommand):
    help = (
        "Build case pages: link documents to cases by diarienummer and pair "
        "initiative documents (interpellation/fråga) with their answer's dnr "
        "via titles. Re-runnable; rebuilds all document links."
    )

    def handle(self, *args, **options):
        with pipeline.ingestion_lock() as acquired:
            if not acquired:
                raise CommandError("Another ingestion run is active (data/ingestion.lock).")
            try:
                stats = pipeline.build_cases()
            except Exception as exc:
                raise CommandError(f"Case building failed: {exc}")
        self.stdout.write(
            self.style.SUCCESS(
                "Ärenden: {cases} ärenden, {links} dokumentlänkar, {paired} parade via titel.".format(**stats)
            )
        )
