"""Plain data structures passed between providers, strategy and execution."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Market:
    id: str
    question: str
    provider: str
    yes_price: Optional[float] = None      # market-implied probability of YES
    bid: Optional[float] = None
    ask: Optional[float] = None
    spread: Optional[float] = None
    liquidity: Optional[float] = None
    volume: Optional[float] = None
    end_date: Optional[str] = None
    active: bool = True
    closed: bool = False
    resolved: bool = False
    resolved_outcome: Optional[str] = None
    token_id: Optional[str] = None
    ts: Optional[str] = None
    raw: dict = field(default_factory=dict)


@dataclass
class PricePoint:
    ts: str
    price: float


@dataclass
class ProviderHealth:
    provider: str
    ok: bool
    latency_ms: float = 0.0
    error: Optional[str] = None
    stale: bool = False
    eligible_markets: int = 0


@dataclass
class Decision:
    market_id: str
    question: str
    market_prob: Optional[float]
    est_prob: Optional[float]
    edge: Optional[float]
    confidence: float
    decision: str                 # TRADE / HOLD
    direction: Optional[str]      # YES / NO / None
    features: dict = field(default_factory=dict)
    data_ts: Optional[str] = None
    reason: str = ""
    risk_flags: list = field(default_factory=list)
    stage_results: dict = field(default_factory=dict)
    exec_type: str = "STRATEGY"
