from app.assets.align import Word, estimate_words
from app.assets.subtitles import build_ass, group_words, timestamp


def words_from(pairs):
    return [Word(t, s, e) for t, s, e in pairs]


def test_timestamp_format():
    assert timestamp(0) == "0:00:00.00"
    assert timestamp(1.5) == "0:00:01.50"
    assert timestamp(61.234) == "0:01:01.23"
    assert timestamp(3661.0) == "1:01:01.00"


def test_timestamp_rounding_carries_cleanly():
    assert timestamp(1.999) == "0:00:02.00"
    assert timestamp(59.999) == "0:01:00.00"


def test_group_words_respects_max_size():
    ws = words_from([(f"w{i}", i * 0.3, i * 0.3 + 0.25) for i in range(9)])
    for chunk in group_words(ws, max_words=3):
        assert len(chunk.words) <= 3


def test_group_words_breaks_on_sentence_end():
    ws = words_from([("Nvidia", 0.0, 0.3), ("wins.", 0.3, 0.6), ("Rivals", 0.6, 0.9)])
    chunks = group_words(ws, max_words=3)
    assert len(chunks) == 2
    assert chunks[0].words[-1].text == "wins."


def test_group_words_breaks_on_long_pause():
    ws = words_from([("one", 0.0, 0.3), ("two", 0.3, 0.6), ("three", 2.0, 2.3)])
    chunks = group_words(ws, max_words=5, max_gap=0.65)
    assert len(chunks) == 2


def test_build_ass_has_header_and_one_event_per_word():
    ws = words_from([("Nvidia", 0.0, 0.4), ("opens", 0.4, 0.8), ("NVLink", 0.8, 1.3)])
    ass = build_ass(ws)
    assert "[Script Info]" in ass and "PlayResX: 1080" in ass and "PlayResY: 1920" in ass
    assert "[V4+ Styles]" in ass and "Style: Kinetic," in ass
    events = [ln for ln in ass.splitlines() if ln.startswith("Dialogue:")]
    # one event per word, regardless of how the words are chunked for display
    assert len(events) == 3
    # each event shows its own chunk, with exactly one word recoloured as active
    for event in events:
        assert event.count("\\c&H") == 1
    assert "Nvidia" in events[0] and "NVLink" in events[-1]


def test_build_ass_events_are_ordered_and_non_zero_length():
    ws = words_from([("a", 0.0, 0.2), ("bb", 0.2, 0.5), ("ccc", 0.5, 0.9)])
    events = [ln for ln in build_ass(ws).splitlines() if ln.startswith("Dialogue:")]
    starts = [e.split(",")[1] for e in events]
    ends = [e.split(",")[2] for e in events]
    assert starts == sorted(starts)
    assert all(s < e for s, e in zip(starts, ends))


def test_braces_in_text_are_escaped():
    ass = build_ass(words_from([("{weird}", 0.0, 0.4), ("word", 0.4, 0.8)]))
    assert "\\{weird\\}" in ass


def test_estimate_words_covers_the_full_duration():
    ws = estimate_words("Nvidia opens its networking stack to rivals today", 6.0)
    assert len(ws) == 8
    assert ws[0].start == 0.0
    assert abs(ws[-1].end - 6.0) < 0.01
    assert all(a.end <= b.start + 1e-6 for a, b in zip(ws, ws[1:]))


def test_estimate_words_handles_empty_input():
    assert estimate_words("", 5.0) == []
    assert estimate_words("hello", 0.0) == []


def test_alignment_sanity_rejects_partial_coverage():
    """The real bug this guards: Whisper resuming partway through the audio produced
    subtitles that started 17s into a 34s voiceover and looked broken to a viewer."""
    from app.assets.align import _alignment_is_sane

    script = " ".join(f"word{i}" for i in range(100))
    good = [Word(f"word{i}", i * 0.3, i * 0.3 + 0.25) for i in range(100)]
    ok, _ = _alignment_is_sane(good, script, 30.0)
    assert ok

    too_few = good[:30]
    ok, why = _alignment_is_sane(too_few, script, 30.0)
    assert not ok and "words recovered" in why

    late_start = [Word(w.text, w.start + 20, w.end + 20) for w in good]
    ok, why = _alignment_is_sane(late_start, script, 30.0)
    assert not ok and "first word" in why


def test_alignment_sanity_rejects_early_stop():
    from app.assets.align import _alignment_is_sane

    script = " ".join(f"word{i}" for i in range(100))
    stops_early = [Word(f"word{i}", i * 0.1, i * 0.1 + 0.08) for i in range(100)]
    ok, why = _alignment_is_sane(stops_early, script, 40.0)
    assert not ok and "last word" in why


def _event_spans(ass: str):
    def secs(t):
        h, m, s = t.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)
    out = []
    for line in ass.splitlines():
        if line.startswith("Dialogue:"):
            parts = line.split(",")
            out.append((secs(parts[1]), secs(parts[2])))
    return out


def test_captions_do_not_blink_off_between_phrases():
    """Regression: inter-phrase pauses left 11 blank gaps (~5s) in a 42s video, which
    looks like broken rendering. Chunks now hold until the next one starts."""
    from app.assets.subtitles import MAX_HOLD_SECONDS

    ws = []
    t = 0.0
    for phrase in range(6):
        for w in range(3):
            ws.append(Word(f"p{phrase}w{w}", round(t, 3), round(t + 0.3, 3)))
            t += 0.35
        t += 0.6            # inter-phrase pause, shorter than MAX_HOLD
    spans = _event_spans(build_ass(ws))
    gaps = [round(b[0] - a[1], 3) for a, b in zip(spans, spans[1:]) if b[0] - a[1] > 0.01]
    assert not gaps, f"captions blank out for {gaps}"
    assert MAX_HOLD_SECONDS >= 0.6


def test_long_silence_still_clears_the_screen():
    """A real silence should clear captions rather than leaving a stale line hanging."""
    ws = [Word("one", 0.0, 0.3), Word("two", 0.3, 0.6),
          Word("later", 8.0, 8.3), Word("again", 8.3, 8.6)]
    spans = _event_spans(build_ass(ws))
    gaps = [b[0] - a[1] for a, b in zip(spans, spans[1:]) if b[0] - a[1] > 0.01]
    assert gaps and max(gaps) > 5.0


def test_final_caption_has_a_tail():
    ws = [Word("last", 0.0, 0.4), Word("word", 0.4, 0.8)]
    spans = _event_spans(build_ass(ws))
    assert spans[-1][1] > 0.8


def test_inline_override_colour_uses_the_six_digit_form():
    """Style fields take &HAABBGGRR; inline \\c overrides take &HBBGGRR& . Mixing them
    up is tolerated by libass but is not valid ASS for other players."""
    from app.assets.subtitles import ACCENT, ass_override_color

    inline = ass_override_color(255, 214, 0)
    assert inline == "&H00D6FF&"
    assert ACCENT == "&H0000D6FF"          # style-field form keeps the alpha byte

    ass = build_ass(words_from([("Nvidia", 0.0, 0.4), ("opens", 0.4, 0.8)]))
    for line in ass.splitlines():
        if line.startswith("Dialogue:"):
            assert "\\c&H00D6FF&" in line
            assert "\\c&H0000D6FF" not in line


def test_wrapping_is_enabled_so_captions_cannot_overflow():
    """Regression: WrapStyle 2 disables automatic wrapping, so a chunk wider than the
    frame ran off both edges instead of breaking onto a second line."""
    ass = build_ass(words_from([("Competitors", 0.0, 0.5), ("could", 0.5, 0.8),
                                ("match", 0.8, 1.2)]))
    assert "WrapStyle: 0" in ass
    assert "WrapStyle: 2" not in ass


def test_chunks_stay_within_a_printable_width():
    ws = words_from([("Competitors", 0.0, 0.5), ("could", 0.5, 0.8), ("match", 0.8, 1.2),
                     ("raw", 1.2, 1.4), ("compute", 1.4, 1.9)])
    for chunk in group_words(ws):
        rendered = " ".join(w.text for w in chunk.words)
        # 92pt DejaVu Sans Bold averages ~0.6em advance; ~900px usable => ~16 chars/line.
        assert len(rendered) <= 22, f"chunk too wide for one line: {rendered!r}"
