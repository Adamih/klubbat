"""Views for the AI features: settings page and the document summary section."""

import pytest
from django.urls import reverse

pytestmark = pytest.mark.django_db


def test_settings_page_renders_form(client):
    res = client.get(reverse("website:settings"))
    assert res.status_code == 200
    content = res.content.decode()
    assert 'id="ai-settings-form"' in content
    assert 'id="ai-base-url"' in content
    assert 'id="ai-api-key"' in content
    assert 'id="ai-model"' in content


def test_base_links_settings_page(client):
    res = client.get(reverse("website:search"))
    assert 'href="/installningar/"' in res.content.decode()


def test_document_detail_has_summary_section_when_text_exists(client, stored_version):
    document, version = stored_version
    version.processing_status = version.Status.PROCESSED
    version.markdown = "# Sammanfattning av saken\n\nHandläggningen gäller detaljplanen."
    version.save(update_fields=["processing_status", "markdown"])
    res = client.get(reverse("website:document-detail", args=[document.pk]))
    assert res.status_code == 200
    content = res.content.decode()
    assert 'id="ai-summary-button"' in content
    assert 'data-doc-title' in content
    assert 'id="ai-summary-output"' in content


def test_document_detail_has_no_summary_section_without_text(client, stored_version):
    document, _ = stored_version
    res = client.get(reverse("website:document-detail", args=[document.pk]))
    assert res.status_code == 200
    assert 'id="ai-summary-button"' not in res.content.decode()