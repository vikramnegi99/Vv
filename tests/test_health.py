"""Provider failure handling, scheduler recovery and lifecycle states."""
from agent import health, portfolio
from agent.challenge import MAIN_ACCOUNT, ChallengeManager
from agent.main import Agent
from agent.providers import build_provider
from agent.providers.mock import MockProvider
from agent.util import add_hours, iso, utcnow


def test_provider_failure_runs_canary_and_preserves_main(cfg, db):
    ChallengeManager(cfg, db).ensure_initialized("mock")
    broken = MockProvider(cfg, healthy=False)
    agent = Agent(cfg, db, broken)
    summary = agent.run_cycle(mode="auto")
    assert summary["provider_ok"] is False
    assert summary["canary"] is not None
    assert summary["canary"]["status"] == "PASSED"
    # Main bankroll untouched, and no main positions were opened.
    assert portfolio.get_account(db, MAIN_ACCOUNT)["cash"] == 1000.00
    assert portfolio.open_positions(db, MAIN_ACCOUNT) == []


def test_repeated_provider_failures_mark_degraded(cfg, db):
    ChallengeManager(cfg, db).ensure_initialized("mock")
    agent = Agent(cfg, db, MockProvider(cfg, healthy=False))
    for _ in range(3):
        agent.run_cycle(mode="auto")
    assert health.get_state(db)["status"] == "DEGRADED"


def test_scheduler_gap_is_detected(cfg, db):
    ChallengeManager(cfg, db).ensure_initialized("mock")
    health.init_state(db)
    # Pretend the last cycle was two hours ago (a missed schedule).
    old = iso(add_hours(utcnow(), -2))
    db.execute("UPDATE agent_state SET last_cycle_utc = ? WHERE id = 1", (old,))
    agent = Agent(cfg, db, build_provider(cfg, "mock"))
    agent.run_cycle(mode="auto")
    assert health.get_state(db)["status"] == "DEGRADED"


def test_healthy_cycle_is_running(cfg, db):
    ChallengeManager(cfg, db).ensure_initialized("mock")
    agent = Agent(cfg, db, build_provider(cfg, "mock"))
    agent.run_cycle(mode="auto")
    assert health.get_state(db)["status"] == "RUNNING"


def test_strategy_inactivity_is_review_required_not_dead(cfg, db):
    ChallengeManager(cfg, db).ensure_initialized("mock")
    health.init_state(db)
    db.set_setting(health._KEY_INACTIVE, 25)
    state, reason = health.detect_state(
        db, cfg, provider_ok=True, stale=False, risk_locked=False,
        inactivity_scans=25, bankrupt=False, corruption=False)
    assert state == health.REVIEW_REQUIRED
    assert "no strategy signal" in reason


def test_bankruptcy_is_dead(cfg, db):
    ChallengeManager(cfg, db).ensure_initialized("mock")
    health.init_state(db)
    state, _ = health.detect_state(
        db, cfg, provider_ok=True, stale=False, risk_locked=False,
        inactivity_scans=0, bankrupt=True, corruption=False)
    assert state == health.DEAD


def test_corruption_is_dead(cfg, db):
    ChallengeManager(cfg, db).ensure_initialized("mock")
    health.init_state(db)
    state, _ = health.detect_state(
        db, cfg, provider_ok=True, stale=False, risk_locked=False,
        inactivity_scans=0, bankrupt=False, corruption=True)
    assert state == health.DEAD


def test_lifecycle_transitions_are_logged(cfg, db):
    health.init_state(db)
    health.transition(db, health.DEGRADED, "test reason")
    events = db.query("SELECT * FROM lifecycle_events")
    assert len(events) == 1
    assert events[0]["to_state"] == health.DEGRADED
