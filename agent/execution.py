"""Simulated execution engine.

Places PAPER orders only. There is no code path that reaches a live order
endpoint. Every order is written with its requested price, simulated fill,
quantity, notional, fee, slippage and execution category
(STRATEGY / EXPLORATION / CANARY).
"""
from __future__ import annotations

from typing import Optional

from . import portfolio
from .models import Market
from .risk import RiskEngine
from .util import new_id, utcnow_iso


class ExecutionEngine:
    def __init__(self, cfg, db):
        self.cfg = cfg
        self.db = db
        self.risk = RiskEngine(cfg)
        self.fee_bps = float(cfg.get_path("costs.fee_bps", 20))
        self.slippage_bps = float(cfg.get_path("costs.slippage_bps", 10))

    # -- pricing ------------------------------------------------------------
    def entry_price(self, market: Market, side: str) -> Optional[float]:
        """Adverse entry price: buy YES at the ask, buy NO at (1 - bid)."""
        if side == "YES":
            return market.ask if market.ask is not None else market.yes_price
        if market.bid is not None:
            return 1.0 - market.bid
        if market.yes_price is not None:
            return 1.0 - market.yes_price
        return None

    def size(self, eq: float, price: float, exec_type: str) -> float:
        if price is None or price <= 0 or eq <= 0:
            return 0.0
        max_notional = self.risk.max_position_pct * eq
        if exec_type == "EXPLORATION":
            max_notional = min(self.risk.exploration_max_stake, max_notional)
        qty = round(max_notional / price, 2)
        return qty

    # -- execution ----------------------------------------------------------
    def execute(self, account_id: str, market: Market, side: str, exec_type: str,
                strategy_version: str, scan_id: Optional[str], reason: str,
                prices: dict, quantity: Optional[float] = None) -> dict:
        """Attempt a simulated fill. Returns a result dict; never records a
        rejected order as an executed trade."""
        price = self.entry_price(market, side)
        if price is None or not (0.0 < price < 1.0):
            return {"ok": False, "reason": "INVALID_PRICE"}

        eq = portfolio.equity(self.db, account_id, prices)
        qty = quantity if quantity is not None else self.size(eq, price, exec_type)
        if qty <= 0:
            return {"ok": False, "reason": "ZERO_QUANTITY"}

        # Risk gate - final check immediately before execution.
        verdict = self.risk.evaluate(self.db, account_id, market, side, price, qty,
                                     prices, exec_type)
        if not verdict.ok:
            self.db.execute(
                "INSERT INTO orders(id, account_id, market_id, question, side, action, requested_price,"
                " quantity, exec_type, status, strategy_version, reason, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (new_id("ord_"), account_id, market.id, market.question, side, "BUY", price,
                 qty, exec_type, "REJECTED", strategy_version,
                 f"{verdict.reason}: {'; '.join(verdict.flags)}", utcnow_iso()),
            )
            return {"ok": False, "reason": verdict.reason, "flags": verdict.flags}

        # Simulated fill with adverse slippage.
        fill = round(price * (1.0 + self.slippage_bps / 10_000.0), 6)
        fill = min(fill, 0.9999)
        notional = round(fill * qty, 6)
        fee = round(notional * self.fee_bps / 10_000.0, 6)
        slippage = round((fill - price) * qty, 6)

        # Debit cash.
        self.db.execute(
            "UPDATE accounts SET cash = cash - ? WHERE id = ?", (notional + fee, account_id)
        )

        order_id = new_id("ord_")
        now = utcnow_iso()
        self.db.execute(
            "INSERT INTO orders(id, account_id, market_id, question, side, action, requested_price,"
            " fill_price, quantity, notional, fee, slippage, exec_type, status, strategy_version,"
            " reason, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (order_id, account_id, market.id, market.question, side, "BUY", price, fill, qty,
             notional, fee, slippage, exec_type, "FILLED", strategy_version, reason, now),
        )

        pos_id = new_id("pos_")
        self.db.execute(
            "INSERT INTO positions(id, account_id, market_id, question, side, quantity, entry_price,"
            " entry_time, exec_type, strategy_version, status, current_price, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pos_id, account_id, market.id, market.question, side, qty, fill, now, exec_type,
             strategy_version, "OPEN", market.yes_price, now),
        )

        self.db.execute(
            "INSERT INTO trades(id, order_id, account_id, market_id, question, side, quantity,"
            " entry_price, fees, slippage, status, exec_type, opened_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (new_id("trd_"), order_id, account_id, market.id, market.question, side, qty, fill,
             fee, slippage, "OPEN", exec_type, now),
        )

        return {
            "ok": True, "order_id": order_id, "position_id": pos_id, "side": side,
            "requested_price": price, "fill_price": fill, "quantity": qty,
            "notional": notional, "fee": fee, "slippage": slippage, "exec_type": exec_type,
        }

    # -- maintenance --------------------------------------------------------
    def mark_to_market(self, account_id: str, quotes: dict) -> None:
        """quotes maps market_id -> latest implied YES probability (float)."""
        now = utcnow_iso()
        for pos in portfolio.open_positions(self.db, account_id):
            price = quotes.get(pos["market_id"])
            if price is None:
                price = pos["current_price"]
            self.db.execute(
                "UPDATE positions SET current_price = ?, updated_at = ? WHERE id = ?",
                (price, now, pos["id"]),
            )

    def settle(self, account_id: str, resolved: dict) -> list[dict]:
        """Settle positions whose outcome the provider has confirmed.

        `resolved` maps market_id -> "YES" / "NO". Only confirmed outcomes
        settle; transient price moves never do.
        """
        settled = []
        for pos in portfolio.open_positions(self.db, account_id):
            outcome = resolved.get(pos["market_id"])
            if outcome not in ("YES", "NO"):
                continue
            qty = pos["quantity"]
            won = (pos["side"] == outcome)
            proceeds = round(qty * (1.0 if won else 0.0), 6)
            entry_order = self.db.query_one(
                "SELECT fee FROM orders WHERE account_id = ? AND market_id = ? AND status='FILLED' "
                "ORDER BY created_at LIMIT 1",
                (account_id, pos["market_id"]),
            )
            entry_fee = float(entry_order["fee"]) if entry_order else 0.0
            realized = round(proceeds - qty * pos["entry_price"] - entry_fee, 6)
            now = utcnow_iso()

            self.db.execute(
                "UPDATE accounts SET cash = cash + ? WHERE id = ?", (proceeds, account_id)
            )
            self.db.execute(
                "UPDATE positions SET status='CLOSED', settled=1, exit_price=?, exit_time=?,"
                " realized_pnl=?, updated_at=? WHERE id=?",
                (1.0 if won else 0.0, now, realized, now, pos["id"]),
            )
            self.db.execute(
                "UPDATE trades SET status='CLOSED', exit_price=?, realized_pnl=?, closed_at=? "
                "WHERE account_id=? AND market_id=? AND status='OPEN'",
                (1.0 if won else 0.0, realized, now, account_id, pos["market_id"]),
            )
            settled.append({"market_id": pos["market_id"], "side": pos["side"],
                            "outcome": outcome, "realized_pnl": realized})
        return settled
