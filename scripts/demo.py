"""Terminal demo: how one model's price changed since it launched.

Run:  uv run --with rich python scripts/demo.py openai/gpt-4o   (rich is optional)
Calls the same tool function the MCP server exposes, so the output is what Claude sees.
"""

import asyncio
import logging
import sys

from model_price_radar.server import model_price_history


async def main(model: str) -> None:
    print(f"→ model_price_history(model='{model}')\n")
    out = await model_price_history(model=model)
    try:
        from rich.console import Console
        from rich.markdown import Markdown
        Console(width=110).print(Markdown(out))
    except ImportError:
        print(out)


if __name__ == "__main__":
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "openai/gpt-4o"))
