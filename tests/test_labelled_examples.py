"""Migration and admin workflow for independent labelled examples."""

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.urls import reverse

from core.models import Decision, LabelledExample, Tag, Topic


@pytest.mark.django_db(transaction=True)
def test_migration_preserves_manual_examples_and_removes_overrides(stored_version):
    document, version = stored_version
    version.markdown = "Texten handlar om ett bibliotek."
    version.save(update_fields=["markdown"])
    old = [("core", "0010_decision_page_vote_page_documentpage")]
    new = [("core", "0011_labelled_examples")]
    executor = MigrationExecutor(connection)
    executor.migrate(old)
    try:
        apps = executor.loader.project_state(old).apps
        LegacyTag = apps.get_model("core", "Tag")
        LegacyDecision = apps.get_model("core", "Decision")
        LegacyTopic = apps.get_model("core", "Topic")
        topic = LegacyTopic.objects.create(slug="kultur", label="Kultur och fritid")
        decision = LegacyDecision.objects.create(
            document_version_id=version.pk, sequence=1,
            printed_agenda_title="Nytt bibliotek", decision_text="Kommunfullmäktige beslutar att öppna biblioteket.",
            parser_version="test",
        )
        LegacyTag.objects.create(document_id=document.pk, topic=topic, is_manual=True, classifier_version="manual")
        LegacyTag.objects.create(decision=decision, topic=topic, is_manual=True, classifier_version="manual")
        generated = LegacyTag.objects.create(
            document_id=document.pk, topic=LegacyTopic.objects.create(slug="skola", label="Skola"),
            is_manual=False, classifier_version="topic-centroid-v2",
        )
        executor = MigrationExecutor(connection)
        executor.migrate(new)
        examples = list(LabelledExample.objects.order_by("pk"))
        assert len(examples) == 2
        assert {example.topic_id for example in examples} == {topic.pk}
        by_title = {example.title: example for example in examples}
        assert by_title[document.title].text == version.markdown
        assert by_title[document.title].source_url == document.primary_source_url
        assert by_title["Nytt bibliotek"].text == decision.decision_text
        assert by_title["Nytt bibliotek"].source_url == version.original_url
        assert list(Tag.objects.values_list("pk", flat=True)) == [generated.pk]
        # Neither source deletion nor reparsing can cascade into example data.
        document.delete()
        assert LabelledExample.objects.count() == 2
    finally:
        MigrationExecutor(connection).migrate(new)


@pytest.mark.django_db
def test_admin_copies_tags_to_editable_examples(admin_client, stored_version):
    document, version = stored_version
    version.markdown = "Kulturskolan behöver nya lokaler."
    version.save(update_fields=["markdown"])
    topic = Topic.objects.create(slug="kultur", label="Kultur och fritid")
    decision = Decision.objects.create(
        document_version=version, sequence=1, printed_agenda_title="Kulturskolan",
        decision_text="Nya lokaler ska ordnas.", parser_version="test",
    )
    tags = [Tag.objects.create(document=document, topic=topic), Tag.objects.create(decision=decision, topic=topic)]
    payload = {"action": "save_labelled_examples", "_selected_action": [tag.pk for tag in tags]}
    url = reverse("admin:core_tag_changelist")
    assert admin_client.post(url, payload).status_code == 302
    assert LabelledExample.objects.count() == 2
    assert admin_client.post(url, payload).status_code == 302
    assert LabelledExample.objects.count() == 2
    example = LabelledExample.objects.get(title=document.title)
    assert example.text == version.markdown
    assert example.source_url == document.primary_source_url
    other = Topic.objects.create(slug="skola", label="Skola")
    response = admin_client.post(reverse("admin:core_labelledexample_change", args=[example.pk]), {
        "title": example.title, "text": example.text, "topic": other.pk, "source_url": example.source_url,
    })
    assert response.status_code == 302
    example.refresh_from_db()
    assert example.topic == other
    assert Tag.objects.get(document=document).topic == topic
    # Generated classifications cannot be turned into overrides in admin.
    assert admin_client.get(reverse("admin:core_tag_add")).status_code == 403
    assert admin_client.post(reverse("admin:core_tag_change", args=[tags[0].pk]), {"topic": other.pk}).status_code == 403


@pytest.mark.django_db
def test_copying_examples_requires_example_add_permission(client, django_user_model, stored_version):
    from django.contrib.auth.models import Permission

    document, _ = stored_version
    topic = Topic.objects.create(slug="kultur", label="Kultur och fritid")
    tag = Tag.objects.create(document=document, topic=topic)
    user = django_user_model.objects.create_user(username="viewer", is_staff=True)
    user.user_permissions.add(Permission.objects.get(codename="view_tag"))
    client.force_login(user)
    response = client.get(reverse("admin:core_tag_changelist"))
    assert response.status_code == 200
    assert "save_labelled_examples" not in response.content.decode()
    client.post(reverse("admin:core_tag_changelist"), {
        "action": "save_labelled_examples", "_selected_action": [tag.pk],
    })
    assert not LabelledExample.objects.exists()
