"""Signal / edge / cost / threshold pipeline.

Pipeline:
  market data -> validation -> features -> probability estimate -> signal
  -> edge check -> transaction-cost check -> (risk check happens in risk.py)

Every stage outcome is recorded so the dashboard can show *why* a market was
rejected. A HOLD always carries a machine-readable reason code.
"""
from __future__ import annotations

from .estimator import estimate_probability
from .models import Decision, Market, PricePoint

# Exact rejection reason codes (also surfaced in the rejection analytics panel).
R_MARKET_INELIGIBLE = "MARKET_INELIGIBLE"
R_INVALID_PRICE = "INVALID_PRICE"
R_STALE_DATA = "STALE_DATA"
R_INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
R_INSUFFICIENT_LIQUIDITY = "INSUFFICIENT_LIQUIDITY"
R_SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
R_INSUFFICIENT_EDGE = "INSUFFICIENT_EDGE"
R_CONFIDENCE_TOO_LOW = "CONFIDENCE_TOO_LOW"
R_EDGE_BELOW_COSTS = "EDGE_BELOW_COSTS"
R_AI_HOLD = "AI_HOLD"

ALL_REASONS = [
    R_MARKET_INELIGIBLE, R_INVALID_PRICE, R_STALE_DATA, R_INSUFFICIENT_HISTORY,
    R_INSUFFICIENT_LIQUIDITY, R_SPREAD_TOO_WIDE, R_INSUFFICIENT_EDGE,
    R_CONFIDENCE_TOO_LOW, R_EDGE_BELOW_COSTS, R_AI_HOLD,
]


def estimated_round_trip_cost(market: Market, cfg) -> float:
    """Spread + simulated fees + simulated slippage, both sides."""
    fee = 2.0 * float(cfg.get_path("costs.fee_bps", 20)) / 10_000.0
    slip = 2.0 * float(cfg.get_path("costs.slippage_bps", 10)) / 10_000.0
    spread = market.spread or 0.0
    return round(spread + fee + slip, 6)


def _thresholds(cfg, mode: str):
    if mode == "EXPLORATION":
        return (
            float(cfg.get_path("strategy.exploration.min_edge", 0.02)),
            float(cfg.get_path("strategy.exploration.min_confidence", 0.35)),
        )
    return (
        float(cfg.get_path("strategy.min_edge", 0.05)),
        float(cfg.get_path("strategy.min_confidence", 0.55)),
    )


def evaluate_market(market: Market, history: list[PricePoint], cfg,
                    mode: str = "STRATEGY") -> Decision:
    """Run the full signal pipeline for one market. Never raises on bad data."""
    min_edge, min_conf = _thresholds(cfg, mode)
    max_age = float(cfg.get_path("strategy.max_data_age_seconds", 900))
    max_spread = float(cfg.get_path("strategy.max_spread", 0.06))
    min_liq = float(cfg.get_path("strategy.min_liquidity", 5000.0))
    min_points = int(cfg.get_path("strategy.min_history_points", 4))

    stages: dict = {}
    base = dict(market_id=market.id, question=market.question, exec_type=mode)

    def hold(reason, **kw):
        kw.setdefault("est_prob", None)
        kw.setdefault("edge", None)
        kw.setdefault("confidence", 0.0)
        return Decision(market_prob=market.yes_price, decision="HOLD", direction=None,
                        reason=reason, stage_results=stages, **base, **kw)

    # --- validation --------------------------------------------------------
    if market.closed or market.resolved or not market.active:
        stages["validation"] = False
        return hold(R_MARKET_INELIGIBLE)
    if market.yes_price is None or not (0.0 < market.yes_price < 1.0):
        stages["validation"] = False
        return hold(R_INVALID_PRICE)
    stages["validation"] = True

    # --- estimate ----------------------------------------------------------
    est, confidence, feats = estimate_probability(market, history, cfg)
    stages["estimated"] = est is not None
    if est is None:
        return hold(R_INVALID_PRICE, confidence=confidence,
                    features=feats, data_ts=market.ts)
    data_age = feats.get("data_age_seconds")

    # --- data quality ------------------------------------------------------
    if data_age is not None and data_age > max_age:
        stages["freshness"] = False
        return hold(R_STALE_DATA, est_prob=est, confidence=confidence,
                    features=feats, data_ts=market.ts)
    stages["freshness"] = True

    if (feats.get("n_points") or 0) < min_points:
        stages["history"] = False
        return hold(R_INSUFFICIENT_HISTORY, est_prob=est, confidence=confidence,
                    features=feats, data_ts=market.ts)
    stages["history"] = True

    if market.liquidity is not None and market.liquidity < min_liq:
        stages["liquidity"] = False
        return hold(R_INSUFFICIENT_LIQUIDITY, est_prob=est, confidence=confidence,
                    features=feats, data_ts=market.ts)
    stages["liquidity"] = True

    if market.spread is not None and market.spread > max_spread:
        stages["spread"] = False
        return hold(R_SPREAD_TOO_WIDE, est_prob=est, confidence=confidence,
                    features=feats, data_ts=market.ts)
    stages["spread"] = True

    # --- signal + edge -----------------------------------------------------
    edge = abs(est - market.yes_price)
    direction = "YES" if est > market.yes_price else "NO"
    stages["edge_value"] = round(edge, 6)

    if edge < min_edge:
        stages["edge"] = False
        return hold(R_INSUFFICIENT_EDGE, est_prob=est, edge=edge, confidence=confidence,
                    features=feats, data_ts=market.ts)
    stages["edge"] = True

    if confidence < min_conf:
        stages["confidence"] = False
        return hold(R_CONFIDENCE_TOO_LOW, est_prob=est, edge=edge, confidence=confidence,
                    features=feats, data_ts=market.ts)
    stages["confidence"] = True

    # --- transaction-cost check -------------------------------------------
    cost = estimated_round_trip_cost(market, cfg)
    stages["estimated_cost"] = cost
    if edge <= cost:
        stages["cost"] = False
        return hold(R_EDGE_BELOW_COSTS, est_prob=est, edge=edge, confidence=confidence,
                    features=feats, data_ts=market.ts)
    stages["cost"] = True

    # --- pass --------------------------------------------------------------
    return Decision(
        market_id=market.id, question=market.question,
        market_prob=market.yes_price, est_prob=est, edge=round(edge, 6),
        confidence=confidence, decision="TRADE", direction=direction,
        features=feats, data_ts=market.ts,
        reason=f"edge {edge:.4f} > cost {cost:.4f}",
        stage_results=stages, exec_type=mode,
    )
