"""Risk engine.

Every simulated execution must pass this gate immediately before it happens.
A rejected order is NEVER recorded as an executed trade.

Hard limits (conservative paper defaults, from config.yaml):
  * max position           1%  of current equity
  * max total exposure     15% of current equity
  * max daily loss         3%  of the day's starting equity
  * max drawdown           10% -> new orders locked pending review
  * exploration stake      $2 per order, max 3 orders in the challenge
  * max open positions     10
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import portfolio


@dataclass
class RiskResult:
    ok: bool
    reason: str = ""
    flags: list = field(default_factory=list)


class RiskEngine:
    def __init__(self, cfg):
        self.cfg = cfg

    # -- limit accessors ----------------------------------------------------
    @property
    def max_position_pct(self) -> float:
        return float(self.cfg.get_path("risk.max_position_pct", 0.01))

    @property
    def max_exposure_pct(self) -> float:
        return float(self.cfg.get_path("risk.max_total_exposure_pct", 0.15))

    @property
    def max_daily_loss_pct(self) -> float:
        return float(self.cfg.get_path("risk.max_daily_loss_pct", 0.03))

    @property
    def max_drawdown_pct(self) -> float:
        return float(self.cfg.get_path("risk.max_drawdown_pct", 0.10))

    @property
    def max_open_positions(self) -> int:
        return int(self.cfg.get_path("risk.max_open_positions", 10))

    @property
    def exploration_max_stake(self) -> float:
        return float(self.cfg.get_path("risk.exploration_max_stake", 2.0))

    @property
    def exploration_max_orders(self) -> int:
        return int(self.cfg.get_path("risk.exploration_max_orders", 3))

    # -- state helpers ------------------------------------------------------
    def drawdown(self, db, account_id: str) -> float:
        return portfolio.max_drawdown(db, account_id)

    def is_locked(self, db, account_id: str) -> bool:
        return self.drawdown(db, account_id) >= self.max_drawdown_pct

    # -- the gate -----------------------------------------------------------
    def evaluate(self, db, account_id: str, market, side: str, price: float,
                 quantity: float, prices: dict, exec_type: str = "STRATEGY") -> RiskResult:
        flags: list = []
        notional = quantity * price
        acct = portfolio.get_account(db, account_id)
        if acct is None:
            return RiskResult(False, "NO_ACCOUNT", flags)

        eq = portfolio.equity(db, account_id, prices)

        # 1. drawdown lock
        dd = self.drawdown(db, account_id)
        if dd >= self.max_drawdown_pct:
            flags.append(f"drawdown {dd:.2%} >= {self.max_drawdown_pct:.0%}")
            return RiskResult(False, "RISK_DRAWDOWN_LOCK", flags)

        # 2. daily loss
        start = portfolio.day_start_equity(db, account_id, eq)
        if start > 0:
            loss_pct = (start - eq) / start
            if loss_pct >= self.max_daily_loss_pct:
                flags.append(f"daily loss {loss_pct:.2%} >= {self.max_daily_loss_pct:.0%}")
                return RiskResult(False, "RISK_DAILY_LOSS", flags)

        # 3. max open positions
        open_pos = portfolio.open_positions(db, account_id)
        if len(open_pos) >= self.max_open_positions:
            flags.append(f"open positions {len(open_pos)} >= {self.max_open_positions}")
            return RiskResult(False, "RISK_MAX_POSITIONS", flags)

        # 4. duplicate prevention
        for pos in open_pos:
            if pos["market_id"] == market.id:
                flags.append(f"open position already on {market.id}")
                return RiskResult(False, "RISK_DUPLICATE_POSITION", flags)

        # 5. minimum notional
        if notional < float(self.cfg.get_path("risk.min_order_notional", 0.5)):
            flags.append(f"notional {notional:.2f} below minimum")
            return RiskResult(False, "RISK_MIN_NOTIONAL", flags)

        # 6. per-position size
        if eq > 0 and notional > self.max_position_pct * eq + 1e-9:
            flags.append(f"notional {notional:.2f} > {self.max_position_pct:.0%} of equity")
            return RiskResult(False, "RISK_POSITION_SIZE", flags)

        # 7. aggregate exposure
        exposure = portfolio.open_exposure(db, account_id)
        if eq > 0 and (exposure + notional) > self.max_exposure_pct * eq + 1e-9:
            flags.append(f"exposure {exposure + notional:.2f} > {self.max_exposure_pct:.0%} of equity")
            return RiskResult(False, "RISK_TOTAL_EXPOSURE", flags)

        # 8. cash
        fee = notional * float(self.cfg.get_path("costs.fee_bps", 20)) / 10_000.0
        if notional + fee > float(acct["cash"]) + 1e-9:
            flags.append("insufficient cash")
            return RiskResult(False, "RISK_INSUFFICIENT_CASH", flags)

        # 9. exploration-specific caps
        if exec_type == "EXPLORATION":
            if notional > self.exploration_max_stake + 1e-9:
                flags.append(f"exploration notional {notional:.2f} > ${self.exploration_max_stake}")
                return RiskResult(False, "RISK_EXPLORATION_STAKE", flags)
            row = db.query_one(
                "SELECT COUNT(*) AS c FROM orders WHERE account_id = ? AND exec_type = 'EXPLORATION' "
                "AND status = 'FILLED'",
                (account_id,),
            )
            used = int(row["c"]) if row else 0
            if used >= self.exploration_max_orders:
                flags.append(f"exploration orders {used} >= {self.exploration_max_orders}")
                return RiskResult(False, "RISK_EXPLORATION_CAP", flags)

        return RiskResult(True, "OK", flags)
