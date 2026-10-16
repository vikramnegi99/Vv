"""Canary: end-to-end pipeline test on an isolated account."""
from agent import portfolio
from agent.challenge import CANARY_ACCOUNT, MAIN_ACCOUNT, ChallengeManager
from agent.main import Agent
from agent.providers import build_provider


def test_canary_end_to_end(cfg, db):
    ChallengeManager(cfg, db).ensure_initialized("mock")
    agent = Agent(cfg, db, build_provider(cfg, "mock"))
    result = agent.run_canary(reason="test")
    assert result["status"] == "PASSED"
    assert result["settled"]                      # ran through settlement
    assert result["fill_price"] > 0
    # The position was opened and settled on the canary account.
    assert portfolio.open_positions(db, CANARY_ACCOUNT) == []
    closed = db.query_one(
        "SELECT COUNT(*) c FROM positions WHERE account_id=? AND status='CLOSED'",
        (CANARY_ACCOUNT,))
    assert closed["c"] >= 1


def test_canary_never_touches_main_bankroll(cfg, db):
    ChallengeManager(cfg, db).ensure_initialized("mock")
    agent = Agent(cfg, db, build_provider(cfg, "mock"))
    main_before = portfolio.get_account(db, MAIN_ACCOUNT)["cash"]
    canary_before = portfolio.get_account(db, CANARY_ACCOUNT)["cash"]
    agent.run_canary(reason="test")
    assert portfolio.get_account(db, MAIN_ACCOUNT)["cash"] == main_before
    assert portfolio.get_account(db, CANARY_ACCOUNT)["cash"] != canary_before


def test_canary_is_labelled_and_recorded(cfg, db):
    cm = ChallengeManager(cfg, db)
    cm.ensure_initialized("mock")
    agent = Agent(cfg, db, build_provider(cfg, "mock"))
    agent.run_canary(reason="test")
    st = cm.state()
    assert st["canary_done"] == 1
    assert "CANARY" in (st["canary_result"] or "") or "PASSED" in (st["canary_result"] or "")
    order = db.query_one("SELECT * FROM orders WHERE exec_type='CANARY'")
    assert order is not None and order["status"] == "FILLED"
