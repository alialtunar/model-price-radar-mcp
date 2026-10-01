"""model-price-radar MCP server.

Tools read OpenRouter's public model catalog (live) and monthly archived copies of it
(Wayback Machine, since 2023) so the model can answer "what is cheapest for this job" and
"did this model get cheaper". Transport: stdio. No API keys.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from . import __version__, history, openrouter
from .http import SourceError
from .openrouter import CAPABILITIES, Model

mcp = MCPServer(
    "model_price_radar_mcp",
    version=__version__,
    instructions=(
        "LLM prices from OpenRouter: live catalog (~450 models) plus monthly price history since 2023. "
        "Prices are US$ per 1M tokens (input/output); 'blended' assumes 3 input tokens per output token. "
        "Typical flow: find_models for candidates -> estimate_cost for the user's workload -> "
        "model_price_history or price_changes for trends -> compare_providers for one model. "
        "OpenRouter prices usually equal the provider's list price. Always state the date of prices."
    ),
)

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True)
Format = Annotated[Literal["markdown", "json"], Field(description="'markdown' (default) or 'json'.")]
Capability = Literal["tools", "vision", "reasoning", "structured_outputs", "audio_input", "image_output", "free"]


def _error(e: Exception) -> str:
    if isinstance(e, SourceError):
        return f"Error: {e}"
    return f"Error: unexpected {type(e).__name__}: {e}"


def _usd(v: float | None) -> str:
    if v is None:
        return "—"
    if v == 0:
        return "free"
    return f"${v:,.4f}".rstrip("0").rstrip(".") if v < 1 else f"${v:,.2f}"


def _uptime(v: float | None) -> str:
    return "—" if v is None else f"{v}%"


def _pct(old: float, new: float) -> str:
    if old == 0:
        return "new price" if new else "0%"
    return f"{100 * (new - old) / old:+.0f}%"


async def _resolve(query: str, ids: list[str], created: dict[str, int] | None = None) -> str:
    hits = openrouter.resolve(query, ids, created)
    if not hits:
        raise SourceError(f"No model matches '{query}'. Use find_models(query=...) to search.")
    return hits[0]


# ---------- tools ----------

@mcp.tool(name="find_models", annotations=READ_ONLY)
async def find_models(
    query: Annotated[str | None, Field(description="Words in the model id or name, e.g. 'claude', 'llama 70b', 'qwen coder'.")] = None,
    needs: Annotated[list[Capability], Field(description="Required capabilities, e.g. ['tools', 'vision'].")] = [],
    max_input_price: Annotated[float | None, Field(ge=0, description="Max $ per 1M input tokens.")] = None,
    max_output_price: Annotated[float | None, Field(ge=0, description="Max $ per 1M output tokens.")] = None,
    min_context: Annotated[int | None, Field(ge=0, description="Minimum context window in tokens, e.g. 128000.")] = None,
    sort: Annotated[Literal["cheapest", "newest", "context"], Field(description="Ranking.")] = "cheapest",
    limit: Annotated[int, Field(ge=1, le=50)] = 15,
    response_format: Format = "markdown",
) -> str:
    """Search OpenRouter's live catalog by name, capabilities, price ceilings and context size.
    'cheapest' ranks by blended price (3 input : 1 output tokens)."""
    try:
        ms = await openrouter.models()
    except Exception as e:
        return _error(e)
    words = (query or "").lower().split()
    found = [m for m in ms
             if all(w in f"{m.id} {m.name}".lower() for w in words)
             and all(c in m.capabilities for c in needs)
             and (max_input_price is None or m.input <= max_input_price)
             and (max_output_price is None or m.output <= max_output_price)
             and (min_context is None or m.context >= min_context)
             and not m.id.startswith(("openrouter/", "~"))]  # routers have no fixed price; "~" ids are aliases
    key = {"cheapest": lambda m: (m.blended, -m.context), "newest": lambda m: -m.created, "context": lambda m: -m.context}[sort]
    found.sort(key=key)
    if response_format == "json":
        return json.dumps({"matches": len(found), "models": [m.to_dict() for m in found[:limit]]}, indent=2)
    if not found:
        return "No models match. Loosen the price ceilings or capabilities."
    lines = [f"## {len(found)} models match · prices today, $ per 1M tokens", "",
             "| Model | Input | Output | Blended | Context | Capabilities | Added |", "|---|---|---|---|---|---|---|"]
    lines += [f"| `{m.id}` | {_usd(m.input)} | {_usd(m.output)} | {_usd(m.blended)} | {m.context:,} | "
              f"{', '.join(c for c in m.capabilities if c != 'free') or '—'} | {m.created_date} |" for m in found[:limit]]
    if len(found) > limit:
        lines.append(f"_…{len(found) - limit} more._")
    return "\n".join(lines)


@mcp.tool(name="estimate_cost", annotations=READ_ONLY)
async def estimate_cost(
    models: Annotated[list[str], Field(min_length=1, max_length=10, description="Model ids or names, e.g. ['openai/gpt-4o-mini', 'claude haiku'].")],
    input_tokens: Annotated[int, Field(ge=0, description="Input tokens per call.")],
    output_tokens: Annotated[int, Field(ge=0, description="Output tokens per call.")],
    calls: Annotated[int, Field(ge=1, description="Number of calls, e.g. per day or per month.")] = 1,
    response_format: Format = "markdown",
) -> str:
    """Cost of a workload on several models at today's prices, cheapest first."""
    try:
        ms = {m.id: m for m in await openrouter.models()}
    except Exception as e:
        return _error(e)
    rows, missing = [], []
    for q in models:
        hits = openrouter.resolve(q, list(ms), {m.id: m.created for m in ms.values()})
        if not hits:
            missing.append(q)
            continue
        m = ms[hits[0]]
        per_call = (input_tokens * m.input + output_tokens * m.output) / 1_000_000
        rows.append({"model": m.id, "per_call": round(per_call, 6), "total": round(per_call * calls, 4),
                     "input_per_m": m.input, "output_per_m": m.output})
    rows.sort(key=lambda r: r["total"])
    if response_format == "json":
        return json.dumps({"calls": calls, "input_tokens": input_tokens, "output_tokens": output_tokens,
                           "estimates": rows, "not_found": missing}, indent=2)
    lines = [f"## Cost for {calls:,} call(s) × {input_tokens:,} in / {output_tokens:,} out tokens · today's prices", "",
             "| Model | Per call | Total | vs cheapest |", "|---|---|---|---|"]
    cheapest = rows[0]["total"] if rows else 0

    def ratio(total: float) -> str:
        if total == cheapest:
            return "—"
        return f"{total / cheapest:.1f}×" if cheapest else "∞"

    lines += [f"| `{r['model']}` | ${r['per_call']:.6f} | ${r['total']:,.4f} | {ratio(r['total'])} |" for r in rows]
    if missing:
        lines.append(f"\n_Not found: {', '.join(missing)}. Use find_models to get exact ids._")
    return "\n".join(lines)


@mcp.tool(name="model_price_history", annotations=READ_ONLY)
async def model_price_history(
    model: Annotated[str, Field(description="Model id or name, e.g. 'openai/gpt-4o' or 'llama 3.1 70b'.")],
    response_format: Format = "markdown",
) -> str:
    """Monthly price history of one model since it appeared on OpenRouter (archived copies since
    2023) plus today's price. Shows every price change with its % and the overall change."""
    try:
        snaps, live = await asyncio.gather(history.snapshots(), openrouter.models())
    except Exception as e:
        return _error(e)
    today = date.today().isoformat()
    timeline = [*snaps, {"date": today, "prices": {m.id: [m.input, m.output] for m in live}}]
    ids = sorted({i for s in timeline for i in s["prices"]})
    try:
        model_id = await _resolve(model, ids, {m.id: m.created for m in live})
    except SourceError as e:
        return _error(e)
    points = [(s["date"], s["prices"][model_id]) for s in timeline if model_id in s["prices"]]
    periods: list[dict[str, Any]] = []
    for d, (inp, out) in points:
        if periods and periods[-1]["input"] == inp and periods[-1]["output"] == out:
            periods[-1]["to"] = d
        else:
            periods.append({"from": d, "to": d, "input": inp, "output": out})
    on_now = model_id in timeline[-1]["prices"]
    first, last = periods[0], periods[-1]
    summary = {"model": model_id, "first_seen": first["from"], "listed_today": on_now,
               "input_change": _pct(first["input"], last["input"]), "output_change": _pct(first["output"], last["output"])}
    if response_format == "json":
        return json.dumps({**summary, "periods": periods}, indent=2)
    lines = [f"## Price history · `{model_id}`",
             f"First seen {first['from']} · " + ("still listed today" if on_now else f"no longer listed (last seen {last['to']})") +
             f" · input {summary['input_change']}, output {summary['output_change']} since first seen", "",
             "| From | To | Input $/1M | Output $/1M | Change |", "|---|---|---|---|---|"]
    prev = None
    for p in periods:
        change = "" if prev is None else f"in {_pct(prev['input'], p['input'])}, out {_pct(prev['output'], p['output'])}"
        lines.append(f"| {p['from']} | {p['to']} | {_usd(p['input'])} | {_usd(p['output'])} | {change} |")
        prev = p
    lines.append("\n_Dates are when archived copies were taken (about monthly); a change happened between two rows._")
    return "\n".join(lines)


@mcp.tool(name="price_changes", annotations=READ_ONLY)
async def price_changes(
    since: Annotated[str, Field(description="Compare against the archived month closest to this, 'YYYY-MM' (default: 3 months ago).")] = "",
    limit: Annotated[int, Field(ge=1, le=40)] = 12,
    response_format: Format = "markdown",
) -> str:
    """What changed in LLM pricing since a month: biggest price drops and increases, newly free
    models, and how many models were added or removed."""
    try:
        snaps, live = await asyncio.gather(history.snapshots(), openrouter.models())
    except Exception as e:
        return _error(e)
    if not snaps:
        return "No price history available."
    target = since or (date.today() - timedelta(days=90)).strftime("%Y-%m")
    base = min(snaps, key=lambda s: abs((date.fromisoformat(s["date"]) - date.fromisoformat(target[:7] + "-15")).days))
    old, new = base["prices"], {m.id: [m.input, m.output] for m in live}
    changed = []
    for mid in old.keys() & new.keys():
        (oi, oo), (ni, no) = old[mid], new[mid]
        ob, nb = (3 * oi + oo) / 4, (3 * ni + no) / 4
        if ob != nb and ob > 0:
            changed.append({"model": mid, "before": [oi, oo], "now": [ni, no], "blended_change_pct": round(100 * (nb - ob) / ob)})
    drops = sorted((c for c in changed if c["blended_change_pct"] < 0), key=lambda c: c["blended_change_pct"])
    raises = sorted((c for c in changed if c["blended_change_pct"] > 0), key=lambda c: -c["blended_change_pct"])
    added = sorted(new.keys() - old.keys())
    removed = sorted(old.keys() - new.keys())
    newly_free = sorted(mid for mid in old.keys() & new.keys() if new[mid] == [0, 0] and old[mid] != [0, 0])
    data = {"since": base["date"], "today": date.today().isoformat(), "models_then": len(old), "models_now": len(new),
            "added": len(added), "removed": len(removed), "drops": drops[:limit], "increases": raises[:limit],
            "newly_free": newly_free, "added_examples": added[:limit]}
    if response_format == "json":
        return json.dumps(data, indent=2)

    def table(rows: list[dict[str, Any]]) -> list[str]:
        if not rows:
            return ["_None._"]
        return ["| Model | Then (in / out) | Now (in / out) | Blended |", "|---|---|---|---|"] + [
            f"| `{r['model']}` | {_usd(r['before'][0])} / {_usd(r['before'][1])} | {_usd(r['now'][0])} / {_usd(r['now'][1])} | {r['blended_change_pct']:+d}% |"
            for r in rows]

    lines = [f"## LLM price changes · {base['date']} → today", f"{len(old)} models then, {len(new)} now: "
             f"{len(added)} added, {len(removed)} removed. Prices $ per 1M tokens.", "",
             "### Biggest drops", *table(drops[:limit]), "", "### Increases", *table(raises[:limit])]
    if newly_free:
        lines += ["", "### Became free", ", ".join(f"`{m}`" for m in newly_free[:limit])]
    return "\n".join(lines)


@mcp.tool(name="compare_providers", annotations=READ_ONLY)
async def compare_providers(
    model: Annotated[str, Field(description="Model id or name, e.g. 'meta-llama/llama-3.3-70b-instruct'.")],
    response_format: Format = "markdown",
) -> str:
    """Same model, different providers: price, context, max output, quantization and recent uptime
    for every provider that serves it on OpenRouter, cheapest first."""
    try:
        live = await openrouter.models()
        model_id = await _resolve(model, [m.id for m in live], {m.id: m.created for m in live})
        name, offers = await openrouter.endpoints(model_id)
    except Exception as e:
        return _error(e)
    if response_format == "json":
        return json.dumps({"model": model_id, "name": name, "providers": offers}, indent=2)
    if not offers:
        return f"No provider offers listed for `{model_id}`."
    lines = [f"## Providers for `{model_id}` · today", "",
             "| Provider | Input $/1M | Output $/1M | Context | Max output | Quantization | Uptime (30 min) |",
             "|---|---|---|---|---|---|---|"]
    lines += [f"| {o['provider']} | {_usd(o['input_per_m'])} | {_usd(o['output_per_m'])} | {o['context'] or '—'} | "
              f"{o['max_output'] or '—'} | {o['quantization'] or '—'} | {_uptime(o['uptime_30m'])} |"
              for o in offers]
    if len(offers) > 1:
        lo, hi = offers[0], offers[-1]
        lines.append(f"\n_Cheapest ({lo['provider']}) vs most expensive ({hi['provider']}): input {_pct(hi['input_per_m'], lo['input_per_m'])}._")
    return "\n".join(lines)


# ---------- prompts ----------

@mcp.prompt(name="cheapest_model_for", description="Pick the cheapest model that can do a job, with cost estimates.")
def cheapest_model_for(task: str = "summarize 2,000 support tickets a day", needs: str = "tools") -> str:
    return (
        f"Task: {task}. Required capabilities: {needs or 'none'}.\n"
        "1. Estimate input/output tokens per call and calls per month for the task; state your assumptions.\n"
        f"2. Call find_models(needs={[n.strip() for n in needs.split(',') if n.strip()]}, sort='cheapest', limit=20) and pick 4-6 "
        "candidates of different quality tiers (include one frontier model as a reference).\n"
        "3. Call estimate_cost for them. 4. For the top pick, call model_price_history and compare_providers.\n"
        "5. Recommend one model with the monthly cost, a cheaper fallback, and the quality trade-off. Use only tool numbers."
    )


@mcp.prompt(name="monthly_price_report", description="What changed in LLM pricing recently.")
def monthly_price_report(since: str = "") -> str:
    arg = f"since='{since}'" if since else ""
    return (
        f"1. Call price_changes({arg}).\n"
        "2. For the 3 biggest drops, call model_price_history to show the longer trend.\n"
        "3. Write a short report: biggest drops (with %), increases, newly free models, how many models were added. "
        "Cite dates. Do not invent prices."
    )


def main() -> None:
    import logging

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("mcp").setLevel(logging.WARNING)
    mcp.run()


if __name__ == "__main__":
    main()
