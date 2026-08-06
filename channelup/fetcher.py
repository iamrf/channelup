"""RSS fetching and normalization. Pure functions — no DB, no network sockets held
past the fetch — so they run safely under ``asyncio.to_thread``."""
from __future__ import annotations

import html
import logging
import re
from typing import Optional

import feedparser

log = logging.getLogger("channelup.fetcher")

TAGS = re.compile(r"<[^>]+>")
WS = re.compile(r"[ \t]{2,}")


def clean(raw: str) -> str:
    """Strip HTML tags, unescape entities, collapse whitespace."""
    return WS.sub(" ", html.unescape(TAGS.sub(" ", raw or ""))).strip()


def image_of(entry) -> Optional[str]:
    """Return the first usable image URL for an entry, if any."""
    for m in entry.get("media_content", []) + entry.get("media_thumbnail", []):
        if m.get("url"):
            return m["url"]
    for e in entry.get("enclosures", []):
        if e.get("type", "").startswith("image/"):
            return e.get("href")
    if m := re.search(r'<img[^>]+src="([^"]+)"', entry.get("summary", "")):
        return m.group(1)
    return None


def parse_source(source: str) -> list[dict]:
    """Parse one feed (URL or XML string) into normalized item dicts."""
    out: list[dict] = []
    parsed = feedparser.parse(source)
    for e in parsed.entries:
        link = e.get("link") or e.get("id")
        if not link:
            continue
        body = clean((e.get("content") or [{}])[0].get("value") or e.get("summary", ""))
        out.append({
            "title": clean(e.get("title", "")),
            "link": link,
            "text": body[:4000],
            "image": image_of(e),
        })
    return out


def fetch_sources(sources: list[str]) -> list[dict]:
    """Fetch a list of feeds, skipping any that fail without aborting the rest."""
    items: list[dict] = []
    for url in sources:
        try:
            items += parse_source(url)
        except Exception:
            log.exception("fetch failed: %s", url)
    return items