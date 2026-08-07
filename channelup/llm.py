"""LLM interaction: single-item rewrite and curate batch selection.

All calls funnel through ``_chat`` so retry/backoff and provider-specific payload
shaping live in one place. The Gemini default model is 2.5 Flash-Lite.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Callable

import aiohttp

from .config import Config

log = logging.getLogger("channelup.llm")

RETRY_STATUSES = (429, 500, 502, 503, 504)
DEFAULT_BASES = {
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
}

_SELECT_PROMPT = """You are a news editor for a Telegram channel writing in {language}.
Below are candidate stories from RSS feeds. Pick the top {n} that would be most
engaging for the audience (impact, novelty, relevance). Return ONLY a JSON array of
source URLs you chose, e.g. ["https://a/1","https://b/2"]. No commentary."""


async def _chat(session: aiohttp.ClientSession, cfg: Config, system: str, user: str) -> str:
    if cfg.llm_provider == "gemini":
        base = cfg.llm_base_url or "https://generativelanguage.googleapis.com/v1beta"
        url = f"{base}/models/{cfg.llm_model}:generateContent"
        headers = {"x-goog-api-key": cfg.llm_api_key, "Content-Type": "application/json"}
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
        }
        pick: Callable[[dict], str] = lambda d: d["candidates"][0]["content"]["parts"][0]["text"]
    else:
        base = cfg.llm_base_url or DEFAULT_BASES.get(cfg.llm_provider, DEFAULT_BASES["openai"])
        url = f"{base}/chat/completions"
        headers = {"Authorization": f"Bearer {cfg.llm_api_key}", "Content-Type": "application/json"}
        payload = {
            "model": cfg.llm_model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        pick = lambda d: d["choices"][0]["message"]["content"]

    status = None
    for attempt in range(3):
        try:
            async with session.post(url, json=payload, headers=headers, timeout=60) as r:
                status = r.status
                if status in RETRY_STATUSES:
                    await asyncio.sleep(2 ** attempt * 5)
                    continue
                r.raise_for_status()
                data = await r.json()
                return pick(data).strip()
        except asyncio.TimeoutError:
            log.warning("LLM request timed out (attempt %d)", attempt + 1)
            await asyncio.sleep(2 ** attempt * 3)
        except Exception:
            if attempt == 2:
                raise
            log.exception("LLM request failed (attempt %d)", attempt + 1)
            await asyncio.sleep(2 ** attempt * 3)

    raise RuntimeError(f"LLM unavailable after retries (HTTP {status})")


async def rewrite(session: aiohttp.ClientSession, item: dict, cfg: Config, system_prompt: str) -> str:
    """Rewrite one item. Raises RuntimeError after retries are exhausted."""
    user = f"Title: {item['title']}\n\n{item['text']}\n\nSource: {item['link']}"
    return await _chat(session, cfg, system_prompt, user)


def _parse_selected_links(response: str, items: list[dict]) -> list[str]:
    """Best-effort parse of the LLM's JSON/URL answer back to source links."""
    try:
        parsed = json.loads(response)
        if isinstance(parsed, list):
            return [str(x) for x in parsed]
    except (ValueError, TypeError):
        pass
    # fallback: any URLs that actually belong to the candidate set
    wanted = {i["link"] for i in items}
    found = re.findall(r'https?://[^\s"\'\]\[,}]+', response)
    return [u.rstrip(".,)") for u in found if u.rstrip(".,)") in wanted]


async def select_top(session: aiohttp.ClientSession, items: list[dict], cfg: Config,
                     language: str, top_n: int) -> list[dict]:
    """Ask the LLM which candidates are most engaging; return the selected ones.

    Falls back to the first ``top_n`` items if the model answer cannot be mapped
    back to a candidate URL, so a parse failure never blocks publishing.
    """
    if not items:
        return []
    top_n = min(top_n, len(items))
    listing = "\n".join(
        f"{i + 1}. {it['title']}\n   {it['link']}\n   {(it.get('text') or '')[:200]}"
        for i, it in enumerate(items)
    )
    system = _SELECT_PROMPT.format(language=language, n=top_n)
    user = f"Candidate stories ({len(items)}):\n\n{listing}"
    response = await _chat(session, cfg, system, user)
    links = _parse_selected_links(response, items)

    chosen = [it for it in items if it["link"] in links][:top_n]
    if not chosen:
        chosen = items[:top_n]
    return chosen