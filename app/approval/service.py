"""Stage 5 orchestration: push a preview card, and process the button press back.

Two directions of traffic:
  push_for_approval()  — our DB + rendered assets  -> three Telegram messages
  poll_once()          — Telegram's button presses  -> our DB status update

Polling (not a webhook) is deliberate: it needs no public HTTPS endpoint, no domain, no
TLS cert — the whole point of this project is to run on a VPS or a laptop. getUpdates
long-polls for up to 25s per call, so a tight loop calling poll_once() is not busy-waiting.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.approval.cards import (
    build_keyboard, build_metadata_text, build_photo_caption, build_video_caption, decide,
    parse_callback_data,
)
from app.approval.telegram_client import TelegramClient, TelegramError
from app.db import session_scope
from app.models import ApprovalCard, AssetBundle, Draft, TelegramCursor, Topic, TopicStatus
from app.settings import get_settings

log = logging.getLogger(__name__)


def is_configured() -> bool:
    settings = get_settings()
    return bool(settings.telegram_bot_token and settings.telegram_chat_id)


def _client() -> TelegramClient:
    return TelegramClient(get_settings().telegram_bot_token)


def push_for_approval(topic_id: int, client: TelegramClient | None = None) -> dict:
    """Send the three-message preview card and move the topic to pending_approval.

    Message layout (three separate sends, not one — a video caption tops out at 1024
    characters, nowhere near enough for title + summary + keywords + hashtags):
      1. hero image  — caption: title + summary        (spec: "Article Title & Summary" + "Hero Image")
      2. video       — caption: short label + buttons   (spec: "45-Second Rendered Video" + action buttons)
      3. text        — full metadata block              (spec: "Optimized Metadata")
    Only the video message (#2) carries the buttons and is the one later edited in
    place once a decision comes back — see process_decision().
    """
    if not is_configured():
        return {"sent": False, "reason": "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set"}

    with session_scope() as s:
        topic = s.get(Topic, topic_id)
        draft = s.scalar(select(Draft).where(Draft.topic_id == topic_id))
        bundle = s.scalar(select(AssetBundle).where(AssetBundle.topic_id == topic_id))
        if topic is None or draft is None or bundle is None:
            return {"sent": False, "reason": "topic, draft, or asset bundle not found"}
        article, metadata = dict(draft.article), dict(draft.meta)
        entity = topic.primary_entity
        hero_path, video_path = bundle.hero_image, bundle.video

    settings = get_settings()
    chat_id = settings.telegram_chat_id
    client = client or _client()

    try:
        photo_res = client.send_photo(chat_id, hero_path, build_photo_caption(article))
        video_res = client.send_video(
            chat_id, video_path, build_video_caption(article, entity),
            reply_markup=build_keyboard(topic_id),
        )
        meta_res = client.send_message(chat_id, build_metadata_text(metadata))
    except TelegramError as exc:
        log.error("failed to push approval card for topic %s: %s", topic_id, exc)
        return {"sent": False, "reason": str(exc)}

    with session_scope() as s:
        s.query(ApprovalCard).filter(ApprovalCard.topic_id == topic_id).delete()
        s.add(ApprovalCard(
            topic_id=topic_id, chat_id=str(chat_id), message_id=video_res["message_id"],
            photo_message_id=photo_res.get("message_id"),
            metadata_message_id=meta_res.get("message_id"),
        ))
        topic = s.get(Topic, topic_id)
        topic.status = TopicStatus.pending_approval
        topic.error = ""

    log.info("pushed approval card for topic %s (message_id=%s)", topic_id, video_res["message_id"])
    return {"sent": True, "message_id": video_res["message_id"]}


def process_decision(update: dict, client: TelegramClient | None = None,
                     publisher=None) -> dict:
    """Handle one Telegram update. Ignores anything that isn't a button press on a
    card we sent, rather than raising — the poller sees plenty of update shapes."""
    callback = update.get("callback_query")
    if not callback:
        return {"handled": False, "reason": "not a callback_query update"}

    client = client or _client()
    if publisher is None:
        from app.publishers.web import publish_topic as publisher
    settings = get_settings()
    callback_id = callback["id"]
    message = callback.get("message", {})
    chat_id = message.get("chat", {}).get("id")

    if str(chat_id) != str(settings.telegram_chat_id):
        # Belt-and-braces: a private bot in one chat should never see a callback query
        # from anywhere else, but this fails closed rather than trusting the payload.
        client.answer_callback_query(callback_id, "Not authorized for this chat.")
        return {"handled": False, "reason": "chat_id mismatch"}

    try:
        action, topic_id = parse_callback_data(callback.get("data", ""))
    except ValueError as exc:
        client.answer_callback_query(callback_id, "Unrecognized action.")
        return {"handled": False, "reason": str(exc)}

    with session_scope() as s:
        topic = s.get(Topic, topic_id)
        if topic is None:
            client.answer_callback_query(callback_id, "Topic not found.")
            return {"handled": False, "reason": "topic not found"}

        new_status, toast, changed = decide(topic.status, action)
        if changed:
            topic.status = new_status
            card = s.scalar(select(ApprovalCard).where(ApprovalCard.topic_id == topic_id))
            if card is not None:
                card.decision = action
                card.decided_at = datetime.now(timezone.utc)

    # Approve -> publish to the website. Done before answering the callback so the
    # toast and the edited caption can say what actually happened.
    published: dict | None = None
    if changed and action == "approve":
        published = publisher(topic_id)
        if published.get("published"):
            toast = "Approved and published to the website."
        else:
            toast = "Approved, but website publish failed."
            log.error("topic %s approved but not published: %s", topic_id, published.get("reason"))

    client.answer_callback_query(callback_id, toast)

    if changed and message.get("message_id") is not None:
        if action != "approve":
            icon = "❌ Discarded"
        elif published and published.get("published"):
            icon = f"✅ Approved · 🌐 Published ({published['path']})"
        else:
            icon = f"✅ Approved · ⚠️ website publish failed: {(published or {}).get('reason', '')[:200]}"
        caption = f"{message.get('caption', '')}\n\n{icon}".strip()
        client.edit_message_caption(chat_id, message["message_id"], caption)

    log.info("topic %s: %s (changed=%s)", topic_id, action, changed)
    return {"handled": True, "action": action, "topic_id": topic_id, "changed": changed,
            "published": published}


def poll_once(client: TelegramClient | None = None) -> int:
    """One getUpdates round-trip. Returns how many updates were processed."""
    client = client or _client()
    with session_scope() as s:
        cursor = s.get(TelegramCursor, 1)
        offset = (cursor.last_update_id + 1) if cursor else None

    updates = client.get_updates(offset=offset, timeout=25)
    max_seen = None
    for update in updates:
        try:
            process_decision(update, client)
        except TelegramError as exc:
            # A bad button press shouldn't stop the cursor from advancing past it —
            # that would wedge the poller replaying the same broken update forever.
            log.error("error processing update %s: %s", update.get("update_id"), exc)
        max_seen = update["update_id"]

    if max_seen is not None:
        with session_scope() as s:
            cursor = s.get(TelegramCursor, 1)
            if cursor is None:
                s.add(TelegramCursor(id=1, last_update_id=max_seen))
            else:
                cursor.last_update_id = max(cursor.last_update_id, max_seen)

    return len(updates)


def poll_forever(stop_after: int | None = None) -> None:
    """Run poll_once() in a loop. Each call blocks up to 25s server-side, so this is
    a long-poll loop, not a busy-wait. `stop_after` is for tests/scripts only."""
    client = _client()
    n = 0
    try:
        while stop_after is None or n < stop_after:
            poll_once(client)
            n += 1
    finally:
        client.close()
