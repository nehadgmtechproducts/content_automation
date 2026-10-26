"""The story's picture, taken from one of the story's own sources (COVER_IMAGE_MODE=source).

Every news page names its lead photograph in its metadata (`og:image`), so after the article text is scraped
we can fetch the picture too. This is a copyright decision, not only a technical one, so the code is cautious:

  * agency and stock pictures are never used. Outlets license AFP, AP, Reuters, Getty, Shutterstock and the
    like, and those agencies enforce. A picture is skipped when its file name, its credit or its caption
    names one (e.g. `.../ap_6ada610b9ae68-...jpg`, "(AP Photo/...)", `GettyImages-1326039.jpg`);
  * logos, placeholders and outlet-branded frames are skipped (an outlet's own logo is not a picture of the story);
  * the picture is never altered beyond a centre crop and resize, and always travels with its credit and a link
    to the page it came from: the outlet's name goes in the picture's caption on the website and on the share image;
  * a story about a sexual offence or a child (app/identity_guard.py) never uses one: a photograph of a real
    victim is exactly what the law forbids us to show.

Nothing here raises: no usable picture simply means the caller falls back to the AI illustration or gradient.
"""
from __future__ import annotations

import io
import json
import logging
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from PIL import Image, ImageStat

from app.providers.image_nvidia import fit_cover

log = logging.getLogger(__name__)

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
           "Accept-Language": "en"}
MIN_WIDTH, MIN_HEIGHT = 800, 450
MAX_BYTES = 12 * 1024 * 1024
OUT_WIDTH, OUT_HEIGHT = 1600, 900
_http_client: httpx.Client | None = None          # test hook: a client with a fake transport

# Agencies and stock libraries. `ap`, `afp`, `epa`, `pa` only count when they stand alone, so "map_" or "spa-" do not match.
_AGENCY = re.compile(
    r"getty|shutterstock|alamy|istock|depositphotos|adobestock|dreamstime|wireimage|zumapress|\bimago\b|apimages"
    r"|associated[-_ ]?press|agence[-_ ]?france|reuters|bloomberg|xinhua|anadolu|pa[-_ ]?media|pa[-_ ]?images"
    r"|(?<![a-z0-9])(?:ap|afp|epa)(?![a-z0-9])",
    re.IGNORECASE,
)
_NOT_A_PICTURE = re.compile(r"logo|placeholder|default[-_]|brand(?:ed)?[-_]|favicon|sprite|avatar|icon|badge|blank|fallback|share[-_]?image", re.IGNORECASE)


@dataclass
class SourcePhoto:
    path: Path
    outlet: str          # "The Guardian": shown as the credit
    page_url: str        # the article the picture came from: the credit links here
    image_url: str
    credit: str          # e.g. "Image: The Guardian"


class _Meta(HTMLParser):
    """Collects what a page says about its picture: meta tags, link rel=image_src and JSON-LD blocks."""
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, list[str]] = {}
        self.links: list[str] = []
        self.jsonld: list[str] = []
        self.captions: list[str] = []          # the first few <figcaption>s: pages credit the lead photo there
        self._in_ld = False
        self._in_caption = False

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            if key and a.get("content"):
                self.meta.setdefault(key, []).append(a["content"])
        elif tag == "link" and "image_src" in a.get("rel", "").lower() and a.get("href"):
            self.links.append(a["href"])
        elif tag == "script" and "ld+json" in a.get("type", "").lower():
            self._in_ld = True
            self.jsonld.append("")
        elif tag == "figcaption" and len(self.captions) < 3:
            self._in_caption = True
            self.captions.append("")

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_ld = False
        elif tag == "figcaption":
            self._in_caption = False

    def handle_data(self, data):
        if self._in_ld:
            self.jsonld[-1] += data
        elif self._in_caption and self.captions:
            self.captions[-1] += data


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def page_facts(html: str, base_url: str) -> dict:
    """{'images': [urls, best first], 'outlet': str, 'notes': [credit/caption text to screen for agencies]}.
    The notes include the page's first captions: if the lead photo is credited to an agency there ("Photograph:
    Jane Doe/Reuters"), the whole page is skipped, because we cannot tell which picture a caption belongs to."""
    p = _Meta()
    try:
        p.feed(html[:600_000])
    except Exception:  # noqa: BLE001 - malformed HTML must not stop us
        pass
    urls: list[str] = []
    notes: list[str] = [*p.meta.get("og:image:alt", []), *p.meta.get("twitter:image:alt", [])]
    for key in ("og:image", "og:image:secure_url", "twitter:image", "twitter:image:src"):
        urls += p.meta.get(key, [])
    urls += p.links
    for block in p.jsonld:                       # structured data names the credit holder, and sometimes a larger picture
        try:
            data = json.loads(block)
        except ValueError:
            continue
        for node in _walk(data):
            image = node.get("image") or node.get("thumbnailUrl")
            for item in (image if isinstance(image, list) else [image]):
                if isinstance(item, str):
                    urls.append(item)
                elif isinstance(item, dict):
                    if isinstance(item.get("url"), str):
                        urls.append(item["url"])
                    notes += [str(item.get(k)) for k in ("creditText", "copyrightNotice", "caption", "description") if item.get(k)]
                    for k in ("creator", "copyrightHolder", "author"):
                        v = item.get(k)
                        notes.append(v.get("name", "") if isinstance(v, dict) else str(v or ""))
    unique: list[str] = []
    for u in urls:
        absolute = urljoin(base_url, u.strip())
        if absolute.startswith(("http://", "https://")) and absolute not in unique:
            unique.append(absolute)
    outlet = (p.meta.get("og:site_name") or [""])[0].strip()
    notes += [re.sub(r"\s+", " ", c).strip() for c in p.captions]
    return {"images": unique, "outlet": outlet, "notes": [n for n in notes if n]}


def is_agency_or_stock(*texts: str) -> bool:
    return any(_AGENCY.search(t) for t in texts if t)


def outlet_name(site_name: str, page_url: str) -> str:
    if site_name and len(site_name) <= 40:
        return site_name
    return re.sub(r"^www\.", "", urlparse(page_url).netloc)


def _download(client: httpx.Client, url: str, referer: str) -> Image.Image | None:
    resp = client.get(url, headers={"Referer": referer, "Accept": "image/avif,image/webp,image/jpeg,image/png,*/*;q=0.5"})
    if resp.status_code != 200 or len(resp.content) > MAX_BYTES:
        return None
    if not resp.headers.get("content-type", "image/").lower().startswith("image/"):
        return None
    image = Image.open(io.BytesIO(resp.content))
    image.load()
    return image.convert("RGB")


def acceptable(image: Image.Image) -> str | None:
    """None if the picture is good enough to be a story's picture; otherwise the reason it is not."""
    w, h = image.size
    if w < MIN_WIDTH or h < MIN_HEIGHT:
        return f"too small ({w}x{h})"
    if not 1.0 <= w / h <= 2.4:
        return f"odd shape ({w}x{h}): a banner or a strip, not a photo"
    small = image.resize((64, 64))
    if max(ImageStat.Stat(small).stddev) < 9:
        return "nearly one flat colour: a placeholder"
    return None


def find_source_photo(sources: list[dict], out_path: Path) -> SourcePhoto | None:
    """The first acceptable picture among the story's sources (given best source first), saved to `out_path`
    as a 1600x900 JPEG (centre crop). `sources` are dicts with `url` (the article page)."""
    client = _http_client or httpx.Client(headers=HEADERS, timeout=25, follow_redirects=True)
    for src in sources:
        page_url = src.get("url", "")
        try:
            page = client.get(page_url, headers=HEADERS)
            if page.status_code != 200:
                log.info("source photo: %s answered %s", page_url[:70], page.status_code)
                continue
            facts = page_facts(page.text, str(page.url))
            outlet = outlet_name(facts["outlet"], page_url)
            # One flagged picture or caption condemns the WHOLE page: a page often offers several renditions of the
            # same agency photo, and only some of their file names say so (seen: CNBC's Getty picture).
            flagged = [u for u in facts["images"] if is_agency_or_stock(u, urlparse(u).path.rsplit("/", 1)[-1])]
            if flagged or is_agency_or_stock(*facts["notes"]):
                log.info("source photo: skipped %s entirely: it carries an agency/stock picture or credit", outlet)
                continue
            for image_url in facts["images"][:4]:
                name = urlparse(image_url).path.rsplit("/", 1)[-1]
                if _NOT_A_PICTURE.search(image_url):
                    log.info("source photo: skipped a logo/placeholder from %s (%s)", outlet, name[:40])
                    continue
                image = _download(client, image_url, referer=page_url)
                if image is None:
                    continue
                why_not = acceptable(image)
                if why_not:
                    log.info("source photo: skipped %s from %s: %s", name[:40], outlet, why_not)
                    continue
                out_path.parent.mkdir(parents=True, exist_ok=True)
                fit_cover(image, OUT_WIDTH, OUT_HEIGHT).save(out_path, quality=90)
                return SourcePhoto(path=out_path, outlet=outlet, page_url=page_url, image_url=image_url, credit=f"Image: {outlet}")
        except Exception as exc:  # noqa: BLE001 - one bad source must not stop the next one
            log.info("source photo: %s failed (%s)", page_url[:70], type(exc).__name__)
    return None
