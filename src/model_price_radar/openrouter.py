"""Live OpenRouter catalog: public endpoints, no key."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .history import per_million
from .http import SourceError, fetch

API = "https://openrouter.ai/api/v1"
SOURCE = "OpenRouter"
CAPABILITIES = ("tools", "vision", "reasoning", "structured_outputs", "audio_input", "image_output", "free")


@dataclass
class Model:
    id: str
    name: str
    input: float            # $ per 1M input tokens
    output: float           # $ per 1M output tokens
    context: int = 0
    created: int = 0
    cache_read: float | None = None
    input_modalities: list[str] = field(default_factory=list)
    output_modalities: list[str] = field(default_factory=list)
    parameters: list[str] = field(default_factory=list)
    expiration_date: str | None = None

    @property
    def free(self) -> bool:
        return self.input == 0 and self.output == 0

    @property
    def blended(self) -> float:
        """$ per 1M tokens at a typical 3:1 input:output mix (used to rank by price)."""
        return round((3 * self.input + self.output) / 4, 4)

    @property
    def capabilities(self) -> list[str]:
        caps = []
        if "tools" in self.parameters:
            caps.append("tools")
        if "image" in self.input_modalities:
            caps.append("vision")
        if "reasoning" in self.parameters or "include_reasoning" in self.parameters:
            caps.append("reasoning")
        if "structured_outputs" in self.parameters or "response_format" in self.parameters:
            caps.append("structured_outputs")
        if "audio" in self.input_modalities:
            caps.append("audio_input")
        if "image" in self.output_modalities:
            caps.append("image_output")
        if self.free:
            caps.append("free")
        return caps

    @property
    def created_date(self) -> str:
        return datetime.fromtimestamp(self.created, tz=timezone.utc).strftime("%Y-%m-%d") if self.created else ""

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "input_per_m": self.input, "output_per_m": self.output,
                "blended_per_m": self.blended, "context": self.context, "created": self.created_date,
                "capabilities": self.capabilities, **({"cache_read_per_m": self.cache_read} if self.cache_read is not None else {}),
                **({"expires": self.expiration_date} if self.expiration_date else {})}


def _model(m: dict[str, Any]) -> Model | None:
    p = m.get("pricing") or {}
    inp, out = per_million(p.get("prompt")), per_million(p.get("completion"))
    if not m.get("id") or inp is None or out is None:
        return None
    arch = m.get("architecture") or {}
    return Model(
        id=m["id"], name=m.get("name") or m["id"], input=inp, output=out,
        context=int(m.get("context_length") or 0), created=int(m.get("created") or 0),
        cache_read=per_million(p.get("input_cache_read")),
        input_modalities=arch.get("input_modalities") or [], output_modalities=arch.get("output_modalities") or [],
        parameters=m.get("supported_parameters") or [], expiration_date=m.get("expiration_date"),
    )


async def models() -> list[Model]:
    _, text = await fetch(f"{API}/models", source=SOURCE)
    try:
        data = json.loads(text)
    except ValueError as e:
        raise SourceError("OpenRouter returned something that is not JSON. Try again.") from e
    return [m for m in (_model(x) for x in data.get("data", [])) if m]


def resolve(query: str, ids: list[str], created: dict[str, int] | None = None) -> list[str]:
    """Exact id first; otherwise ids containing every word of the query, newest model first
    ('claude sonnet' means the current Sonnet), then shortest id."""
    q = query.strip().lower()
    if q in ids:
        return [q]
    lowered = {i.lower(): i for i in ids}
    if q in lowered:
        return [lowered[q]]
    words = [w for w in re.split(r"[\s/:_]+", q) if w]
    hits = [i for i in ids if all(w in i.lower() for w in words)]
    created = created or {}
    # "~vendor/model-latest" ids are aliases of a real model: rank them after real ids.
    return sorted(hits, key=lambda i: (i.startswith("~"), -created.get(i, 0), len(i), i))


async def endpoints(model_id: str) -> tuple[str, list[dict[str, Any]]]:
    """(model name, per-provider offers) for one model."""
    _, text = await fetch(f"{API}/models/{model_id}/endpoints", source=SOURCE)
    try:
        data = json.loads(text).get("data") or {}
    except ValueError as e:
        raise SourceError("OpenRouter returned something that is not JSON. Try again.") from e
    offers = []
    for e in data.get("endpoints") or []:
        p = e.get("pricing") or {}
        inp, out = per_million(p.get("prompt")), per_million(p.get("completion"))
        if inp is None or out is None:
            continue
        offers.append({
            "provider": e.get("provider_name") or e.get("name") or "?", "input_per_m": inp, "output_per_m": out,
            "context": e.get("context_length"), "max_output": e.get("max_completion_tokens"),
            "quantization": None if e.get("quantization") in (None, "unknown") else e.get("quantization"),
            "uptime_30m": round(e["uptime_last_30m"], 1) if isinstance(e.get("uptime_last_30m"), (int, float)) else None,
        })
    return data.get("name") or model_id, sorted(offers, key=lambda o: (3 * o["input_per_m"] + o["output_per_m"]))
