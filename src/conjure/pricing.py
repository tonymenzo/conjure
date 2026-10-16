"""What a model's tokens cost, for clients whose provider doesn't say.

orchestral prices each response with its provider's table. That works
for its native providers (Anthropic, OpenAI, Google, Groq), but an
``openai_compatible`` endpoint (a model access gateway, vLLM, LiteLLM) is
served through orchestral's vLLM client, which prices every call at $0
on the assumption the server is self-hosted. A gateway's Claude or GPT
calls then showed as free.

A :class:`PriceBook` gives an ``llms`` entry its prices:

1. the entry's ``pricing:`` (USD per million tokens), by exact model
   id, longest model-id prefix, or ``default``;
2. else the public list price from orchestral's provider tables, by
   exact id or longest prefix (``claude-sonnet-5-5`` takes
   ``claude-sonnet-5``'s price);
3. else unknown: tokens are counted, and the cost shows as unpriced
   rather than $0.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Mapping

_PROVIDERS = ("anthropic", "openai", "google", "groq")
_SEPARATORS = "-.:@/_"


@dataclass(frozen=True)
class Price:
    input: float  # USD per million input (prompt) tokens
    output: float  # USD per million output (completion) tokens
    cached_input: float | None = None  # USD per million cached input tokens
    source: str = ""  # "config", or "list:<provider>"

    def cost(self, tokens: Mapping[str, Any]) -> float:
        prompt = int(tokens.get("prompt_tokens") or 0)
        cached = int(tokens.get("cached_prompt_tokens") or tokens.get("cache_read_input_tokens") or 0)
        completion = int(tokens.get("completion_tokens") or 0)
        cached_rate = self.input if self.cached_input is None else self.cached_input
        return (max(0, prompt - cached) * self.input + cached * cached_rate + completion * self.output) / 1e6

    def public(self) -> dict[str, Any]:
        out: dict[str, Any] = {"input": self.input, "output": self.output, "source": self.source}
        if self.cached_input is not None:
            out["cached_input"] = self.cached_input
        return out


def _base_id(model: str) -> str:
    # "anthropic/claude-sonnet-5-5" or "us.anthropic.claude-..." → the model id
    model = model.rsplit("/", 1)[-1]
    for vendor in ("anthropic.", "openai.", "google."):
        if vendor in model:
            model = model.split(vendor, 1)[1]
    return model


def _match(model: str, keys: list[str]) -> str | None:
    """Exact id, else the longest key that ``model`` extends at a
    separator (``gpt-5`` matches ``gpt-5-turbo``, not ``gpt-50``)."""
    if model in keys:
        return model
    hits = [
        k for k in keys
        if k != "default" and model.startswith(k) and len(model) > len(k) and model[len(k)] in _SEPARATORS
    ]
    return max(hits, key=len) if hits else None


@lru_cache(maxsize=1)
def _list_tables() -> dict[str, dict[str, dict[str, float]]]:
    tables = {}
    for provider in _PROVIDERS:
        try:
            module = importlib.import_module(f"orchestral.llm.{provider}.pricing_model")
            tables[provider] = dict(module.pricing_model.rates)
        except Exception:
            continue
    return tables


def list_price(model: str) -> Price | None:
    base = _base_id(model)
    for provider, rates in _list_tables().items():
        key = _match(base, list(rates))
        if key is None:
            continue
        r = rates[key]
        if "prompt_tokens" not in r or "completion_tokens" not in r:
            continue
        cached = r.get("cached_prompt_tokens", r.get("cache_read_input_tokens"))
        return Price(float(r["prompt_tokens"]), float(r["completion_tokens"]),
                     None if cached is None else float(cached), f"list:{provider}")
    return None


class PriceBook:
    """Prices for one ``llms`` entry: its ``pricing:`` overrides, then
    list prices (when ``use_list_prices``)."""

    def __init__(self, overrides: Mapping[str, Mapping[str, Any]] | None = None, *, use_list_prices: bool = True):
        self._overrides = {str(k): dict(v) for k, v in (overrides or {}).items()}
        self._use_list = use_list_prices

    def price(self, model: str | None) -> Price | None:
        model = model or ""
        key = _match(model, list(self._overrides)) or _match(_base_id(model), list(self._overrides))
        if key is None and "default" in self._overrides:
            key = "default"
        if key is not None:
            o = self._overrides[key]
            try:
                return Price(float(o["input"]), float(o["output"]),
                             None if o.get("cached_input") is None else float(o["cached_input"]), "config")
            except (KeyError, TypeError, ValueError):
                return None
        return list_price(model) if self._use_list and model else None
