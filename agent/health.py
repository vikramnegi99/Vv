"""Lifecycle state machine, heartbeat and fail-safe detection.

States: RUNNING, DEGRADED, REVIEW_REQUIRED, LOCKED, DEAD.

DEAD is reserved for explicitly fatal conditions (bankroll bankruptcy or
unrecoverable state corruption). Strategy inactivity alone never marks the
agent DEAD - it raises REVIEW_REQUIRED.
"""
from __future__ import annotations

from .util import utcnow_iso

RUNNING = "RUNNING"
DEGRADED = "DEGRADED"
REVIEW_REQUIRED = "REVIEW_REQUIRED"
LOCKED = "LOCKED"
DEAD = "DEAD"

ALL_STATES = [RUNNING, DEGRADED, REVIEW_REQUIRED, LOCKED, DEAD]

_KEY_FAILS = "consecutive_provider_failures"
_KEY_INACTIVE = "consecutive_inactive_scans"
_KEY_SIMFAIL = "consecutive_simulation_failures"


def init_state(db) -> None:
    if db.query_one("SELECT id FROM agent_state WHERE id = 1") is None:
        db.execute(
            "INSERT INTO agent_state(id, status, updated_at) VALUES (1, ?, ?)",
            (RUNNING, utcnow_iso()),
        )


def get_state(db) -> dict:
    init_state(db)
    return dict(db.query_one("SELECT * FROM agent_state WHERE id = 1"))


def log_event(db, from_state: str, to_state: str, reason: str) -> None:
    db.execute(
        "INSERT INTO lifecycle_events(ts, from_state, to_state, reason) VALUES (?,?,?,?)",
        (utcnow_iso(), from_state, to_state, reason),
    )


def transition(db, new_state: str, reason: str) -> None:
    cur = get_state(db)
    if cur["status"] == new_state:
        return
    db.execute("UPDATE agent_state SET status = ?, updated_at = ? WHERE id = 1",
               (new_state, utcnow_iso()))
    log_event(db, cur["status"], new_state, reason)


def record_heartbeat(db, *, success: bool, api_latency_ms=None, gap_minutes=None) -> None:
    now = utcnow_iso()
    fields = ["last_cycle_utc = ?"]
    params = [now]
    if success:
        fields.append("last_success_utc = ?")
        params.append(now)
    if api_latency_ms is not None:
        fields.append("last_api_utc = ?")
        params.append(now)
        fields.append("last_api_latency_ms = ?")
        params.append(float(api_latency_ms))
    if gap_minutes is not None:
        fields.append("last_scan_gap_minutes = ?")
        params.append(float(gap_minutes))
    fields.append("updated_at = ?")
    params.append(now)
    params.append(1)
    db.execute(f"UPDATE agent_state SET {', '.join(fields)} WHERE id = ?", params)


def bump(db, key: str, reset: bool = False) -> int:
    cur = int(db.get_setting(key, 0) or 0)
    cur = 0 if reset else cur + 1
    db.set_setting(key, cur)
    return cur


def get_counter(db, key: str) -> int:
    return int(db.get_setting(key, 0) or 0)


def db_integrity_ok(db) -> bool:
    try:
        row = db.query_one("PRAGMA integrity_check")
        return row is not None and list(row)[0] == "ok"
    except Exception:  # noqa: BLE001
        return False


def detect_state(db, cfg, *, provider_ok: bool, stale: bool, risk_locked: bool,
                 inactivity_scans: int, bankrupt: bool, corruption: bool) -> tuple[str, str]:
    """Return (state, reason). Precedence: DEAD > LOCKED > DEGRADED > REVIEW_REQUIRED > RUNNING."""
    max_fails = 3
    max_gap = float(cfg.get_path("challenge.max_scan_gap_minutes", 30))
    inactivity_limit = 20

    if bankrupt:
        return DEAD, "bankroll bankruptcy"
    if corruption:
        return DEAD, "unrecoverable database corruption"
    if risk_locked:
        return LOCKED, "drawdown limit reached - orders locked pending review"

    fails = get_counter(db, _KEY_FAILS)
    sim_fails = get_counter(db, _KEY_SIMFAIL)
    state_row = get_state(db)
    gap = state_row.get("last_scan_gap_minutes")
    degraded_reasons = []
    if not provider_ok:
        degraded_reasons.append("market provider unavailable")
    if stale:
        degraded_reasons.append("stale market data")
    if fails >= max_fails:
        degraded_reasons.append(f"{fails} consecutive provider failures")
    if sim_fails >= max_fails:
        degraded_reasons.append(f"{sim_fails} consecutive simulation failures")
    if gap is not None and gap > max_gap:
        degraded_reasons.append(f"scheduled execution gap {gap:.1f}m > {max_gap:.0f}m")
    if degraded_reasons:
        return DEGRADED, "; ".join(degraded_reasons)

    if inactivity_scans >= inactivity_limit:
        return REVIEW_REQUIRED, f"{inactivity_scans} scans with no strategy signal"

    return RUNNING, "healthy"
