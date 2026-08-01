"""Replay-report flattening for StockAdvisor context diagnostics.

The live Advisor persists a nested ``advisor_context`` object so that the
contract remains extensible. Replay CSVs need stable scalar columns for
comparison, while also retaining the complete diagnostic payload as JSON.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Mapping, Optional


_CONTEXT_COLUMNS = (
    "advisor_context_present",
    "advisor_context_as_of",
    "advisor_context_symbol",
    "stock_rank_influence",
    "stock_rank_availability",
    "stock_rank_rank_time",
    "stock_rank_age_seconds",
    "stock_rank_fresh",
    "stock_rank_position",
    "stock_rank_universe_size",
    "stock_rank_attention_tier",
    "stock_rank_direction",
    "stock_rank_classification",
    "stock_rank_total_score",
    "stock_rank_movement_score",
    "stock_rank_quality_score",
    "stock_rank_range_penalty",
    "stock_rank_stall_penalty",
    "stock_rank_reason_codes",
    "market_regime_influence",
    "market_regime_availability",
    "market_regime_state",
    "market_regime_confidence",
    "market_regime_age_seconds",
    "market_regime_buy_support",
    "market_regime_sell_support",
    "market_regime_continuation_support",
    "market_regime_reversal_support",
    "market_regime_reason_codes",
    "market_regime_evidence",
    "market_regime_hysteresis",
    "market_regime_metrics",
    "advisor_diagnostics_json",
)


def _empty_context_fields() -> Dict[str, Any]:
    output = {column: None for column in _CONTEXT_COLUMNS}
    output["advisor_context_present"] = False
    return output


def _require_mapping(value: Any, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"Advisor reporting requires {field} to be an object")
    return value


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def flatten_advisor_context_for_csv(
    *,
    advisor_action: Optional[str],
    advisor_diagnostics: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Return stable CSV fields for one setup-evaluation diagnostic.

    Setup evaluations that never reached StockAdvisor intentionally return
    blank context columns. Once an Advisor action exists, the nested context is
    mandatory: a missing contract is a replay-report failure rather than a
    silently incomplete row.
    """

    if advisor_action is None:
        return _empty_context_fields()

    diagnostics = _require_mapping(
        advisor_diagnostics,
        field="advisor_diagnostics for an Advisor-evaluated candidate",
    )
    context = _require_mapping(
        diagnostics.get("advisor_context"),
        field="advisor_diagnostics.advisor_context",
    )
    stock_rank = _require_mapping(
        context.get("stock_rank"),
        field="advisor_context.stock_rank",
    )
    market_regime = _require_mapping(
        context.get("market_regime"),
        field="advisor_context.market_regime",
    )

    return {
        "advisor_context_present": True,
        "advisor_context_as_of": context.get("as_of"),
        "advisor_context_symbol": context.get("symbol"),
        "stock_rank_influence": context.get("stock_rank_influence"),
        "stock_rank_availability": stock_rank.get("availability"),
        "stock_rank_rank_time": stock_rank.get("rank_time"),
        "stock_rank_age_seconds": stock_rank.get("age_seconds"),
        "stock_rank_fresh": stock_rank.get("fresh"),
        "stock_rank_position": stock_rank.get("rank_position"),
        "stock_rank_universe_size": stock_rank.get("universe_size"),
        "stock_rank_attention_tier": stock_rank.get("attention_tier"),
        "stock_rank_direction": stock_rank.get("direction"),
        "stock_rank_classification": stock_rank.get("classification"),
        "stock_rank_total_score": stock_rank.get("total_score"),
        "stock_rank_movement_score": stock_rank.get("movement_score"),
        "stock_rank_quality_score": stock_rank.get("quality_score"),
        "stock_rank_range_penalty": stock_rank.get("range_penalty"),
        "stock_rank_stall_penalty": stock_rank.get("stall_penalty"),
        "stock_rank_reason_codes": _json(stock_rank.get("reason_codes", [])),
        "market_regime_influence": context.get("market_regime_influence"),
        "market_regime_availability": market_regime.get("availability"),
        "market_regime_state": market_regime.get("state"),
        "market_regime_confidence": market_regime.get("confidence"),
        "market_regime_age_seconds": market_regime.get("age_seconds"),
        "market_regime_buy_support": market_regime.get("buy_support"),
        "market_regime_sell_support": market_regime.get("sell_support"),
        "market_regime_continuation_support": market_regime.get(
            "continuation_support"
        ),
        "market_regime_reversal_support": market_regime.get("reversal_support"),
        "market_regime_reason_codes": _json(
            market_regime.get("reason_codes", [])
        ),
        "market_regime_evidence": _json(market_regime.get("evidence", [])),
        "market_regime_hysteresis": _json(
            market_regime.get("hysteresis", {})
        ),
        "market_regime_metrics": _json(market_regime.get("metrics", {})),
        "advisor_diagnostics_json": _json(diagnostics),
    }


__all__ = ["flatten_advisor_context_for_csv"]
