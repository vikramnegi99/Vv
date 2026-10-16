"""Orchestration, persistence of the funnel, rejection logging and the dashboard."""
import os

from agent import dashboard, health
from agent.challenge import ChallengeManager
from agent.db import DB
from agent.main import Agent
from agent.providers import build_provider


def test_scan_persists_funnel_and_decisions(cfg, db):
    ChallengeManager(cfg, db).ensure_initialized("mock")
    agent = Agent(cfg, db, build_provider(cfg, "mock"))
    summary = agent.run_cycle(mode="auto")
    scan = db.query_one("SELECT * FROM scans WHERE id = ?", (summary["scan_id"],))
    assert scan is not None
    assert scan["discovered"] == summary["funnel"]["discovered"]
    assert db.query_one("SELECT COUNT(*) c FROM decisions")["c"] > 0
    assert db.query_one("SELECT COUNT(*) c FROM market_snapshots")["c"] > 0


def test_rejection_reasons_are_logged(cfg, db):
    ChallengeManager(cfg, db).ensure_initialized("mock")
    agent = Agent(cfg, db, build_provider(cfg, "mock"))
    agent.run_cycle(mode="auto")
    rows = db.query("SELECT reason, COUNT(*) c FROM rejections GROUP BY reason")
    assert rows, "expected rejection rows"
    # Every rejection row carries a non-empty reason code.
    assert all(r["reason"] for r in rows)


def test_no_forced_trade_when_data_invalid(cfg, db):
    from agent.providers.mock import MockProvider
    # Markets with no valid price => no eligible markets => canary, never a forced trade.
    prov = MockProvider(cfg, n_markets=3)
    for m in prov.discover_markets():
        m.yes_price = None
    ChallengeManager(cfg, db).ensure_initialized("mock")
    agent = Agent(cfg, db, prov)
    summary = agent.run_cycle(mode="auto")
    assert summary["funnel"]["executed"] == 0
    assert summary["canary"] is not None
    assert db.query_one(
        "SELECT COUNT(*) c FROM positions WHERE account_id='MAIN'")["c"] == 0


def test_dashboard_is_self_contained(cfg, db, tmp_path):
    ChallengeManager(cfg, db).ensure_initialized("mock")
    agent = Agent(cfg, db, build_provider(cfg, "mock"))
    agent.run_cycle(mode="auto")
    out = str(tmp_path / "index.html")
    dashboard.render(db, cfg, out)
    assert os.path.exists(out)
    html = open(out, encoding="utf-8").read()
    assert "PAPER ONLY" in html
    assert "REAL_TRADING=false" in html
    assert "<script src=" not in html          # no external scripts
    assert "cdn" not in html.lower()
    assert "Signal funnel" in html


def test_migration_and_backup(cfg, tmp_path):
    path = str(tmp_path / "state.db")
    db = DB(path)
    assert db.current_version() == 0
    version = db.migrate(backup_dir=str(tmp_path / "backups"))
    assert version >= 1
    dest = db.backup(str(tmp_path / "backups"), tag="manual")
    assert dest and os.path.exists(dest)
    db.close()


def test_migration_is_idempotent(cfg, tmp_path):
    path = str(tmp_path / "state.db")
    db = DB(path)
    v1 = db.migrate()
    v2 = db.migrate()
    assert v1 == v2
    db.close()
