"""Preview-card content and the approve/discard decision logic.

Split from telegram_client.py on purpose: everything here is a pure function — no
network, no database — so it's fully unit-testable, and it's the layer to touch if a
Slack adapter is ever added alongside Telegram (same split as app/providers/).
"""
from __future__ import annotations

from app.approval.telegram_client import MAX_CAPTION_CHARS, MAX_MESSAGE_CHARS, trim
from app.models import TopicStatus


def build_photo_caption(article: dict) -> str:
    """Hero image caption — covers the spec's 'Article Title & Summary' bullet."""
    title = article.get("title", "Untitled")
    dek = article.get("dek", "")
    body = f"\U0001f4f0 {title}"
    if dek:
        body += f"\n\n{dek}"
    return trim(body, MAX_CAPTION_CHARS)


def build_video_caption(article: dict, entity: str = "") -> str:
    """Short caption on the video message — this is the one the buttons attach to."""
    title = article.get("title", "Untitled")
    label = f" — {entity}" if entity else ""
    return trim(f"\U0001f3ac 45-second video draft{label}\n{title}", MAX_CAPTION_CHARS)


def build_metadata_text(metadata: dict, keyword_limit: int = 6) -> str:
    """Covers the spec's 'Optimized Metadata' bullet: thumbnail, keywords, hashtags,
    music suggestion. Sent as its own message — the metadata block routinely runs
    longer than a caption allows once hashtags are included."""
    keywords = metadata.get("target_keywords", [])[:keyword_limit]
    hashtags = metadata.get("hashtags", [])
    lines = [
        "\U0001f4ca Suggested metadata",
        f"Thumbnail text: {metadata.get('thumbnail_text') or '—'}",
        f"Keywords: {', '.join(keywords) or '—'}",
        f"Hashtags: {' '.join(hashtags) or '—'}",
        f"Music mood: {metadata.get('music_mood') or '—'}",
        f"Category: {metadata.get('category') or '—'}",
    ]
    return trim("\n".join(lines), MAX_MESSAGE_CHARS)


def build_keyboard(topic_id: int) -> dict:
    """Inline keyboard attached to the video message. callback_data stays under
    Telegram's 64-byte limit by design — 'approve:' + a topic id is always tiny."""
    return {
        "inline_keyboard": [[
            {"text": "✅ Approve & Deploy", "callback_data": f"approve:{topic_id}"},
            {"text": "❌ Discard", "callback_data": f"discard:{topic_id}"},
        ]]
    }


def parse_callback_data(data: str) -> tuple[str, int]:
    """Inverse of build_keyboard's callback_data. Raises on anything unrecognized —
    the caller must treat that as 'ignore this button press', not crash the poller."""
    action, sep, raw_id = data.partition(":")
    if not sep or action not in ("approve", "discard") or not raw_id.isdigit():
        raise ValueError(f"unrecognized callback_data: {data!r}")
    return action, int(raw_id)


# Maps a decision action to the resulting status, kept as a pure function so the state
# machine is testable without a database. Called from app.approval.service, which owns
# the actual read/write.
_ACTION_TO_STATUS = {"approve": TopicStatus.approved, "discard": TopicStatus.discarded}


def decide(current_status: TopicStatus, action: str) -> tuple[TopicStatus, str, bool]:
    """Apply one button press to a topic's current status.

    Returns (new_status, toast_message_for_the_user, changed). `changed` is False for
    a topic that isn't awaiting approval — already decided (a double-tap, or two
    callback deliveries for one press) or never reached that stage — so a repeat press
    answers with a clear toast instead of erroring or double-applying the decision.
    """
    if action not in _ACTION_TO_STATUS:
        raise ValueError(f"unknown action {action!r}")

    if current_status != TopicStatus.pending_approval:
        if current_status in (TopicStatus.approved, TopicStatus.discarded):
            return current_status, f"Already {current_status.value}.", False
        return current_status, f"Not awaiting approval (status: {current_status.value}).", False

    new_status = _ACTION_TO_STATUS[action]
    if new_status is TopicStatus.approved:
        toast = "Approved. (Publishing isn't built yet — this only records the decision.)"
    else:
        toast = "Discarded."
    return new_status, toast, True
