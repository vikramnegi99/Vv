"""Account, position and P&L accounting.

Positions are binary contracts. A YES position is worth `quantity * p`; a NO
position is worth `quantity * (1 - p)`, where p is the market's implied
probability of YES. Settlement pays 1.0 per winning contract and 0.0 per
losing contract, and only happens when the provider confirms the outcome.
"""
from __future__ import annotations

from typing import Optional

from .util import utcnow_iso


def ensure_account(db, account_id: str, kind: str, starting_balance: float) -> bool:
    """Create the account once. Never resets an existing balance."""
    if db.query_one("SELECT id FROM accounts WHERE id = ?", (account_id,)) is not None:
        return False
    db.execute(
        "INSERT INTO accounts(id, kind, starting_balance, cash, created_at) VALUES (?,?,?,?,?)",
        (account_id, kind, starting_balance, starting_balance, utcnow_iso()),
    )
    return True


def get_account(db, account_id: str):
    return db.query_one("SELECT * FROM accounts WHERE id = ?", (account_id,))


def open_positions(db, account_id: str) -> list:
    return db.query(
        "SELECT * FROM positions WHERE account_id = ? AND status = 'OPEN' ORDER BY entry_time",
        (account_id,),
    )


def position_value(pos, price_yes: Optional[float]) -> float:
    price = price_yes if price_yes is not None else pos["current_price"]
    if price is None:
        price = pos["entry_price"]
    if pos["side"] == "YES":
        return pos["quantity"] * price
    return pos["quantity"] * (1.0 - price)


def open_exposure(db, account_id: str) -> float:
    """Cost basis of all open positions (used for the exposure limit)."""
    return round(sum(p["quantity"] * p["entry_price"] for p in open_positions(db, account_id)), 6)


def market_value(db, account_id: str, prices: dict) -> float:
    total = 0.0
    for pos in open_positions(db, account_id):
        total += position_value(pos, prices.get(pos["market_id"]))
    return round(total, 6)


def equity(db, account_id: str, prices: dict) -> float:
    acct = get_account(db, account_id)
    if acct is None:
        return 0.0
    return round(acct["cash"] + market_value(db, account_id, prices), 6)


def realized_pnl(db, account_id: str) -> float:
    row = db.query_one(
        "SELECT COALESCE(SUM(realized_pnl),0) AS s FROM positions "
        "WHERE account_id = ? AND status = 'CLOSED'",
        (account_id,),
    )
    return round(float(row["s"]), 6)


def unrealized_pnl(db, account_id: str, prices: dict) -> float:
    total = 0.0
    for pos in open_positions(db, account_id):
        total += position_value(pos, prices.get(pos["market_id"])) - pos["quantity"] * pos["entry_price"]
    return round(total, 6)


def total_fees(db, account_id: str) -> float:
    row = db.query_one(
        "SELECT COALESCE(SUM(fee),0) AS s FROM orders WHERE account_id = ? AND status = 'FILLED'",
        (account_id,),
    )
    return round(float(row["s"]), 6)


def total_slippage(db, account_id: str) -> float:
    row = db.query_one(
        "SELECT COALESCE(SUM(slippage),0) AS s FROM orders WHERE account_id = ? AND status = 'FILLED'",
        (account_id,),
    )
    return round(float(row["s"]), 6)


def peak_equity(db, account_id: str) -> float:
    row = db.query_one(
        "SELECT MAX(equity) AS m FROM equity_snapshots WHERE account_id = ?", (account_id,)
    )
    val = row["m"] if row and row["m"] is not None else None
    if val is None:
        acct = get_account(db, account_id)
        return float(acct["starting_balance"]) if acct else 0.0
    return float(val)


def max_drawdown(db, account_id: str) -> float:
    """Peak-to-trough drawdown over the recorded equity curve, as a fraction."""
    rows = db.query(
        "SELECT equity FROM equity_snapshots WHERE account_id = ? ORDER BY ts", (account_id,)
    )
    peak = None
    worst = 0.0
    for row in rows:
        e = float(row["equity"])
        peak = e if peak is None else max(peak, e)
        if peak and peak > 0:
            worst = max(worst, (peak - e) / peak)
    return round(worst, 6)


def day_start_equity(db, account_id: str, fallback: float) -> float:
    """Equity at the start of the current UTC day (first snapshot today)."""
    today = utcnow_iso()[:10]
    row = db.query_one(
        "SELECT equity FROM equity_snapshots WHERE account_id = ? AND substr(ts,1,10) = ? "
        "ORDER BY ts LIMIT 1",
        (account_id, today),
    )
    return float(row["equity"]) if row else float(fallback)


def daily_pnl(db, account_id: str, prices: dict) -> float:
    start = day_start_equity(db, account_id, equity(db, account_id, prices))
    return round(equity(db, account_id, prices) - start, 6)


def record_snapshot(db, account_id: str, prices: dict) -> dict:
    acct = get_account(db, account_id)
    cash = float(acct["cash"]) if acct else 0.0
    eq = equity(db, account_id, prices)
    exp = open_exposure(db, account_id)
    rp = realized_pnl(db, account_id)
    up = unrealized_pnl(db, account_id, prices)
    db.execute(
        "INSERT INTO equity_snapshots(ts, account_id, cash, equity, exposure, realized_pnl, unrealized_pnl) "
        "VALUES (?,?,?,?,?,?,?)",
        (utcnow_iso(), account_id, cash, eq, exp, rp, up),
    )
    return {"cash": cash, "equity": eq, "exposure": exp,
            "realized_pnl": rp, "unrealized_pnl": up}
