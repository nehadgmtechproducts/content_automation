"""Exercises real request-building against a fake transport — no network, no bot token.

httpx.MockTransport intercepts at the HTTP layer, so every method here proves the
client builds the right URL, method, and payload shape, and correctly parses Telegram's
{"ok": true/false, ...} envelope — the parts that are actually worth verifying, since
the endpoints themselves are Telegram's contract, not ours.
"""
import json

import httpx
import pytest

from app.approval.telegram_client import MAX_UPLOAD_BYTES, TelegramClient, TelegramError


def make_client(handler) -> TelegramClient:
    transport = httpx.MockTransport(handler)
    return TelegramClient("TEST:TOKEN", session=httpx.Client(transport=transport))


def test_get_me_hits_the_right_url_and_parses_result():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        return httpx.Response(200, json={"ok": True, "result": {"id": 1, "username": "test_bot"}})

    result = make_client(handler).get_me()
    assert captured["url"] == "https://api.telegram.org/botTEST:TOKEN/getMe"
    assert captured["method"] == "POST"
    assert result["username"] == "test_bot"


def test_ok_false_response_raises_with_telegram_description():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "description": "Unauthorized"})

    with pytest.raises(TelegramError, match="Unauthorized"):
        make_client(handler).get_me()


def test_non_json_response_raises_cleanly():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="<html>Bad Gateway</html>")

    with pytest.raises(TelegramError, match="502"):
        make_client(handler).get_me()


def test_send_photo_uploads_the_file_and_caption(tmp_path):
    photo = tmp_path / "hero.jpg"
    photo.write_bytes(b"\xff\xd8\xff fake jpeg bytes")
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 101}})

    result = make_client(handler).send_photo("-100123", str(photo), caption="Nvidia opens NVLink")
    assert captured["url"].endswith("/sendPhoto")
    assert b"hero.jpg" in captured["body"]
    assert b"Nvidia opens NVLink" in captured["body"]
    assert b"fake jpeg bytes" in captured["body"]
    assert result["message_id"] == 101


def test_send_video_attaches_the_inline_keyboard(tmp_path):
    video = tmp_path / "final.mp4"
    video.write_bytes(b"fake mp4 bytes")
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 202}})

    keyboard = {"inline_keyboard": [[{"text": "Approve", "callback_data": "approve:5"}]]}
    make_client(handler).send_video("-100123", str(video), caption="draft", reply_markup=keyboard)
    assert b"approve:5" in captured["body"]
    assert b"supports_streaming" in captured["body"]


def test_send_video_refuses_a_file_over_the_upload_limit(tmp_path):
    video = tmp_path / "huge.mp4"
    video.write_bytes(b"\x00")
    # Don't actually allocate 50MB in a test — truncate to set just the reported size.
    with open(video, "r+b") as fh:
        fh.truncate(MAX_UPLOAD_BYTES + 1)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("should never reach the network for an oversized file")

    with pytest.raises(TelegramError, match="upload limit"):
        make_client(handler).send_video("-100123", str(video))


def test_edit_message_caption_clears_keyboard_by_default():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content.decode()
        return httpx.Response(200, json={"ok": True, "result": {}})

    make_client(handler).edit_message_caption("-100123", 202, "Approved!")
    import urllib.parse
    parsed = urllib.parse.parse_qs(captured["body"])
    markup = json.loads(parsed["reply_markup"][0])
    assert markup == {"inline_keyboard": []}


def test_answer_callback_query_sends_the_toast_text():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content.decode()
        return httpx.Response(200, json={"ok": True, "result": True})

    make_client(handler).answer_callback_query("cbq123", text="Approved.")
    assert "Approved" in captured["body"]
    assert "cbq123" in captured["body"]


def test_get_updates_passes_offset_and_timeout():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content.decode()
        return httpx.Response(200, json={"ok": True, "result": [{"update_id": 5}]})

    result = make_client(handler).get_updates(offset=42, timeout=25)
    assert "offset=42" in captured["body"]
    assert "timeout=25" in captured["body"]
    assert result == [{"update_id": 5}]


def test_get_updates_with_no_offset_omits_it():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content.decode()
        return httpx.Response(200, json={"ok": True, "result": []})

    make_client(handler).get_updates()
    assert "offset" not in captured["body"]


def test_missing_token_fails_fast_before_any_request():
    with pytest.raises(TelegramError, match="no Telegram bot token"):
        TelegramClient("")


def test_cli_logging_never_emits_the_bot_token(caplog):
    """Telegram puts the token in the URL path and httpx logs URLs at INFO."""
    import logging

    from app.cli import _setup_logging

    _setup_logging(verbose=True)
    for name in ("httpx", "httpcore"):
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING
