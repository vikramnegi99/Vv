"""Deterministic synthetic provider.

Used for two purposes only:
  1. the engine canary (proving the execution pipeline end to end), and
  2. unit tests.

It NEVER represents real market data. Every market it returns is labelled
provider='mock' and any fill produced from it is tagged CANARY.
"""
from __future__ import annotations

import math
import random
from typing import Optional

from ..models import Market, PricePoint, ProviderHealth
from ..util import utcnow_iso, iso, utcnow
from datetime import timedelta

from .base import MarketProvider


class MockProvider(MarketProvider):
    name = "mock"
    instrument_class = "synthetic-canary"

    def __init__(self, cfg, *, seed: Optional[int] = None, n_markets: int = 6,
                 healthy: bool = True, resolve_all: bool = False,
                 resolve_ids: Optional[list[str]] = None, price_shift: float = 0.0):
        super().__init__(cfg)
        base_seed = seed if seed is not None else int(cfg.get_path("provider.mock.seed", 42))
        self.rng = random.Random(base_seed)
        self.n_markets = n_markets
        self._healthy = healthy
        self.resolve_all = resolve_all
        self.resolve_ids = set(resolve_ids or [])
        self.price_shift = price_shift
        self._markets = self._build()

    def _build(self) -> dict[str, Market]:
        out: dict[str, Market] = {}
        for i in range(self.n_markets):
            mid = f"MOCK-{i + 1:03d}"
            yes = round(0.30 + 0.08 * i, 4)
            # A deterministic wobble so the baseline estimator has something to see.
            drift = 0.02 * math.sin(i + 1)
            yes = max(0.05, min(0.95, yes + drift + self.price_shift))
            bid = round(max(0.01, yes - 0.01), 4)
            ask = round(min(0.99, yes + 0.01), 4)
            resolved = self.resolve_all or mid in self.resolve_ids
            out[mid] = Market(
                id=mid,
                question=f"[SYNTHETIC] Will mock event {i + 1} resolve YES?",
                provider=self.name,
                yes_price=yes,
                bid=bid,
                ask=ask,
                spread=round(ask - bid, 4),
                liquidity=50_000.0,
                volume=120_000.0,
                end_date=iso(utcnow() + timedelta(days=30)),
                active=True,
                closed=resolved,
                resolved=resolved,
                resolved_outcome="YES" if resolved else None,
                token_id=f"mock-token-{i + 1}",
                ts=utcnow_iso(),
                raw={"synthetic": True},
            )
        return out

    def health(self) -> ProviderHealth:
        return ProviderHealth(
            provider=self.name,
            ok=self._healthy,
            latency_ms=0.0,
            error=None if self._healthy else "synthetic provider forced unhealthy",
            stale=False,
            eligible_markets=len(self._markets) if self._healthy else 0,
        )

    def discover_markets(self, limit: int = 50) -> list[Market]:
        if not self._healthy:
            return []
        return list(self._markets.values())[:limit]

    def get_latest_quote(self, market_id: str) -> Optional[Market]:
        return self._markets.get(market_id)

    def get_price_history(self, market_id: str, points: int = 48) -> list[PricePoint]:
        m = self._markets.get(market_id)
        if m is None:
            return []
        rng = random.Random(hash(market_id) & 0xFFFF)
        hist: list[PricePoint] = []
        price = m.yes_price - 0.03
        now = utcnow()
        for k in range(points):
            price += rng.uniform(-0.006, 0.008)
            price = max(0.02, min(0.98, price))
            t = now - timedelta(minutes=5 * (points - k))
            hist.append(PricePoint(ts=iso(t), price=round(price, 4)))
        if hist:
            hist[-1] = PricePoint(ts=hist[-1].ts, price=m.yes_price)
        return hist


def canary_scenario(cfg, seed: int = 1234):
    """A fixed synthetic scenario engineered so the real strategy pipeline
    produces a signal. Used ONLY by the canary - never presented as market data."""
    market = Market(
        id="CANARY-SYN-001",
        question="[CANARY / SYNTHETIC TEST] Pipeline self-check - not a real market",
        provider="mock",
        yes_price=0.40,
        bid=0.39,
        ask=0.41,
        spread=0.02,
        liquidity=50_000.0,
        volume=120_000.0,
        end_date=iso(utcnow() + timedelta(days=30)),
        active=True,
        closed=False,
        token_id="canary-token",
        ts=utcnow_iso(),
        raw={"synthetic": True, "canary": True},
    )
    hist: list[PricePoint] = []
    price = 0.46
    now = utcnow()
    for k in range(24):
        price += 0.005
        hist.append(PricePoint(ts=iso(now - timedelta(minutes=5 * (24 - k))),
                               price=round(min(0.58, price), 4)))
    return market, hist
