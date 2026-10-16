"""Provider registry."""
from __future__ import annotations

from .base import MarketProvider
from .mock import MockProvider
from .polymarket import PolymarketProvider

_REGISTRY = {
    "polymarket": PolymarketProvider,
    "mock": MockProvider,
}


def build_provider(cfg, name: str | None = None) -> MarketProvider:
    name = (name or cfg.get_path("provider.primary", "polymarket")).lower()
    if name not in _REGISTRY:
        raise ValueError(f"unknown provider '{name}'; available: {sorted(_REGISTRY)}")
    return _REGISTRY[name](cfg)


__all__ = ["MarketProvider", "MockProvider", "PolymarketProvider", "build_provider"]
