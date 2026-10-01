"""Mock OpenRouter + Wayback, and a small bundled history, shaped like the real data."""

from __future__ import annotations

import json

import httpx
import pytest

from model_price_radar import history, http


def or_model(mid, name, prompt, completion, ctx=128000, created=1720000000, params=("tools",), inputs=("text",)):
    return {"id": mid, "name": name, "created": created, "context_length": ctx,
            "architecture": {"input_modalities": list(inputs), "output_modalities": ["text"]},
            "pricing": {"prompt": prompt, "completion": completion}, "supported_parameters": list(params)}


LIVE = {"data": [
    or_model("openai/gpt-4o", "OpenAI: GPT-4o", "0.0000025", "0.00001", params=("tools", "structured_outputs"), inputs=("text", "image")),
    or_model("openai/gpt-4o-mini", "OpenAI: GPT-4o-mini", "0.00000015", "0.0000006", params=("tools",), inputs=("text", "image")),
    or_model("meta-llama/llama-3.1-70b-instruct", "Meta: Llama 3.1 70B", "0.0000004", "0.0000004", ctx=131072, params=()),
    or_model("qwen/qwen3-coder:free", "Qwen3 Coder (free)", "0", "0", ctx=262144, created=1750000000),
    or_model("deepseek/deepseek-r1", "DeepSeek R1", "0.0000005", "0.000002", created=1760000000, params=("reasoning", "tools")),
    or_model("openrouter/auto", "Auto Router", "-1", "-1"),
]}

# Bundled months (what scripts/build_history.py would write)
BUNDLE = {"built": "2025-07-02", "snapshots": [
    {"date": "2024-09-05", "prices": {"openai/gpt-4o": [5.0, 15.0], "openai/gpt-4o-mini": [0.15, 0.6],
                                      "meta-llama/llama-3.1-70b-instruct": [0.3, 0.3], "anthropic/claude-3.5-sonnet": [3.0, 15.0],
                                      "qwen/qwen3-coder:free": [0.2, 0.2]}},
    {"date": "2025-07-01", "prices": {"openai/gpt-4o": [2.5, 10.0], "openai/gpt-4o-mini": [0.15, 0.6],
                                      "meta-llama/llama-3.1-70b-instruct": [0.1, 0.28], "anthropic/claude-3.5-sonnet": [3.0, 15.0],
                                      "qwen/qwen3-coder:free": [0.2, 0.2]}},
]}

# One month archived after the bundle was built (fetched from Wayback on demand)
NEWER = {"data": [or_model("openai/gpt-4o", "GPT-4o", "0.0000025", "0.00001"),
                  or_model("meta-llama/llama-3.1-70b-instruct", "Llama", "0.0000001", "0.00000028"),
                  or_model("qwen/qwen3-coder:free", "Qwen3 Coder", "0", "0")]}

ENDPOINTS = {"data": {"name": "Meta: Llama 3.1 70B", "endpoints": [
    {"provider_name": "Novita", "quantization": "fp8", "context_length": 131072, "max_completion_tokens": 16384,
     "pricing": {"prompt": "0.0000005", "completion": "0.0000005"}, "uptime_last_30m": 99.5},
    {"provider_name": "DeepInfra", "quantization": "unknown", "context_length": 131072,
     "pricing": {"prompt": "0.0000004", "completion": "0.0000004"}, "uptime_last_30m": 100},
]}}


class Net:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.wayback_down = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append(url)
        if url == "https://openrouter.ai/api/v1/models":
            return httpx.Response(200, json=LIVE)
        if url.startswith("https://openrouter.ai/api/v1/models/") and url.endswith("/endpoints"):
            mid = url.removeprefix("https://openrouter.ai/api/v1/models/").removesuffix("/endpoints")
            if mid == "meta-llama/llama-3.1-70b-instruct":
                return httpx.Response(200, json=ENDPOINTS)
            return httpx.Response(404)
        if url.startswith("https://web.archive.org/"):
            if self.wayback_down:
                return httpx.Response(503)
            if "/cdx/search/cdx" in url:
                rows = [["timestamp", "statuscode"], ["20250701000000", "200"], ["20250901000000", "200"]]
                frm = request.url.params.get("from", "")
                return httpx.Response(200, text=json.dumps(rows[:1] + [r for r in rows[1:] if r[0] >= frm]))
            if "20250901000000id_" in url:
                return httpx.Response(200, json=NEWER)
            return httpx.Response(404)
        return httpx.Response(500)


@pytest.fixture(autouse=True)
def net(tmp_path, monkeypatch):
    mock = Net()
    http.set_transport(httpx.MockTransport(mock), retry_delays=(0.0, 0.0))
    http.set_disk_cache(tmp_path / "cache")
    monkeypatch.setattr(history, "load_bundled", lambda: json.loads(json.dumps(BUNDLE)))
    yield mock
    http.set_transport(None)
    http.set_disk_cache(None)
