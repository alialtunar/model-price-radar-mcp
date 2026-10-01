"""Live smoke test against OpenRouter (and the Wayback Machine for months newer than the bundle).

Run:  uv run python scripts/smoke_live.py
Every check prints PASS/FAIL so you can see which endpoint changed shape.
"""

import asyncio
import logging
import sys

from model_price_radar.server import compare_providers, estimate_cost, find_models, model_price_history, price_changes

CHECKS = [
    ("Find models (tools + vision, cheapest)", find_models, dict(needs=["tools", "vision"], limit=5), "| Model |"),
    ("Estimate cost", estimate_cost, dict(models=["gpt-4o-mini", "claude haiku"], input_tokens=2000, output_tokens=500, calls=1000), "| Model |"),
    ("Price history (gpt-4o, since 2024)", model_price_history, dict(model="openai/gpt-4o"), "First seen"),
    ("Price changes (since 2025-01)", price_changes, dict(since="2025-01", limit=5), "### Biggest drops"),
    ("Providers (llama 3.3 70b)", compare_providers, dict(model="meta-llama/llama-3.3-70b-instruct"), "| Provider |"),
]


async def main() -> int:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    failed = 0
    for name, fn, kwargs, must in CHECKS:
        out = await fn(**kwargs)
        ok = not out.startswith("Error") and must in out
        failed += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
        print("      " + out.replace("\n", "\n      ")[:700] + ("\n      …" if len(out) > 700 else ""))
        print()
    print(f"{len(CHECKS) - failed}/{len(CHECKS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
