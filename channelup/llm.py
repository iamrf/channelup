"""LLM rewrite: send an item plus the (per-channel) system prompt to the provider."""
from __future__ import annotations

import asyncio
import logging
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


async def rewrite(session: aiohttp.ClientSession, item: dict, cfg: Config, system_prompt: str) -> str:
    """Rewrite one item through the configured provider, retrying on 429/5xx/timeout.

    Returns the stripped rewritten text. Raises RuntimeError after retries are
    exhausted so the caller can treat the item as failed (and NOT mark it seen).
    """
    user = f"Title: {item['title']}\n\n{item['text']}\n\nSource: {item['link']}"

    if cfg.llm_provider == "gemini":
        base = cfg.llm_base_url or "https://generativelanguage.googleapis.com/v1beta"
        url = f"{base}/models/{cfg.llm_model}:generateContent"
        headers = {"x-goog-api-key": cfg.llm_api_key, "Content-Type": "application/json"}
        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
        }
        pick: Callable[[dict], str] = lambda d: d["candidates"][0]["content"]["parts"][0]["text"]
    else:
        base = cfg.llm_base_url or DEFAULT_BASES.get(cfg.llm_provider, DEFAULT_BASES["openai"])
        url = f"{base}/chat/completions"
        headers = {"Authorization": f"Bearer {cfg.llm_api_key}", "Content-Type": "application/json"}
        payload = {
            "model": cfg.llm_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user},
            ],
        }
        pick = lambda d: d["choices"][0]["message"]["content"]

    status = None
    for attempt in range(3):
        try:
            async with session.post(url, json=payload, headers=headers, timeout=30) as r:
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