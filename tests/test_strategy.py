"""Signal pipeline: approvals, rejections and the probability-floor guarantee."""
from agent.models import Market, PricePoint
from agent.strategy import (
    evaluate_market, R_INSUFFICIENT_EDGE, R_INVALID_PRICE, R_MARKET_INELIGIBLE,
    R_STALE_DATA, R_INSUFFICIENT_HISTORY, R_SPREAD_TOO_WIDE, R_INSUFFICIENT_LIQUIDITY,
)
from agent.util import iso, utcnow
from datetime import timedelta


def _hist(prices):
    now = utcnow()
    return [PricePoint(ts=iso(now - timedelta(minutes=5 * (len(prices) - i))), price=p)
            for i, p in enumerate(prices)]


def test_engineered_scenario_produces_a_trade(cfg, scenario):
    market, hist = scenario
    d = evaluate_market(market, hist, cfg, "STRATEGY")
    assert d.decision == "TRADE"
    assert d.direction in ("YES", "NO")
    assert d.edge >= float(cfg.get_path("strategy.min_edge"))
    assert d.confidence >= float(cfg.get_path("strategy.min_confidence"))


def test_closed_market_is_ineligible(cfg, scenario):
    market, hist = scenario
    market.closed = True
    d = evaluate_market(market, hist, cfg, "STRATEGY")
    assert d.decision == "HOLD" and d.reason == R_MARKET_INELIGIBLE


def test_missing_price_is_invalid(cfg, scenario):
    market, hist = scenario
    market.yes_price = None
    d = evaluate_market(market, hist, cfg, "STRATEGY")
    assert d.decision == "HOLD" and d.reason == R_INVALID_PRICE


def test_stale_data_holds(cfg, scenario):
    market, hist = scenario
    market.ts = iso(utcnow() - timedelta(hours=2))
    d = evaluate_market(market, hist, cfg, "STRATEGY")
    assert d.decision == "HOLD" and d.reason == R_STALE_DATA


def test_insufficient_history_holds(cfg, scenario):
    market, hist = scenario
    d = evaluate_market(market, hist[:1], cfg, "STRATEGY")
    assert d.decision == "HOLD" and d.reason == R_INSUFFICIENT_HISTORY


def test_wide_spread_holds(cfg, scenario):
    market, hist = scenario
    market.spread = 0.5
    d = evaluate_market(market, hist, cfg, "STRATEGY")
    assert d.decision == "HOLD" and d.reason == R_SPREAD_TOO_WIDE


def test_low_liquidity_holds(cfg, scenario):
    market, hist = scenario
    market.liquidity = 1.0
    d = evaluate_market(market, hist, cfg, "STRATEGY")
    assert d.decision == "HOLD" and d.reason == R_INSUFFICIENT_LIQUIDITY


def test_flat_history_gives_no_edge(cfg):
    market = Market(id="flat", question="flat", provider="mock", yes_price=0.5,
                    bid=0.49, ask=0.51, spread=0.02, liquidity=50000.0,
                    active=True, ts=iso(utcnow()))
    d = evaluate_market(market, _hist([0.5] * 20), cfg, "STRATEGY")
    assert d.decision == "HOLD" and d.reason == R_INSUFFICIENT_EDGE


def test_probability_floor_does_not_create_artificial_edge(cfg):
    # Market at 0.01, no real signal -> estimate clamps to the 0.02 floor,
    # giving an edge of 0.01 which is still below min_edge (0.05).
    market = Market(id="floor", question="floor", provider="mock", yes_price=0.01,
                    bid=0.005, ask=0.015, spread=0.01, liquidity=50000.0,
                    active=True, ts=iso(utcnow()))
    d = evaluate_market(market, _hist([0.02] * 20), cfg, "STRATEGY")
    assert d.decision == "HOLD"
    assert d.reason in (R_INSUFFICIENT_EDGE,)


def test_exploration_uses_relaxed_but_bounded_thresholds(cfg, scenario):
    market, hist = scenario
    prod = evaluate_market(market, hist, cfg, "STRATEGY")
    expl = evaluate_market(market, hist, cfg, "EXPLORATION")
    assert expl.exec_type == "EXPLORATION"
    # Exploration must never be *stricter* than production.
    assert expl.confidence >= prod.confidence - 1e-9
