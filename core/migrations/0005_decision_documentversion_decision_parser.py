import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0004_vote_vote_type_vote_reservation_body_seat"),
    ]

    operations = [
        migrations.AddField(
            model_name="documentversion",
            name="decision_parser_version",
            field=models.CharField(blank=True, max_length=100, null=True),
        ),
        migrations.CreateModel(
            name="Decision",
            fields=[
                (
                    "id",
                    models.AutoField(auto_created=True, primary_key=True, serialize=False),
                ),
                ("sequence", models.IntegerField()),
                ("printed_agenda_number", models.IntegerField(blank=True, null=True)),
                ("printed_agenda_title", models.CharField(blank=True, max_length=300)),
                ("printed_dnr", models.CharField(blank=True, max_length=100)),
                ("decision_text", models.TextField()),
                (
                    "outcome",
                    models.CharField(
                        choices=[
                            ("bifall", "Bifall"),
                            ("avslag", "Avslag"),
                            ("avvisning", "Avvisning"),
                            ("ateremittera", "Återremittering"),
                            ("bordlaggning", "Bordläggning"),
                            ("val", "Val/förordnande"),
                            ("procedur", "Procedurellt"),
                            ("annat", "Annat"),
                        ],
                        default="annat",
                        max_length=20,
                    ),
                ),
                ("parser_version", models.CharField(max_length=100)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "agenda_item",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="decisions",
                        to="core.agendaitem",
                    ),
                ),
                (
                    "chunk",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="decisions",
                        to="core.chunk",
                    ),
                ),
                (
                    "document_version",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="decisions",
                        to="core.documentversion",
                    ),
                ),
            ],
            options={
                "ordering": ["document_version_id", "sequence"],
            },
        ),
        migrations.AddConstraint(
            model_name="decision",
            constraint=models.UniqueConstraint(
                fields=("document_version", "sequence"), name="uniq_decision_version_seq"
            ),
        ),
    ]
