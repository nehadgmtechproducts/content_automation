from datetime import datetime, timedelta, timezone

from app.freshness import check_freshness

NOW = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)


def test_fresh_topic_passes():
    detected = NOW - timedelta(hours=2)
    check = check_freshness(detected, max_age_hours=24, now=NOW)
    assert check.fresh
    assert check.age_hours == 2.0


def test_topic_right_at_the_limit_passes():
    detected = NOW - timedelta(hours=24)
    check = check_freshness(detected, max_age_hours=24, now=NOW)
    assert check.fresh


def test_stale_topic_is_refused():
    """The actual bug this guards: a week-old topic must not be processed as current."""
    detected = NOW - timedelta(days=7)
    check = check_freshness(detected, max_age_hours=24, now=NOW)
    assert not check.fresh
    assert check.age_hours == 168.0
    assert "stale" in check.reason
    assert "168" in check.reason


def test_reason_names_the_configured_limit():
    detected = NOW - timedelta(hours=50)
    check = check_freshness(detected, max_age_hours=24, now=NOW)
    assert "24" in check.reason


def test_naive_datetime_is_treated_as_utc():
    """RawItem/Topic timestamps are always tz-aware in practice, but this must not
    crash if one ever isn't."""
    detected_naive = (NOW - timedelta(hours=3)).replace(tzinfo=None)
    check = check_freshness(detected_naive, max_age_hours=24, now=NOW)
    assert check.fresh
    assert check.age_hours == 3.0


def test_future_timestamp_does_not_crash_or_falsely_refuse():
    detected = NOW + timedelta(hours=1)
    check = check_freshness(detected, max_age_hours=24, now=NOW)
    assert check.fresh
    assert check.age_hours < 0


def test_threshold_is_configurable_not_hardcoded():
    detected = NOW - timedelta(hours=30)
    assert not check_freshness(detected, max_age_hours=24, now=NOW).fresh
    assert check_freshness(detected, max_age_hours=48, now=NOW).fresh


# --- detection: freshness by PUBLISH time, not ingest time -------------------------
from dataclasses import dataclass

from app.freshness import event_time, split_fresh


@dataclass
class FakeItem:
    name: str
    published_at: datetime | None
    fetched_at: datetime


def test_three_day_old_story_fetched_today_is_stale():
    """The reported bug: Apple story published Oct 2-5, fetched minutes ago, was
    treated as breaking because only fetched_at was consulted."""
    item = FakeItem("apple", published_at=NOW - timedelta(days=3), fetched_at=NOW - timedelta(minutes=5))
    fresh, stale = split_fresh([item], max_age_hours=24, now=NOW)
    assert fresh == [] and stale == [item]


def test_recently_published_item_is_fresh():
    item = FakeItem("new", published_at=NOW - timedelta(hours=2), fetched_at=NOW - timedelta(minutes=5))
    fresh, stale = split_fresh([item], max_age_hours=24, now=NOW)
    assert fresh == [item] and stale == []


def test_missing_publish_date_falls_back_to_fetch_time():
    item = FakeItem("nodate", published_at=None, fetched_at=NOW - timedelta(minutes=5))
    assert event_time(item) == item.fetched_at
    fresh, _ = split_fresh([item], max_age_hours=24, now=NOW)
    assert fresh == [item]


def test_mixed_batch_is_partitioned():
    a = FakeItem("old", NOW - timedelta(days=3), NOW)
    b = FakeItem("new", NOW - timedelta(hours=1), NOW)
    fresh, stale = split_fresh([a, b], max_age_hours=24, now=NOW)
    assert [i.name for i in fresh] == ["new"]
    assert [i.name for i in stale] == ["old"]
