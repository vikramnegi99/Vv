"""MarketProvider interface.

A provider supplies, for one clearly-defined instrument class:
  * market discovery
  * latest quotes
  * historical prices
  * eligibility / resolution state

Providers are strictly read-only. None of them can place an order.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from ..models import Market, PricePoint, ProviderHealth


class MarketProvider(ABC):
    name = "base"
    #: human-readable instrument class, used in reports
    instrument_class = "unspecified"

    def __init__(self, cfg):
        self.cfg = cfg

    @abstractmethod
    def health(self) -> ProviderHealth:
        """Probe the provider and return a health record (never raises)."""

    @abstractmethod
    def discover_markets(self, limit: int = 50) -> list[Market]:
        """Return active, tradeable markets ordered by liquidity/volume."""

    @abstractmethod
    def get_latest_quote(self, market_id: str) -> Optional[Market]:
        """Return a fresh quote for one market, or None on failure."""

    @abstractmethod
    def get_price_history(self, market_id: str, points: int = 48) -> list[PricePoint]:
        """Return recent price history, oldest first. May return [] if absent."""
