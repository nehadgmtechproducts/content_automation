"""Pure logic: no network, no database. This is the layer worth testing thoroughly,
since telegram_client.py is a thin, mostly-untestable-without-mocks REST wrapper."""
import pytest

from app.approval.cards import (
    build_keyboard, build_metadata_text, build_photo_caption, build_video_caption,
    decide, parse_callback_data,
)
from app.models import TopicStatus


def test_photo_caption_includes_title_and_summary():
    caption = build_photo_caption({"title": "Nvidia opens NVLink", "dek": "A big shift."})
    assert "Nvidia opens NVLink" in caption
    assert "A big shift." in caption


def test_photo_caption_handles_missing_dek():
    caption = build_photo_caption({"title": "Nvidia opens NVLink"})
    assert "Nvidia opens NVLink" in caption


def test_photo_caption_respects_telegram_length_limit():
    huge_dek = "x" * 2000
    caption = build_photo_caption({"title": "T", "dek": huge_dek})
    assert len(caption) <= 1024


def test_video_caption_includes_entity_label():
    caption = build_video_caption({"title": "Nvidia opens NVLink"}, entity="Nvidia")
    assert "Nvidia" in caption and "Nvidia opens NVLink" in caption


def test_metadata_text_covers_every_spec_field():
    metadata = {
        "thumbnail_text": "Nvidia opens NVLink",
        "target_keywords": ["nvidia nvlink", "ai interconnect", "gpu"],
        "hashtags": ["#Nvidia", "#AI"],
        "music_mood": "tense electronic",
        "category": "Semiconductors",
    }
    text = build_metadata_text(metadata)
    assert "Nvidia opens NVLink" in text
    assert "nvidia nvlink" in text and "gpu" in text
    assert "#Nvidia" in text and "#AI" in text
    assert "tense electronic" in text
    assert "Semiconductors" in text


def test_metadata_text_handles_empty_fields_gracefully():
    text = build_metadata_text({})
    assert "—" in text  # placeholders, not crashes or "None"
    assert "None" not in text


def test_metadata_text_respects_keyword_limit():
    metadata = {"target_keywords": [f"kw{i}" for i in range(20)]}
    text = build_metadata_text(metadata, keyword_limit=6)
    assert "kw5" in text and "kw6" not in text


def test_keyboard_has_approve_and_discard_with_matching_topic_id():
    kb = build_keyboard(topic_id=42)
    row = kb["inline_keyboard"][0]
    assert len(row) == 2
    assert row[0]["callback_data"] == "approve:42"
    assert row[1]["callback_data"] == "discard:42"
    assert "Approve" in row[0]["text"] and "Discard" in row[1]["text"]


def test_callback_data_round_trips():
    kb = build_keyboard(topic_id=777)
    for button in kb["inline_keyboard"][0]:
        action, topic_id = parse_callback_data(button["callback_data"])
        assert topic_id == 777
        assert action in ("approve", "discard")


@pytest.mark.parametrize("bad", ["", "approve", "approve:", "approve:abc", "delete:5", "5:approve"])
def test_callback_data_rejects_malformed_input(bad):
    with pytest.raises(ValueError):
        parse_callback_data(bad)


def test_decide_approve_from_pending():
    status, toast, changed = decide(TopicStatus.pending_approval, "approve")
    assert status == TopicStatus.approved
    assert changed
    assert toast == "Approved."


def test_decide_discard_from_pending():
    status, toast, changed = decide(TopicStatus.pending_approval, "discard")
    assert status == TopicStatus.discarded
    assert changed
    assert toast == "Discarded."


def test_decide_is_idempotent_on_double_press():
    """The real-world case this guards: a user double-taps Approve, or Telegram
    redelivers an update. The second press must not error or re-apply anything."""
    status, toast, changed = decide(TopicStatus.approved, "approve")
    assert status == TopicStatus.approved
    assert not changed
    assert "Already approved" in toast


def test_decide_rejects_a_press_on_a_topic_never_sent_for_approval():
    status, toast, changed = decide(TopicStatus.assets_ready, "approve")
    assert status == TopicStatus.assets_ready
    assert not changed
    assert "Not awaiting approval" in toast


def test_decide_rejects_unknown_action():
    with pytest.raises(ValueError):
        decide(TopicStatus.pending_approval, "delete")


def test_decide_discard_after_already_approved_does_not_flip_it():
    """Two different button presses racing (rare, but the update queue can reorder)
    must not let a Discard undo an already-recorded Approve."""
    status, _toast, changed = decide(TopicStatus.approved, "discard")
    assert status == TopicStatus.approved
    assert not changed
