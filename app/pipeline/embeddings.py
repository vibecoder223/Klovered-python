"""Embeddings — mistral-embed @ 1024 dims. Port of lib/embeddings.ts.

Serialized rate gate (concurrency 1 + min interval): mistral-embed is capped
per-minute and bursting via concurrent calls previously caused 429 storms.
"""

import asyncio
import os
import time

import httpx

from ..config import get_settings

MISTRAL_EMBED_URL = "https://api.mistral.ai/v1/embeddings"
MISTRAL_EMBED_MODEL = os.getenv("MISTRAL_EMBED_MODEL", "mistral-embed")
EMBED_DIMS = 1024

_EMBED_BATCH_SIZE = 128
_MAX_RETRIES = 4
_EMBED_MIN_INTERVAL_S = float(os.getenv("EMBED_MIN_INTERVAL_MS", "1050")) / 1000

_gate_lock = asyncio.Lock()
_last_embed_at = 0.0


def has_embeddings() -> bool:
    return bool(get_settings().llm_key)


async def _embed_batch(batch: list[str]) -> list[list[float]]:
    global _last_embed_at
    key = get_settings().llm_key
    attempt = 0
    async with _gate_lock:
        wait = _EMBED_MIN_INTERVAL_S - (time.monotonic() - _last_embed_at)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_embed_at = time.monotonic()

        async with httpx.AsyncClient(timeout=60.0) as client:
            while True:
                res = await client.post(
                    MISTRAL_EMBED_URL,
                    headers={"content-type": "application/json", "authorization": f"Bearer {key}"},
                    json={"model": MISTRAL_EMBED_MODEL, "input": batch},
                )
                if res.status_code == 429:
                    retry_after = float(res.headers.get("retry-after", "5"))
                    base = max(1.0, retry_after)
                    backoff = min(30.0, base * (2**attempt))
                    if attempt < _MAX_RETRIES:
                        await asyncio.sleep(backoff * (0.7 + 0.6 * (os.urandom(1)[0] / 255)))
                        attempt += 1
                        continue
                    raise RuntimeError(f"Mistral embed 429 after {_MAX_RETRIES} retries")
                if res.status_code >= 400:
                    raise RuntimeError(f"Mistral embed failed: {res.status_code} {res.text[:300]}")
                j = res.json()
                data = sorted(j["data"], key=lambda d: d["index"])
                return [d["embedding"] for d in data]


async def embed_texts(texts: list[str], input_type: str = "document") -> list[list[float]]:
    if not texts:
        return []
    if not has_embeddings():
        raise RuntimeError("Embeddings unavailable: set MISTRAL_API_KEY.")

    batches = [
        (i, texts[i : i + _EMBED_BATCH_SIZE]) for i in range(0, len(texts), _EMBED_BATCH_SIZE)
    ]
    out: list[list[float] | None] = [None] * len(texts)
    results = await asyncio.gather(*(_embed_batch(batch) for _, batch in batches))
    for (start, batch), embs in zip(batches, results):
        for i, e in enumerate(embs):
            out[start + i] = e
    return out  # type: ignore[return-value]
