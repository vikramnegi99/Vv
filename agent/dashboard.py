"""Renders the research terminal as a single self-contained HTML file.

No external CDN, no frameworks: inline CSS + a tiny bit of vanilla JS. Every
number comes from the stored database - nothing is invented for display.
"""
from __future__ import annotations

import html
import json
from typing import Optional

from . import health, portfolio
from .challenge import MAIN_ACCOUNT
from .util import utcnow_iso


def _money(x: Optional[float]) -> str:
    if x is None:
        return "-"
    return f"${x:,.2f}"


def _pct(x: Optional[float], digits: int = 2) -> str:
    if x is None:
        return "-"
    return f"{x * 100:.{digits}f}%"


def _num(x) -> str:
    if x is None:
        return "-"
    try:
        return f"{int(x):,}"
    except (TypeError, ValueError):
        return str(x)


def _sparkline(values: list[float], width: int = 640, height: int = 90) -> str:
    if len(values) < 2:
        return '<div class="muted">Not enough data for an equity curve yet.</div>'
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    pts = []
    for i, v in enumerate(values):
        x = 4 + i * (width - 8) / (len(values) - 1)
        y = height - 6 - (v - lo) / span * (height - 12)
        pts.append(f"{x:.1f},{y:.1f}")
    color = "#3ddc97" if values[-1] >= values[0] else "#ff6b6b"
    return (f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" class="spark">'
            f'<polyline fill="none" stroke="{color}" stroke-width="2" points="{" ".join(pts)}"/>'
            f'</svg>')


def _state_class(state: str) -> str:
    return {
        "RUNNING": "ok", "DEGRADED": "warn", "REVIEW_REQUIRED": "warn",
        "LOCKED": "bad", "DEAD": "bad",
    }.get(state, "muted")


def _card(title: str, body: str) -> str:
    return f'<section class="card"><h2>{html.escape(title)}</h2>{body}</section>'


def _kv_table(rows: list[tuple[str, str]]) -> str:
    body = "".join(
        f"<tr><th>{html.escape(k)}</th><td>{v}</td></tr>" for k, v in rows
    )
    return f'<table class="kv">{body}</table>'


def collect(db, cfg) -> dict:
    st = health.get_state(db)
    ch = db.query_one("SELECT * FROM challenge WHERE id = 1")
    ch = dict(ch) if ch else {}

    prices = {p["market_id"]: p["current_price"]
              for p in portfolio.open_positions(db, MAIN_ACCOUNT)}
    acct = portfolio.get_account(db, MAIN_ACCOUNT)
    equity = portfolio.equity(db, MAIN_ACCOUNT, prices)
    cash = float(acct["cash"]) if acct else 0.0
    starting = float(acct["starting_balance"]) if acct else 0.0
    exposure = portfolio.open_exposure(db, MAIN_ACCOUNT)
    realized = portfolio.realized_pnl(db, MAIN_ACCOUNT)
    unrealized = portfolio.unrealized_pnl(db, MAIN_ACCOUNT, prices)
    lifetime = round(realized + unrealized, 6)
    daily = portfolio.daily_pnl(db, MAIN_ACCOUNT, prices)
    dd = portfolio.max_drawdown(db, MAIN_ACCOUNT)

    latest_scan = db.query_one("SELECT * FROM scans ORDER BY ts DESC LIMIT 1")
    latest_scan = dict(latest_scan) if latest_scan else {}
    ph = db.query_one("SELECT * FROM provider_health ORDER BY ts DESC LIMIT 1")
    ph = dict(ph) if ph else {}

    rejections = db.query(
        "SELECT reason, COUNT(*) AS c FROM rejections GROUP BY reason ORDER BY c DESC LIMIT 15")
    total_rej = sum(int(r["c"]) for r in rejections) or 1

    trades = db.query(
        "SELECT * FROM trades ORDER BY opened_at DESC LIMIT ?",
        (int(cfg.get_path("dashboard.max_trade_rows", 100)),))

    curve = [float(r["equity"]) for r in db.query(
        "SELECT equity FROM equity_snapshots WHERE account_id = ? ORDER BY ts", (MAIN_ACCOUNT,))]

    def count_exec(kind: str) -> int:
        row = db.query_one("SELECT COUNT(*) AS c FROM orders WHERE exec_type = ? AND status='FILLED'",
                           (kind,))
        return int(row["c"]) if row else 0

    strat_realized = db.query_one(
        "SELECT COALESCE(SUM(realized_pnl),0) AS s FROM positions "
        "WHERE account_id=? AND exec_type='STRATEGY' AND status='CLOSED'", (MAIN_ACCOUNT,))
    strat_realized = float(strat_realized["s"]) if strat_realized else 0.0

    settled_strategy = db.query_one(
        "SELECT COUNT(*) AS c, COALESCE(SUM(realized_pnl),0) AS pnl FROM positions "
        "WHERE account_id=? AND exec_type='STRATEGY' AND status='CLOSED'", (MAIN_ACCOUNT,))
    settled_strategy = dict(settled_strategy) if settled_strategy else {"c": 0, "pnl": 0.0}

    return dict(
        state=st, challenge=ch, equity=equity, cash=cash, starting=starting,
        exposure=exposure, realized=realized, unrealized=unrealized, lifetime=lifetime,
        daily=daily, drawdown=dd, latest_scan=latest_scan, ph=ph, rejections=rejections,
        total_rej=total_rej, trades=trades, curve=curve, count_exec=count_exec,
        strat_realized=strat_realized, settled_strategy=settled_strategy,
        generated_at=utcnow_iso(),
    )


def render(db, cfg, output_path: str) -> str:
    d = collect(db, cfg)
    st = d["state"]
    ch = d["challenge"]
    scan = d["latest_scan"]
    ph = d["ph"]

    elapsed = 0.0
    remaining = float(cfg.get_path("challenge.deadline_hours", 45))
    deadline_remaining = float(cfg.get_path("challenge.first_trade_deadline_hours", 24))
    if ch.get("start_utc"):
        from .util import hours_since
        elapsed = hours_since(ch["start_utc"])
        remaining = max(0.0, float(cfg.get_path("challenge.deadline_hours", 45)) - elapsed)
        deadline_remaining = max(0.0, float(cfg.get_path("challenge.first_trade_deadline_hours", 24)) - elapsed)

    first_trade_status = ("MET (" + str(ch.get("first_trade_type") or "") + ")") if ch.get("first_trade_met") \
        else ("PENDING" + (" - DEADLINE_MISSED" if ch.get("deadline_alert") else ""))
    canary_status = "not run"
    if ch.get("canary_result"):
        try:
            cr = json.loads(ch["canary_result"])
            canary_status = cr.get("status", "unknown")
        except (TypeError, json.JSONDecodeError):
            canary_status = "recorded"

    returns_pct = (d["lifetime"] / d["starting"]) if d["starting"] else 0.0
    strategy_score = d["strat_realized"]

    top = (
        f'<div class="topbar">'
        f'<span class="badge {_state_class(st["status"])}">{html.escape(st["status"])}</span>'
        f'<span class="pill">PAPER ONLY &middot; REAL_TRADING=false</span>'
        f'<span class="pill">Last scan: {html.escape(scan.get("ts") or "never")}</span>'
        f'<span class="pill">Now: {html.escape(d["generated_at"])}</span>'
        f'<span class="pill">Elapsed: {elapsed:.1f}h / {float(cfg.get_path("challenge.deadline_hours",45)):.0f}h</span>'
        f'<span class="pill">Provider: {html.escape(str(ph.get("provider") or "-"))} '
        f'({"ok" if ph.get("ok") else "down"})</span>'
        f'</div>'
    )

    capital = _kv_table([
        ("Starting capital", _money(d["starting"])),
        ("Current balance (cash)", _money(d["cash"])),
        ("Current equity", _money(d["equity"])),
        ("Lifetime P&L", _money(d["lifetime"])),
        ("24h P&L", _money(d["daily"])),
        ("Return %", _pct(returns_pct)),
        ("Max drawdown", _pct(d["drawdown"])),
        ("Available cash", _money(d["cash"])),
        ("Open exposure", _money(d["exposure"])),
    ])

    challenge = _kv_table([
        ("Hours elapsed", f"{elapsed:.1f} / {float(cfg.get_path('challenge.deadline_hours',45)):.0f}"),
        ("Hours remaining", f"{remaining:.1f}h"),
        ("First-trade deadline", f"{deadline_remaining:.1f}h left"),
        ("First-trade requirement", html.escape(first_trade_status)),
        ("Challenge status", html.escape(str(ch.get("status") or "PENDING"))),
        ("Strategy score (realized P&L)", _money(strategy_score)),
        ("Strategy trades", _num(d["count_exec"]("STRATEGY"))),
        ("Exploration trades", _num(d["count_exec"]("EXPLORATION"))),
        ("Canary tests", _num(d["count_exec"]("CANARY")) + f" ({html.escape(canary_status)})"),
        ("Exploration orders used", f"{ch.get('exploration_orders', 0)} / "
                                   f"{cfg.get_path('risk.exploration_max_orders', 3)}"),
    ])

    funnel = _kv_table([
        ("Markets discovered", _num(scan.get("discovered"))),
        ("Eligible markets", _num(scan.get("eligible"))),
        ("Candidates", _num(scan.get("candidates"))),
        ("Markets analyzed", _num(scan.get("analyzed"))),
        ("Strategy signals", _num(scan.get("signals"))),
        ("Signals rejected", _num(scan.get("rejected"))),
        ("Risk-approved orders", _num(scan.get("risk_approved"))),
        ("Paper orders executed", _num(scan.get("executed"))),
    ])

    rej_rows = "".join(
        f"<tr><td>{html.escape(str(r['reason']))}</td><td>{int(r['c'])}</td>"
        f"<td>{int(r['c']) / d['total_rej'] * 100:.1f}%</td></tr>" for r in d["rejections"]
    ) or '<tr><td colspan="3" class="muted">No rejections recorded yet.</td></tr>'
    rejections = (f'<table class="data"><thead><tr><th>Reason</th><th>Count</th><th>%</th></tr></thead>'
                  f'<tbody>{rej_rows}</tbody></table>')

    rows_html = []
    for t in d["trades"]:
        entry = f"{t['entry_price']:.4f}" if t["entry_price"] is not None else "-"
        exitp = f"{t['exit_price']:.4f}" if t["exit_price"] is not None else "-"
        rows_html.append(
            "<tr><td>{}</td><td class='q'>{}</td><td>{}</td><td>{}</td><td>{}</td>"
            "<td>{}</td><td>{:.2f}</td><td>{:.4f}</td><td>{:.4f}</td><td>{:.4f}</td>"
            "<td>{}</td></tr>".format(
                html.escape(str(t["opened_at"] or "")),
                html.escape((t["question"] or "")[:60]),
                html.escape(t["side"]), html.escape(t["exec_type"]), entry, exitp,
                t["quantity"] or 0.0, t["fees"] or 0.0, t["slippage"] or 0.0,
                t["realized_pnl"] or 0.0, html.escape(t["status"])))
    trade_rows = "".join(rows_html) or '<tr><td colspan="11" class="muted">No trades yet.</td></tr>'
    trades = (f'<div class="scroll"><table class="data"><thead><tr><th>Time</th><th>Instrument</th>'
              f'<th>Side</th><th>Type</th><th>Entry</th><th>Exit</th><th>Qty</th><th>Fees</th>'
              f'<th>Slip</th><th>Realized P&L</th><th>Status</th></tr></thead>'
              f'<tbody>{trade_rows}</tbody></table></div>')

    gap = st.get("last_scan_gap_minutes")
    system = _kv_table([
        ("Last successful cycle", html.escape(str(st.get("last_success_utc") or "-"))),
        ("Last cycle", html.escape(str(st.get("last_cycle_utc") or "-"))),
        ("Last API response", html.escape(str(st.get("last_api_utc") or "-"))),
        ("API latency", f"{ph.get('latency_ms', '-')} ms"),
        ("Data freshness", "fresh" if scan else "no scan yet"),
        ("Database health", "ok" if health.db_integrity_ok(db) else "CORRUPT"),
        ("Scheduler health", "ok" if (gap is None or gap <= float(cfg.get_path('challenge.max_scan_gap_minutes',30))) else f"gap {gap:.1f}m"),
        ("Config version", html.escape(str(cfg.get_path("meta.config_version", "-")))),
        ("Strategy version", html.escape(str(cfg.get_path("meta.strategy_version", "-")))),
        ("Software version", html.escape(str(cfg.get_path("meta.software_version", "-")))),
    ])

    body = f"""
{top}
<div class="grid">
  {_card("Capital", capital)}
  {_card("Challenge", challenge)}
  {_card("Signal funnel", funnel)}
  {_card("Equity curve", _sparkline(d["curve"]))}
  {_card("Rejection analytics", rejections)}
  {_card("System health", system)}
</div>
{_card("Trade history", trades)}
<div class="foot">
  <button onclick="location.reload()">Refresh</button>
  <label><input type="checkbox" id="auto" onchange="toggleAuto()"> Auto-refresh (60s)</label>
  <span class="muted">Last updated: {html.escape(d["generated_at"])}</span>
</div>
"""

    doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(str(cfg.get_path("dashboard.title", "Paper Trading Terminal")))}</title>
<style>
:root {{ --bg:#0b0f14; --card:#121922; --line:#1f2a37; --fg:#e6edf3; --muted:#8b98a5;
        --ok:#3ddc97; --warn:#ffcc66; --bad:#ff6b6b; --accent:#4c8dff; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--fg);
       font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }}
.topbar {{ display:flex; flex-wrap:wrap; gap:8px; align-items:center; padding:12px 16px;
          background:#0e141b; border-bottom:1px solid var(--line); position:sticky; top:0; z-index:5; }}
.badge {{ padding:3px 10px; border-radius:999px; font-weight:700; font-size:12px; }}
.badge.ok {{ background:rgba(61,220,151,.15); color:var(--ok); }}
.badge.warn {{ background:rgba(255,204,102,.15); color:var(--warn); }}
.badge.bad {{ background:rgba(255,107,107,.15); color:var(--bad); }}
.badge.muted {{ background:#1b2530; color:var(--muted); }}
.pill {{ background:#141c25; border:1px solid var(--line); border-radius:999px;
        padding:3px 10px; font-size:12px; color:var(--muted); }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:12px; padding:12px 16px; }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px;
        margin:0 16px 12px; overflow:hidden; }}
.grid .card {{ margin:0; }}
.card h2 {{ margin:0 0 10px; font-size:13px; letter-spacing:.06em; text-transform:uppercase;
           color:var(--muted); font-weight:600; }}
table {{ width:100%; border-collapse:collapse; }}
table.kv th {{ text-align:left; color:var(--muted); font-weight:500; padding:4px 0; }}
table.kv td {{ text-align:right; padding:4px 0; font-variant-numeric:tabular-nums; }}
table.data th, table.data td {{ padding:6px 8px; border-bottom:1px solid var(--line);
        text-align:left; font-size:12.5px; white-space:nowrap; }}
table.data td.q {{ white-space:normal; max-width:220px; }}
table.data thead th {{ color:var(--muted); font-weight:600; position:sticky; top:0; background:var(--card); }}
.scroll {{ overflow-x:auto; }}
.spark {{ width:100%; height:110px; display:block; }}
.foot {{ display:flex; gap:14px; align-items:center; flex-wrap:wrap; padding:10px 16px 40px; }}
button {{ background:var(--accent); color:#fff; border:0; border-radius:8px; padding:8px 14px;
         font-size:13px; cursor:pointer; }}
.muted {{ color:var(--muted); }}
@media (max-width:520px) {{ .card {{ margin:0 10px 10px; }} .grid {{ padding:10px; gap:10px; }} }}
</style>
</head>
<body>
{body}
<script>
let t=null;
function toggleAuto(){{
  const on=document.getElementById('auto').checked;
  if(t){{clearInterval(t);t=null;}}
  if(on){{t=setInterval(()=>location.reload(),60000);}}
}}
</script>
</body>
</html>
"""
    import os
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as fh:
        fh.write(doc)
    return output_path
