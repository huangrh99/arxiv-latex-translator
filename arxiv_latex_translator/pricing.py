"""Token price estimates for the Codex models used by the service."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any


PRICING_SOURCE_URL = "https://developers.openai.com/api/docs/pricing"
DEFAULT_PRICING_MODEL = "gpt-6.2-sol"


@dataclass(frozen=True)
class TokenPricing:
    model: str
    input_usd_per_million: Decimal
    cached_input_usd_per_million: Decimal
    output_usd_per_million: Decimal
    tier: str = "Standard"
    context: str = "short"

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        for key, item in value.items():
            if isinstance(item, Decimal):
                value[key] = float(item)
        return value


TOKEN_PRICING: dict[str, TokenPricing] = {
    "gpt-6.2-sol": TokenPricing(
        model="gpt-6.2-sol",
        input_usd_per_million=Decimal("2.00"),
        cached_input_usd_per_million=Decimal("0.10"),
        output_usd_per_million=Decimal("10.00"),
    ),
    "gpt-6-sol": TokenPricing(
        model="gpt-6-sol",
        input_usd_per_million=Decimal("2.00"),
        cached_input_usd_per_million=Decimal("0.20"),
        output_usd_per_million=Decimal("10.00"),
    ),
}


def configured_pricing_model() -> str:
    """Return the configured model, using the service's Sol default for estimates."""

    return os.environ.get("CODEX_MODEL", "").strip().lower() or DEFAULT_PRICING_MODEL


def resolve_pricing(model: str | None = None) -> TokenPricing | None:
    return TOKEN_PRICING.get((model or configured_pricing_model()).strip().lower())


def token_cost(
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
    *,
    model: str | None = None,
) -> dict[str, Any]:
    """Calculate an estimated USD cost from cumulative usage counters.

    ``cached_input_tokens`` is a subset of ``input_tokens``. Only the remaining
    input tokens use the regular input rate, which prevents cached input from
    being charged twice.
    """

    input_total = max(0, int(input_tokens))
    cached = min(input_total, max(0, int(cached_input_tokens)))
    output = max(0, int(output_tokens))
    uncached = input_total - cached
    pricing_model = (model or configured_pricing_model()).strip().lower()
    pricing = resolve_pricing(pricing_model)
    result: dict[str, Any] = {
        "uncached_input_tokens": uncached,
        "cost_usd": None,
        "cost_currency": "USD",
        "pricing_model": pricing_model,
        "pricing_tier": "Standard",
        "pricing_context": "short",
        "pricing_source_url": PRICING_SOURCE_URL,
        "pricing_available": pricing is not None,
    }
    if pricing is None:
        return result
    cost = (
        Decimal(uncached) * pricing.input_usd_per_million
        + Decimal(cached) * pricing.cached_input_usd_per_million
        + Decimal(output) * pricing.output_usd_per_million
    ) / Decimal(1_000_000)
    result["cost_usd"] = float(cost.quantize(Decimal("0.000001")))
    result["pricing_tier"] = pricing.tier
    result["pricing_context"] = pricing.context
    result["pricing_rates_usd_per_million"] = pricing.as_dict()
    return result
