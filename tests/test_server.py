import gzip
import json
from datetime import date

from mcp.client import Client

from model_price_radar import __version__, history, openrouter
from model_price_radar.server import (
    compare_providers, estimate_cost, find_models, mcp, model_price_history, price_changes,
)


# ---- data layer ----

def test_per_million():
    assert history.per_million("0.0000025") == 2.5
    assert history.per_million("0") == 0.0
    assert history.per_million("-1") is None and history.per_million(None) is None


async def test_live_models_skip_variable_prices_and_read_capabilities():
    ms = {m.id: m for m in await openrouter.models()}
    assert "openrouter/auto" not in ms
    gpt = ms["openai/gpt-4o"]
    assert gpt.input == 2.5 and gpt.output == 10.0 and gpt.blended == 4.375
    assert set(gpt.capabilities) == {"tools", "vision", "structured_outputs"}
    assert "free" in ms["qwen/qwen3-coder:free"].capabilities
    assert "reasoning" in ms["deepseek/deepseek-r1"].capabilities


def test_resolve():
    ids = ["openai/gpt-4o", "openai/gpt-4o-mini", "meta-llama/llama-3.1-70b-instruct"]
    assert openrouter.resolve("openai/gpt-4o", ids) == ["openai/gpt-4o"]
    assert openrouter.resolve("gpt-4o", ids)[0] == "openai/gpt-4o"  # shortest match first
    assert openrouter.resolve("llama 70b", ids) == ["meta-llama/llama-3.1-70b-instruct"]
    assert openrouter.resolve("claude", ids) == []


async def test_snapshots_add_months_archived_after_the_bundle():
    snaps = await history.snapshots()
    assert [s["date"] for s in snaps] == ["2024-09-05", "2025-07-01", "2025-09-01"]


async def test_snapshots_survive_wayback_outage(net):
    net.wayback_down = True
    assert [s["date"] for s in await history.snapshots()] == ["2024-09-05", "2025-07-01"]


def test_real_bundle_is_valid_if_present():
    from importlib import resources
    path = resources.files("model_price_radar").joinpath("data/history.json.gz")
    if not path.is_file():
        return
    data = json.loads(gzip.decompress(path.read_bytes()))
    dates = [s["date"] for s in data["snapshots"]]
    assert dates == sorted(dates) and len(dates) >= 10
    assert all(isinstance(v, list) and len(v) == 2 for s in data["snapshots"] for v in s["prices"].values())


# ---- tools ----

async def test_find_models_filters_and_sorts():
    data = json.loads(await find_models(needs=["tools"], max_input_price=1, response_format="json"))
    assert [m["id"] for m in data["models"]] == ["qwen/qwen3-coder:free", "openai/gpt-4o-mini", "deepseek/deepseek-r1"]
    out = await find_models(query="llama", min_context=100000)
    assert "meta-llama/llama-3.1-70b-instruct" in out and "gpt-4o" not in out
    newest = json.loads(await find_models(sort="newest", response_format="json"))
    assert newest["models"][0]["id"] == "deepseek/deepseek-r1"
    assert "openrouter/auto" not in json.dumps(newest)


async def test_estimate_cost_ranks_and_reports_missing():
    data = json.loads(await estimate_cost(models=["gpt-4o", "gpt-4o-mini", "nonexistent-model"],
                                          input_tokens=1000, output_tokens=500, calls=1000, response_format="json"))
    assert [r["model"] for r in data["estimates"]] == ["openai/gpt-4o-mini", "openai/gpt-4o"]
    assert data["estimates"][1]["total"] == 7.5  # (1000*2.5 + 500*10)/1e6 * 1000
    assert data["not_found"] == ["nonexistent-model"]


async def test_price_history_groups_unchanged_months():
    data = json.loads(await model_price_history(model="llama 3.1 70b", response_format="json"))
    assert data["model"] == "meta-llama/llama-3.1-70b-instruct" and data["first_seen"] == "2024-09-05"
    assert [(p["from"], p["input"]) for p in data["periods"]] == [
        ("2024-09-05", 0.3), ("2025-07-01", 0.1), (date.today().isoformat(), 0.4)]
    assert data["periods"][1]["to"] == "2025-09-01"
    assert data["input_change"] == "+33%"


async def test_price_history_of_removed_model():
    out = await model_price_history(model="claude-3.5-sonnet")
    assert "no longer listed (last seen 2025-07-01)" in out


async def test_price_history_unknown_model():
    assert (await model_price_history(model="totally unknown")).startswith("Error:")


async def test_price_changes_since_bundle_month():
    data = json.loads(await price_changes(since="2024-09", response_format="json"))
    assert data["since"] == "2024-09-05"
    drops = {d["model"]: d["blended_change_pct"] for d in data["drops"]}
    assert drops["openai/gpt-4o"] == -42  # blended 7.5 -> 4.375
    assert data["newly_free"] == ["qwen/qwen3-coder:free"]
    assert data["removed"] == 1 and data["added"] == 1  # claude-3.5-sonnet out, deepseek-r1 in


async def test_compare_providers():
    data = json.loads(await compare_providers(model="llama 3.1 70b", response_format="json"))
    assert [p["provider"] for p in data["providers"]] == ["DeepInfra", "Novita"]
    assert data["providers"][0]["quantization"] is None and data["providers"][1]["quantization"] == "fp8"
    out = await compare_providers(model="meta-llama/llama-3.1-70b-instruct")
    assert "| DeepInfra | $0.4 | $0.4 |" in out and "-20%" in out


# ---- MCP protocol ----

async def test_protocol_end_to_end():
    async with Client(mcp) as client:
        assert client.server_info.version == __version__
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert set(tools) == {"find_models", "estimate_cost", "model_price_history", "price_changes", "compare_providers"}
        assert all(t.annotations.read_only_hint for t in tools.values())
        prompts = {p.name for p in (await client.list_prompts()).prompts}
        assert prompts == {"cheapest_model_for", "monthly_price_report"}
        res = await client.call_tool("find_models", {"needs": ["vision"]})
        assert not res.is_error and "openai/gpt-4o" in res.content[0].text
        bad = await client.call_tool("find_models", {"needs": ["telepathy"]})
        assert bad.is_error


def test_resolve_prefers_newest_model():
    ids = ["anthropic/claude-sonnet-4", "anthropic/claude-sonnet-4.5"]
    created = {"anthropic/claude-sonnet-4": 1, "anthropic/claude-sonnet-4.5": 2}
    assert openrouter.resolve("claude sonnet", ids, created)[0] == "anthropic/claude-sonnet-4.5"
