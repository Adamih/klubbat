"""Views for the AI features: settings page and the document summary section."""

import pytest
from django.urls import reverse
from selectolax.parser import HTMLParser

from core.models import DocumentPage

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


@pytest.mark.parametrize("with_pages", [False, True])
def test_document_detail_has_summary_section_when_text_exists(client, stored_version, with_pages):
    document, version = stored_version
    version.processing_status = version.Status.PROCESSED
    version.markdown = "# Sammanfattning av saken\n\nHandläggningen gäller detaljplanen."
    version.save(update_fields=["processing_status", "markdown"])
    if with_pages:
        # Insert out of order to check that the summary reads pages in PDF order.
        DocumentPage.objects.create(document_version=version, number=2, text="Beslutet gäller kollektivtrafiken.")
        DocumentPage.objects.create(document_version=version, number=1, text=version.markdown)
    res = client.get(reverse("website:document-detail", args=[document.pk]))
    assert res.status_code == 200
    content = res.content.decode()
    assert 'id="ai-summary-button"' in content
    assert 'data-doc-title' in content
    assert 'id="ai-summary-output"' in content
    html = HTMLParser(content)
    document_text = html.css_first(".doc-text")
    assert document_text is not None
    text = document_text.text(separator=" ", strip=True)
    assert "Handläggningen gäller detaljplanen." in text
    assert "Originalkälla" not in text
    if with_pages:
        assert [page.attributes["id"] for page in document_text.css(".doc-page")] == ["page-1", "page-2"]
        assert text.index("Handläggningen") < text.index("Beslutet")
        assert "Beslutet gäller kollektivtrafiken." in text


def test_document_detail_has_no_summary_section_without_text(client, stored_version):
    document, _ = stored_version
    res = client.get(reverse("website:document-detail", args=[document.pk]))
    assert res.status_code == 200
    assert 'id="ai-summary-button"' not in res.content.decode()
