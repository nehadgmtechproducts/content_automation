"""Stage 2b — pull clean full text for the top source articles.

Chain: Firecrawl (the one paid key you hold) -> trafilatura (free, local) -> give up.
Every article that fails is simply dropped; synthesis needs >= 2 to establish consensus
and will refuse to run below that rather than write from a single outlet's framing.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

import httpx

from app.settings import get_settings, providers_config

log = logging.getLogger(__name__)

FIRECRAWL_SCRAPE = "https://api.firecrawl.dev/v1/scrape"
# Roughly 4k tokens of article body; anything longer is boilerplate or a liveblog.
MAX_CHARS = 16000


@dataclass
class ScrapeResult:
    url: str
    markdown: str
    title: str = ""
    byline: str = ""
    published_at: datetime | None = None
    scraper: str = ""

    @property
    def ok(self) -> bool:
        # Below ~600 chars it's a paywall stub or a cookie wall, not an article.
        return len(self.markdown.strip()) >= 600


def _parse_iso(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def scrape_firecrawl(url: str, timeout: float = 60.0) -> ScrapeResult | None:
    key = get_settings().firecrawl_api_key
    if not key:
        return None
    payload = {
        "url": url,
        "formats": ["markdown"],
        "onlyMainContent": True,
        "blockAds": True,
        "removeBase64Images": True,
    }
    try:
        resp = httpx.post(
            FIRECRAWL_SCRAPE,
            json=payload,
            headers={"Authorization": f"Bearer {key}"},
            timeout=timeout,
        )
        resp.raise_for_status()
        body = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("firecrawl failed for %s: %s", url, exc)
        return None

    data = body.get("data") or {}
    meta = data.get("metadata") or {}
    return ScrapeResult(
        url=url,
        markdown=(data.get("markdown") or "")[:MAX_CHARS],
        title=meta.get("title") or meta.get("ogTitle") or "",
        byline=meta.get("author") or "",
        published_at=_parse_iso(meta.get("publishedTime") or meta.get("modifiedTime")),
        scraper="firecrawl",
    )


def scrape_trafilatura(url: str, timeout: float = 30.0) -> ScrapeResult | None:
    try:
        import trafilatura
        from trafilatura.settings import use_config
    except ImportError:  # pragma: no cover
        return None

    try:
        resp = httpx.get(
            url,
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; content-engine/0.1)"},
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("trafilatura fetch failed for %s: %s", url, exc)
        return None

    cfg = use_config()
    cfg.set("DEFAULT", "EXTRACTION_TIMEOUT", "0")  # signal-based timeout breaks in workers
    text = trafilatura.extract(
        resp.text, output_format="markdown", include_comments=False,
        include_tables=True, favor_precision=True, config=cfg,
    )
    if not text:
        return None

    meta = trafilatura.extract_metadata(resp.text)
    return ScrapeResult(
        url=url,
        markdown=text[:MAX_CHARS],
        title=getattr(meta, "title", "") or "",
        byline=getattr(meta, "author", "") or "",
        published_at=_parse_iso(getattr(meta, "date", None)),
        scraper="trafilatura",
    )


_SCRAPERS = {"firecrawl": scrape_firecrawl, "trafilatura": scrape_trafilatura}


def scrape(url: str) -> ScrapeResult | None:
    """Run the configured scraper, falling back down the chain on failure.

    A Google News redirect link is first resolved to the publisher's real URL; the returned
    result carries that real URL so the source list links to the outlet, not to Google."""
    from app.scrape import gnews

    if gnews.is_google_news_url(url):
        real = gnews.resolve(url)
        if real is None:
            log.warning("could not resolve google news link %s", url[:80])
            return None
        log.info("google news link resolved to %s", real)
        url = real
    cfg = providers_config()["scrape"]
    order = [cfg.get("active", "firecrawl")]
    fallback = cfg.get("fallback")
    if fallback and fallback not in order:
        order.append(fallback)

    for name in order:
        fn = _SCRAPERS.get(name)
        if fn is None:
            continue
        result = fn(url)
        if result and result.ok:
            log.info("scraped %s via %s (%d chars)", url, name, len(result.markdown))
            return result
        if result:
            log.info("%s returned too little for %s (%d chars), trying next",
                     name, url, len(result.markdown.strip()))
    log.warning("all scrapers failed for %s", url)
    return None
