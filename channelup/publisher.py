"""Publishing to a single Telegram channel."""
from __future__ import annotations

import logging
from typing import Optional

import aiohttp
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile

log = logging.getLogger("channelup.publisher")

_SOURCE_LINK = "\n\n🔗 <a href=\"{link}\">منبع</a>"
_PHOTO_CAPTION_LIMIT = 1024
_TEXT_LIMIT = 4096


async def fetch_image_bytes(session: aiohttp.ClientSession, url: str) -> Optional[bytes]:
    try:
        async with session.get(url, timeout=10) as resp:
            if resp.status == 200:
                content_type = resp.headers.get("Content-Type", "")
                if content_type.startswith("image/") or url.lower().endswith(
                    (".jpg", ".jpeg", ".png", ".webp")
                ):
                    return await resp.read()
    except Exception as e:
        log.warning("Failed to download image %s: %s", url, e)
    return None


def _clean_html(text: str) -> str:
    for tag in ("<p>", "</p>", "<br>", "<br/>"):
        text = text.replace(tag, "\n\n" if tag in ("</p>", "<br>", "<br/>") else "")
    return text


async def publish(
    bot: Bot, session: aiohttp.ClientSession, item: dict, text: str, telegram_target: str,
    append_source: bool = True,
) -> None:
    """Post one item to ``telegram_target`` (photo + caption when available).

    ``append_source`` controls whether the source link is appended. ``raw``-mode
    posts already carry their own ``target_link`` and pass ``False``.
    """
    clean_text = _clean_html(text).strip()
    clean_text += _SOURCE_LINK.format(link=item["link"]) if append_source else ""
    body = clean_text

    sent_with_photo = False
    if item.get("image"):
        img_bytes = await fetch_image_bytes(session, item["image"])
        if img_bytes:
            try:
                if len(body) <= _PHOTO_CAPTION_LIMIT:
                    photo_file = BufferedInputFile(img_bytes, filename="image.jpg")
                    await bot.send_photo(telegram_target, photo_file, caption=body, parse_mode="HTML")
                else:
                    photo_file = BufferedInputFile(img_bytes, filename="image.jpg")
                    await bot.send_photo(telegram_target, photo_file)
                    await bot.send_message(telegram_target, body[:_TEXT_LIMIT], parse_mode="HTML",
                                           disable_web_page_preview=True)
                sent_with_photo = True
            except TelegramAPIError as e:
                if "chat not found" in str(e).lower():
                    raise
                log.warning("Photo sending failed (%s), sending as text", e.message)

    if not sent_with_photo:
        await bot.send_message(telegram_target, body[:_TEXT_LIMIT], parse_mode="HTML",
                               disable_web_page_preview=False)