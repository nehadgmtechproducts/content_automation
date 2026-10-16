"""The circuit breaker: a billing failure must stop the scheduler from burning stories,
and recovery must be automatic when the provider comes back."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.db as db
from app import pipeline, production
from app.models import Base, Draft, ScrapedArticle, Topic, TopicStatus
from app.synth import engine_fal
from app.synth.engine import BillingError, SynthesisError


@pytest.fixture()
def media(monkeypatch, tmp_path):
    monkeypatch.setattr(production, "get_settings", lambda: SimpleNamespace(media_path=tmp_path))
    return tmp_path


def test_no_marker_means_production_is_allowed(media):
    assert production.allowed() is True and production.status() is None


def test_paused_stays_paused_until_the_probe_succeeds(media):
    production.pause("fal locked")
    assert production.allowed(probe=lambda: False) is False
    assert production.status()["reason"] == "fal locked"
    assert production.allowed(probe=lambda: True) is True
    assert production.status() is None            # marker cleared by the successful probe


def test_without_a_probe_the_pause_expires(media):
    production.pause("x")
    assert production.allowed() is False
    later = datetime.now(timezone.utc) + production.FALLBACK_EXPIRY + timedelta(seconds=1)
    assert production.allowed(now=later) is True


def test_resume_reports_whether_anything_was_paused(media):
    assert production.resume() is False
    production.pause("x")
    assert production.resume() is True


def test_billing_error_is_a_synthesis_error_but_not_the_other_way_round():
    assert issubclass(BillingError, SynthesisError) and not issubclass(SynthesisError, BillingError)


def _fake_status_error(detail):
    import openai
    resp = httpx.Response(403, request=httpx.Request("POST", "https://fal.run/x"), json={"detail": detail})
    return openai.APIStatusError("boom", response=resp, body={"detail": detail})


def test_fal_locked_account_raises_billing_error_other_errors_do_not():
    assert isinstance(engine_fal._explain(_fake_status_error("User is locked. Reason: TOP_UP."), "x"), BillingError)
    other = engine_fal._explain(_fake_status_error("bad request shape"), "x")
    assert isinstance(other, SynthesisError) and not isinstance(other, BillingError)


@pytest.fixture()
def topic_with_sources(monkeypatch, tmp_path):
    eng = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    monkeypatch.setattr(db, "_SessionLocal", sessionmaker(bind=eng, expire_on_commit=False))
    monkeypatch.setattr(production, "get_settings", lambda: SimpleNamespace(media_path=tmp_path))
    monkeypatch.setattr(pipeline, "get_settings", lambda: SimpleNamespace(synthesis_provider="fal"))
    with db.session_scope() as s:
        t = Topic(title="T", primary_entity="E", status=TopicStatus.scraped)
        s.add(t); s.flush()
        for i in (1, 2):
            s.add(ScrapedArticle(topic_id=t.id, source_index=i, url=f"https://x{i}.com/a", domain=f"x{i}.com",
                                 title="t", markdown="m", scraper="trafilatura"))
        return t.id


def test_billing_failure_during_synthesis_pauses_production(monkeypatch, topic_with_sources, tmp_path):
    def locked(_docs):
        raise BillingError("fal locked")
    monkeypatch.setattr(engine_fal, "synthesize", locked)
    out = pipeline.run_synthesis(topic_with_sources)
    assert out["ok"] is False
    assert production.status() and "fal locked" in production.status()["reason"]


def test_ordinary_synthesis_failure_does_not_pause_production(monkeypatch, topic_with_sources):
    def bad(_docs):
        raise SynthesisError("no fact was corroborated by two or more sources")
    monkeypatch.setattr(engine_fal, "synthesize", bad)
    pipeline.run_synthesis(topic_with_sources)
    assert production.status() is None             # a bad STORY must not stop the others


def test_poll_while_paused_ingests_but_fires_nothing(monkeypatch, topic_with_sources):
    production.pause("fal locked")
    monkeypatch.setattr(pipeline, "ingest_once",
                        lambda: SimpleNamespace(inserted=7, skipped_duplicates=1, failed_sources=[]))
    monkeypatch.setattr(engine_fal, "probe", lambda: False)
    monkeypatch.setattr(pipeline, "run_detection", lambda: pytest.fail("must not detect while paused"))
    out = pipeline.run_poll()
    assert out["inserted"] == 7 and out["topics_fired"] == 0 and out["paused"]["reason"] == "fal locked"


def test_poll_resumes_and_detects_once_the_probe_works(monkeypatch, topic_with_sources):
    production.pause("fal locked")
    monkeypatch.setattr(pipeline, "ingest_once",
                        lambda: SimpleNamespace(inserted=0, skipped_duplicates=0, failed_sources=[]))
    monkeypatch.setattr(engine_fal, "probe", lambda: True)
    monkeypatch.setattr(pipeline, "run_detection", lambda: [])
    out = pipeline.run_poll()
    assert "paused" not in out and production.status() is None


def test_failed_topics_do_not_use_up_the_daily_budget():
    from app.detect.velocity import remaining_topic_budget
    from sqlalchemy.orm import Session
    eng = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    now = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)
    with Session(eng) as s:
        s.add(Topic(title="ok", primary_entity="x", status=TopicStatus.assets_ready, created_at=now))
        for i in range(3):
            s.add(Topic(title=f"f{i}", primary_entity="x", status=TopicStatus.failed, created_at=now))
        s.flush()
        assert remaining_topic_budget(s, 5, now) == 4
