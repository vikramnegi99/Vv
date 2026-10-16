"""24-hour deadline logic, 45-hour completion and the exploration policy."""
from agent.challenge import ChallengeManager
from agent.util import add_hours, iso, utcnow


def shift_start(db, hours):
    db.execute("UPDATE challenge SET start_utc = ? WHERE id = 1",
               (iso(add_hours(utcnow(), -hours)),))


def test_hour0_initialises_once(cfg, db):
    cm = ChallengeManager(cfg, db)
    st = cm.ensure_initialized("mock")
    assert st["initialized"] == 1
    assert st["software_version"]
    assert st["provider"] == "mock"
    first_start = st["start_utc"]
    cm.ensure_initialized("mock")
    assert cm.state()["start_utc"] == first_start  # not re-stamped


def test_24h_deadline_alert_when_no_trade(cfg, db):
    cm = ChallengeManager(cfg, db)
    cm.ensure_initialized("mock")
    shift_start(db, 25)
    notes = cm.run_checkpoints({})
    st = cm.state()
    assert st["deadline_alert"] == 1
    assert st["hour24_done"] == 1
    assert any("DEADLINE_MISSED" in n for n in notes)


def test_24h_satisfied_by_strategy_trade(cfg, db):
    cm = ChallengeManager(cfg, db)
    cm.ensure_initialized("mock")
    cm.mark_first_trade("STRATEGY")
    shift_start(db, 25)
    cm.run_checkpoints({})
    assert cm.state()["deadline_alert"] == 0


def test_24h_satisfied_by_completed_canary(cfg, db):
    cm = ChallengeManager(cfg, db)
    cm.ensure_initialized("mock")
    cm.record_canary('{"status": "PASSED"}')
    shift_start(db, 25)
    cm.run_checkpoints({})
    assert cm.state()["deadline_alert"] == 0


def test_hour12_enables_exploration_when_no_trade(cfg, db):
    cm = ChallengeManager(cfg, db)
    cm.ensure_initialized("mock")
    shift_start(db, 13)
    cm.run_checkpoints({})
    assert cm.state()["exploration_enabled"] == 1
    assert cm.exploration_allowed(eligible_markets=5) is True


def test_hour12_skipped_if_strategy_trade_exists(cfg, db):
    cm = ChallengeManager(cfg, db)
    cm.ensure_initialized("mock")
    cm.mark_first_trade("STRATEGY")
    shift_start(db, 13)
    cm.run_checkpoints({})
    assert cm.state()["exploration_enabled"] == 0
    assert cm.exploration_allowed(eligible_markets=5) is False


def test_exploration_not_allowed_before_hour12(cfg, db):
    cm = ChallengeManager(cfg, db)
    cm.ensure_initialized("mock")
    shift_start(db, 3)
    assert cm.exploration_allowed(eligible_markets=5) is False


def test_exploration_respects_order_cap(cfg, db):
    cm = ChallengeManager(cfg, db)
    cm.ensure_initialized("mock")
    shift_start(db, 13)
    cm.run_checkpoints({})
    for _ in range(int(cfg.get_path("risk.exploration_max_orders", 3))):
        cm.bump_exploration_orders()
    assert cm.exploration_allowed(eligible_markets=5) is False


def test_45h_completion(cfg, db):
    cm = ChallengeManager(cfg, db)
    cm.ensure_initialized("mock")
    shift_start(db, 46)
    notes = cm.run_checkpoints({})
    st = cm.state()
    assert st["hour45_done"] == 1
    assert st["status"] == "COMPLETE"
    assert any("HOUR45" in n for n in notes)
