"""Story grouping: which ongoing story is a new draft an update of?"""
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.db as db
from app.models import Base, Draft, Topic, TopicStatus
from app.stories import (
    ANY_ENTITY_MIN, SAME_ENTITY_MIN, STORY_WINDOW_DAYS, Known, assign_story, choose_story, fallback_story,
)

NEW = np.array([1.0, 0.0])


def vec(cos: float) -> np.ndarray:
    """A unit vector whose dot product with NEW is exactly `cos`."""
    return np.array([cos, float(np.sqrt(1 - cos**2))])


def known(key, entity, title="t"):
    return Known(key, key.upper(), entity, title)


def choose(cands, new_entity="Apple"):
    ks = [k for k, _ in cands]
    return choose_story("new", new_entity, ks, NEW, np.vstack([vec(c) for _, c in cands]))


# ----------------------------------------------------------------- the pure rule
def test_thresholds_are_the_calibrated_ones():
    assert (SAME_ENTITY_MIN, ANY_ENTITY_MIN, STORY_WINDOW_DAYS) == (0.45, 0.75, 7)


def test_same_entity_joins_at_a_moderate_similarity():
    assert choose([(known("s1", "Apple"), 0.58)]).story_key == "s1"      # real follow-up pair: 0.58


def test_same_entity_does_not_join_a_different_story():
    assert choose([(known("s1", "Apple"), 0.31)]) is None                 # real different-story pair: 0.31


def test_a_different_entity_needs_a_much_higher_similarity():
    """Real pair: Apple vs Nvidia agent-permission stories score 0.52 and must stay separate."""
    assert choose([(known("s1", "Nvidia"), 0.52)]) is None
    assert choose([(known("s1", "Nvidia"), 0.80)]).story_key == "s1"


def test_entity_match_ignores_case_and_spacing():
    assert choose([(known("s1", " apple "), 0.5)], new_entity="Apple").story_key == "s1"


def test_a_missing_entity_never_counts_as_the_same_entity():
    assert choose([(known("s1", ""), 0.6)], new_entity="") is None


def test_the_most_similar_story_wins():
    best = choose([(known("a", "Apple"), 0.55), (known("b", "Apple"), 0.70), (known("c", "Apple"), 0.60)])
    assert best.story_key == "b"


def test_no_candidates_means_a_new_story():
    assert choose_story("x", "Apple", [], NEW, np.zeros((0, 2))) is None


def test_fallback_story_uses_the_label_then_the_title():
    art = {"slug": "my-slug", "title": "A long headline"}
    assert fallback_story(art, {"story_label": "Short name"}) == ("my-slug", "Short name")
    assert fallback_story(art, {}) == ("my-slug", "A long headline")
    assert fallback_story(art, {"story_label": "  "}) == ("my-slug", "A long headline")


# ----------------------------------------------------------------- the database step
class FakeEmbedder:
    """title -> similarity-to-the-new-title, so tests control the geometry exactly."""
    def __init__(self, sims: dict[str, float]):
        self.sims = sims

    def encode(self, texts):
        out = [NEW]                                   # the new draft is texts[0]
        out += [vec(self.sims.get(t, 0.0)) for t in texts[1:]]
        return np.vstack(out)


@pytest.fixture()
def database(monkeypatch):
    eng = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    monkeypatch.setattr(db, "_SessionLocal", sessionmaker(bind=eng, expire_on_commit=False))
    return eng


NOW = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)


def add(title, entity, age_days=0.0, meta=None, slug=None):
    with db.session_scope() as s:
        t = Topic(title=title, primary_entity=entity, status=TopicStatus.synthesized)
        s.add(t); s.flush()
        s.add(Draft(topic_id=t.id, article={"title": title, "slug": slug or title.lower().replace(" ", "-")},
                    script={}, meta=meta or {}, created_at=NOW - timedelta(days=age_days)))
        return t.id


def meta_of(tid):
    with db.session_scope() as s:
        return s.query(Draft).filter_by(topic_id=tid).one().meta


def test_a_follow_up_joins_the_existing_story(database):
    first = add("OpenAI voice model", "OpenAI", 1, {"story_key": "voice", "story_label": "OpenAI voice"})
    new = add("Developers get voice API", "OpenAI")
    out = assign_story(new, FakeEmbedder({"OpenAI voice model": 0.76}), NOW)
    assert out["story_key"] == "voice" and out["joined"] == "OpenAI voice model"
    assert meta_of(new)["story_key"] == "voice" and meta_of(new)["story_label"] == "OpenAI voice"


def test_a_different_subject_starts_its_own_story_with_its_own_label(database):
    add("Apple permissions", "Apple", 1, {"story_key": "apple", "story_label": "Apple permissions"})
    new = add("Nvidia agent safety", "Nvidia", meta={"story_label": "Nvidia agent safety platform"}, slug="nv-1")
    assign_story(new, FakeEmbedder({"Apple permissions": 0.52}), NOW)
    assert meta_of(new)["story_key"] == "nv-1"
    assert meta_of(new)["story_label"] == "Nvidia agent safety platform"


def test_stories_older_than_the_window_do_not_attract_follow_ups(database):
    add("Old story", "Apple", STORY_WINDOW_DAYS + 1, {"story_key": "old", "story_label": "Old"})
    new = add("Follow up", "Apple", slug="fu-1")
    assign_story(new, FakeEmbedder({"Old story": 0.9}), NOW)
    assert meta_of(new)["story_key"] == "fu-1"


def test_drafts_that_predate_stories_can_still_be_joined(database):
    legacy = add("Legacy headline", "Apple", 2, meta={}, slug="legacy-slug")      # no story_key at all
    new = add("Follow up", "Apple")
    assign_story(new, FakeEmbedder({"Legacy headline": 0.6}), NOW)
    assert meta_of(new)["story_key"] == "legacy-slug"
    assert legacy != new


def test_assigning_twice_changes_nothing(database):
    add("A", "Apple", 1, {"story_key": "k", "story_label": "K"})
    new = add("B", "Apple")
    first = assign_story(new, FakeEmbedder({"A": 0.7}), NOW)
    again = assign_story(new, FakeEmbedder({"A": 0.0}), NOW)        # a different answer must be ignored
    assert first["story_key"] == again["story_key"] == "k"


def test_an_embedder_failure_makes_the_draft_its_own_story_and_does_not_raise(database):
    class Boom:
        def encode(self, _):
            raise RuntimeError("model unavailable")

    add("A", "Apple", 1, {"story_key": "k", "story_label": "K"})
    new = add("B", "Apple", slug="b-slug")
    out = assign_story(new, Boom(), NOW)
    assert out["assigned"] and meta_of(new)["story_key"] == "b-slug"


def test_the_first_draft_ever_is_simply_a_new_story(database):
    new = add("Only one", "Apple", slug="only")
    out = assign_story(new, None, NOW)             # no candidates: the embedder must not even be loaded
    assert out["story_key"] == "only" and out["joined"] is None
