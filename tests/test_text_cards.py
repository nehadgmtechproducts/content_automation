from pathlib import Path

from PIL import Image, ImageDraw

from app.providers.base import BrollRequest
from app.providers.broll_text import (
    CAPTION_TOP, HEIGHT, TEXT_TOP, WIDTH, TextCardBroll, fit_text, render_card,
)
from app.providers.fonts import bold_font


def _draw():
    return ImageDraw.Draw(Image.new("RGB", (WIDTH, HEIGHT)))


def test_card_is_a_full_vertical_frame(tmp_path):
    out = render_card("Weights planned for October", "Technology", 3, 4, "Reflection AI", tmp_path / "c.jpg")
    assert Image.open(out).size == (WIDTH, HEIGHT)


def test_short_text_uses_a_large_font_and_long_text_shrinks_to_fit():
    d = _draw()
    big, lines_a, _ = fit_text(d, "Weights due in October", bold_font(), WIDTH - 180, CAPTION_TOP - TEXT_TOP)
    small, lines_b, _ = fit_text(d, "word " * 40, bold_font(), WIDTH - 180, CAPTION_TOP - TEXT_TOP)
    assert big.size > small.size


def test_text_never_enters_the_caption_zone_even_when_absurdly_long():
    d = _draw()
    for text in ("x " * 400, "Supercalifragilisticexpialidocious " * 30, "A"):
        font, lines, line_h = fit_text(d, text, bold_font(), WIDTH - 180, CAPTION_TOP - TEXT_TOP)
        assert TEXT_TOP + len(lines) * line_h <= CAPTION_TOP + line_h  # at most a rounding line
        assert all(d.textlength(ln, font=font) <= WIDTH - 180 + 1 for ln in lines)


def test_cards_in_one_video_differ_but_are_deterministic(tmp_path):
    a1 = render_card("Same text", "Politics", 1, 4, "story", tmp_path / "a1.jpg")
    a2 = render_card("Same text", "Politics", 1, 4, "story", tmp_path / "a2.jpg")
    b = render_card("Same text", "Politics", 2, 4, "story", tmp_path / "b.jpg")
    assert a1.read_bytes() == a2.read_bytes()
    assert a1.read_bytes() != b.read_bytes()


def test_provider_returns_a_still_not_a_video(tmp_path):
    clip = TextCardBroll().fetch(BrollRequest(query="q", seconds=10, index=2, text="Hello", label="Finance",
                                              step=2, steps=4), tmp_path)
    assert clip and clip.is_video is False and clip.path.suffix == ".jpg" and clip.path.exists()


def test_provider_has_no_clip_cap_so_each_beat_is_one_card():
    assert TextCardBroll.max_clip_seconds is None


def test_provider_falls_back_to_the_headline_when_a_beat_has_no_text(tmp_path):
    clip = TextCardBroll().fetch(BrollRequest(query="", seconds=5, index=0, context="Headline here"), tmp_path)
    assert clip and clip.query == "Headline here"
    assert TextCardBroll().fetch(BrollRequest(query="", seconds=5, index=0), tmp_path) is None


# --- regressions found by looking at the first render -------------------------------
from app.assets.render import Segment, _video_filter
from app.providers.broll_text import wrap_words
from PIL import ImageFont


def test_words_are_never_split_across_lines():
    d = _draw()
    text = "Reflection AI unveils Beam lower compute according to Reflection"
    font, lines, _ = fit_text(d, text, bold_font(), WIDTH - 180, CAPTION_TOP - TEXT_TOP)
    assert " ".join(lines) == text, f"words were broken: {lines}"


def test_a_word_wider_than_the_line_forces_a_smaller_font_not_a_split():
    d = _draw()
    big = ImageFont.truetype(bold_font(), 132)
    assert wrap_words(d, "Supercalifragilisticexpialidocious", big, WIDTH - 180) is None
    font, lines, _ = fit_text(d, "Supercalifragilisticexpialidocious", bold_font(), WIDTH - 180, CAPTION_TOP - TEXT_TOP)
    assert lines == ["Supercalifragilisticexpialidocious"]
    assert d.textlength(lines[0], font=font) <= WIDTH - 180


def test_text_cards_zoom_gently_so_the_text_margin_is_never_cropped():
    card = _video_filter(0, Segment(Path("/m/textcard_1.jpg"), False, 10.0))
    photo = _video_filter(0, Segment(Path("/m/hero.jpg"), False, 10.0))
    assert "1.06" in card and "1.30" not in card
    assert "1.30" in photo
    # at 1.06x the frame loses 3% per side (~32px) — well inside the 90px margin
    assert (1.06 - 1) / 2 / 1.06 * WIDTH < 90
