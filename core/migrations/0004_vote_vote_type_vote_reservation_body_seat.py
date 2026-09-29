import django.db.models.deletion
from django.db import migrations, models


def set_uppsala_seat_count(apps, schema_editor):
    """Uppsala kommunfullmäktige has 81 seats; known constant for the seeded source."""
    Body = apps.get_model("core", "Body")
    Body.objects.filter(
        authority__slug="uppsala-kommun", slug="kommunfullmaktige"
    ).update(seat_count=81)


def unset_seat_count(apps, schema_editor):
    Body = apps.get_model("core", "Body")
    Body.objects.filter(
        authority__slug="uppsala-kommun", slug="kommunfullmaktige"
    ).update(seat_count=None)


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0003_documentversion_vote_parser_version_vote_voterecord_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="body",
            name="seat_count",
            field=models.IntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="vote",
            name="agenda_title",
            field=models.CharField(blank=True, max_length=300),
        ),
        migrations.AddField(
            model_name="vote",
            name="decision_text",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="vote",
            name="vote_type",
            field=models.CharField(
                choices=[
                    ("bifall", "Bifall"),
                    ("avslag", "Avslag"),
                    ("avvisning", "Avvisning"),
                    ("ateremittera", "Återremittering"),
                    ("bordlaggning", "Bordläggning"),
                    ("val", "Val/förordnande"),
                    ("procedur", "Procedurell röstning"),
                    ("annat", "Annat"),
                ],
                default="annat",
                max_length=20,
            ),
        ),
        migrations.CreateModel(
            name="VoteReservation",
            fields=[
                (
                    "id",
                    models.AutoField(auto_created=True, primary_key=True, serialize=False),
                ),
                ("position", models.IntegerField()),
                (
                    "kind",
                    models.CharField(
                        choices=[("reservation", "Reservation"), ("yttrande", "Särskilt yttrande")],
                        max_length=12,
                    ),
                ),
                ("party_codes", models.CharField(blank=True, max_length=100)),
                ("member_name", models.CharField(blank=True, max_length=200)),
                ("text", models.TextField()),
                (
                    "vote",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="reservations",
                        to="core.vote",
                    ),
                ),
            ],
            options={
                "ordering": ["vote_id", "position"],
            },
        ),
        migrations.RunPython(set_uppsala_seat_count, unset_seat_count),
    ]
