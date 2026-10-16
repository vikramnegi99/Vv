"""The 45-hour challenge: initialisation, checkpoints and the 24-hour rule."""
from __future__ import annotations

from . import portfolio
from .util import add_hours, hours_since, iso, utcnow, utcnow_iso

MAIN_ACCOUNT = "MAIN"
CANARY_ACCOUNT = "CANARY"


class ChallengeManager:
    def __init__(self, cfg, db):
        self.cfg = cfg
        self.db = db
        self.starting_capital = float(cfg.get_path("capital.starting_capital", 1000.0))

    # -- hour 0 -------------------------------------------------------------
    def ensure_initialized(self, provider_name: str) -> dict:
        row = self.db.query_one("SELECT * FROM challenge WHERE id = 1")
        if row is None:
            start = utcnow()
            self.db.execute(
                "INSERT INTO challenge(id, start_utc, deadline_24h_utc, end_45h_utc, status,"
                " software_version, strategy_version, provider, config_version, initialized)"
                " VALUES (1,?,?,?,?,?,?,?,?,1)",
                (
                    iso(start),
                    iso(add_hours(start, float(self.cfg.get_path("challenge.first_trade_deadline_hours", 24)))),
                    iso(add_hours(start, float(self.cfg.get_path("challenge.deadline_hours", 45)))),
                    "RUNNING",
                    self.cfg.get_path("meta.software_version", "2.0.0"),
                    self.cfg.get_path("meta.strategy_version", "baseline"),
                    provider_name,
                    self.cfg.get_path("meta.config_version", "cfg"),
                ),
            )
            # Accounts are created exactly once. The main bankroll is never reset.
            portfolio.ensure_account(self.db, MAIN_ACCOUNT, "MAIN_PAPER", self.starting_capital)
            portfolio.ensure_account(self.db, CANARY_ACCOUNT, "CANARY", self.starting_capital)
            row = self.db.query_one("SELECT * FROM challenge WHERE id = 1")
        # Always guarantee the accounts exist (e.g. after a manual db wipe of accounts only).
        portfolio.ensure_account(self.db, MAIN_ACCOUNT, "MAIN_PAPER", self.starting_capital)
        portfolio.ensure_account(self.db, CANARY_ACCOUNT, "CANARY", self.starting_capital)
        return dict(row)

    def state(self) -> dict:
        return dict(self.db.query_one("SELECT * FROM challenge WHERE id = 1"))

    def hours_elapsed(self) -> float:
        st = self.state()
        return hours_since(st["start_utc"]) if st.get("start_utc") else 0.0

    def hours_remaining(self) -> float:
        return max(0.0, float(self.cfg.get_path("challenge.deadline_hours", 45)) - self.hours_elapsed())

    def hours_to_first_trade_deadline(self) -> float:
        return max(0.0, float(self.cfg.get_path("challenge.first_trade_deadline_hours", 24))
                   - self.hours_elapsed())

    # -- recording ----------------------------------------------------------
    def mark_first_trade(self, exec_type: str) -> None:
        st = self.state()
        if not st.get("first_trade_met"):
            self.db.execute(
                "UPDATE challenge SET first_trade_met = 1, first_trade_type = ?, first_trade_at = ?"
                " WHERE id = 1",
                (exec_type, utcnow_iso()),
            )

    def record_canary(self, result: str) -> None:
        self.db.execute(
            "UPDATE challenge SET canary_done = 1, canary_result = ? WHERE id = 1", (result,)
        )

    def bump_exploration_orders(self) -> None:
        self.db.execute("UPDATE challenge SET exploration_orders = exploration_orders + 1 WHERE id = 1")

    # -- checkpoints --------------------------------------------------------
    def run_checkpoints(self, funnel: dict) -> list[str]:
        """Apply the hour-12 / 24 / 45 checkpoints. Returns messages."""
        notes: list[str] = []
        hours = self.hours_elapsed()
        st = self.state()

        if hours >= 12 and not st.get("hour12_done"):
            if not st.get("first_trade_met"):
                self.db.execute(
                    "UPDATE challenge SET exploration_enabled = 1, hour12_done = 1 WHERE id = 1"
                )
                notes.append("HOUR12: no strategy trade yet -> exploration policy enabled")
            else:
                self.db.execute("UPDATE challenge SET hour12_done = 1 WHERE id = 1")
                notes.append("HOUR12: strategy trade already recorded")

        if hours >= 24 and not st.get("hour24_done"):
            st2 = self.state()
            if not st2.get("first_trade_met") and not st2.get("canary_done"):
                self.db.execute("UPDATE challenge SET deadline_alert = 1, hour24_done = 1 WHERE id = 1")
                notes.append("HOUR24: DEADLINE_MISSED - no real-market order and no canary")
            else:
                self.db.execute("UPDATE challenge SET hour24_done = 1 WHERE id = 1")
                notes.append("HOUR24: first-trade requirement satisfied")

        if hours >= 45 and not st.get("hour45_done"):
            self.db.execute(
                "UPDATE challenge SET hour45_done = 1, status = 'COMPLETE' WHERE id = 1"
            )
            notes.append("HOUR45: challenge complete - final report generated")
        return notes

    def finalize_report(self, report: str) -> None:
        self.db.execute("UPDATE challenge SET final_report = ? WHERE id = 1", (report,))

    # -- exploration policy -------------------------------------------------
    def exploration_allowed(self, eligible_markets: int) -> bool:
        """Bounded exploratory policy: only after hour 12, only if no strategy
        trade yet, only with eligible markets, and only inside the hard caps."""
        st = self.state()
        if self.hours_elapsed() < float(self.cfg.get_path("risk.exploration_enable_hour", 12)):
            return False
        if st.get("first_trade_met"):
            return False
        if int(st.get("exploration_orders", 0)) >= int(self.cfg.get_path("risk.exploration_max_orders", 3)):
            return False
        if eligible_markets <= 0:
            return False
        return True
