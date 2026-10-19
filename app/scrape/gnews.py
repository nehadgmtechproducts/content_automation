"""Resolve a Google News redirect link to the publisher's real article URL.

Items that arrive through the Google News feeds carry `news.google.com/rss/articles/<id>` links.
Those are not pages a scraper can read (they are a JavaScript redirect), so every such source
failed to scrape and stories built on them fell below the two-source minimum.

Google exposes the target through the same endpoint its own page uses: fetch the article page,
read the signature and timestamp it embeds, then ask `batchexecute` (rpc `Fbv4je`) for the URL.
This is an undocumented interface, so every failure returns None and the caller simply falls back
to the original link — a Google change makes sources drop out, it never crashes the pipeline.
"""
from __future__ import annotations

import json
import logging
import re
import urllib.parse
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)

BATCH_URL = "https://news.google.com/_/DotsSplashUi/data/batchexecute"
_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
_SG = re.compile(r'data-n-a-sg="([^"]+)"')
_TS = re.compile(r'data-n-a-ts="([^"]+)"')
_RESULT = re.compile(r'garturlres\\",\\"(https?:[^"\\]+)')


def is_google_news_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.netloc == "news.google.com" and "/articles/" in parsed.path


def resolve(url: str, client: httpx.Client | None = None, timeout: float = 30.0) -> str | None:
    """The publisher URL behind a Google News link, or None if it cannot be determined."""
    if not is_google_news_url(url):
        return None
    article_id = urlparse(url).path.rstrip("/").split("/")[-1]
    own = client is None
    client = client or httpx.Client(headers=_HEADERS, follow_redirects=True, timeout=timeout)
    try:
        page = client.get(f"https://news.google.com/articles/{article_id}")
        page.raise_for_status()
        sg, ts = _SG.search(page.text), _TS.search(page.text)
        if not (sg and ts):
            log.warning("google news page for %s has no signature; layout changed?", article_id[:12])
            return None
        inner = ('["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,null,null,'
                 f'null,0,1],"X","X",1,[1,1,1],1,1,null,0,0,null,0],"{article_id}",{ts.group(1)},"{sg.group(1)}"]')
        body = "f.req=" + urllib.parse.quote(json.dumps([[["Fbv4je", inner, None, "generic"]]]))
        resp = client.post(BATCH_URL, content=body,
                           headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"})
        resp.raise_for_status()
        match = _RESULT.search(resp.text)
        if not match:
            log.warning("google news did not return a target for %s", article_id[:12])
            return None
        target = match.group(1)
        return None if urlparse(target).netloc.endswith("google.com") else target
    except httpx.HTTPError as exc:
        log.warning("could not resolve google news link %s: %s", article_id[:12], exc)
        return None
    finally:
        if own:
            client.close()
