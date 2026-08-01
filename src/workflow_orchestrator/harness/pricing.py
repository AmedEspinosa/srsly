"""Token-to-cost estimation — SRS FR-21.

Claude Code reports ``total_cost_usd`` directly in its ``result`` event, so no
estimation is needed there. Codex reports token counts only, so the cost ceiling
needs a price table to enforce against.

Prices are USD per million tokens and are deliberately configurable: they change,
and a stale table silently mis-enforces the ceiling. Override per model with
``WORKFLOW_PRICE_<MODEL>_INPUT`` / ``_OUTPUT`` environment variables.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

MILLION = 1_000_000


@dataclass(frozen=True)
class ModelPrice:
    input_per_mtok: float
    output_per_mtok: float
    cached_input_per_mtok: float | None = None

    def estimate(
        self, *, input_tokens: int, output_tokens: int, cached_input_tokens: int = 0
    ) -> float:
        cached_rate = (
            self.cached_input_per_mtok
            if self.cached_input_per_mtok is not None
            else self.input_per_mtok * 0.1
        )
        billed_input = max(input_tokens - cached_input_tokens, 0)
        return (
            billed_input * self.input_per_mtok
            + cached_input_tokens * cached_rate
            + output_tokens * self.output_per_mtok
        ) / MILLION


#: Fallback used when a model is not in the table. Deliberately on the expensive
#: side: over-estimating stops a run early, under-estimating blows the ceiling.
DEFAULT_PRICE = ModelPrice(input_per_mtok=5.00, output_per_mtok=25.00)

PRICES: dict[str, ModelPrice] = {
    "gpt-5": ModelPrice(input_per_mtok=1.25, output_per_mtok=10.00),
    "gpt-5-codex": ModelPrice(input_per_mtok=1.25, output_per_mtok=10.00),
    "o3": ModelPrice(input_per_mtok=2.00, output_per_mtok=8.00),
    "claude-opus-5": ModelPrice(input_per_mtok=5.00, output_per_mtok=25.00),
    "claude-sonnet-5": ModelPrice(input_per_mtok=3.00, output_per_mtok=15.00),
    "claude-haiku-4-5": ModelPrice(input_per_mtok=1.00, output_per_mtok=5.00),
}


def _env_override(model: str) -> ModelPrice | None:
    key = model.upper().replace("-", "_").replace(".", "_")
    raw_in = os.environ.get(f"WORKFLOW_PRICE_{key}_INPUT")
    raw_out = os.environ.get(f"WORKFLOW_PRICE_{key}_OUTPUT")
    if raw_in is None or raw_out is None:
        return None
    try:
        return ModelPrice(input_per_mtok=float(raw_in), output_per_mtok=float(raw_out))
    except ValueError:
        return None


def price_for(model: str | None) -> ModelPrice:
    if not model:
        return DEFAULT_PRICE
    override = _env_override(model)
    if override is not None:
        return override
    if model in PRICES:
        return PRICES[model]
    # Match on prefix so dated snapshots resolve to their base model.
    for name, price in PRICES.items():
        if model.startswith(name):
            return price
    return DEFAULT_PRICE


def estimate_cost(
    model: str | None,
    *,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
) -> float:
    return price_for(model).estimate(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached_input_tokens,
    )
