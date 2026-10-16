"""Signal pipeline: approvals, rejections and the probability-floor guarantee."""
from agent.models import Market, PricePoint
from agent.strategy import (
    evaluate_market, R_INSUFFICIENT_EDGE, R_INVALID_PRICE, R_MARKET_INELIGIBLE,
    R_STALE_DATA, R_INSUFFICIENT_HISTORY, R_SPREAD_TOO_WIDE, R_INSUFFICIENT_LIQUIDITY,
    R_MARKET_NEAR_RESOLVED,
)  # noqa: F401
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
    # A market pinned at 0.01 with no real signal: the estimator clamps up to
    # the 0.02 floor, but the resulting edge (0.01) is still far below the
    # minimum edge (0.05), so the clamp cannot manufacture a trade.
    from agent.estimator import estimate_probability
    market = Market(id="floor", question="floor", provider="mock", yes_price=0.01,
                    bid=0.005, ask=0.015, spread=0.01, liquidity=50000.0,
                    active=True, ts=iso(utcnow()))
    est, _conf, _feats = estimate_probability(market, _hist([0.001] * 20), cfg)
    assert est >= float(cfg.get_path("strategy.probability_floor"))   # clamped
    assert abs(est - market.yes_price) < float(cfg.get_path("strategy.min_edge"))


def test_near_resolved_market_is_not_traded(cfg, scenario):
    # A market at 0.9995 is effectively resolved; mean-reversion there is
    # spurious and the spread cost is punitive. It must be rejected.
    market, hist = scenario
    market.yes_price = 0.9995
    market.bid = 0.999
    market.ask = 0.9999
    market.spread = 0.0009
    d = evaluate_market(market, hist, cfg, "STRATEGY")
    assert d.decision == "HOLD" and d.reason == R_MARKET_NEAR_RESOLVED


def test_near_zero_market_is_not_traded(cfg, scenario):
    market, hist = scenario
    market.yes_price = 0.001
    market.bid = 0.0005
    market.ask = 0.002
    market.spread = 0.0015
    d = evaluate_market(market, hist, cfg, "STRATEGY")
    assert d.decision == "HOLD" and d.reason == R_MARKET_NEAR_RESOLVED


def test_exploration_uses_relaxed_but_bounded_thresholds(cfg, scenario):
    market, hist = scenario
    prod = evaluate_market(market, hist, cfg, "STRATEGY")
    expl = evaluate_market(market, hist, cfg, "EXPLORATION")
    assert expl.exec_type == "EXPLORATION"
    # Exploration must never be *stricter* than production.
    assert expl.confidence >= prod.confidence - 1e-9
