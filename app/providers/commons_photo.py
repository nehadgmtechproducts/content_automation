"""A file photo of the story's main subject, from Wikimedia Commons (free-licence images only).

Used after a photo from the story's own sources (app/providers/source_photo.py) has not been found. The
risk here is not copyright (every image has a free licence) but RELEVANCE: a wrong picture misleads. A plain
search of Commons by name returns the wrong thing (in testing "Michael Gray" gave a footballer and a singer,
not the actor in the story; "BBC" gave logos and a sports car). So this does not search images by name:

  1. find who/what the story is about: the people, organisations and places named in its headline and opening
     (spaCy, ranked people first, countries and continents excluded: too generic to illustrate an event);
  2. resolve each to a Wikipedia article, choosing between same-named articles by how well their summary matches the
     story's own words (so "Michael Gray" + "Shazam!" picks the actor); a disambiguation page is never used;
  3. take that article's lead image, which Wikipedia editors chose to depict the subject, and only if it is on
     Commons with a licence that allows this use: CC0, public domain, or CC BY (share-alike is off by default
     because our share card is an adaptation of the photo);
  4. credit it in full: "File photo. Photo: <author> / Wikimedia Commons (<licence>), cropped", linking to the file's
     Commons page. It is a photograph OF the subject, not of the event, and is always labelled "File photo".

Nothing here raises: no confident match simply means the caller falls back to the AI illustration or gradient.
"""
from __future__ import annotations

import html
import io
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import httpx
from PIL import Image, ImageFilter

from app.providers.image_nvidia import fit_cover

log = logging.getLogger(__name__)

WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
HEADERS = {"User-Agent": "TheBrief-content-engine/0.1 (https://github.com/nehadgmtechproducts/content_automation; news site photo lookup)"}
OUT_WIDTH, OUT_HEIGHT = 1600, 900
MIN_LANDSCAPE_WIDTH, MIN_PORTRAIT_SHORT_SIDE = 1000, 600
MAX_SUBJECTS = 4
_http_client: httpx.Client | None = None          # test hook: a client with a fake transport

# Too generic to illustrate an event: a photo of "France" or "the U.S." says nothing about this story.
_TOO_GENERIC = {
    "us", "u.s.", "usa", "u.s", "united states", "america", "uk", "u.k.", "united kingdom", "britain", "england", "european union", "eu",
    "china", "russia", "ukraine", "india", "france", "germany", "italy", "spain", "japan", "israel", "iran", "canada", "australia", "brazil",
    "mexico", "turkey", "pakistan", "europe", "asia", "africa", "north america", "south america", "middle east", "the world", "world",
    "washington", "new york", "california", "texas", "florida", "london", "paris", "beijing", "moscow", "delhi", "new delhi", "gaza",
    "reuters", "associated press", "ap", "afp", "cnn", "fox news", "the guardian", "guardian", "the new york times", "npr", "cnbc",
}
# File names that are not a photograph of the subject.
_NOT_A_PHOTO = re.compile(r"logo|flag|map\b|maps\b|locator|coat[ _]of[ _]arms|seal\b|emblem|diagram|chart|graph|screenshot|signature|"
                          r"poster|stamp|icon|banner|infobox|wordmark|\bsvg\b|\.svg|\.gif|\.pdf|\.tif", re.IGNORECASE)
_STOPWORDS = set("""about after again against being between could during first from have into more most other over said says some such than
that their there these they this those through under until very were what when where which while with would your also been both each
mark news story report reports year years week weeks today monday tuesday wednesday thursday friday saturday sunday""".split())
_ALLOWED_LICENCE = re.compile(r"^(cc0|cc[- ]zero|public domain|pd\b|pd[- ]|no known copyright|cc[- ]by(?![- ]?(sa|nc|nd))[- ]\d)", re.IGNORECASE)
_SHARE_ALIKE = re.compile(r"^cc[- ]by[- ]sa[- ]\d", re.IGNORECASE)


@dataclass
class Subject:
    name: str
    label: str            # PERSON | ORG | GPE | LOC | FAC
    count: int = 1


@dataclass
class CommonsPhoto:
    path: Path
    author: str
    licence: str
    page_url: str         # the file's Commons page (the credit links here)
    subject: str
    credit: str           # the caption: "File photo. Photo: ... / Wikimedia Commons (CC BY 4.0), cropped"
    label: str            # painted onto the share image: "File photo · Jane Doe / Wikimedia Commons"


# ---------------------------------------------------------------- who is the story about

def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z][a-z'-]{3,}", text.lower()) if w not in _STOPWORDS}


def _in_headline(name: str, headline: str) -> bool:
    return bool(re.search(rf"(?<![A-Za-z]){re.escape(name)}(?![A-Za-z])", headline, re.IGNORECASE))


def subjects_of(headline: str, body: str = "", entity: str = "") -> list[Subject]:
    """What the story is ABOUT: people, organisations and places named in its HEADLINE, best first.

    Names are read from the body text (sentence case, where the entity finder is reliable: on a Title Case headline it
    reads fragments like "Orders Trump" as a person), and a name only counts if the headline also names it. So a
    quoted expert or an official mentioned in passing is never the subject. A lone surname in the headline ("Trump")
    is expanded to the most-mentioned full name in the body ("Donald Trump"). People come first (a portrait is the
    most useful and least ambiguous picture), then organisations, then places; countries and continents are dropped."""
    from app.detect.entities import _nlp

    doc = _nlp()(body[:3000])
    people: dict[str, Subject] = {}
    others: dict[str, Subject] = {}
    for ent in doc.ents:
        if ent.label_ not in {"PERSON", "ORG", "GPE", "LOC", "FAC"}:
            continue
        name = re.sub(r"[’']s$", "", re.sub(r"\s+", " ", ent.text).strip(" ,.;:'\"()"))
        if len(name) < 3 or name.lower() in _TOO_GENERIC:
            continue
        bucket = people if (ent.label_ == "PERSON" and " " in name) else others
        key = name.lower()
        if key in bucket:
            bucket[key].count += 1
        else:
            bucket[key] = Subject(name, ent.label_ if bucket is others else "PERSON")

    found: dict[str, Subject] = {}
    surname_owners: dict[str, int] = {}
    for person in people.values():
        surname_owners[person.name.split()[-1].lower()] = surname_owners.get(person.name.split()[-1].lower(), 0) + 1
    for person in sorted(people.values(), key=lambda s: -s.count):          # most-mentioned full name first
        surname = person.name.split()[-1]
        # a full name in the headline always counts; a lone surname only when it can mean exactly one person in the
        # story ("Trump" is Donald OR Melania when the text mentions both: then we do not guess)
        if _in_headline(person.name, headline) or (_in_headline(surname, headline) and surname_owners[surname.lower()] == 1):
            found.setdefault(person.name.lower(), person)
    for other in others.values():
        single = " " not in other.name
        if single and any(pn.split()[-1].lower() == other.name.lower() for pn in (s.name for s in people.values())):
            continue                                                         # "Trump"/"Musk": already covered by the full name
        if _in_headline(other.name, headline) and other.name.lower() not in found:
            found[other.name.lower()] = other
    if entity and entity.lower() not in _TOO_GENERIC and _in_headline(entity, headline):
        found.setdefault(entity.lower(), Subject(entity, "ORG", 0))
    rank = {"PERSON": 0, "ORG": 1, "FAC": 2, "LOC": 2, "GPE": 2}
    first_at = lambda s: (headline.lower().find(s.name.split()[-1].lower()) if s.label == "PERSON" else headline.lower().find(s.name.lower()))   # noqa: E731
    return sorted(found.values(), key=lambda s: (rank[s.label], first_at(s), -s.count))[:MAX_SUBJECTS]


# ---------------------------------------------------------------- Wikipedia: the right article

def resolve_article(client: httpx.Client, subject: Subject, context: set[str]) -> dict | None:
    """The Wikipedia article about `subject` (as the story means it), with its lead image file name. When several
    articles share the name, the one whose summary shares the most words with the story wins, and it must share at
    least one. A disambiguation page, or a name with no clear article, gives None."""
    params = {"action": "query", "format": "json", "generator": "search", "gsrsearch": subject.name, "gsrnamespace": 0, "gsrlimit": 8,
              "prop": "pageimages|extracts|description", "piprop": "name", "exintro": 1, "explaintext": 1, "exchars": 700, "exlimit": "max"}
    pages = (client.get(WIKIPEDIA_API, params=params, headers=HEADERS).json().get("query") or {}).get("pages", {})
    name_tokens = {t for t in re.findall(r"[a-z]+", subject.name.lower()) if len(t) > 1}   # kept out of the context overlap
    cands = []
    for p in sorted(pages.values(), key=lambda p: p.get("index", 99)):
        title, extract = p.get("title", ""), p.get("extract", "")
        if "may refer to" in extract[:200] or title.lower().endswith("(disambiguation)"):
            continue
        bare = re.sub(r"\s*\(.*?\)\s*", "", title).strip()
        bare_tokens = [w for w in re.findall(r"[a-z]+", bare.lower()) if w != "the"]
        if sorted(bare_tokens) != sorted(w for w in re.findall(r"[a-z]+", subject.name.lower()) if w != "the"):
            continue                            # "List of executive orders in the second Trump presidency" is not "Trump"
        if not p.get("pageimage"):
            continue
        overlap = len(_words(extract + " " + p.get("description", "")) & (context - name_tokens))
        exact = True                             # (the filter above already requires the same name)
        cands.append({"title": title, "image": p["pageimage"], "overlap": overlap, "exact": exact, "page": p})
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0]                          # one article carries exactly this name: no ambiguity
    best = max(cands, key=lambda c: c["overlap"])
    return best if best["overlap"] >= 1 else None    # several same-named articles and no clue which: do not guess


# ---------------------------------------------------------------- Commons: licence, author, file

def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value or ""))).strip()


def commons_file(client: httpx.Client, file_name: str, allow_sharealike: bool = False) -> dict | None:
    """Licence-checked facts about File:<file_name> on Commons, or None. A file that is not on Commons (Wikipedia's
    fair-use images live only on Wikipedia) simply is not found."""
    params = {"action": "query", "format": "json", "titles": f"File:{file_name}", "prop": "imageinfo", "iiprop": "url|size|mime|extmetadata",
              "iiurlwidth": OUT_WIDTH, "iiextmetadatafilter": "Artist|LicenseShortName|LicenseUrl|Copyrighted|NonFree|AttributionRequired"}
    pages = (client.get(COMMONS_API, params=params, headers=HEADERS).json().get("query") or {}).get("pages", {})
    page = next(iter(pages.values()), {})
    info = (page.get("imageinfo") or [None])[0]
    if not info:
        return None
    meta = {k: _clean(v.get("value", "")) for k, v in (info.get("extmetadata") or {}).items()}
    licence = meta.get("LicenseShortName", "")
    if meta.get("NonFree", "").lower() == "true":
        return None
    ok = bool(_ALLOWED_LICENCE.match(licence)) or (allow_sharealike and bool(_SHARE_ALIKE.match(licence)))
    if not ok:
        log.info("commons: %s has licence %r: not used", file_name[:40], licence)
        return None
    author = meta.get("Artist", "")
    author = re.sub(r"(?i)^(own work|unknown author|unknown)$", "", author).strip()
    is_pd = bool(re.match(r"(?i)(cc0|cc[- ]zero|public domain|pd|no known copyright)", licence))
    if not author and not is_pd:
        return None                              # a CC BY licence demands attribution: no author, no use
    if len(author) > 60:
        author = author[:57].rstrip() + "…"
    if info.get("mime") not in ("image/jpeg", "image/png"):
        return None
    return {"author": author or "Unknown author", "licence": licence, "licence_url": meta.get("LicenseUrl", ""),
            "width": info.get("width", 0), "height": info.get("height", 0), "download": info.get("thumburl") or info.get("url"),
            "page_url": info.get("descriptionurl") or f"https://commons.wikimedia.org/wiki/File:{file_name.replace(' ', '_')}"}


def to_cover(image: Image.Image) -> Image.Image:
    """A 16:9 picture. A landscape photo is centre-cropped. A portrait is NOT cropped (a centre crop of a head-and-shoulders
    shot cuts the face): it is shown whole, centred over a blurred, darkened copy of itself."""
    w, h = image.size
    if w / h >= 1.3:
        return fit_cover(image, OUT_WIDTH, OUT_HEIGHT)
    backdrop = fit_cover(image, OUT_WIDTH, OUT_HEIGHT).filter(ImageFilter.GaussianBlur(40))
    backdrop = Image.blend(backdrop, Image.new("RGB", backdrop.size, (10, 10, 14)), 0.45)
    scale = OUT_HEIGHT / h
    front = image.resize((max(1, round(w * scale)), OUT_HEIGHT), Image.LANCZOS)
    backdrop.paste(front, ((OUT_WIDTH - front.width) // 2, 0))
    return backdrop


def build_credit(author: str, licence: str) -> str:
    return f"File photo. Photo: {author} / Wikimedia Commons ({licence}), cropped"


def build_label(author: str) -> str:
    short = author if len(author) <= 28 else author[:25].rstrip() + "…"
    return f"File photo · {short} / Wikimedia Commons"


# ---------------------------------------------------------------- the public entry point

def find_commons_photo(headline: str, body: str, keywords: list[str], entity: str, out_path: Path,
                       allow_sharealike: bool = False) -> CommonsPhoto | None:
    """A licence-safe file photo of the story's subject, saved to `out_path` as a 1600x900 JPEG, or None."""
    client = _http_client or httpx.Client(timeout=25, follow_redirects=True)
    context = _words(" ".join([headline, body[:1500], *keywords]))
    try:
        subjects = subjects_of(headline, body, entity)
    except Exception as exc:  # noqa: BLE001
        log.info("commons: could not read the story's subjects (%s)", type(exc).__name__)
        return None
    for subject in subjects:
        try:
            article = resolve_article(client, subject, context)
            if not article:
                log.info("commons: no confident Wikipedia article for %r", subject.name)
                continue
            file_name = article["image"]
            if _NOT_A_PHOTO.search(file_name):
                log.info("commons: %r lead image %r is not a photograph", subject.name, file_name[:40])
                continue
            info = commons_file(client, file_name, allow_sharealike)
            if not info or not info["download"]:
                continue
            resp = client.get(info["download"], headers=HEADERS)
            if resp.status_code != 200:
                continue
            image = Image.open(io.BytesIO(resp.content)); image.load(); image = image.convert("RGB")
            w, h = image.size
            if min(w, h) < MIN_PORTRAIT_SHORT_SIDE or (w / h >= 1.3 and w < MIN_LANDSCAPE_WIDTH) or not 0.5 <= w / h <= 2.4:
                log.info("commons: %r image is %dx%d: too small or an odd shape", subject.name, w, h)
                continue
            out_path.parent.mkdir(parents=True, exist_ok=True)
            to_cover(image).save(out_path, quality=90)
            return CommonsPhoto(path=out_path, author=info["author"], licence=info["licence"], page_url=info["page_url"], subject=article["title"],
                                credit=build_credit(info["author"], info["licence"]), label=build_label(info["author"]))
        except Exception as exc:  # noqa: BLE001 - one bad lookup must not stop the next subject
            log.info("commons: lookup for %r failed (%s)", subject.name, type(exc).__name__)
    return None
