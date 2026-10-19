"""Google News link resolution — against fake pages; no network.

The real endpoint was checked once by hand (two live links resolved to cnn.com and yahoo.com);
these tests pin the request shape and the "never crash, fall back" behaviour."""
import httpx

from app.scrape import client as scrape_client
from app.scrape import gnews

LINK = "https://news.google.com/rss/articles/CBMiABC123"
PAGE = '<c-wiz><div data-n-a-sg="SIG123" data-n-a-ts="1700000000"></div></c-wiz>'
# batchexecute wraps the answer as a JSON string inside JSON, so its quotes arrive backslash-escaped.
ANSWER = 'wrb.fr garturlres\\",\\"https://www.cnn.com/2026/10/19/story\\",1'


def fake(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def good(request):
    if request.url.path.startswith("/articles/"):
        return httpx.Response(200, text=PAGE)
    body = request.content.decode()
    assert "SIG123" in body and "1700000000" in body and "CBMiABC123" in body and "Fbv4je" in body
    return httpx.Response(200, text=ANSWER)


def test_recognises_only_google_news_article_links():
    assert gnews.is_google_news_url(LINK)
    assert not gnews.is_google_news_url("https://www.cnn.com/2026/10/19/story")
    assert not gnews.is_google_news_url("https://news.google.com/topics/abc")


def test_resolves_to_the_publisher_url():
    assert gnews.resolve(LINK, client=fake(good)) == "https://www.cnn.com/2026/10/19/story"


def test_a_page_without_a_signature_returns_none():
    assert gnews.resolve(LINK, client=fake(lambda r: httpx.Response(200, text="<html></html>"))) is None


def test_http_errors_return_none_instead_of_raising():
    assert gnews.resolve(LINK, client=fake(lambda r: httpx.Response(429))) is None


def test_a_non_google_url_is_left_alone():
    assert gnews.resolve("https://www.cnn.com/a") is None


def test_scrape_uses_the_resolved_url_and_gives_up_cleanly_when_it_cannot(monkeypatch):
    seen = []
    monkeypatch.setattr(scrape_client, "providers_config", lambda: {"scrape": {"active": "trafilatura"}})
    monkeypatch.setitem(scrape_client._SCRAPERS, "trafilatura", lambda u: seen.append(u) or scrape_client.ScrapeResult(
        url=u, markdown="x" * 700, scraper="trafilatura"))
    monkeypatch.setattr(gnews, "resolve", lambda u: "https://www.cnn.com/real")
    assert scrape_client.scrape(LINK).url == "https://www.cnn.com/real" and seen == ["https://www.cnn.com/real"]

    monkeypatch.setattr(gnews, "resolve", lambda u: None)
    assert scrape_client.scrape(LINK) is None and len(seen) == 1       # never scrapes the redirect page
