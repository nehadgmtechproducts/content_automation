"""Thin wrapper over the Telegram Bot API — plain REST/JSON, no SDK needed.

Every method posts to https://api.telegram.org/bot<TOKEN>/<Method> and expects back
{"ok": true, "result": ...} or {"ok": false, "description": "..."}. Kept deliberately
raw (same pattern as the Firecrawl/Pexels/ElevenLabs adapters) rather than pulling in a
bot-framework dependency for what is five HTTP calls.

`session` is injectable so tests can pass an httpx.Client(transport=httpx.MockTransport)
and exercise real request-building logic without a network call or a real bot token.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx

API_ROOT = "https://api.telegram.org"
MAX_CAPTION_CHARS = 1024        # Telegram's limit on photo/video captions
MAX_MESSAGE_CHARS = 4096        # limit on plain sendMessage text
MAX_UPLOAD_BYTES = 50 * 1024 * 1024   # bot API's file upload ceiling


class TelegramError(RuntimeError):
    pass


def trim(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


class TelegramClient:
    def __init__(self, token: str, session: httpx.Client | None = None):
        if not token:
            raise TelegramError("no Telegram bot token configured")
        self.token = token
        self._base = f"{API_ROOT}/bot{token}"
        # getUpdates long-polls for up to 25s server-side; the client timeout must
        # comfortably exceed that or every poll looks like a network failure.
        self._client = session or httpx.Client(timeout=40.0)

    def _call(self, method: str, data: dict | None = None, files: dict | None = None) -> dict:
        resp = self._client.post(f"{self._base}/{method}", data=data, files=files)
        try:
            body = resp.json()
        except ValueError as exc:
            raise TelegramError(
                f"{method}: non-JSON response (HTTP {resp.status_code}): {resp.text[:200]}"
            ) from exc
        if not body.get("ok"):
            raise TelegramError(f"{method} failed: {body.get('description', body)}")
        return body["result"]

    def get_me(self) -> dict:
        """Cheap connectivity check — confirms the token is valid."""
        return self._call("getMe")

    def send_photo(self, chat_id: str, photo_path: str, caption: str = "",
                   reply_markup: dict | None = None) -> dict:
        data = {"chat_id": chat_id, "caption": trim(caption, MAX_CAPTION_CHARS)}
        if reply_markup is not None:
            data["reply_markup"] = json.dumps(reply_markup)
        with open(photo_path, "rb") as fh:
            files = {"photo": (Path(photo_path).name, fh, "image/jpeg")}
            return self._call("sendPhoto", data=data, files=files)

    def send_video(self, chat_id: str, video_path: str, caption: str = "",
                   reply_markup: dict | None = None) -> dict:
        size = Path(video_path).stat().st_size
        if size > MAX_UPLOAD_BYTES:
            raise TelegramError(
                f"{video_path} is {size / 1e6:.1f}MB, over the bot API's "
                f"{MAX_UPLOAD_BYTES / 1e6:.0f}MB upload limit"
            )
        data = {
            "chat_id": chat_id, "caption": trim(caption, MAX_CAPTION_CHARS),
            "supports_streaming": "true",
        }
        if reply_markup is not None:
            data["reply_markup"] = json.dumps(reply_markup)
        with open(video_path, "rb") as fh:
            files = {"video": (Path(video_path).name, fh, "video/mp4")}
            return self._call("sendVideo", data=data, files=files)

    def send_message(self, chat_id: str, text: str, reply_markup: dict | None = None) -> dict:
        data = {"chat_id": chat_id, "text": trim(text, MAX_MESSAGE_CHARS)}
        if reply_markup is not None:
            data["reply_markup"] = json.dumps(reply_markup)
        return self._call("sendMessage", data=data)

    def edit_message_caption(self, chat_id: str, message_id: int, caption: str,
                             reply_markup: dict | None = None) -> dict:
        data = {
            "chat_id": chat_id, "message_id": message_id,
            "caption": trim(caption, MAX_CAPTION_CHARS),
            # Explicit empty keyboard clears the buttons — omitting reply_markup here
            # would leave the old (now-stale) Approve/Discard buttons in place.
            "reply_markup": json.dumps(reply_markup if reply_markup is not None else {"inline_keyboard": []}),
        }
        return self._call("editMessageCaption", data=data)

    def answer_callback_query(self, callback_query_id: str, text: str = "",
                              show_alert: bool = False) -> dict:
        data = {
            "callback_query_id": callback_query_id, "text": trim(text, 200),
            "show_alert": "true" if show_alert else "false",
        }
        return self._call("answerCallbackQuery", data=data)

    def get_updates(self, offset: int | None = None, timeout: int = 25) -> list[dict]:
        data = {"timeout": timeout, "allowed_updates": json.dumps(["callback_query"])}
        if offset is not None:
            data["offset"] = offset
        return self._call("getUpdates", data=data)

    def close(self) -> None:
        self._client.close()
