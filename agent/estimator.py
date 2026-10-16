"""Baseline probability estimator (transparent, no paid credentials).

This is the DEFAULT estimator. It is a documented, bounded heuristic:

    anchor     = mean of the last `smoothing_window` prices
    momentum   = last_price - price `momentum_window` points ago
    raw        = anchor + momentum_weight * momentum
    estimate   = shrink(raw toward the market price by `shrinkage`)

An optional LLM analyzer can be plugged in as a *separate* configurable
component; it is off by default and is never required.

The probability floor/ceiling only clamp the estimate. They cannot create an
artificial edge: the floor is 0.02 and the ceiling 0.98, both far below the
minimum edge (0.05), so a clamped estimate can never manufacture a trade.
"""
from __future__ import annotations

from typing import Optional

from .features import compute_features
from .models import Market, PricePoint
from .util import clamp


def _confidence(features: dict, cfg) -> float:
    """Confidence in [0,1] driven by data quality, not by the estimate itself."""
    smooth_w = max(1, int(cfg.get_path("strategy.estimator.smoothing_window", 12)))
    max_age = float(cfg.get_path("strategy.max_data_age_seconds", 900))
    max_spread = float(cfg.get_path("strategy.max_spread", 0.06))

    n = features.get("n_points") or 0
    data_q = clamp(n / smooth_w, 0.0, 1.0)

    age = features.get("data_age_seconds")
    fresh_q = 1.0 if age is None else clamp(1.0 - (age / max_age), 0.0, 1.0)

    spread = features.get("spread")
    spread_q = 1.0 if spread is None else clamp(1.0 - (spread / max_spread), 0.0, 1.0)

    vol = features.get("volatility") or 0.0
    vol_q = clamp(1.0 / (1.0 + 25.0 * vol), 0.0, 1.0)

    return round(0.35 * data_q + 0.25 * fresh_q + 0.25 * spread_q + 0.15 * vol_q, 4)


def estimate_probability(market: Market, history: list[PricePoint], cfg):
    """Return (estimated_prob, confidence, features)."""
    feats = compute_features(market, history, cfg)
    floor = float(cfg.get_path("strategy.probability_floor", 0.02))
    ceil = float(cfg.get_path("strategy.probability_ceiling", 0.98))
    weight = float(cfg.get_path("strategy.estimator.momentum_weight", 0.35))
    shrink = float(cfg.get_path("strategy.estimator.shrinkage", 0.15))

    market_price = market.yes_price
    anchor = feats.get("anchor_mean")
    if anchor is None:
        anchor = market_price
    if anchor is None:
        return None, 0.0, feats

    momentum = feats.get("momentum") or 0.0
    raw = anchor + weight * momentum

    if market_price is not None:
        est = (1.0 - shrink) * raw + shrink * market_price
    else:
        est = raw

    est = clamp(est, floor, ceil)
    return round(est, 6), _confidence(feats, cfg), feats
