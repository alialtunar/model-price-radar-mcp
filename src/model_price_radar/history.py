"""Price history: monthly Wayback Machine copies of openrouter.ai/api/v1/models.

The months up to the package build are bundled (data/history.json.gz, built by
scripts/build_history.py) so history answers are instant; months archived after the build
are fetched from the Wayback Machine on demand and cached on disk.
"""

from __future__ import annotations

import gzip
import json
from importlib import resources
from typing import Any

from .http import SourceError, fetch

MODELS_URL = "openrouter.ai/api/v1/models"
CDX = "https://web.archive.org/cdx/search/cdx"
WEB = "https://web.archive.org/web"

Prices = dict[str, list[float]]  # model id -> [input $/M tokens, output $/M tokens]


def per_million(raw: Any) -> float | None:
    """OpenRouter prices are $ per token as strings; '-1' means variable (routers)."""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return None if v < 0 else round(v * 1_000_000, 4)


def compact(models_json: dict[str, Any]) -> Prices:
    out: Prices = {}
    for m in models_json.get("data", []):
        p = m.get("pricing") or {}
        inp, outp = per_million(p.get("prompt")), per_million(p.get("completion"))
        if m.get("id") and inp is not None and outp is not None:
            out[m["id"]] = [inp, outp]
    return out


def load_bundled() -> dict[str, Any]:
    try:
        raw = resources.files("model_price_radar").joinpath("data/history.json.gz").read_bytes()
    except (FileNotFoundError, OSError):
        return {"built": "", "snapshots": []}
    return json.loads(gzip.decompress(raw))


async def archived_months(after: str = "") -> list[tuple[str, str]]:
    """[(date 'YYYY-MM-DD', wayback timestamp)] one per month, oldest first, strictly after `after`."""
    params = {"url": MODELS_URL, "output": "json", "fl": "timestamp,statuscode",
              "filter": "statuscode:200", "collapse": "timestamp:6"}
    if after:
        params["from"] = after.replace("-", "")[:8]
    _, text = await fetch(CDX, params, source="Wayback Machine index")
    if not text.strip():
        return []
    try:
        rows = json.loads(text)[1:]
    except ValueError as e:
        raise SourceError("The Wayback Machine index returned something unreadable.") from e
    out = [(f"{r[0][:4]}-{r[0][4:6]}-{r[0][6:8]}", r[0]) for r in rows]
    return [o for o in out if o[0] > after]


async def archived_prices(timestamp: str) -> Prices:
    _, text = await fetch(f"{WEB}/{timestamp}id_/https://{MODELS_URL}", source="Wayback Machine", disk=True)
    try:
        return compact(json.loads(text))
    except ValueError as e:
        raise SourceError(f"The archived price list from {timestamp[:8]} is unreadable.") from e


async def snapshots(include_newer: bool = True) -> list[dict[str, Any]]:
    """[{'date', 'prices'}] oldest first: bundled months + any newer archived months.
    Newer months that fail to load are skipped (history is still useful without them)."""
    data = load_bundled()
    snaps = list(data.get("snapshots", []))
    if include_newer:
        last = snaps[-1]["date"] if snaps else ""
        try:
            for date, ts in await archived_months(last):
                try:
                    snaps.append({"date": date, "prices": await archived_prices(ts)})
                except SourceError:
                    continue
        except SourceError:
            pass
    return snaps
