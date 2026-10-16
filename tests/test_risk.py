"""Hard risk limits."""
from agent.challenge import MAIN_ACCOUNT, ChallengeManager
from agent.risk import RiskEngine
from agent.util import iso, utcnow


def _setup(cfg, db, scenario):
    ChallengeManager(cfg, db).ensure_initialized("mock")
    market, _ = scenario
    return RiskEngine(cfg), market, {market.id: market.yes_price}


def test_position_size_limit(cfg, db, scenario):
    risk, market, quotes = _setup(cfg, db, scenario)
    # 5% of a $1000 account is far above the 1% cap.
    v = risk.evaluate(db, MAIN_ACCOUNT, market, "YES", 0.4, 125.0, quotes)
    assert v.ok is False and v.reason == "RISK_POSITION_SIZE"


def test_within_position_size_passes(cfg, db, scenario):
    risk, market, quotes = _setup(cfg, db, scenario)
    v = risk.evaluate(db, MAIN_ACCOUNT, market, "YES", 0.4, 25.0, quotes)  # $10 notional
    assert v.ok is True


def test_exploration_stake_limit(cfg, db, scenario):
    risk, market, quotes = _setup(cfg, db, scenario)
    v = risk.evaluate(db, MAIN_ACCOUNT, market, "YES", 0.4, 25.0, quotes, "EXPLORATION")
    assert v.ok is False and v.reason == "RISK_EXPLORATION_STAKE"


def test_exploration_within_stake_passes(cfg, db, scenario):
    risk, market, quotes = _setup(cfg, db, scenario)
    v = risk.evaluate(db, MAIN_ACCOUNT, market, "YES", 0.4, 4.0, quotes, "EXPLORATION")  # $1.60
    assert v.ok is True


def test_exploration_order_cap(cfg, db, scenario):
    risk, market, quotes = _setup(cfg, db, scenario)
    for i in range(int(cfg.get_path("risk.exploration_max_orders", 3))):
        db.execute(
            "INSERT INTO orders(id, account_id, market_id, side, action, quantity, exec_type,"
            " status, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (f"o{i}", MAIN_ACCOUNT, f"m{i}", "YES", "BUY", 1.0, "EXPLORATION", "FILLED",
             iso(utcnow())),
        )
    v = risk.evaluate(db, MAIN_ACCOUNT, market, "YES", 0.4, 4.0, quotes, "EXPLORATION")
    assert v.ok is False and v.reason == "RISK_EXPLORATION_CAP"


def test_max_open_positions(cfg, db, scenario):
    risk, market, quotes = _setup(cfg, db, scenario)
    for i in range(int(cfg.get_path("risk.max_open_positions", 10))):
        db.execute(
            "INSERT INTO positions(id, account_id, market_id, side, quantity, entry_price,"
            " entry_time, exec_type, status) VALUES (?,?,?,?,?,?,?,?,?)",
            (f"p{i}", MAIN_ACCOUNT, f"m{i}", "YES", 1.0, 0.4, iso(utcnow()), "STRATEGY", "OPEN"),
        )
    v = risk.evaluate(db, MAIN_ACCOUNT, market, "YES", 0.4, 1.0, quotes)
    assert v.ok is False and v.reason == "RISK_MAX_POSITIONS"


def test_drawdown_lock(cfg, db, scenario):
    risk, market, quotes = _setup(cfg, db, scenario)
    db.execute("INSERT INTO equity_snapshots(ts, account_id, equity) VALUES (?,?,?)",
               (iso(utcnow()), MAIN_ACCOUNT, 1000.0))
    db.execute("INSERT INTO equity_snapshots(ts, account_id, equity) VALUES (?,?,?)",
               (iso(utcnow()), MAIN_ACCOUNT, 880.0))
    assert risk.is_locked(db, MAIN_ACCOUNT) is True
    v = risk.evaluate(db, MAIN_ACCOUNT, market, "YES", 0.4, 1.0, quotes)
    assert v.ok is False and v.reason == "RISK_DRAWDOWN_LOCK"


def test_daily_loss_limit(cfg, db, scenario):
    risk, market, quotes = _setup(cfg, db, scenario)
    db.execute("INSERT INTO equity_snapshots(ts, account_id, equity) VALUES (?,?,?)",
               (iso(utcnow()), MAIN_ACCOUNT, 1000.0))
    db.execute("UPDATE accounts SET cash = 960.0 WHERE id = ?", (MAIN_ACCOUNT,))
    v = risk.evaluate(db, MAIN_ACCOUNT, market, "YES", 0.4, 1.0, quotes)
    assert v.ok is False and v.reason == "RISK_DAILY_LOSS"


def test_insufficient_cash(cfg, db, scenario):
    risk, market, quotes = _setup(cfg, db, scenario)
    # Equity is high (an open position has appreciated) but free cash is tiny.
    db.execute(
        "INSERT INTO positions(id, account_id, market_id, side, quantity, entry_price,"
        " entry_time, exec_type, status, current_price) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("big", MAIN_ACCOUNT, "other", "YES", 1111.0, 0.10, iso(utcnow()), "STRATEGY",
         "OPEN", 0.90),
    )
    db.execute("UPDATE accounts SET cash = 0.10 WHERE id = ?", (MAIN_ACCOUNT,))
    v = risk.evaluate(db, MAIN_ACCOUNT, market, "YES", 0.4, 25.0, quotes)
    assert v.ok is False and v.reason == "RISK_INSUFFICIENT_CASH"


def test_min_notional(cfg, db, scenario):
    risk, market, quotes = _setup(cfg, db, scenario)
    v = risk.evaluate(db, MAIN_ACCOUNT, market, "YES", 0.4, 0.5, quotes)
    assert v.ok is False and v.reason == "RISK_MIN_NOTIONAL"
