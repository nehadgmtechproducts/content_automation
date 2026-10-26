"""Regression: run_assets referenced article/script/meta outside the scope that defined
them, so it crashed for every FRESH topic — the path the approval flow depends on.
This runs the real function against an in-memory DB with the heavy parts stubbed."""
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.db as db
from app import pipeline
from app.models import AssetBundle, Base, Draft, Topic, TopicStatus


def test_run_assets_succeeds_for_a_fresh_topic(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "_SessionLocal", sessionmaker(bind=engine, expire_on_commit=False))

    with db.session_scope() as s:
        t = Topic(title="T", primary_entity="Ent", status=TopicStatus.synthesized)
        s.add(t); s.flush(); tid = t.id
        s.add(Draft(topic_id=tid, article={"title": "A"}, script={"beats": []}, meta={}))

    seen = {}
    def fake_produce(topic_id, article, script, meta, entity, sources=None):
        seen.update(article=article, script=script, meta=meta, entity=entity)
        p = Path("/tmp/x")
        return SimpleNamespace(directory=p, hero_image=p, voiceover=p, subtitles=p, video=p,
                               duration=40.0, providers_used={"tts": "piper"})
    monkeypatch.setattr(pipeline, "produce_assets", fake_produce)
    monkeypatch.setattr(pipeline.approval_service, "is_configured", lambda: False)  # never touch Telegram

    out = pipeline.run_assets(tid)

    assert out["ok"] is True
    assert seen["entity"] == "Ent" and seen["article"] == {"title": "A"}
    with db.session_scope() as s:
        assert s.get(Topic, tid).status == TopicStatus.assets_ready
        assert s.query(AssetBundle).filter_by(topic_id=tid).count() == 1
