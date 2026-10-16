"""Approve button -> website publish, end to end against an in-memory database.

Uses a fake Telegram client and a fake publisher: no network, no files, no bot token.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.db as db
from app.approval import service
from app.approval.cards import build_keyboard  # noqa: F401  (import check)
from app.models import ApprovalCard, Base, Topic, TopicStatus

CHAT = "-100123"


class FakeClient:
    def __init__(self):
        self.toasts, self.captions = [], []

    def answer_callback_query(self, _id, text):
        self.toasts.append(text)

    def edit_message_caption(self, _chat, _mid, caption):
        self.captions.append(caption)


@pytest.fixture()
def topic_id(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "_SessionLocal", sessionmaker(bind=engine, expire_on_commit=False))
    monkeypatch.setattr(service, "get_settings", lambda: type("S", (), {"telegram_chat_id": CHAT})())
    with db.session_scope() as s:
        t = Topic(title="T", primary_entity="E", status=TopicStatus.pending_approval)
        s.add(t); s.flush()
        s.add(ApprovalCard(topic_id=t.id, chat_id=CHAT, message_id=5))
        return t.id


def press(action, tid):
    return {"callback_query": {"id": "cb", "data": f"{action}:{tid}",
                               "message": {"chat": {"id": int(CHAT)}, "message_id": 5, "caption": "Card"}}}


def status(tid):
    with db.session_scope() as s:
        return s.get(Topic, tid).status


def test_approve_publishes_and_edits_card(topic_id):
    calls = []
    def publisher(tid):
        calls.append(tid); return {"published": True, "slug": "x", "path": "/articles/x"}
    client = FakeClient()
    out = service.process_decision(press("approve", topic_id), client, publisher=publisher)
    assert calls == [topic_id] and out["published"]["published"]
    assert client.toasts == ["Approved and published to the website."]
    assert "Published (/articles/x)" in client.captions[0]


def test_failed_publish_keeps_topic_approved_and_says_so(topic_id):
    client = FakeClient()
    service.process_decision(press("approve", topic_id), client,
                             publisher=lambda tid: {"published": False, "reason": "disk full"})
    assert status(topic_id) == TopicStatus.approved          # retryable, not lost
    assert client.toasts == ["Approved, but website publish failed."]
    assert "disk full" in client.captions[0]


def test_discard_never_publishes(topic_id):
    def boom(_): raise AssertionError("must not publish a discarded topic")
    client = FakeClient()
    service.process_decision(press("discard", topic_id), client, publisher=boom)
    assert status(topic_id) == TopicStatus.discarded
    assert "Discarded" in client.captions[0]


def test_double_tap_publishes_only_once(topic_id):
    calls = []
    def publisher(tid):
        calls.append(tid); return {"published": True, "slug": "x", "path": "/articles/x"}
    client = FakeClient()
    service.process_decision(press("approve", topic_id), client, publisher=publisher)
    # first publish happened; simulate the second tap arriving while status is approved
    service.process_decision(press("approve", topic_id), client, publisher=publisher)
    assert calls == [topic_id]
