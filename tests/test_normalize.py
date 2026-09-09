from app.ingest.normalize import (
    canonicalize, domain_of, publisher_from_title, resolve_google_news,
    strip_tracking_params, url_hash,
)


def test_canonicalize_collapses_equivalent_urls():
    variants = [
        "https://www.techcrunch.com/2026/09/01/story/",
        "http://techcrunch.com/2026/09/01/story",
        "https://TechCrunch.com/2026/09/01/story?utm_source=twitter&utm_medium=social",
        "https://www.techcrunch.com/2026/09/01/story#section-2",
    ]
    canonical = {canonicalize(v) for v in variants}
    assert canonical == {"https://techcrunch.com/2026/09/01/story"}
    assert len({url_hash(canonicalize(v)) for v in variants}) == 1


def test_strip_tracking_keeps_meaningful_params():
    out = strip_tracking_params("https://x.com/a?id=42&utm_source=n&fbclid=zz&page=2")
    assert "id=42" in out and "page=2" in out
    assert "utm_source" not in out and "fbclid" not in out


def test_canonicalize_handles_junk():
    assert canonicalize("") == ""
    assert canonicalize("techcrunch.com/story") == "https://techcrunch.com/story"
    assert domain_of("https://www.theverge.com/x") == "theverge.com"


def test_publisher_from_title_splits_google_news_format():
    title, domain = publisher_from_title("Nvidia licenses NVLink - The Verge")
    assert title == "Nvidia licenses NVLink"
    assert domain == "theverge.com"


def test_publisher_from_title_handles_unknown_outlet():
    title, domain = publisher_from_title("Some scoop - Obscure Daily")
    assert title == "Some scoop"
    # Unknown outlets still get a stable pseudo-domain so distinct-domain counting works.
    assert domain == "obscure-daily.unknown"


def test_publisher_from_title_without_separator():
    assert publisher_from_title("No separator here") == ("No separator here", None)


def test_resolve_google_news_uses_source_element_for_attribution():
    url, domain, title = resolve_google_news(
        "https://news.google.com/rss/articles/CBMiOpaque?oc=5",
        "Nvidia licenses NVLink - Ars Technica",
        source_href="https://arstechnica.com/ai/2026/09/nvlink/",
    )
    assert domain == "arstechnica.com"
    assert url == "https://arstechnica.com/ai/2026/09/nvlink"
    assert title == "Nvidia licenses NVLink"


def test_google_news_source_root_is_attribution_only_not_an_article_link():
    """Regression: <source url> is the publisher's site ROOT. Treating it as the article
    link made the scraper fetch winbuzzer.com's homepage and file it as a source."""
    url, domain, _title = resolve_google_news(
        "https://news.google.com/rss/articles/CBMiOpaque?oc=5",
        "Publishers sue OpenAI - WinBuzzer",
        source_href="https://www.winbuzzer.com",
    )
    assert domain == "winbuzzer.com"          # attribution still correct
    assert "winbuzzer.com" not in url         # but we do not scrape the homepage
    assert url.startswith("https://news.google.com/")


def test_is_article_url_rejects_roots_and_section_indexes():
    from app.ingest.normalize import is_article_url

    assert not is_article_url("https://winbuzzer.com/")
    assert not is_article_url("https://theverge.com")
    assert not is_article_url("https://theverge.com/tech")
    assert is_article_url("https://techcrunch.com/2026/09/05/seattle-times-sue-openai")
    assert is_article_url("https://theverge.com/ai-artificial-intelligence/990932/lawsuit")


def test_resolve_google_news_falls_back_to_title_publisher():
    """Opaque modern ids can't be decoded — attribution must still be correct, or three
    outlets would collapse into one 'news.google.com' domain and nothing would fire."""
    _url, domain, title = resolve_google_news(
        "https://news.google.com/rss/articles/CBMiQQFodHRwOpaqueNotBase64Url?oc=5",
        "Nvidia licenses NVLink - TechCrunch",
    )
    assert domain == "techcrunch.com"
    assert title == "Nvidia licenses NVLink"
