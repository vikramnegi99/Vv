"""Human-readable status and final-report generation."""
from __future__ import annotations

import json

from . import health, portfolio
from .challenge import CANARY_ACCOUNT, MAIN_ACCOUNT
from .util import hours_since, utcnow_iso


def build_status(db, cfg) -> dict:
    st = health.get_state(db)
    ch = db.query_one("SELECT * FROM challenge WHERE id = 1")
    ch = dict(ch) if ch else {}
    prices = {p["market_id"]: p["current_price"]
              for p in portfolio.open_positions(db, MAIN_ACCOUNT)}
    acct = portfolio.get_account(db, MAIN_ACCOUNT)
    latest = db.query_one("SELECT * FROM scans ORDER BY ts DESC LIMIT 1")
    latest = dict(latest) if latest else {}
    return {
        "state": st["status"],
        "challenge_status": ch.get("status"),
        "hours_elapsed": round(hours_since(ch["start_utc"]), 2) if ch.get("start_utc") else 0.0,
        "starting_capital": float(acct["starting_balance"]) if acct else None,
        "cash": float(acct["cash"]) if acct else None,
        "equity": portfolio.equity(db, MAIN_ACCOUNT, prices),
        "lifetime_pnl": round(portfolio.realized_pnl(db, MAIN_ACCOUNT)
                              + portfolio.unrealized_pnl(db, MAIN_ACCOUNT, prices), 6),
        "max_drawdown": portfolio.max_drawdown(db, MAIN_ACCOUNT),
        "first_trade_met": bool(ch.get("first_trade_met")),
        "canary_done": bool(ch.get("canary_done")),
        "deadline_alert": bool(ch.get("deadline_alert")),
        "latest_scan": latest,
    }


def _count(db, sql, params=()):
    row = db.query_one(sql, params)
    return int(row["c"]) if row else 0


def build_report(db, cfg) -> str:
    status = build_status(db, cfg)
    ch = dict(db.query_one("SELECT * FROM challenge WHERE id = 1") or {})
    strat = _count(db, "SELECT COUNT(*) c FROM orders WHERE exec_type='STRATEGY' AND status='FILLED'")
    expl = _count(db, "SELECT COUNT(*) c FROM orders WHERE exec_type='EXPLORATION' AND status='FILLED'")
    canary = _count(db, "SELECT COUNT(*) c FROM orders WHERE exec_type='CANARY' AND status='FILLED'")
    rej = db.query("SELECT reason, COUNT(*) c FROM rejections GROUP BY reason ORDER BY c DESC")

    lines = [
        "# Paper Trading Agent - Challenge Report",
        "",
        f"- Generated: {utcnow_iso()}",
        f"- Software version: {cfg.get_path('meta.software_version')}",
        f"- Strategy version: {cfg.get_path('meta.strategy_version')}",
        f"- Config version: {cfg.get_path('meta.config_version')}",
        f"- Provider: {ch.get('provider')} (paper only; REAL_TRADING=false)",
        "",
        "## Bankroll",
        f"- Starting capital: ${status['starting_capital']:.2f}",
        f"- Current equity: ${status['equity']:.2f}",
        f"- Lifetime P&L: ${status['lifetime_pnl']:.2f}",
        f"- Max drawdown: {status['max_drawdown'] * 100:.2f}%",
        "",
        "## Challenge",
        f"- Status: {status['challenge_status']} / lifecycle {status['state']}",
        f"- Hours elapsed: {status['hours_elapsed']}",
        f"- First-trade requirement met: {status['first_trade_met']}",
        f"- Canary completed: {status['canary_done']}",
        f"- Deadline alert: {status['deadline_alert']}",
        "",
        "## Execution counts (labelled separately)",
        f"- STRATEGY trades: {strat}",
        f"- EXPLORATION trades: {expl}",
        f"- CANARY tests: {canary}",
        "",
        "## Latest scan funnel",
    ]
    ls = status["latest_scan"]
    for k in ("discovered", "eligible", "candidates", "analyzed", "signals",
              "rejected", "risk_approved", "executed"):
        lines.append(f"- {k}: {ls.get(k)}")
    lines.append("")
    lines.append("## Rejection reasons")
    if rej:
        for r in rej:
            lines.append(f"- {r['reason']}: {r['c']}")
    else:
        lines.append("- none recorded")
    lines.append("")
    lines.append("## Canary detail")
    if ch.get("canary_result"):
        try:
            lines.append("```json")
            lines.append(json.dumps(json.loads(ch["canary_result"]), indent=2))
            lines.append("```")
        except (TypeError, json.JSONDecodeError):
            lines.append(str(ch["canary_result"]))
    else:
        lines.append("- not run")
    return "\n".join(lines)
