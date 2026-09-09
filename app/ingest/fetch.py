"""Stage 1 — poll every configured source and persist normalized RawItems."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import feedparser
import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db import session_scope
from app.ingest.normalize import canonicalize, domain_of, resolve_google_news, url_hash
from app.models import RawItem
from app.settings import feeds_config

log = logging.getLogger(__name__)

GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"
HN_SEARCH = "https://hn.algolia.com/api/v1/search_by_date"


@dataclass
class FetchedItem:
    source_id: str
    source_tier: int
    url: str
    title: str
    summary: str = ""
    published_at: datetime | None = None
    source_href: str | None = None


@dataclass
class SourceResult:
    source_id: str
    ok: bool
    count: int = 0
    error: str = ""


@dataclass
class IngestReport:
    inserted: int = 0
    skipped_duplicates: int = 0
    sources: list[SourceResult] = field(default_factory=list)

    @property
    def failed_sources(self) -> list[str]:
        return [s.source_id for s in self.sources if not s.ok]


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime(*value[:6], tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _entry_summary(entry: Any) -> str:
    raw = entry.get("summary") or ""
    # Feeds embed markup in summaries; strip it crudely — this text is only used for
    # NER and embedding, never rendered.
    import re

    return re.sub(r"<[^>]+>", " ", raw).strip()[:1000]


class _HostLimiter:
    """Serializes requests per host and spaces them out.

    Sources are polled concurrently, which means four subreddits hit reddit.com at the
    same instant — that is what turns a working feed into a 429. This keeps the
    concurrency across hosts while staying polite to each one individually.
    """

    def __init__(self, delay_seconds: float = 1.0, overrides: dict[str, float] | None = None):
        self.delay = delay_seconds
        # Some hosts need far more room than the global default — reddit.com rate-limits
        # unauthenticated RSS aggressively, and four subreddits share that one host.
        self.overrides = overrides or {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._last: dict[str, float] = {}

    def _delay_for(self, host: str) -> float:
        for suffix, value in self.overrides.items():
            if host == suffix or host.endswith("." + suffix):
                return float(value)
        return self.delay

    def _lock(self, host: str) -> asyncio.Lock:
        if host not in self._locks:
            self._locks[host] = asyncio.Lock()
        return self._locks[host]

    async def wait(self, host: str) -> None:
        async with self._lock(host):
            previous = self._last.get(host)
            if previous is not None:
                delay = self._delay_for(host)
                elapsed = asyncio.get_event_loop().time() - previous
                if elapsed < delay:
                    await asyncio.sleep(delay - elapsed)
            self._last[host] = asyncio.get_event_loop().time()


async def _get(client: httpx.AsyncClient, url: str, limiter: "_HostLimiter | None" = None,
               retries: int = 2, **kwargs) -> httpx.Response:
    from urllib.parse import urlparse

    host = urlparse(url).hostname or ""
    for attempt in range(retries + 1):
        if limiter is not None:
            await limiter.wait(host)
        resp = await client.get(url, **kwargs)
        # 429 is the common failure when several feeds share a host; back off and retry
        # rather than dropping the source for the whole poll.
        if resp.status_code == 429 and attempt < retries:
            retry_after = resp.headers.get("retry-after")
            try:
                delay = float(retry_after) if retry_after else 0.0
            except ValueError:
                delay = 0.0
            await asyncio.sleep(max(delay, 2.0 * (attempt + 1)))
            continue
        resp.raise_for_status()
        return resp
    resp.raise_for_status()
    return resp


async def _fetch_rss(client: httpx.AsyncClient, src: dict, limiter=None) -> list[FetchedItem]:
    resp = await _get(client, src["url"], limiter=limiter)
    parsed = feedparser.parse(resp.text)
    out = []
    for entry in parsed.entries:
        link = entry.get("link")
        if not link:
            continue
        out.append(
            FetchedItem(
                source_id=src["id"],
                source_tier=src["tier"],
                url=link,
                title=entry.get("title", "").strip(),
                summary=_entry_summary(entry),
                published_at=_parse_time(entry.get("published_parsed") or entry.get("updated_parsed")),
            )
        )
    return out


async def _fetch_google_news(client: httpx.AsyncClient, src: dict, limiter=None) -> list[FetchedItem]:
    params = {"q": src["query"], "hl": "en-US", "gl": "US", "ceid": "US:en"}
    resp = await _get(client, GOOGLE_NEWS_RSS, params=params, limiter=limiter)
    parsed = feedparser.parse(resp.text)
    out = []
    for entry in parsed.entries:
        link = entry.get("link")
        if not link:
            continue
        source_href = None
        src_el = entry.get("source")
        if isinstance(src_el, dict):
            source_href = src_el.get("href")
        out.append(
            FetchedItem(
                source_id=src["id"],
                source_tier=src["tier"],
                url=link,
                title=entry.get("title", "").strip(),
                summary=_entry_summary(entry),
                published_at=_parse_time(entry.get("published_parsed")),
                source_href=source_href,
            )
        )
    return out


async def _fetch_hackernews(client: httpx.AsyncClient, src: dict, limiter=None) -> list[FetchedItem]:
    params = {
        "tags": "story",
        "numericFilters": f"points>{src.get('min_points', 100)}",
        "hitsPerPage": 50,
    }
    resp = await _get(client, HN_SEARCH, params=params, limiter=limiter)
    out = []
    for hit in resp.json().get("hits", []):
        link = hit.get("url")
        if not link:
            continue  # Ask HN / text posts carry no external story
        created = hit.get("created_at")
        published = None
        if created:
            try:
                published = datetime.fromisoformat(created.replace("Z", "+00:00"))
            except ValueError:
                pass
        out.append(
            FetchedItem(
                source_id=src["id"],
                source_tier=src["tier"],
                url=link,
                title=(hit.get("title") or "").strip(),
                published_at=published,
            )
        )
    return out


async def _fetch_reddit(client: httpx.AsyncClient, src: dict, limiter=None) -> list[FetchedItem]:
    """Reddit via RSS.

    The .json endpoint now returns 403 to non-browser clients, and old.reddit.com serves
    an HTML interstitial, so RSS is the only endpoint that answers a polite bot UA.

    Two consequences, both acceptable:
      * RSS carries no score, so `min_score` filtering isn't possible — `limit` caps the
        volume instead.
      * Item links are reddit permalinks (external URLs in the body are wrapped in
        redirect proxies), so each item is attributed to reddit.com rather than to a
        publisher. That is the honest reading anyway: Reddit's contribution to a cluster
        is "the community is discussing this" — one corroborating tier-3 signal, not a
        newsroom. It can never fire a topic alone, because `require_tier1` is on.
    """
    url = f"https://www.reddit.com/r/{src['subreddit']}/hot/.rss"
    resp = await _get(client, url, params={"limit": src.get("limit", 25)}, limiter=limiter)
    parsed = feedparser.parse(resp.text)
    out = []
    for entry in parsed.entries:
        link = entry.get("link")
        title = (entry.get("title") or "").strip()
        if not link or not title:
            continue
        out.append(
            FetchedItem(
                source_id=src["id"],
                source_tier=src["tier"],
                url=link,
                title=title,
                published_at=_parse_time(entry.get("published_parsed") or entry.get("updated_parsed")),
            )
        )
    return out


_FETCHERS = {
    "rss": _fetch_rss,
    "google_news": _fetch_google_news,
    "hackernews": _fetch_hackernews,
    "reddit": _fetch_reddit,
}


def _to_row(item: FetchedItem) -> dict | None:
    """Normalize one fetched item into a RawItem row dict."""
    title = item.title
    if item.source_id.startswith("gnews") or domain_of(item.url) == "news.google.com":
        resolved, domain, title = resolve_google_news(item.url, item.title, item.source_href)
    else:
        resolved = canonicalize(item.url)
        domain = domain_of(resolved)

    if not resolved or not title or not domain:
        return None

    return {
        "source_id": item.source_id,
        "source_tier": item.source_tier,
        "url": item.url,
        "canonical_url": resolved,
        "url_hash": url_hash(resolved),
        "domain": domain,
        "title": title,
        "summary": item.summary,
        "published_at": item.published_at,
    }


async def _poll_all() -> tuple[list[dict], list[SourceResult]]:
    cfg = feeds_config()
    defaults = cfg.get("defaults", {})
    headers = {"User-Agent": defaults.get("user_agent", "content-engine/0.1")}
    timeout = defaults.get("timeout_seconds", 20)

    results: list[SourceResult] = []
    rows: list[dict] = []
    limiter = _HostLimiter(
        defaults.get("per_host_delay_seconds", 1.5),
        defaults.get("host_delay_overrides", {}),
    )

    async with httpx.AsyncClient(
        headers=headers, timeout=timeout, follow_redirects=True
    ) as client:

        async def run(src: dict) -> None:
            fetcher = _FETCHERS.get(src["type"])
            if fetcher is None:
                results.append(SourceResult(src["id"], False, error=f"unknown type {src['type']}"))
                return
            try:
                items = await fetcher(client, src, limiter)
            except Exception as exc:  # one bad feed must not stop the poll
                log.warning("source %s failed: %s", src["id"], exc)
                results.append(SourceResult(src["id"], False, error=str(exc)[:200]))
                return
            converted = [r for r in (_to_row(i) for i in items) if r]
            rows.extend(converted)
            results.append(SourceResult(src["id"], True, count=len(converted)))

        await asyncio.gather(*(run(s) for s in cfg["sources"]))

    return rows, results


def ingest_once() -> IngestReport:
    """Poll every source and insert new items. Safe to run concurrently."""
    rows, results = asyncio.run(_poll_all())

    # Deduplicate within this batch first — the same story arrives from several feeds.
    seen: set[str] = set()
    unique = []
    for row in rows:
        if row["url_hash"] in seen:
            continue
        seen.add(row["url_hash"])
        unique.append(row)

    inserted = 0
    if unique:
        with session_scope() as s:
            # ON CONFLICT DO NOTHING against url_hash handles cross-batch duplicates
            # without a read-then-write race between concurrent workers.
            # RETURNING, not .rowcount: a bulk ON CONFLICT insert reports -1 there.
            stmt = (
                pg_insert(RawItem)
                .values(unique)
                .on_conflict_do_nothing(index_elements=["url_hash"])
                .returning(RawItem.id)
            )
            inserted = len(s.execute(stmt).fetchall())

    return IngestReport(
        inserted=inserted,
        skipped_duplicates=len(unique) - inserted,
        sources=sorted(results, key=lambda r: r.source_id),
    )


def unprocessed_items(limit: int = 2000) -> list[RawItem]:
    with session_scope() as s:
        return list(
            s.scalars(
                select(RawItem).where(RawItem.processed.is_(False)).order_by(RawItem.fetched_at).limit(limit)
            )
        )
