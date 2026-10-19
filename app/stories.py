"""Group follow-up reports into ONE story, so the website can show a timeline.

A "story" is the ongoing subject (e.g. "OpenAI real-time voice model"); each article about it is
an "update". When a new draft is written, this step decides whether it continues a recent story
or starts a new one, and records `story_key` / `story_label` in the draft's metadata (a JSON
column, so no database migration). Matching is local and free: the same MiniLM embeddings the
detector uses, no extra LLM call.

The rule was calibrated on real headline pairs:
  follow-ups to one story ........ 0.58 - 0.83 similarity
  same company, different story .. 0.11 - 0.31
  unrelated but both "AI agent
  permissions" (Apple vs Nvidia) . 0.52     <- why a plain cut-off is not enough
so a draft joins a story if it is about the SAME ENTITY and similarity >= 0.45, or about ANY entity
and similarity >= 0.75. Stories stop attracting follow-ups after STORY_WINDOW_DAYS.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Sequence

import numpy as np

log = logging.getLogger(__name__)

SAME_ENTITY_MIN = 0.45
ANY_ENTITY_MIN = 0.75
STORY_WINDOW_DAYS = 7


@dataclass(frozen=True)
class Known:
    """An existing draft, as seen by the matcher."""
    story_key: str
    story_label: str
    entity: str
    title: str


def fallback_story(article: dict, meta: dict) -> tuple[str, str]:
    """(key, label) for a draft that belongs to no earlier story: its own slug and name."""
    label = (meta.get("story_label") or "").strip() or article.get("title") or "Untitled"
    return article["slug"], label


def choose_story(new_title: str, new_entity: str, known: Sequence[Known],
                 new_vec: np.ndarray, known_vecs: np.ndarray) -> Known | None:
    """The best existing story for a new draft, or None. Pure: vectors are passed in
    (already normalized), so the rule is unit-tested without a model."""
    if len(known) == 0:
        return None
    sims = known_vecs @ new_vec
    best: tuple[float, Known] | None = None
    for sim, k in zip(sims, known):
        same = bool(new_entity) and k.entity.strip().lower() == new_entity.strip().lower()
        if sim >= (SAME_ENTITY_MIN if same else ANY_ENTITY_MIN) and (best is None or sim > best[0]):
            best = (float(sim), k)
    return best[1] if best else None


def assign_story(topic_id: int, embedder=None, now: datetime | None = None) -> dict:
    """Set story_key / story_label on a topic's draft. Idempotent; never raises (a failure just
    makes the draft its own story, so synthesis is never blocked by grouping)."""
    from sqlalchemy.orm.attributes import flag_modified

    from app.db import session_scope
    from app.models import Draft, Topic

    now = now or datetime.now(timezone.utc)
    with session_scope() as s:
        draft = s.query(Draft).filter_by(topic_id=topic_id).one_or_none()
        topic = s.get(Topic, topic_id)
        if draft is None or topic is None:
            return {"assigned": False, "reason": "no draft"}
        if draft.meta.get("story_key"):
            return {"assigned": True, "story_key": draft.meta["story_key"], "joined": None}

        title, entity = draft.article.get("title", ""), topic.primary_entity or ""
        key, label = fallback_story(draft.article, draft.meta)
        joined = None
        try:
            cutoff = now - timedelta(days=STORY_WINDOW_DAYS)
            rows = (s.query(Draft, Topic).join(Topic, Topic.id == Draft.topic_id)
                    .filter(Draft.topic_id != topic_id, Draft.created_at >= cutoff).all())
            known = []
            for d, t in rows:
                k, lab = d.meta.get("story_key"), d.meta.get("story_label")
                if not k:                       # drafts that predate stories are their own story
                    k, lab = fallback_story(d.article, d.meta)
                known.append(Known(k, lab, t.primary_entity or "", d.article.get("title", "")))
            if known:
                embedder = embedder or _default_embedder()
                vecs = embedder.encode([title, *[k.title for k in known]])
                match = choose_story(title, entity, known, vecs[0], vecs[1:])
                if match:
                    key, label, joined = match.story_key, match.story_label, match.title
        except Exception as exc:  # noqa: BLE001 - grouping must never block production
            log.warning("story grouping failed for topic %s (%s): own story", topic_id, exc)

        draft.meta = {**draft.meta, "story_key": key, "story_label": label}
        flag_modified(draft, "meta")
    log.info("topic %s -> story %r%s", topic_id, key, f" (joined: {joined})" if joined else " (new)")
    return {"assigned": True, "story_key": key, "joined": joined}


def _default_embedder():
    from app.detect.cluster import default_embedder
    from app.settings import detect_config

    return default_embedder(detect_config()["clustering"]["embedding_model"])
