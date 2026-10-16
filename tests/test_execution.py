"""Execution engine: accounting, fees, slippage, settlement, duplicate prevention."""
import pytest

from agent import portfolio
from agent.challenge import MAIN_ACCOUNT, ChallengeManager
from agent.execution import ExecutionEngine


@pytest.fixture
def ready(cfg, db, scenario):
    ChallengeManager(cfg, db).ensure_initialized("mock")
    market, _ = scenario
    ee = ExecutionEngine(cfg, db)
    quotes = {market.id: market.yes_price}
    return cfg, db, ee, market, quotes


def test_fees_and_slippage_are_recorded(ready):
    cfg, db, ee, market, quotes = ready
    res = ee.execute(MAIN_ACCOUNT, market, "YES", "STRATEGY", "test", "scan", "r", quotes)
    assert res["ok"]
    assert res["fee"] > 0
    assert res["slippage"] > 0
    assert res["fill_price"] > res["requested_price"]  # adverse slippage
    order = db.query_one("SELECT * FROM orders WHERE id = ?", (res["order_id"],))
    assert order["fee"] == pytest.approx(res["fee"])
    assert order["slippage"] == pytest.approx(res["slippage"])


def test_position_and_cash_accounting(ready):
    cfg, db, ee, market, quotes = ready
    before = portfolio.get_account(db, MAIN_ACCOUNT)["cash"]
    res = ee.execute(MAIN_ACCOUNT, market, "YES", "STRATEGY", "test", "scan", "r", quotes)
    after = portfolio.get_account(db, MAIN_ACCOUNT)["cash"]
    assert after == pytest.approx(before - res["notional"] - res["fee"])
    positions = portfolio.open_positions(db, MAIN_ACCOUNT)
    assert len(positions) == 1
    assert positions[0]["quantity"] == pytest.approx(res["quantity"])
    eq = portfolio.equity(db, MAIN_ACCOUNT, quotes)
    expected = after + res["quantity"] * market.yes_price
    assert eq == pytest.approx(expected)


def test_resolution_win_pnl(ready):
    cfg, db, ee, market, quotes = ready
    res = ee.execute(MAIN_ACCOUNT, market, "YES", "STRATEGY", "test", "scan", "r", quotes)
    settled = ee.settle(MAIN_ACCOUNT, {market.id: "YES"})
    assert len(settled) == 1
    expected = res["quantity"] * 1.0 - res["quantity"] * res["fill_price"] - res["fee"]
    assert settled[0]["realized_pnl"] == pytest.approx(expected, abs=1e-6)
    assert portfolio.open_positions(db, MAIN_ACCOUNT) == []


def test_resolution_loss_pnl(ready):
    cfg, db, ee, market, quotes = ready
    res = ee.execute(MAIN_ACCOUNT, market, "YES", "STRATEGY", "test", "scan", "r", quotes)
    settled = ee.settle(MAIN_ACCOUNT, {market.id: "NO"})
    assert settled[0]["realized_pnl"] < 0


def test_no_settlement_without_confirmed_outcome(ready):
    cfg, db, ee, market, quotes = ready
    ee.execute(MAIN_ACCOUNT, market, "YES", "STRATEGY", "test", "scan", "r", quotes)
    settled = ee.settle(MAIN_ACCOUNT, {})           # no confirmed outcome
    assert settled == []
    assert len(portfolio.open_positions(db, MAIN_ACCOUNT)) == 1


def test_duplicate_order_is_prevented(ready):
    cfg, db, ee, market, quotes = ready
    first = ee.execute(MAIN_ACCOUNT, market, "YES", "STRATEGY", "test", "scan", "r", quotes)
    assert first["ok"]
    second = ee.execute(MAIN_ACCOUNT, market, "YES", "STRATEGY", "test", "scan", "r", quotes)
    assert second["ok"] is False
    assert second["reason"] == "RISK_DUPLICATE_POSITION"
    assert len(portfolio.open_positions(db, MAIN_ACCOUNT)) == 1
    # The rejected attempt must be stored as REJECTED, never FILLED.
    rejected = db.query_one("SELECT * FROM orders WHERE status='REJECTED'")
    assert rejected is not None


def test_rejected_order_is_not_a_trade(ready):
    cfg, db, ee, market, quotes = ready
    market.liquidity = 0.0  # ignored here; force a bad qty instead
    bad = ee.execute(MAIN_ACCOUNT, market, "YES", "STRATEGY", "test", "scan", "r", quotes,
                     quantity=0.0)
    assert bad["ok"] is False
    assert db.query_one("SELECT COUNT(*) c FROM positions")["c"] == 0
