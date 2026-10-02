from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0005_decision_documentversion_decision_parser"),
    ]

    operations = [
        migrations.CreateModel(
            name="Case",
            fields=[
                (
                    "id",
                    models.AutoField(auto_created=True, primary_key=True, serialize=False),
                ),
                ("dnr", models.CharField(max_length=100, unique=True)),
                ("title", models.CharField(blank=True, max_length=400)),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("motion", "Motion"),
                            ("interpellation", "Interpellation"),
                            ("fraga", "Fråga"),
                            ("arende", "Ärende"),
                            ("annat", "Annat"),
                        ],
                        default="arende",
                        max_length=20,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "ordering": ["dnr"],
            },
        ),
        migrations.CreateModel(
            name="CaseDocument",
            fields=[
                (
                    "id",
                    models.AutoField(auto_created=True, primary_key=True, serialize=False),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "case",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="case_documents",
                        to="core.case",
                    ),
                ),
                (
                    "document",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="case_links",
                        to="core.document",
                    ),
                ),
            ],
            options={
                "ordering": ["case_id", "id"],
            },
        ),
        migrations.AddConstraint(
            model_name="casedocument",
            constraint=models.UniqueConstraint(
                fields=("case", "document"), name="uniq_case_document"
            ),
        ),
    ]
