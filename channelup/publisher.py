"""Publishing to a single Telegram channel or group."""
from __future__ import annotations

import html
import logging
import re
from typing import Optional
from urllib.parse import urlparse

import aiohttp
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile

log = logging.getLogger("channelup.publisher")

_SOURCE_LINK = '\n\n🔗 <a href="{link}">Source</a>'
_PHOTO_CAPTION_LIMIT = 1024
_TEXT_LIMIT = 4096

# Telegram HTML allowlist (subset we keep through). Others are stripped to text.
_ALLOWED_TAG_RE = re.compile(
    r"</?(?:b|strong|i|em|u|ins|s|strike|del|code|pre|tg-spoiler)(?:\s[^>]*)?>",
    re.IGNORECASE,
)
_A_TAG_RE = re.compile(
    r'<a\s+href=(["\'])(.*?)\1\s*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_P_OPEN_RE = re.compile(r"<p(?:\s[^>]*)?>", re.IGNORECASE)
_P_CLOSE_RE = re.compile(r"</p\s*>", re.IGNORECASE)


def _filename_for(url: str, content_type: str) -> str:
    path = urlparse(url).path.lower()
    for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        if path.endswith(ext):
            return f"image{ext if ext != '.jpeg' else '.jpg'}"
    ct = (content_type or "").split(";")[0].strip().lower()
    return {
        "image/jpeg": "image.jpg",
        "image/png": "image.png",
        "image/webp": "image.webp",
        "image/gif": "image.gif",
    }.get(ct, "image.jpg")


async def fetch_image_bytes(
    session: aiohttp.ClientSession, url: str
) -> Optional[tuple[bytes, str]]:
    """Download an image. Returns ``(bytes, filename)`` or ``None``."""
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return None
            content_type = resp.headers.get("Content-Type", "")
            if not (
                content_type.startswith("image/")
                or url.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif"))
            ):
                return None
            data = await resp.read()
            if not data:
                return None
            return data, _filename_for(url, content_type)
    except Exception as e:
        log.warning("Failed to download image %s: %s", url, e)
    return None


def _clean_html(text: str) -> str:
    """Normalize LLM/HTML into Telegram-safe HTML.

    Keeps ``<b>/<i>/<u>/<s>/<code>/<pre>/<tg-spoiler>/<a href>``; converts
    ``<p>/<br>`` to newlines; strips every other tag (content kept).
    """
    text = _BR_RE.sub("\n", text)
    text = _P_CLOSE_RE.sub("\n\n", text)
    text = _P_OPEN_RE.sub("", text)

    # Preserve anchors with escaped hrefs, then restore after stripping.
    anchors: list[str] = []

    def _stash_a(m: re.Match) -> str:
        href = html.escape(m.group(2), quote=True)
        inner = _TAG_RE.sub("", m.group(3))
        anchors.append(f'<a href="{href}">{inner}</a>')
        return f"\x00A{len(anchors) - 1}\x00"

    text = _A_TAG_RE.sub(_stash_a, text)

    kept: list[str] = []

    def _keep_allowed(m: re.Match) -> str:
        tag = m.group(0)
        # Normalize strong/em aliases Telegram accepts as bold/italic synonyms.
        kept.append(tag)
        return f"\x00T{len(kept) - 1}\x00"

    text = _ALLOWED_TAG_RE.sub(_keep_allowed, text)
    text = _TAG_RE.sub("", text)  # drop remaining tags

    # Restore highest indices first so T1 cannot clobber T10, etc.
    for i in range(len(kept) - 1, -1, -1):
        text = text.replace(f"\x00T{i}\x00", kept[i])
    for i in range(len(anchors) - 1, -1, -1):
        text = text.replace(f"\x00A{i}\x00", anchors[i])

    # Collapse 3+ newlines left by block-tag removal.
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _strip_html(text: str) -> str:
    """Plain-text fallback when Telegram rejects HTML entities."""
    text = _BR_RE.sub("\n", text)
    text = _P_CLOSE_RE.sub("\n\n", text)
    text = _P_OPEN_RE.sub("", text)
    text = _TAG_RE.sub("", text)
    return html.unescape(text).strip()


def prepare_body(item: dict, text: str, append_source: bool = True) -> str:
    """Build the final post body (HTML-cleaned, optional source link)."""
    body = _clean_html(text)
    if append_source and item.get("link"):
        link = html.escape(str(item["link"]), quote=True)
        body += _SOURCE_LINK.format(link=link)
    return body


def message_slots(item: dict, text: str, append_source: bool = True) -> int:
    """How many Telegram messages a publish may send (1, or 2 when photo+long caption).

    Conservative: counts 2 whenever an image URL is present and the body exceeds
    the caption limit, even if the image download later fails (still ≤ Telegram cap).
    """
    body = prepare_body(item, text, append_source)
    if item.get("image") and len(body) > _PHOTO_CAPTION_LIMIT:
        return 2
    return 1


def _fatal_chat_error(exc: TelegramAPIError) -> bool:
    msg = (exc.message or str(exc)).lower()
    return any(
        needle in msg
        for needle in (
            "chat not found",
            "bot is not a member",
            "need administrator rights",
            "not enough rights",
            "have no rights to send",
            "forbidden: bot was kicked",
            "forbidden: bot is not a member",
            "forbidden: can't write",
        )
    )


async def _send_html_message(bot: Bot, chat: str, body: str) -> None:
    """Send HTML text; on parse/entity errors, retry as plain text."""
    clipped = body[:_TEXT_LIMIT]
    try:
        await bot.send_message(
            chat, clipped, parse_mode="HTML", disable_web_page_preview=True
        )
    except TelegramAPIError as e:
        if _fatal_chat_error(e):
            raise
        plain = _strip_html(clipped)[:_TEXT_LIMIT]
        log.warning("HTML send failed (%s); retrying as plain text", e.message)
        await bot.send_message(chat, plain, disable_web_page_preview=True)


async def publish(
    bot: Bot,
    session: aiohttp.ClientSession,
    item: dict,
    text: str,
    telegram_target: str,
    append_source: bool = True,
) -> int:
    """Post one item to ``telegram_target`` (channel or group).

    Returns the number of Telegram messages actually sent (1 or 2).
    ``append_source`` controls whether the item URL is appended. ``raw``-mode
    posts already carry their own ``target_link`` and pass ``False``.
    """
    body = prepare_body(item, text, append_source)

    if item.get("image"):
        fetched = await fetch_image_bytes(session, item["image"])
        if fetched:
            img_bytes, filename = fetched
            photo_file = BufferedInputFile(img_bytes, filename=filename)
            if len(body) <= _PHOTO_CAPTION_LIMIT:
                try:
                    await bot.send_photo(
                        telegram_target,
                        photo_file,
                        caption=body,
                        parse_mode="HTML",
                    )
                    return 1
                except TelegramAPIError as e:
                    if _fatal_chat_error(e):
                        raise
                    log.warning("Photo sending failed (%s), sending as text", e.message)
            else:
                try:
                    await bot.send_photo(telegram_target, photo_file)
                except TelegramAPIError as e:
                    if _fatal_chat_error(e):
                        raise
                    log.warning("Photo sending failed (%s), sending as text", e.message)
                else:
                    await _send_html_message(bot, telegram_target, body)
                    return 2

    await _send_html_message(bot, telegram_target, body)
    return 1
