"""Identity protection backstop: detection, the list of names, the Telegram card, and the pipeline hook.

Every name below is invented. Fake Telegram client throughout: nothing is sent anywhere."""
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.db as db
from app import identity_guard, pipeline
from app.approval import service
from app.approval.cards import build_keyboard, build_review_caption, build_sensitive_warning
from app.models import AssetBundle, Base, Draft, ScrapedArticle, Topic, TopicStatus
from app.synth.engine import CallResult, DRAFT_SYSTEM, FACTS_SYSTEM

CASE = ("Police charged tutor Ravi Kumar Deshmukh, 34, with sexually assaulting a 14-year-old girl, a student at "
        "St. Mary's High School in Kothrud. Priya Nair's mother, Sunita Nair, filed the FIR.")


def payload(body: str, title: str = "A headline") -> dict:
    return {"article": {"title": title, "dek": "dek", "meta_description": "m", "why_it_matters": "w", "key_takeaways": ["k"],
                        "sections": [{"heading": "H", "body_markdown": body}]},
            "metadata": {"title_variants": [], "story_label": "s", "thumbnail_text": "t", "target_keywords": [], "hashtags": []},
            "script": {"beats": [{"narration": "n"}]}}


# ---------- detection ----------

@pytest.mark.parametrize("text", [
    "A man was charged with rape.", "She reported sexual assault to police.", "Charged under the POCSO Act.",
    "The tutor is accused of molesting a student.", "Police are investigating child sexual abuse images.",
    "A 15-year-old was stabbed outside a school.", "Two children were killed in the strike.",
    "The teenager was kidnapped on Monday.", "Sexual harassment complaints rose.",
])
def test_flags_sexual_offences_and_children_in_crimes(text):
    assert identity_guard.sensitive_reasons(text), text


@pytest.mark.parametrize("text", [
    "Elon Musk will give one SpaceX share to every child in America next year.",
    "Two former FBI officials were charged with lying to Congress.",
    "A new vaccine trial began for children aged 5 to 11.",
    "He was grape-picking near the therapist's office.",
    "Meta created a nonvoting stock class for Zuckerberg.",
    "A 34-year-old tutor was charged with fraud.",
])
def test_does_not_flag_ordinary_stories(text):
    assert identity_guard.sensitive_reasons(text) == [], text


def test_a_child_and_a_crime_must_be_in_the_same_sentence():
    assert identity_guard.sensitive_reasons("Children returned to school. A man was killed elsewhere.") == []


# ---------- the assessment ----------

def test_assess_lists_every_person_and_place_so_the_reviewer_can_see_what_identifies_someone():
    result = identity_guard.assess(payload(CASE))
    assert result and "a sexual offence is reported" in result["reasons"]
    for name in ("Priya Nair", "Sunita Nair", "Kothrud"):
        assert name in result["names"], result["names"]


def test_assess_returns_none_for_an_ordinary_story():
    assert identity_guard.assess(payload("SpaceX shares rose one percent in premarket trading.")) is None


def test_the_sources_count_even_when_the_draft_left_the_words_out():
    clean = payload("A tutor was charged after a complaint from a student's family.")
    assert identity_guard.assess(clean) is None
    flagged = identity_guard.assess(clean, ["The tutor is accused of sexually assaulting a 14-year-old girl."])
    assert flagged and flagged["reasons"]


def test_every_field_the_model_writes_is_scanned_keywords_hashtags_and_script_included():
    p = payload("Nothing here.")
    p["metadata"]["hashtags"] = ["#PriyaNairCase"]
    p["script"]["beats"][0]["narration"] = "A girl was raped near Kothrud."
    assert identity_guard.assess(p)["reasons"] == ["a sexual offence is reported"]
    assert "#PriyaNairCase" in identity_guard.draft_text(p)


def test_both_prompts_carry_the_identity_rule():
    assert "IDENTITY PROTECTION" in FACTS_SYSTEM and "NEVER write their name" in FACTS_SYSTEM
    assert "Never name, describe or otherwise identify a child" in DRAFT_SYSTEM and "EVERY field" in DRAFT_SYSTEM


# ---------- the Telegram card ----------

SENSITIVE = {"reasons": ["a sexual offence is reported"], "names": ["Priya Nair", "Kothrud"]}


def test_the_warning_lists_the_names_the_law_and_what_the_button_means():
    text = build_sensitive_warning(SENSITIVE)
    for part in ("SENSITIVE STORY", "a sexual offence is reported", "Priya Nair; Kothrud", "BNS sections 72-73", "POCSO section 23", "nobody identifiable"):
        assert part in text
    assert len(text) <= 4096


def test_the_approve_button_makes_the_reviewer_affirm_only_on_sensitive_stories():
    normal = build_keyboard(7)["inline_keyboard"][0]
    sensitive = build_keyboard(7, sensitive=True)["inline_keyboard"][0]
    assert normal[0]["text"] == "✅ Approve & Deploy" and "identifiable" in sensitive[0]["text"]
    assert normal[0]["callback_data"] == sensitive[0]["callback_data"] == "approve:7"      # same action either way
    assert normal[1] == sensitive[1]
    assert "SENSITIVE" in build_review_caption({"title": "T"}, sensitive=True) and "SENSITIVE" not in build_review_caption({"title": "T"})


class Recorder:
    def __init__(self): self.sent = []
    def send_message(self, chat, text, reply_markup=None): self.sent.append(("message", text, reply_markup)); return {"message_id": len(self.sent)}
    def send_photo(self, chat, path, caption="", reply_markup=None): self.sent.append(("photo", caption, reply_markup)); return {"message_id": len(self.sent)}
    def send_video(self, chat, path, caption="", reply_markup=None): self.sent.append(("video", caption, reply_markup)); return {"message_id": len(self.sent)}


@pytest.fixture()
def database(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "_SessionLocal", sessionmaker(bind=engine, expire_on_commit=False))
    monkeypatch.setattr(service, "is_configured", lambda: True)
    monkeypatch.setattr(service, "get_settings", lambda: SimpleNamespace(telegram_chat_id="-100"))


def make_topic(meta: dict) -> int:
    with db.session_scope() as s:
        t = Topic(title="T", primary_entity="E", status=TopicStatus.assets_ready); s.add(t); s.flush()
        s.add(Draft(topic_id=t.id, article={"title": "T", "dek": "d", "sections": []}, script={}, meta=meta))
        s.add(AssetBundle(topic_id=t.id, directory="/m", hero_image="/m/hero.jpg", video=""))
        return t.id


def test_a_sensitive_card_starts_with_the_warning_and_the_affirming_button(database):
    client = Recorder()
    service.push_for_approval(make_topic({"sensitive": SENSITIVE}), client)
    assert client.sent[0][0] == "message" and "SENSITIVE STORY" in client.sent[0][1]
    kinds = [k for k, _t, _m in client.sent]
    assert kinds[-1] == "photo" and "SENSITIVE" in client.sent[-1][1]
    assert "identifiable" in client.sent[-1][2]["inline_keyboard"][0][0]["text"]


def test_an_ordinary_card_has_no_warning_and_the_normal_button(database):
    client = Recorder()
    service.push_for_approval(make_topic({}), client)
    assert all("SENSITIVE" not in t for _k, t, _m in client.sent)
    assert client.sent[-1][2]["inline_keyboard"][0][0]["text"] == "✅ Approve & Deploy"


# ---------- the pipeline hook ----------

def run_synthesis_with(monkeypatch, body: str, guard=None):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "_SessionLocal", sessionmaker(bind=engine, expire_on_commit=False))
    with db.session_scope() as s:
        t = Topic(title="T", primary_entity="E", status=TopicStatus.scraped); s.add(t); s.flush(); tid = t.id
        for i in (1, 2):
            s.add(ScrapedArticle(topic_id=tid, source_index=i, url=f"https://s{i}.test/a", domain=f"s{i}.test", title="x", markdown=body, scraper="t"))
    fake = CallResult(payload=payload(body), model="m", input_tokens=1, output_tokens=1)
    facts = CallResult(payload={"consensus_facts": []}, model="m", input_tokens=1, output_tokens=1)
    monkeypatch.setattr(pipeline, "get_settings", lambda: SimpleNamespace(synthesis_provider="nvidia"))
    import app.synth.engine_nvidia as nv
    monkeypatch.setattr(nv, "synthesize", lambda docs: (facts, fake, ""))
    monkeypatch.setattr(pipeline.stories, "assign_story", lambda _id: None)
    monkeypatch.setattr(pipeline, "check_freshness", lambda *a, **k: SimpleNamespace(fresh=True, reason=""))
    if guard:
        monkeypatch.setattr(pipeline.identity_guard, "assess", guard)
    out = pipeline.run_synthesis(tid)
    with db.session_scope() as s:
        return out, dict(s.query(Draft).filter_by(topic_id=tid).one().meta)


def test_synthesis_stores_the_flag_with_the_draft(monkeypatch):
    out, meta = run_synthesis_with(monkeypatch, CASE)
    assert out["ok"] and meta["sensitive"]["reasons"] and "Priya Nair" in meta["sensitive"]["names"]


def test_an_ordinary_story_gets_no_flag(monkeypatch):
    out, meta = run_synthesis_with(monkeypatch, "SpaceX shares rose in premarket trading.")
    assert out["ok"] and "sensitive" not in meta


def test_if_the_guard_itself_breaks_the_story_is_flagged_not_lost_and_not_waved_through(monkeypatch):
    def boom(*_a, **_k): raise RuntimeError("spaCy exploded")
    out, meta = run_synthesis_with(monkeypatch, "An ordinary story.", guard=boom)
    assert out["ok"] is True
    assert meta["sensitive"]["reasons"] == ["the automatic identity check failed"]
