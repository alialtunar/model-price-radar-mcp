"""Shared HTTP layer: one client config, retries, two caches.

Archived price lists never change, so they are cached on disk; live prices only live in the
in-memory cache for a few minutes.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx

USER_AGENT = "model-price-radar-mcp/0.1 (+https://github.com/alialtunar/model-price-radar-mcp)"
CACHE_TTL_SECONDS = 10 * 60  # live prices
TIMEOUT_SECONDS = 60.0
RETRY_DELAYS = (2.0, 6.0, 15.0)  # 4 attempts; archive.org (history) often answers 503
_RETRY_STATUS = {429, 500, 502, 503, 504}
_MAX_CONCURRENCY = 4

_cache: dict[str, tuple[float, str]] = {}
_transport: httpx.AsyncBaseTransport | None = None  # tests inject a MockTransport here
_semaphores: dict[int, asyncio.Semaphore] = {}
_disk_dir: Path | None = None


class SourceError(Exception):
    """Error with a message that tells the model what to do next."""


def set_transport(transport: httpx.AsyncBaseTransport | None, retry_delays: tuple[float, ...] | None = None) -> None:
    """Swap the network layer (used by tests). Also clears the memory cache."""
    global _transport, RETRY_DELAYS
    _transport = transport
    if retry_delays is not None:
        RETRY_DELAYS = retry_delays
    _cache.clear()


def set_disk_cache(path: Path | None) -> None:
    global _disk_dir
    _disk_dir = path


def _disk_path(key: str) -> Path | None:
    base = _disk_dir
    if base is None:
        env = os.environ.get("MODEL_PRICE_RADAR_CACHE")
        base = Path(env) if env else Path.home() / ".cache" / "model-price-radar"
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return base / (hashlib.sha256(key.encode()).hexdigest()[:32] + ".json")


def _semaphore() -> asyncio.Semaphore:
    loop_id = id(asyncio.get_running_loop())
    if loop_id not in _semaphores:
        _semaphores[loop_id] = asyncio.Semaphore(_MAX_CONCURRENCY)
    return _semaphores[loop_id]


def _key(url: str, params: dict[str, Any] | None) -> str:
    if not params:
        return url
    return url + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))


async def fetch(url: str, params: dict[str, Any] | None = None, *, source: str = "OpenRouter",
                disk: bool = False) -> tuple[str, str]:
    """GET url and return (final_url, text). Retries 429/5xx/timeouts with backoff.

    disk=True caches the result forever on disk (use only for content that no longer changes).
    """
    key = _key(url, params)
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_TTL_SECONDS:
        return json.loads(hit[1])
    path = _disk_path(key) if disk else None
    if path and path.exists():
        try:
            final_url, text = json.loads(path.read_text())
            return final_url, text
        except (OSError, ValueError):
            pass

    async with _semaphore():
        async with httpx.AsyncClient(
            transport=_transport, timeout=TIMEOUT_SECONDS, follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            last = ""
            for attempt, delay in enumerate((*RETRY_DELAYS, None)):
                try:
                    resp = await client.get(url, params=params)
                except httpx.TimeoutException:
                    last = "timed out"
                except httpx.TransportError as e:
                    last = type(e).__name__
                else:
                    if resp.status_code not in _RETRY_STATUS:
                        break
                    last = f"HTTP {resp.status_code}"
                if delay is None:
                    raise SourceError(
                        f"{source} is not answering ({last}, {attempt + 1} tries). Wait a minute and try again.")
                await asyncio.sleep(delay)

    if resp.status_code == 404:
        raise SourceError(f"{source} has no item at that address (404). Check the model id.")
    if resp.status_code >= 400:
        raise SourceError(f"{source} returned HTTP {resp.status_code}.")
    result = (str(resp.url), resp.text)
    _cache[key] = (time.monotonic(), json.dumps(result))
    if path:
        try:
            path.write_text(json.dumps(result))
        except OSError:
            pass  # cache is best effort
    return result
