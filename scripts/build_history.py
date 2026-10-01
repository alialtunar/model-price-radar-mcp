"""Rebuild the bundled price history from the Wayback Machine.

Run:  uv run python scripts/build_history.py
Fetches one archived copy of openrouter.ai/api/v1/models per month (2023 onward) and writes
src/model_price_radar/data/history.json.gz. archive.org is slow: expect several minutes.
"""

import asyncio
import gzip
import json
import logging
import sys
from datetime import date
from pathlib import Path

from model_price_radar import history
from model_price_radar.http import SourceError

OUT = Path(__file__).resolve().parents[1] / "src" / "model_price_radar" / "data" / "history.json.gz"


async def main() -> int:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    months = await history.archived_months()
    print(f"{len(months)} archived months", flush=True)
    snaps = []
    for d, ts in months:
        try:
            prices = await history.archived_prices(ts)
        except SourceError as e:
            print(f"  {d}: skipped ({e})", flush=True)
            continue
        if not prices:
            print(f"  {d}: empty, skipped", flush=True)
            continue
        snaps.append({"date": d, "prices": prices})
        print(f"  {d}: {len(prices)} models", flush=True)
    if len(snaps) < 3:
        print("Too few months fetched; not overwriting the bundle.")
        return 1
    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = {"built": date.today().isoformat(), "source": f"https://web.archive.org/web/*/{history.MODELS_URL}",
               "snapshots": snaps}
    OUT.write_bytes(gzip.compress(json.dumps(payload, separators=(",", ":")).encode(), mtime=0))
    print(f"wrote {OUT} ({OUT.stat().st_size // 1024} KB, {len(snaps)} months)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
