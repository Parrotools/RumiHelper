import pytest
import trafilatura

from rumi_helper.services.web.extractor import (
    ExtractionError,
    _bs4_extract,
    extract,
)


def test_bs4_extract_filters_non_content_and_hidden_elements():
    html = """
    <html>
      <head><title>Visible title</title><style>.x { color: red; }</style></head>
      <body>
        <header>header secret</header>
        <nav>nav secret</nav>
        <main>
          <p>Visible article paragraph with enough useful content for extraction.</p>
          <script>script secret</script>
          <div aria-hidden="true">aria secret</div>
          <div style="display: none">display secret</div>
          <div style="visibility:hidden">visibility secret</div>
          <div class="sidebar">sidebar secret</div>
          <div class="ad">advert secret</div>
          <p>Second visible paragraph that should remain in the extracted body.</p>
        </main>
        <footer>footer secret</footer>
      </body>
    </html>
    """

    title, text = _bs4_extract(html)

    assert title == "Visible title"
    assert "Visible article paragraph" in text
    assert "Second visible paragraph" in text
    for secret in (
        "header secret",
        "nav secret",
        "script secret",
        "aria secret",
        "display secret",
        "visibility secret",
        "sidebar secret",
        "advert secret",
        "footer secret",
    ):
        assert secret not in text


def test_extract_uses_bs4_body_when_trafilatura_has_no_valid_text(monkeypatch):
    monkeypatch.setattr(trafilatura, "extract", lambda *args, **kwargs: "")
    monkeypatch.setattr(trafilatura, "extract_metadata", lambda *args, **kwargs: None)
    html = """
    <html>
      <head><title>Fallback title</title></head>
      <body><main>
        This fallback body is deliberately long enough to pass the minimum
        extraction threshold and should be returned by BeautifulSoup.
      </main></body>
    </html>
    """

    doc = extract(html, "https://example.com/article")

    assert doc.title == "Fallback title"
    assert "fallback body" in doc.text
    assert doc.url == "https://example.com/article"


def test_metadata_failure_falls_back_to_bs4_title_without_replacing_body(monkeypatch):
    primary_text = "primary trafilatura body " * 10
    monkeypatch.setattr(
        trafilatura,
        "extract",
        lambda *args, **kwargs: primary_text,
    )

    def fail_metadata(*args, **kwargs):
        raise RuntimeError("metadata failure")

    monkeypatch.setattr(trafilatura, "extract_metadata", fail_metadata)
    html = """
    <html>
      <head><title>Recovered title</title></head>
      <body><main>
        This BeautifulSoup body is intentionally different and much longer than
        the primary extraction so replacing the successful trafilatura body would
        be observable in the assertion below.
      </main></body>
    </html>
    """

    doc = extract(html, "https://example.com/article")

    assert doc.title == "Recovered title"
    assert doc.text == primary_text.strip()
    assert "BeautifulSoup body" not in doc.text


def test_extract_rejects_page_without_enough_visible_text(monkeypatch):
    monkeypatch.setattr(trafilatura, "extract", lambda *args, **kwargs: "")
    monkeypatch.setattr(trafilatura, "extract_metadata", lambda *args, **kwargs: None)

    with pytest.raises(ExtractionError):
        extract(
            "<html><head><title>Short</title></head><body>tiny</body></html>",
            "https://example.com/short",
        )
