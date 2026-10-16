"""Feature engineering from raw market + price-history inputs.

Kept deliberately small and transparent: every feature is a plain statistic
that a reviewer can recompute by hand. No hidden thresholds live here.
"""
from __future__ import annotations

import statistics
from typing import Optional

from .models import Market, PricePoint
from .util import hours_since, utcnow


def data_age_seconds(market: Market) -> Optional[float]:
    if not market.ts:
        return None
    return max(0.0, hours_since(market.ts, utcnow()) * 3600.0)


def compute_features(market: Market, history: list[PricePoint], cfg) -> dict:
    prices = [p.price for p in history if p.price is not None]
    n = len(prices)
    feats: dict = {
        "n_points": n,
        "last_price": prices[-1] if prices else None,
        "data_age_seconds": data_age_seconds(market),
        "spread": market.spread,
        "liquidity": market.liquidity,
        "volume": market.volume,
    }
    if n >= 2:
        rets = [
            (prices[i] - prices[i - 1]) / prices[i - 1]
            for i in range(1, n)
            if prices[i - 1] not in (0, None)
        ]
        feats["volatility"] = round(statistics.pstdev(rets), 6) if len(rets) > 1 else 0.0
    else:
        feats["volatility"] = None

    mom_w = int(cfg.get_path("strategy.estimator.momentum_window", 6))
    if n > mom_w:
        feats["momentum"] = round(prices[-1] - prices[-1 - mom_w], 6)
    elif n >= 2:
        feats["momentum"] = round(prices[-1] - prices[0], 6)
    else:
        feats["momentum"] = 0.0

    smooth_w = int(cfg.get_path("strategy.estimator.smoothing_window", 12))
    window = prices[-smooth_w:] if n else []
    feats["anchor_mean"] = round(sum(window) / len(window), 6) if window else None
    return feats
