"""Editorial hero card composed locally with Pillow.

This is the zero-key default. It is not a stand-in for a generated image so much as a
different (and for news, more defensible) choice: a typeset card can't hallucinate a
person's face or invent a logo, and it renders identically every time.
"""
from __future__ import annotations

import colorsys
import hashlib
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from app.providers.base import ImageProvider, ImageRequest
from app.providers.fonts import bold_font, regular_font


def _palette(seed: str) -> tuple[tuple[int, int, int], tuple[int, int, int], tuple[int, int, int]]:
    """Deterministic duotone from the entity name, so a given subject looks consistent."""
    digest = hashlib.sha256(seed.encode()).digest()
    hue = digest[0] / 255.0
    dark = colorsys.hls_to_rgb(hue, 0.13, 0.55)
    light = colorsys.hls_to_rgb((hue + 0.08) % 1.0, 0.38, 0.62)
    accent = colorsys.hls_to_rgb((hue + 0.5) % 1.0, 0.62, 0.95)
    to255 = lambda c: tuple(int(x * 255) for x in c)  # noqa: E731
    return to255(dark), to255(light), to255(accent)


def _gradient(size: tuple[int, int], top: tuple, bottom: tuple) -> Image.Image:
    w, h = size
    base = Image.new("RGB", (1, h))
    px = base.load()
    for y in range(h):
        t = y / max(h - 1, 1)
        px[0, y] = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
    return base.resize((w, h), Image.BILINEAR)


def _fit_text(draw, text, font_path, max_width, start_size, min_size, max_lines):
    """Shrink until the headline fits in max_lines. Returns (font, lines)."""
    for size in range(start_size, min_size - 1, -4):
        font = ImageFont.truetype(font_path, size)
        avg = draw.textlength("MMMMMMMMMM", font=font) / 10
        wrap_at = max(12, int(max_width / max(avg, 1)))
        lines = textwrap.wrap(text, width=wrap_at)
        if len(lines) <= max_lines and all(
            draw.textlength(ln, font=font) <= max_width for ln in lines
        ):
            return font, lines
    font = ImageFont.truetype(font_path, min_size)
    return font, textwrap.wrap(text, width=28)[:max_lines]


class PillowEditorialCard(ImageProvider):
    name = "pillow_card"

    def generate(self, req: ImageRequest, out_path: Path) -> Path:
        w, h = req.width, req.height
        dark, light, accent = _palette(req.entity or req.headline)

        img = _gradient((w, h), light, dark)

        # Soft radial bloom for depth — cheap, and stops the flat-gradient look.
        bloom = Image.new("L", (w, h), 0)
        ImageDraw.Draw(bloom).ellipse(
            [int(w * 0.45), int(-h * 0.35), int(w * 1.35), int(h * 0.75)], fill=110
        )
        bloom = bloom.filter(ImageFilter.GaussianBlur(int(min(w, h) * 0.12)))
        img = Image.composite(Image.new("RGB", (w, h), accent), img, bloom)

        draw = ImageDraw.Draw(img)
        margin = int(w * 0.07)
        text_width = w - 2 * margin

        # Accent rule above the headline.
        draw.rectangle(
            [margin, int(h * 0.20), margin + int(w * 0.10), int(h * 0.20) + max(4, h // 220)],
            fill=accent,
        )

        headline_font, lines = _fit_text(
            draw, req.headline, bold_font(), text_width,
            start_size=int(h * 0.11), min_size=int(h * 0.045), max_lines=4,
        )
        y = int(h * 0.26)
        line_gap = int(headline_font.size * 1.18)
        for line in lines:
            draw.text((margin + 2, y + 2), line, font=headline_font, fill=(0, 0, 0))
            draw.text((margin, y), line, font=headline_font, fill=(255, 255, 255))
            y += line_gap

        if req.subtitle:
            sub_font = ImageFont.truetype(regular_font(), max(14, int(h * 0.032)))
            sub_lines = textwrap.wrap(req.subtitle, width=64)[:2]
            y += int(h * 0.02)
            for line in sub_lines:
                draw.text((margin, y), line, font=sub_font, fill=(232, 232, 236))
                y += int(sub_font.size * 1.35)

        if req.entity:
            tag_font = ImageFont.truetype(bold_font(), max(12, int(h * 0.026)))
            label = req.entity.upper()[:28]
            tw = draw.textlength(label, font=tag_font)
            pad = int(h * 0.014)
            box_h = tag_font.size + pad * 2
            draw.rectangle(
                [margin, h - margin - box_h, margin + tw + pad * 2, h - margin], fill=accent
            )
            draw.text((margin + pad, h - margin - box_h + pad), label,
                      font=tag_font, fill=(12, 12, 16))

        out_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(out_path, quality=92)
        return out_path
