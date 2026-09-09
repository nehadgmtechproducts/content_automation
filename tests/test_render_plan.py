from pathlib import Path

from app.assets.align import Word
from app.assets.render import HERO_SECONDS, beat_spans, build_command, plan_segments

HERO = Path("/tmp/hero.jpg")


def words(n, step=0.4):
    return [Word(f"w{i}", i * step, i * step + step * 0.9) for i in range(n)]


def test_beat_spans_partition_the_words():
    ws = words(12)
    spans = beat_spans(ws, [3, 4, 3, 2])
    assert len(spans) == 4
    assert spans[0][0] == ws[0].start
    assert spans[-1][1] == ws[-1].end
    assert all(a[1] <= b[1] for a, b in zip(spans, spans[1:]))


def test_plan_segments_matches_audio_duration_exactly():
    ws = words(40)
    duration = ws[-1].end
    spans = beat_spans(ws, [10, 10, 10, 10])
    segments = plan_segments(HERO, [None] * 4, spans, duration)
    assert abs(sum(s.duration for s in segments) - duration) < 1e-6
    assert segments[0].duration == HERO_SECONDS


def test_plan_segments_survives_all_beats_missing_broll():
    ws = words(20)
    segments = plan_segments(HERO, [None] * 4, beat_spans(ws, [5, 5, 5, 5]), ws[-1].end)
    assert len(segments) >= 2
    assert all(s.path == HERO for s in segments)


def test_plan_segments_never_produces_a_zero_length_segment():
    ws = words(6, step=0.2)  # total ~1.2s, shorter than the hero card
    segments = plan_segments(HERO, [None] * 4, beat_spans(ws, [2, 2, 1, 1]), ws[-1].end)
    assert all(s.duration > 0 for s in segments)


def test_build_command_maps_streams_and_burns_subtitles():
    ws = words(20)
    segments = plan_segments(HERO, [None] * 4, beat_spans(ws, [5, 5, 5, 5]), ws[-1].end)
    cmd = build_command(segments, Path("/tmp/v.wav"), Path("/tmp/s.ass"), Path("/tmp/o.mp4"))
    joined = " ".join(cmd)
    filtergraph = cmd[cmd.index("-filter_complex") + 1]

    assert "ass='/tmp/s.ass'" in filtergraph
    assert f"concat=n={len(segments)}" in filtergraph
    assert "loudnorm" in filtergraph
    assert "-map [vout]" in joined and "-map [aout]" in joined
    assert "libx264" in joined and "yuv420p" in joined and "+faststart" in joined


def test_build_command_escapes_colons_in_subtitle_path():
    """An unescaped ':' in the path terminates the filter argument and ffmpeg fails."""
    segments = plan_segments(HERO, [None], [(0.0, 5.0)], 5.0)
    cmd = build_command(segments, Path("/tmp/v.wav"),
                        Path("/tmp/odd:name/s.ass"), Path("/tmp/o.mp4"))
    filtergraph = cmd[cmd.index("-filter_complex") + 1]
    assert "odd\\:name" in filtergraph


def test_music_track_is_mixed_and_ducked():
    segments = plan_segments(HERO, [None], [(0.0, 5.0)], 5.0)
    cmd = build_command(segments, Path("/tmp/v.wav"), Path("/tmp/s.ass"),
                        Path("/tmp/o.mp4"), music=Path("/tmp/bed.mp3"))
    filtergraph = cmd[cmd.index("-filter_complex") + 1]
    assert "amix=inputs=2" in filtergraph
    assert "volume=0.10" in filtergraph
