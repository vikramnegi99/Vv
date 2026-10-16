"""Starting capital, persistence and the no-reset guarantee."""
import os

from agent import portfolio
from agent.challenge import MAIN_ACCOUNT, ChallengeManager
from agent.db import DB
from agent.main import Agent
from agent.providers import build_provider


def test_starting_capital_is_exactly_1000(cfg, db):
    ChallengeManager(cfg, db).ensure_initialized("mock")
    acct = portfolio.get_account(db, MAIN_ACCOUNT)
    assert acct["starting_balance"] == 1000.00
    assert acct["cash"] == 1000.00
    assert portfolio.equity(db, MAIN_ACCOUNT, {}) == 1000.00


def test_bankroll_persists_across_restarts(cfg, tmp_path):
    path = str(tmp_path / "state.db")
    db1 = DB(path)
    db1.migrate()
    ChallengeManager(cfg, db1).ensure_initialized("mock")
    agent = Agent(cfg, db1, build_provider(cfg, "mock"))
    agent.run_canary()
    db1.close()

    # Completely separate process / handle.
    db2 = DB(path)
    db2.migrate()
    acct = portfolio.get_account(db2, MAIN_ACCOUNT)
    assert acct["starting_balance"] == 1000.00
    assert acct["cash"] == 1000.00          # canary must not touch main
    db2.close()


def test_no_automatic_bankroll_reset(cfg, db):
    cm = ChallengeManager(cfg, db)
    cm.ensure_initialized("mock")
    # Simulate a spent bankroll, then re-initialise (as a restart would).
    db.execute("UPDATE accounts SET cash = 250.0 WHERE id = ?", (MAIN_ACCOUNT,))
    cm.ensure_initialized("mock")
    assert portfolio.get_account(db, MAIN_ACCOUNT)["cash"] == 250.0
    # A new day / challenge rollover must not reset either.
    db.execute("UPDATE challenge SET start_utc = '2020-01-01T00:00:00+00:00' WHERE id = 1")
    cm.ensure_initialized("mock")
    assert portfolio.get_account(db, MAIN_ACCOUNT)["cash"] == 250.0


def test_accounts_created_once(cfg, db):
    cm = ChallengeManager(cfg, db)
    cm.ensure_initialized("mock")
    created_again = portfolio.ensure_account(db, MAIN_ACCOUNT, "MAIN_PAPER", 1000.0)
    assert created_again is False
