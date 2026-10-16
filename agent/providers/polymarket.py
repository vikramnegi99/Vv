"""Polymarket public-data provider (primary).

Instrument class: binary prediction markets ("Will X happen?"), YES/NO.

Why this provider (see docs/PROVIDER.md for the full evaluation):
  * Official, public, read-only endpoints - no API key, no account, no
    credentials of any kind.
  * The market object carries a machine-readable implied probability
    (outcomePrices) plus a real order-book bid/ask and spread, which is
    exactly what a probability-edge strategy needs.
  * Resolution is published by the provider, so positions can be settled on
    confirmed outcomes rather than on transient price moves.

Limitations are documented in docs/PROVIDER.md. This module is READ-ONLY:
there is no order-placement code path anywhere in it.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Optional

import requests

from ..models import Market, PricePoint, ProviderHealth
from ..util import utcnow_iso, iso
from .base import MarketProvider


def _loads_maybe(value):
    """Polymarket sometimes returns JSON-encoded strings for list fields."""
    if value is None:
        return None
    if isinstance(value, (list, dict)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return None
    return None


def _to_float(value) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


class PolymarketProvider(MarketProvider):
    name = "polymarket"
    instrument_class = "binary prediction markets (YES/NO)"

    def __init__(self, cfg, session: Optional[requests.Session] = None):
        super().__init__(cfg)
        self.gamma = cfg.get_path("provider.polymarket.gamma_base",
                                  "https://gamma-api.polymarket.com")
        self.clob = cfg.get_path("provider.polymarket.clob_base",
                                 "https://clob.polymarket.com")
        self.timeout = int(cfg.get_path("provider.polymarket.request_timeout", 15))
        self.min_volume = float(cfg.get_path("provider.polymarket.min_volume", 0.0))
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "paper-agent/2.0 (research)"})

    # -- low level ----------------------------------------------------------
    def _get(self, url: str, params: Optional[dict] = None):
        started = time.perf_counter()
        resp = self.session.get(url, params=params, timeout=self.timeout)
        latency = (time.perf_counter() - started) * 1000.0
        resp.raise_for_status()
        return resp.json(), latency

    def health(self) -> ProviderHealth:
        try:
            data, latency = self._get(f"{self.gamma}/markets", {"limit": 1})
            ok = isinstance(data, list)
            return ProviderHealth(provider=self.name, ok=ok, latency_ms=round(latency, 1),
                                  error=None if ok else "unexpected payload shape")
        except Exception as exc:  # noqa: BLE001 - health must never raise
            return ProviderHealth(provider=self.name, ok=False, latency_ms=0.0,
                                  error=f"{type(exc).__name__}: {exc}")

    # -- parsing ------------------------------------------------------------
    def _parse_market(self, raw: dict) -> Optional[Market]:
        mid = str(raw.get("id") or raw.get("conditionId") or "")
        if not mid:
            return None
        outcomes = _loads_maybe(raw.get("outcomes")) or []
        prices = _loads_maybe(raw.get("outcomePrices")) or []
        tokens = _loads_maybe(raw.get("clobTokenIds")) or []
        yes_price = None
        if prices:
            yes_price = _to_float(prices[0])
        closed = bool(raw.get("closed"))
        resolved = False
        resolved_outcome = None
        if closed and prices:
            p0 = _to_float(prices[0]) or 0.0
            if p0 >= 0.99:
                resolved, resolved_outcome = True, "YES"
            elif p0 <= 0.01:
                resolved, resolved_outcome = True, "NO"
        bid = _to_float(raw.get("bestBid"))
        ask = _to_float(raw.get("bestAsk"))
        spread = _to_float(raw.get("spread"))
        if spread is None and bid is not None and ask is not None:
            spread = round(ask - bid, 6)
        return Market(
            id=mid,
            question=raw.get("question") or raw.get("title") or mid,
            provider=self.name,
            yes_price=yes_price,
            bid=bid,
            ask=ask,
            spread=spread,
            liquidity=_to_float(raw.get("liquidity")),
            volume=_to_float(raw.get("volume")),
            end_date=raw.get("endDate"),
            active=bool(raw.get("active", True)),
            closed=closed,
            resolved=resolved,
            resolved_outcome=resolved_outcome,
            token_id=str(tokens[0]) if tokens else None,
            ts=utcnow_iso(),
            raw={"slug": raw.get("slug"), "conditionId": raw.get("conditionId")},
        )

    # -- interface ----------------------------------------------------------
    def discover_markets(self, limit: int = 50) -> list[Market]:
        data, _ = self._get(
            f"{self.gamma}/markets",
            {
                "active": "true",
                "closed": "false",
                "limit": max(1, min(limit, 100)),
                "order": "volume24hr",
                "ascending": "false",
            },
        )
        markets: list[Market] = []
        for raw in data or []:
            m = self._parse_market(raw)
            if m is None:
                continue
            if m.volume is not None and m.volume < self.min_volume:
                continue
            markets.append(m)
        return markets

    def get_latest_quote(self, market_id: str) -> Optional[Market]:
        data, _ = self._get(f"{self.gamma}/markets", {"id": market_id})
        if isinstance(data, list) and data:
            return self._parse_market(data[0])
        if isinstance(data, dict):
            return self._parse_market(data)
        return None

    def get_price_history(self, market_id: str, points: int = 48) -> list[PricePoint]:
        quote = self.get_latest_quote(market_id)
        if quote is None or not quote.token_id:
            return []
        data, _ = self._get(
            f"{self.clob}/prices-history",
            {"market": quote.token_id, "interval": "1d", "fidelity": 60},
        )
        history = (data or {}).get("history", []) if isinstance(data, dict) else []
        out: list[PricePoint] = []
        for pt in history[-points:]:
            try:
                ts = datetime.fromtimestamp(int(pt["t"]), tz=timezone.utc)
                out.append(PricePoint(ts=iso(ts), price=float(pt["p"])))
            except (KeyError, TypeError, ValueError):
                continue
        return out
