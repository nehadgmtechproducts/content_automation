"""Database schema.

Flow:  RawItem -> Cluster -> Topic -> {ScrapedArticle, FactSheet, Draft, AssetBundle}
Topic.status drives the whole pipeline.
"""
from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    JSON, Boolean, DateTime, Enum, Float, ForeignKey, Index, Integer, String, Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class TopicStatus(str, enum.Enum):
    detected = "detected"
    scraped = "scraped"
    synthesized = "synthesized"
    assets_ready = "assets_ready"
    failed = "failed"
    # reserved for milestone 2 (stages 5-6) — declared now so the enum never migrates twice
    pending_approval = "pending_approval"
    approved = "approved"
    published = "published"
    discarded = "discarded"


class RawItem(Base):
    """One entry from one feed poll, normalized."""
    __tablename__ = "raw_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[str] = mapped_column(String(64), index=True)
    source_tier: Mapped[int] = mapped_column(Integer, default=2)
    url: Mapped[str] = mapped_column(Text)
    canonical_url: Mapped[str] = mapped_column(Text)
    url_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    domain: Mapped[str] = mapped_column(String(255), index=True)
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text, default="")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    entities: Mapped[list | None] = mapped_column(JSON, default=list)
    embedding: Mapped[list | None] = mapped_column(JSON, default=None)
    processed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    __table_args__ = (Index("ix_raw_items_window", "fetched_at", "processed"),)


class Cluster(Base):
    """A set of RawItems judged to be the same story within one time window."""
    __tablename__ = "clusters"

    id: Mapped[int] = mapped_column(primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    primary_entity: Mapped[str] = mapped_column(String(255), index=True)
    entities: Mapped[list | None] = mapped_column(JSON, default=list)
    representative_title: Mapped[str] = mapped_column(Text)
    distinct_domains: Mapped[int] = mapped_column(Integer, default=0)
    has_tier1: Mapped[bool] = mapped_column(Boolean, default=False)
    velocity_score: Mapped[float] = mapped_column(Float, default=0.0)
    baseline: Mapped[float] = mapped_column(Float, default=0.0)
    fired: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    items: Mapped[list["ClusterItem"]] = relationship(back_populates="cluster", cascade="all, delete-orphan")
    topic: Mapped["Topic | None"] = relationship(back_populates="cluster", uselist=False)


class ClusterItem(Base):
    __tablename__ = "cluster_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    cluster_id: Mapped[int] = mapped_column(ForeignKey("clusters.id", ondelete="CASCADE"), index=True)
    raw_item_id: Mapped[int] = mapped_column(ForeignKey("raw_items.id", ondelete="CASCADE"))
    similarity: Mapped[float] = mapped_column(Float, default=1.0)

    cluster: Mapped[Cluster] = relationship(back_populates="items")
    raw_item: Mapped[RawItem] = relationship()

    __table_args__ = (UniqueConstraint("cluster_id", "raw_item_id", name="uq_cluster_item"),)


class EntityBaseline(Base):
    """EWMA of mentions-per-window per entity. This is what stops 'OpenAI' firing hourly."""
    __tablename__ = "entity_baselines"

    id: Mapped[int] = mapped_column(primary_key=True)
    entity: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    mean: Mapped[float] = mapped_column(Float, default=0.0)
    variance: Mapped[float] = mapped_column(Float, default=0.0)
    observations: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class FiredSuppression(Base):
    """Stops the same story firing again while it is still in the news."""
    __tablename__ = "fired_suppressions"

    id: Mapped[int] = mapped_column(primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    entity: Mapped[str] = mapped_column(String(255), index=True)
    fired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class Topic(Base):
    __tablename__ = "topics"

    id: Mapped[int] = mapped_column(primary_key=True)
    cluster_id: Mapped[int | None] = mapped_column(ForeignKey("clusters.id"), unique=True)
    title: Mapped[str] = mapped_column(Text)
    primary_entity: Mapped[str] = mapped_column(String(255))
    status: Mapped[TopicStatus] = mapped_column(Enum(TopicStatus), default=TopicStatus.detected, index=True)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    cluster: Mapped[Cluster | None] = relationship(back_populates="topic")
    articles: Mapped[list["ScrapedArticle"]] = relationship(back_populates="topic", cascade="all, delete-orphan")
    fact_sheet: Mapped["FactSheet | None"] = relationship(back_populates="topic", uselist=False, cascade="all, delete-orphan")
    draft: Mapped["Draft | None"] = relationship(back_populates="topic", uselist=False, cascade="all, delete-orphan")
    assets: Mapped["AssetBundle | None"] = relationship(back_populates="topic", uselist=False, cascade="all, delete-orphan")


class ScrapedArticle(Base):
    __tablename__ = "scraped_articles"

    id: Mapped[int] = mapped_column(primary_key=True)
    topic_id: Mapped[int] = mapped_column(ForeignKey("topics.id", ondelete="CASCADE"), index=True)
    source_index: Mapped[int] = mapped_column(Integer)   # the [n] used in citations
    url: Mapped[str] = mapped_column(Text)
    domain: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(Text)
    byline: Mapped[str] = mapped_column(Text, default="")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    markdown: Mapped[str] = mapped_column(Text)
    scraper: Mapped[str] = mapped_column(String(32), default="firecrawl")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    topic: Mapped[Topic] = relationship(back_populates="articles")


class FactSheet(Base):
    """Output of synthesis call 1 — auditable, and re-runnable without regenerating prose."""
    __tablename__ = "fact_sheets"

    id: Mapped[int] = mapped_column(primary_key=True)
    topic_id: Mapped[int] = mapped_column(ForeignKey("topics.id", ondelete="CASCADE"), unique=True)
    payload: Mapped[dict] = mapped_column(JSON)
    model: Mapped[str] = mapped_column(String(64), default="")
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    topic: Mapped[Topic] = relationship(back_populates="fact_sheet")


class Draft(Base):
    """Output of synthesis call 2 — article + 45s script + distribution metadata."""
    __tablename__ = "drafts"

    id: Mapped[int] = mapped_column(primary_key=True)
    topic_id: Mapped[int] = mapped_column(ForeignKey("topics.id", ondelete="CASCADE"), unique=True)
    article: Mapped[dict] = mapped_column(JSON)
    script: Mapped[dict] = mapped_column(JSON)
    meta: Mapped[dict] = mapped_column(JSON)
    model: Mapped[str] = mapped_column(String(64), default="")
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    validation_notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    topic: Mapped[Topic] = relationship(back_populates="draft")


class ApprovalCard(Base):
    """The Telegram message(s) sent for one topic's human review.

    `message_id` is the video message — the one carrying the Approve/Discard buttons,
    and the one edited in place once a decision is made. The photo and metadata
    messages are sent for context but never edited.
    """
    __tablename__ = "approval_cards"

    id: Mapped[int] = mapped_column(primary_key=True)
    topic_id: Mapped[int] = mapped_column(ForeignKey("topics.id", ondelete="CASCADE"), unique=True)
    chat_id: Mapped[str] = mapped_column(String(64))
    message_id: Mapped[int] = mapped_column(Integer)
    photo_message_id: Mapped[int | None] = mapped_column(Integer, default=None)
    metadata_message_id: Mapped[int | None] = mapped_column(Integer, default=None)
    decision: Mapped[str] = mapped_column(String(16), default="")
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class TelegramCursor(Base):
    """Singleton row (id=1) tracking the last Telegram update_id we've consumed, so a
    restart of the poller doesn't reprocess old button presses."""
    __tablename__ = "telegram_cursor"

    id: Mapped[int] = mapped_column(primary_key=True)
    last_update_id: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class AssetBundle(Base):
    __tablename__ = "asset_bundles"

    id: Mapped[int] = mapped_column(primary_key=True)
    topic_id: Mapped[int] = mapped_column(ForeignKey("topics.id", ondelete="CASCADE"), unique=True)
    directory: Mapped[str] = mapped_column(Text)
    hero_image: Mapped[str] = mapped_column(Text, default="")
    voiceover: Mapped[str] = mapped_column(Text, default="")
    subtitles: Mapped[str] = mapped_column(Text, default="")
    video: Mapped[str] = mapped_column(Text, default="")
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    providers_used: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    topic: Mapped[Topic] = relationship(back_populates="assets")
