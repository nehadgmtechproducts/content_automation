"""The text-free cover and the hash-named asset publishing."""
import hashlib

import pytest
from PIL import Image

from app.providers.image_pillow import COVER_HEIGHT, COVER_WIDTH, generate_cover
from app.publishers.web import file_hash, prune_assets, publish_asset


# ---------------------------------------------------------------- cover image
def test_cover_has_the_website_size(tmp_path):
    out = generate_cover("Apple", tmp_path / "cover.jpg")
    assert Image.open(out).size == (COVER_WIDTH, COVER_HEIGHT) == (1600, 900)


def test_cover_is_deterministic_per_seed_and_differs_between_seeds(tmp_path):
    a1 = generate_cover("Apple", tmp_path / "a1.jpg").read_bytes()
    a2 = generate_cover("Apple", tmp_path / "a2.jpg").read_bytes()
    n = generate_cover("Nvidia", tmp_path / "n.jpg").read_bytes()
    assert a1 == a2 and a1 != n


def test_cover_carries_no_headline_text_unlike_the_titled_card(tmp_path):
    """The titled card has hard white text; the cover must have none. White (>=250 on every
    channel) pixels are a fair proxy: the gradient/bloom never reaches that bright."""
    from app.providers.base import ImageRequest
    from app.providers.image_pillow import PillowEditorialCard

    def white_share(path):
        px = Image.open(path).convert("RGB").getdata()
        return sum(1 for r, g, b in px if min(r, g, b) >= 250) / len(px)

    card = PillowEditorialCard().generate(
        ImageRequest(headline="Apple tightens disk access controls", subtitle="x", entity="Apple"),
        tmp_path / "card.jpg")
    cover = generate_cover("Apple", tmp_path / "cover.jpg")
    assert white_share(card) > 0.01          # sanity: the proxy does detect text on the card
    assert white_share(cover) == 0.0


# ---------------------------------------------------------------- hash-named assets
def test_publish_asset_names_files_by_content_hash(tmp_path):
    src = tmp_path / "x.jpg"; src.write_bytes(b"one")
    dest = tmp_path / "out"; dest.mkdir()
    name = publish_asset(src, dest, "cover")
    assert name == f"cover-{hashlib.sha256(b'one').hexdigest()[:8]}.jpg"
    assert (dest / name).read_bytes() == b"one"


def test_same_content_gives_the_same_name_and_new_content_a_new_one(tmp_path):
    src = tmp_path / "x.jpg"; dest = tmp_path / "out"; dest.mkdir()
    src.write_bytes(b"one"); first = publish_asset(src, dest, "cover")
    assert publish_asset(src, dest, "cover") == first
    src.write_bytes(b"two"); second = publish_asset(src, dest, "cover")
    assert second != first and file_hash(src) in second


def test_publishing_never_removes_the_old_version(tmp_path):
    """The live page may still reference it until the new JSON is written."""
    src = tmp_path / "x.mp4"; dest = tmp_path / "out"; dest.mkdir()
    src.write_bytes(b"v1"); old = publish_asset(src, dest, "video")
    src.write_bytes(b"v2"); publish_asset(src, dest, "video")
    assert (dest / old).exists()


def test_prune_removes_superseded_and_legacy_names_but_nothing_else(tmp_path):
    for n in ("cover-11111111.jpg", "cover-22222222.jpg", "cover.jpg", "video-33333333.mp4", "notes.txt", "other.jpg"):
        (tmp_path / n).write_bytes(b"x")
    removed = prune_assets(tmp_path, keep={"cover-22222222.jpg", "video-33333333.mp4"})
    assert removed == ["cover-11111111.jpg", "cover.jpg"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["cover-22222222.jpg", "notes.txt", "other.jpg", "video-33333333.mp4"]
