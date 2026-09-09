"""URL canonicalization and Google News publisher resolution.

These are pure functions — every one of them is unit-tested without network access.

Why this file matters: the velocity trigger counts *distinct domains*. If Google News
links stay on news.google.com, three separate outlets covering a story all collapse into
one domain and nothing ever fires. If tracking params aren't stripped, the same article
arrives as three different URLs and everything fires. Both failure modes live here.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import re
from urllib.parse import parse_qs, urlparse, urlunparse

# Params that identify a campaign, not a document.
_TRACKING_PREFIXES = ("utm_", "pk_", "mc_", "ns_", "at_")
_TRACKING_EXACT = {
    "fbclid", "gclid", "dclid", "msclkid", "igshid", "twclid", "ref", "ref_src",
    "referrer", "source", "cmpid", "CMP", "smid", "smtyp", "guccounter",
    "guce_referrer", "guce_referrer_sig", "__twitter_impression", "oc", "hl", "gl", "ceid",
    "sh",
}

# Publisher display name -> domain, for the Google News fallback path.
PUBLISHER_DOMAINS = {
    "techcrunch": "techcrunch.com",
    "the verge": "theverge.com",
    "ars technica": "arstechnica.com",
    "wired": "wired.com",
    "venturebeat": "venturebeat.com",
    "zdnet": "zdnet.com",
    "reuters": "reuters.com",
    "bloomberg": "bloomberg.com",
    "cnbc": "cnbc.com",
    "the information": "theinformation.com",
    "the wall street journal": "wsj.com",
    "financial times": "ft.com",
    "the new york times": "nytimes.com",
    "the guardian": "theguardian.com",
    "engadget": "engadget.com",
    "cnet": "cnet.com",
    "axios": "axios.com",
    "business insider": "businessinsider.com",
    "tom's hardware": "tomshardware.com",
    "the register": "theregister.com",
    "semianalysis": "semianalysis.com",
    "mit technology review": "technologyreview.com",
}


def strip_tracking_params(url: str) -> str:
    """Remove campaign/tracking query params, keeping params that select content."""
    parsed = urlparse(url)
    if not parsed.query:
        return url
    kept = []
    for key, values in parse_qs(parsed.query, keep_blank_values=True).items():
        lowered = key.lower()
        if lowered in _TRACKING_EXACT or lowered.startswith(_TRACKING_PREFIXES):
            continue
        kept.extend(f"{key}={v}" for v in values)
    return urlunparse(parsed._replace(query="&".join(sorted(kept))))


def canonicalize(url: str) -> str:
    """Normalize a URL so the same article always produces the same string.

    Lowercases scheme/host, drops www., forces https, strips the fragment, removes a
    trailing slash, and strips tracking params.
    """
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith("//"):
        url = "https:" + url
    parsed = urlparse(url)
    if not parsed.scheme:
        parsed = urlparse("https://" + url)

    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    # Keep a non-default port; drop 80/443.
    if parsed.port and parsed.port not in (80, 443):
        host = f"{host}:{parsed.port}"

    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if len(path) > 1:
        path = path.rstrip("/")

    rebuilt = urlunparse(("https", host, path, "", parsed.query, ""))
    return strip_tracking_params(rebuilt)


def domain_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def url_hash(canonical: str) -> str:
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _try_decode_gnews_path(url: str) -> str | None:
    """Older Google News article IDs are base64 blobs containing the publisher URL.

    Newer (CBMi/AU_yqL) IDs are opaque and need a network round-trip, so this returns
    None for those and the caller falls back to the <source> element or the title suffix.
    """
    match = re.search(r"/(?:rss/)?articles/([A-Za-z0-9_\-]+)", url)
    if not match:
        return None
    blob = match.group(1)
    padded = blob + "=" * (-len(blob) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded)
    except (binascii.Error, ValueError):
        return None
    found = re.search(rb"https?://[\w\-./%?=&+:#~]+", raw)
    if not found:
        return None
    candidate = found.group(0).decode("utf-8", errors="ignore")
    # The blob is length-prefixed protobuf; trailing bytes can smear onto the URL.
    candidate = re.split(r"[\x00-\x1f]", candidate)[0]
    if domain_of(candidate) in ("news.google.com", ""):
        return None
    return candidate


def publisher_from_title(title: str) -> tuple[str, str | None]:
    """Google News titles are 'Real headline - Publisher'. Split them.

    Returns (clean_title, publisher_domain_or_None).
    """
    if " - " not in title:
        return title, None
    head, _, tail = title.rpartition(" - ")
    publisher = tail.strip()
    domain = PUBLISHER_DOMAINS.get(publisher.lower())
    if domain is None and "." in publisher and " " not in publisher:
        domain = publisher.lower()  # already looks like a domain
    if domain is None:
        # Unknown publisher: still split the title, and synthesize a stable pseudo-domain
        # so two articles from the same unknown outlet don't count as two domains.
        slug = re.sub(r"[^a-z0-9]+", "-", publisher.lower()).strip("-")
        domain = f"{slug}.unknown" if slug else None
    return head.strip(), domain


def is_article_url(url: str) -> bool:
    """Whether a URL points at a specific article rather than a site root or section.

    Scraping a homepage yields navigation boilerplate that looks like a successful
    scrape — it has a title and plenty of text — and then poisons the fact sheet with
    content from unrelated stories. Cheaper to reject it here.
    """
    parsed = urlparse(url)
    path = (parsed.path or "/").strip("/")
    if not path:
        return False
    segments = [p for p in path.split("/") if p]
    # A single short segment is almost always a section index ("/tech", "/ai").
    if len(segments) == 1 and len(segments[0]) < 12 and "-" not in segments[0]:
        return False
    return True


def resolve_google_news(url: str, title: str, source_href: str | None = None) -> tuple[str, str, str]:
    """Map a Google News entry back to its true publisher.

    Returns (resolved_url, domain, clean_title).

    The <source url="..."> element identifies the PUBLISHER, not the article — it points
    at the site root (e.g. "https://www.winbuzzer.com"). Using it as the article link
    scrapes the outlet's homepage, which reads as a successful scrape and quietly feeds
    navigation text into synthesis. So it is used for attribution only; the article URL
    comes from a decodable article id, otherwise the Google link is kept for the scraper
    to follow.
    """
    clean_title, title_domain = publisher_from_title(title)

    attributed_domain = ""
    if source_href:
        d = domain_of(source_href)
        if d and d != "news.google.com":
            attributed_domain = d

    decoded = _try_decode_gnews_path(url)
    if decoded and is_article_url(decoded):
        return canonicalize(decoded), (attributed_domain or domain_of(decoded)), clean_title

    if source_href and attributed_domain and is_article_url(source_href):
        # Rare, but some feeds do put the full article URL here.
        return canonicalize(source_href), attributed_domain, clean_title

    # Can't recover the target URL; keep the Google link (the scraper follows redirects)
    # but attribute the domain correctly so distinct-domain counting stays honest.
    return canonicalize(url), (attributed_domain or title_domain or "news.google.com"), clean_title
