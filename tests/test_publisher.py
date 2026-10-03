"""Publisher: HTML cleanup, photo/text paths, source links, group/channel targets."""
from __future__ import annotations

import asyncio
from typing import Any, Optional

import pytest
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile

from channelup.publisher import (
    _PHOTO_CAPTION_LIMIT,
    _clean_html,
    _strip_html,
    fetch_image_bytes,
    message_slots,
    prepare_body,
    publish,
)


class FakeBot:
    def __init__(self, fail_photo: Optional[Exception] = None,
                 fail_message: Optional[Exception] = None,
                 fail_message_once: Optional[Exception] = None):
        self.messages: list[dict[str, Any]] = []
        self.photos: list[dict[str, Any]] = []
        self._fail_photo = fail_photo
        self._fail_message = fail_message
        self._fail_message_once = fail_message_once

    async def send_message(self, chat, text, **kwargs):
        if self._fail_message_once is not None:
            err, self._fail_message_once = self._fail_message_once, None
            raise err
        if self._fail_message is not None:
            raise self._fail_message
        self.messages.append({"chat": chat, "text": text, **kwargs})

    async def send_photo(self, chat, photo=None, **kwargs):
        if self._fail_photo is not None:
            raise self._fail_photo
        self.photos.append({"chat": chat, "photo": photo, **kwargs})


class FakeResp:
    def __init__(self, status=200, body=b"\xff\xd8\xff",
                 content_type="image/jpeg"):
        self.status = status
        self.headers = {"Content-Type": content_type}
        self._body = body

    async def read(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSession:
    def __init__(self, resp: Optional[FakeResp] = None, error: Exception | None = None):
        self._resp = resp or FakeResp()
        self._error = error
        self.gets: list[str] = []

    def get(self, url, **kwargs):
        self.gets.append(url)
        if self._error is not None:
            raise self._error
        return self._resp


def run(coro):
    return asyncio.run(coro)


def _item(link="https://ex.com/a?q=1&x=y", image=None, **kw):
    data = {"title": "T", "link": link, "text": "body", "image": image}
    data.update(kw)
    return data


# ── HTML helpers ────────────────────────────────────────────────────────────

def test_clean_html_converts_p_br_and_keeps_allowed_tags():
    raw = "<p>Hello <b>world</b></p><br/><i>line</i><script>x</script>"
    out = _clean_html(raw)
    assert "<b>world</b>" in out
    assert "<i>line</i>" in out
    assert "<p>" not in out and "<br" not in out.lower()
    assert "<script>" not in out
    assert "x" in out  # inner text of stripped tag kept


def test_clean_html_escapes_anchor_hrefs():
    raw = '<a href="https://ex.com/a?q=1&x=y">click</a>'
    out = _clean_html(raw)
    assert 'href="https://ex.com/a?q=1&amp;x=y"' in out
    assert "click" in out


def test_strip_html_plain_fallback():
    assert _strip_html("<b>Hi</b> &amp; <br/>there") == "Hi & \nthere"


def test_prepare_body_appends_escaped_source_link():
    body = prepare_body(_item(link='https://ex.com/a?q=1&x="y"'), "Hello <p>x</p>")
    assert "Hello" in body and "x" in body
    assert 'href="https://ex.com/a?q=1&amp;x=&quot;y&quot;"' in body
    assert ">Source</a>" in body
    assert "Refrence" not in body


def test_prepare_body_skips_source_when_disabled():
    body = prepare_body(_item(), "Hello", append_source=False)
    assert "Source" not in body
    assert body == "Hello"


def test_message_slots_two_when_image_and_long_body():
    long = "x" * (_PHOTO_CAPTION_LIMIT + 10)
    assert message_slots(_item(image="https://img/a.jpg"), long) == 2
    assert message_slots(_item(image=None), long) == 1
    assert message_slots(_item(image="https://img/a.jpg"), "short") == 1


# ── publish paths ───────────────────────────────────────────────────────────

def test_publish_text_only_to_channel_and_group():
    async def scenario():
        bot = FakeBot()
        session = FakeSession()
        for target in ("@public_channel", "-1001234567890"):
            bot.messages.clear()
            n = await publish(bot, session, _item(), "<b>Hi</b>", target)
            assert n == 1
            assert bot.messages[0]["chat"] == target
            assert bot.messages[0]["parse_mode"] == "HTML"
            assert bot.messages[0]["disable_web_page_preview"] is True
            assert "<b>Hi</b>" in bot.messages[0]["text"]
            assert "Source" in bot.messages[0]["text"]
        return True

    assert run(scenario())


def test_publish_photo_with_caption():
    async def scenario():
        bot = FakeBot()
        session = FakeSession(FakeResp(body=b"imgdata", content_type="image/png"))
        n = await publish(
            bot, session, _item(image="https://cdn/x.png"), "Caption <b>ok</b>", "@ch"
        )
        assert n == 1
        assert len(bot.photos) == 1 and not bot.messages
        assert bot.photos[0]["parse_mode"] == "HTML"
        assert "Caption" in bot.photos[0]["caption"]
        assert isinstance(bot.photos[0]["photo"], BufferedInputFile)
        assert bot.photos[0]["photo"].filename == "image.png"
        return True

    assert run(scenario())


def test_publish_long_caption_splits_into_photo_then_message():
    async def scenario():
        bot = FakeBot()
        session = FakeSession()
        long = "L" * (_PHOTO_CAPTION_LIMIT + 50)
        n = await publish(
            bot, session, _item(image="https://cdn/a.jpg"), long, "-100999"
        )
        assert n == 2
        assert len(bot.photos) == 1
        assert "caption" not in bot.photos[0] or bot.photos[0].get("caption") is None
        assert len(bot.messages) == 1
        assert bot.messages[0]["chat"] == "-100999"
        assert long[:20] in bot.messages[0]["text"]
        return True

    assert run(scenario())


def test_publish_photo_failure_falls_back_to_text():
    async def scenario():
        err = TelegramAPIError(method="sendPhoto", message="Bad Request: wrong file")
        bot = FakeBot(fail_photo=err)
        session = FakeSession()
        n = await publish(
            bot, session, _item(image="https://cdn/a.jpg"), "Fallback", "@ch"
        )
        assert n == 1
        assert bot.photos == []
        assert len(bot.messages) == 1
        assert "Fallback" in bot.messages[0]["text"]
        return True

    assert run(scenario())


def test_publish_chat_not_found_is_fatal():
    async def scenario():
        err = TelegramAPIError(method="sendMessage", message="Bad Request: chat not found")
        bot = FakeBot(fail_message=err)
        session = FakeSession()
        with pytest.raises(TelegramAPIError):
            await publish(bot, session, _item(), "x", "@missing")
        return True

    assert run(scenario())


def test_publish_group_forbidden_is_fatal():
    async def scenario():
        err = TelegramAPIError(
            method="sendMessage",
            message="Forbidden: bot is not a member of the supergroup chat",
        )
        bot = FakeBot(fail_message=err)
        with pytest.raises(TelegramAPIError):
            await publish(bot, FakeSession(), _item(), "x", "-1001")
        return True

    assert run(scenario())


def test_publish_bad_html_retries_plain_text():
    async def scenario():
        err = TelegramAPIError(
            method="sendMessage",
            message="Bad Request: can't parse entities",
        )
        bot = FakeBot(fail_message_once=err)
        n = await publish(bot, FakeSession(), _item(), "<b>Broken", "@ch",
                          append_source=False)
        assert n == 1
        assert len(bot.messages) == 1
        assert bot.messages[0].get("parse_mode") is None
        assert "Broken" in bot.messages[0]["text"]
        return True

    assert run(scenario())


def test_publish_raw_mode_no_source_suffix():
    async def scenario():
        bot = FakeBot()
        text = 'Hello\n\n🔗 <a href="https://t.me/chan">https://t.me/chan</a>'
        await publish(bot, FakeSession(), _item(), text, "@ch", append_source=False)
        assert "Source" not in bot.messages[0]["text"]
        assert "https://t.me/chan" in bot.messages[0]["text"]
        return True

    assert run(scenario())


def test_fetch_image_bytes_rejects_non_image():
    async def scenario():
        session = FakeSession(FakeResp(content_type="text/html", body=b"<html>"))
        assert await fetch_image_bytes(session, "https://ex.com/page") is None
        return True

    assert run(scenario())


def test_fetch_image_bytes_accepts_extension_without_content_type():
    async def scenario():
        session = FakeSession(FakeResp(content_type="application/octet-stream",
                                       body=b"abc"))
        got = await fetch_image_bytes(session, "https://cdn/pic.webp")
        assert got is not None
        data, name = got
        assert data == b"abc" and name == "image.webp"
        return True

    assert run(scenario())
