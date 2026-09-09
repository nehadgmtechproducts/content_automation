"""Kinetic ASS subtitles driven by real word timestamps.

One Dialogue event per word: the whole 2-4 word chunk stays on screen while the word
being spoken is recolored and popped. That gives the word-by-word highlight without
relying on karaoke templates, and it renders identically in any libass build.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.assets.align import Word
from app.providers.fonts import subtitle_font_name

PLAY_W, PLAY_H = 1080, 1920


def ass_color(r: int, g: int, b: int, alpha: int = 0) -> str:
    """Colour for a *style field* — &HAABBGGRR, reversed byte order from RGB."""
    return f"&H{alpha:02X}{b:02X}{g:02X}{r:02X}"


def ass_override_color(r: int, g: int, b: int) -> str:
    """Colour for an *inline override* such as \\c — &HBBGGRR& , six digits plus a
    trailing '&' and no alpha. This is a different format from the style-field form
    above; libass tolerates the 8-digit style value here, but other renderers are not
    required to, and the .ass file is an artifact users may open in a normal player.
    """
    return f"&H{b:02X}{g:02X}{r:02X}&"


WHITE = ass_color(255, 255, 255)
ACCENT = ass_color(255, 214, 0)      # amber highlight — style-field form
ACCENT_INLINE = ass_override_color(255, 214, 0)   # same colour, inline-override form
OUTLINE = ass_color(8, 8, 12)
SHADOW = ass_color(0, 0, 0, 0x60)


def timestamp(seconds: float) -> str:
    """ASS uses H:MM:SS.cs (centiseconds, single-digit hour)."""
    seconds = max(seconds, 0.0)
    hours, rem = divmod(int(seconds), 3600)
    minutes, secs = divmod(rem, 60)
    centis = int(round((seconds - int(seconds)) * 100))
    if centis == 100:  # rounding can carry
        centis, secs = 0, secs + 1
        if secs == 60:
            secs, minutes = 0, minutes + 1
    return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"


def escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


@dataclass
class Chunk:
    words: list[Word]

    @property
    def start(self) -> float:
        return self.words[0].start

    @property
    def end(self) -> float:
        return self.words[-1].end


def group_words(words: list[Word], max_words: int = 3, max_chars: int = 18,
                max_gap: float = 0.65) -> list[Chunk]:
    """Break the word stream into on-screen chunks.

    Breaks on: chunk size, line length, a long pause, or sentence-ending punctuation —
    so a chunk never straddles two sentences.
    """
    chunks: list[Chunk] = []
    current: list[Word] = []

    for word in words:
        if current:
            gap = word.start - current[-1].end
            projected = sum(len(w.text) + 1 for w in current) + len(word.text)
            ends_sentence = current[-1].text.rstrip().endswith((".", "!", "?", ":"))
            if (len(current) >= max_words or projected > max_chars
                    or gap > max_gap or ends_sentence):
                chunks.append(Chunk(current))
                current = []
        current.append(word)

    if current:
        chunks.append(Chunk(current))
    return chunks


# How long a finished chunk may stay on screen waiting for the next one. Without this
# the captions blink off in every inter-phrase pause — 11 blank gaps totalling ~5s in a
# 42-second video, which reads as broken rendering rather than as pacing.
MAX_HOLD_SECONDS = 0.9
# Tail after the final word, so the last caption doesn't vanish on the last syllable.
FINAL_TAIL_SECONDS = 0.5


def build_ass(words: list[Word], font_size: int = 92, margin_v: int = 430) -> str:
    """Render the full .ass file.

    margin_v of 430 keeps captions clear of the bottom UI overlays on Reels/Shorts/TikTok.
    """
    font = subtitle_font_name()
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {PLAY_W}
PlayResY: {PLAY_H}
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Kinetic,{font},{font_size},{WHITE},{ACCENT},{OUTLINE},{SHADOW},-1,0,0,0,100,100,1,0,1,7,4,2,90,90,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    lines = []
    chunks = group_words(words)
    for ci, chunk in enumerate(chunks):
        # Carry the chunk through the pause before the next one, capped so a genuine
        # silence still clears the screen.
        if ci + 1 < len(chunks):
            chunk_end = min(chunks[ci + 1].start, chunk.end + MAX_HOLD_SECONDS)
        else:
            chunk_end = chunk.end + FINAL_TAIL_SECONDS
        chunk_end = max(chunk_end, chunk.end)

        for i, word in enumerate(chunk.words):
            # The last word of a chunk holds the whole chunk on screen until chunk_end.
            end = chunk_end if i == len(chunk.words) - 1 else chunk.words[i + 1].start
            if end <= word.start:
                end = word.start + 0.08

            rendered = []
            for j, w in enumerate(chunk.words):
                token = escape(w.text)
                if j == i:
                    # Recolour + a 120ms scale pop on the word being spoken.
                    rendered.append(
                        f"{{\\c{ACCENT_INLINE}\\fscx104\\fscy104"
                        f"\\t(0,120,\\fscx112\\fscy112)"
                        f"\\t(120,240,\\fscx104\\fscy104)}}{token}{{\\r}}"
                    )
                else:
                    rendered.append(token)
            text = " ".join(rendered)
            lines.append(
                f"Dialogue: 0,{timestamp(word.start)},{timestamp(end)},Kinetic,,0,0,0,,{text}"
            )

    return header + "\n".join(lines) + "\n"


def write_ass(words: list[Word], path) -> "object":
    from pathlib import Path

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_ass(words), encoding="utf-8")
    return path
