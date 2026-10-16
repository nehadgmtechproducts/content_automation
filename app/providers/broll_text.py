"""Text-card "b-roll": a typographic video with no generated footage.

Each beat becomes a full-screen card showing that beat's on-screen line over a gradient,
with the renderer's usual slow zoom, the voiceover and the burned-in captions on top.
Free, deterministic, and — unlike generated footage — incapable of inventing a face, a
logo, or a scene, which makes it the safe default for sensitive stories and the fallback
whenever the footage budget is exhausted.

Layout is governed by two rules learned the hard way:
  * Captions are bottom-aligned with a 430px margin, so they occupy roughly y>=1250 on the
    1920px canvas. Card text must end above CAPTION_TOP or the two collide.
  * Platform UI (profile, buttons) covers the top ~250px and the right edge, so the
    category label sits inside TOP_SAFE and text keeps a wide left/right margin.
"""
from __future__ import annotations

import colorsys
import hashlib
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from app.providers.base import BrollClip, BrollProvider, BrollRequest
from app.providers.fonts import bold_font, regular_font

WIDTH, HEIGHT = 1080, 1920
MARGIN_X = 90
TOP_SAFE = 250            # nothing important above this
TEXT_TOP = 420            # text block starts here...
CAPTION_TOP = 1180        # ...and must end above this (captions begin ~1250)
MAX_LINES = 6


def _palette(seed: str, step: int) -> tuple[tuple, tuple, tuple]:
    """Same hue family for a whole video (keyed on the story), shifted a little per beat so
    the cards read as a sequence instead of one repeated slide."""
    digest = hashlib.sha256(seed.encode()).digest()
    hue = (digest[0] / 255.0 + 0.045 * step) % 1.0
    dark = colorsys.hls_to_rgb(hue, 0.10, 0.50)
    mid = colorsys.hls_to_rgb(hue, 0.24, 0.55)
    accent = colorsys.hls_to_rgb((hue + 0.5) % 1.0, 0.62, 0.95)
    to255 = lambda c: tuple(int(x * 255) for x in c)  # noqa: E731
    return to255(dark), to255(mid), to255(accent)


def _gradient(top: tuple, bottom: tuple) -> Image.Image:
    col = Image.new("RGB", (1, HEIGHT))
    px = col.load()
    for y in range(HEIGHT):
        t = y / (HEIGHT - 1)
        px[0, y] = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
    return col.resize((WIDTH, HEIGHT), Image.BILINEAR)


def wrap_words(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont,
               max_width: int) -> list[str] | None:
    """Greedy wrap by MEASURED width, never splitting a word. Returns None if a single word
    is wider than max_width at this size (the caller then tries a smaller font)."""
    lines: list[str] = []
    current = ""
    for word in text.split():
        if draw.textlength(word, font=font) > max_width:
            return None
        trial = f"{current} {word}".strip()
        if draw.textlength(trial, font=font) <= max_width:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def fit_text(draw: ImageDraw.ImageDraw, text: str, font_path: str, max_width: int,
             max_height: int, start: int = 132, minimum: int = 56):
    """Largest font size whose whole-word wrap fits max_width x max_height. Returns
    (font, lines, line_height). If even the minimum size overflows, the text is cut at a
    word boundary with an ellipsis, so it can never run into the caption zone."""
    for size in range(start, minimum - 1, -4):
        font = ImageFont.truetype(font_path, size)
        lines = wrap_words(draw, text, font, max_width)
        line_h = int(size * 1.18)
        if lines is not None and len(lines) <= MAX_LINES and len(lines) * line_h <= max_height:
            return font, lines, line_h

    font = ImageFont.truetype(font_path, minimum)
    line_h = int(minimum * 1.18)
    cap = max(1, min(MAX_LINES, max_height // line_h))
    words = text.split()
    lines: list[str] = []
    while words and len(lines) < cap:
        line = ""
        while words and draw.textlength(f"{line} {words[0]}".strip(), font=font) <= max_width:
            line = f"{line} {words.pop(0)}".strip()
        if not line:                        # one word wider than the whole line: hard-trim it
            w = words.pop(0)
            while w and draw.textlength(w + "…", font=font) > max_width:
                w = w[:-1]
            line = w + "…"
        lines.append(line)
    if words:                               # text remains: mark the cut
        last = lines[-1].rstrip(" ,;:.-").removesuffix("…")
        while last and draw.textlength(last + "…", font=font) > max_width:
            last = last[:-1].rstrip()       # make room: the ellipsis itself takes width
        lines[-1] = last + "…"
    return font, lines, line_h


def render_card(text: str, label: str, step: int, steps: int, seed: str, out: Path) -> Path:
    dark, mid, accent = _palette(seed or text, step)
    img = _gradient(mid if step % 2 else dark, dark if step % 2 else mid)

    # Soft accent bloom, alternating corners, for depth without any imagery.
    bloom = Image.new("L", (WIDTH, HEIGHT), 0)
    x0 = int(WIDTH * (0.35 if step % 2 else -0.35))
    ImageDraw.Draw(bloom).ellipse([x0, int(HEIGHT * 0.05), x0 + int(WIDTH * 1.1), int(HEIGHT * 0.55)], fill=95)
    bloom = bloom.filter(ImageFilter.GaussianBlur(150))
    img = Image.composite(Image.new("RGB", (WIDTH, HEIGHT), accent), img, bloom)
    draw = ImageDraw.Draw(img)

    # Category label + accent rule, inside the top safe zone.
    label_font = ImageFont.truetype(bold_font(), 40)
    draw.rectangle([MARGIN_X, TOP_SAFE - 70, MARGIN_X + 110, TOP_SAFE - 62], fill=accent)
    if label:
        draw.text((MARGIN_X, TOP_SAFE - 40), label.upper(), font=label_font, fill=(235, 235, 240))

    # Beat progress dots, top-right but clear of the platform UI column.
    if steps > 1:
        r, gap = 11, 38
        x = WIDTH - 200 - (steps - 1) * gap
        for i in range(steps):
            filled = i < step
            draw.ellipse([x + i * gap, TOP_SAFE - 52, x + i * gap + 2 * r, TOP_SAFE - 52 + 2 * r],
                         fill=accent if filled else None, outline=accent, width=3)

    font, lines, line_h = fit_text(
        draw, text.strip(), bold_font(), WIDTH - 2 * MARGIN_X, CAPTION_TOP - TEXT_TOP)
    y = TEXT_TOP
    for line in lines:
        draw.text((MARGIN_X + 3, y + 4), line, font=font, fill=(0, 0, 0))     # shadow
        draw.text((MARGIN_X, y), line, font=font, fill=(255, 255, 255))
        y += line_h

    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, quality=92)
    return out


class TextCardBroll(BrollProvider):
    name = "text_cards"
    max_clip_seconds = None      # a still serves any length; one card per beat

    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or {}

    def fetch(self, req: BrollRequest, out_dir: Path) -> BrollClip | None:
        text = (req.text or req.context or req.query).strip()
        if not text:
            return None
        out = render_card(text, req.label, req.step, req.steps, req.context or text,
                          Path(out_dir) / f"textcard_{req.index}.jpg")
        return BrollClip(path=out, is_video=False, query=text)
