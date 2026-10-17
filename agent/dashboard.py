"""Renders the research terminal as a single self-contained HTML file.

Layout: everything lives in clean bordered boxes. No top status/timestamp
strip. Large bold numbers, colour-coded P&L, and a per-trade breakdown that
shows which market each trade was on and what it made or lost.

No external CDN, no frameworks - inline CSS plus a little vanilla JS. Every
number comes from the stored database.
"""
from __future__ import annotations

import html
import json
import os
from typing import Optional

from . import health, portfolio
from .challenge import MAIN_ACCOUNT
from .util import hours_since, utcnow_iso


# --------------------------------------------------------------------------- #
# formatting helpers
# --------------------------------------------------------------------------- #
def _money(x: Optional[float], signed: bool = False) -> str:
    if x is None:
        return "-"
    if signed and x > 0:
        return f"+${x:,.2f}"
    return f"${x:,.2f}"


def _pct(x: Optional[float], digits: int = 2, signed: bool = False) -> str:
    if x is None:
        return "-"
    sign = "+" if (signed and x > 0) else ""
    return f"{sign}{x * 100:.{digits}f}%"


def _num(x) -> str:
    if x is None:
        return "-"
    try:
        return f"{int(x):,}"
    except (TypeError, ValueError):
        return str(x)


def _tone(x: Optional[float]) -> str:
    if x is None:
        return ""
    return "pos" if x > 0 else ("neg" if x < 0 else "")


def _sparkline(values: list[float], width: int = 640, height: int = 140) -> str:
    if len(values) < 2:
        return '<div class="muted">Not enough data for an equity curve yet.</div>'
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    pts = []
    for i, v in enumerate(values):
        x = 6 + i * (width - 12) / (len(values) - 1)
        y = height - 8 - (v - lo) / span * (height - 16)
        pts.append(f"{x:.1f},{y:.1f}")
    color = "#2ee6a6" if values[-1] >= values[0] else "#ff5d6c"
    last = pts[-1].split(",")
    return (
        f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" class="spark">'
        f'<polyline fill="none" stroke="{color}" stroke-width="3" stroke-linejoin="round" '
        f'stroke-linecap="round" points="{" ".join(pts)}"/>'
        f'<circle cx="{last[0]}" cy="{last[1]}" r="5" fill="{color}"/>'
        f'</svg>'
    )


def _state_class(state: str) -> str:
    return {"RUNNING": "ok", "DEGRADED": "warn", "REVIEW_REQUIRED": "warn",
            "LOCKED": "bad", "DEAD": "bad"}.get(state, "muted")


def _kpi(label: str, value: str, sub: str = "", *, hl: bool = False,
         value_cls: str = "") -> str:
    cls = "kpi hl" if hl else "kpi"
    return (f'<div class="{cls}">'
            f'<div class="kpi-label">{html.escape(label)}</div>'
            f'<div class="kpi-value {value_cls}">{value}</div>'
            f'<div class="kpi-sub">{sub}</div></div>')


def _kv(rows: list[tuple[str, str]]) -> str:
    body = "".join(f"<tr><th>{html.escape(k)}</th><td>{v}</td></tr>" for k, v in rows)
    return f'<table class="kv">{body}</table>'


def _card(title: str, body: str) -> str:
    return f'<section class="card"><h2>{html.escape(title)}</h2>{body}</section>'


# --------------------------------------------------------------------------- #
# data
# --------------------------------------------------------------------------- #
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
    daily = portfolio.daily_pnl(db, MAIN_ACCOUNT, prices)
    dd = portfolio.max_drawdown(db, MAIN_ACCOUNT)

    # ---- every fill is one trade record ----------------------------------
    trades = []
    for p in db.query("SELECT * FROM positions WHERE account_id = ? ORDER BY entry_time DESC",
                      (MAIN_ACCOUNT,)):
        order = db.query_one(
            "SELECT fee, slippage FROM orders WHERE account_id = ? AND market_id = ? "
            "AND status = 'FILLED' ORDER BY created_at LIMIT 1",
            (MAIN_ACCOUNT, p["market_id"]))
        fee = float(order["fee"]) if order else 0.0
        cost = p["quantity"] * p["entry_price"]
        if p["status"] == "OPEN":
            mark = p["current_price"]
            pnl = round(portfolio.position_value(p, mark) - cost - fee, 4)
        else:
            mark = p["exit_price"]
            pnl = round(p["realized_pnl"] or 0.0, 4)
        trades.append({
            "market_id": p["market_id"], "question": p["question"], "side": p["side"],
            "exec_type": p["exec_type"], "quantity": p["quantity"],
            "entry_price": p["entry_price"], "mark": mark, "pnl": pnl,
            "status": p["status"], "fee": fee, "entry_time": p["entry_time"],
        })

    open_trades = [t for t in trades if t["status"] == "OPEN"]
    closed_trades = [t for t in trades if t["status"] == "CLOSED"]
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    realized_total = round(sum(t["pnl"] for t in closed_trades), 4)
    unrealized_total = round(sum(t["pnl"] for t in open_trades), 4)
    net_pnl = round(realized_total + unrealized_total, 4)
    decided = len(wins) + len(losses)
    win_rate = (len(wins) / decided) if decided else None
    avg_pnl = (net_pnl / len(trades)) if trades else None

    by_market: dict = {}
    for t in trades:
        e = by_market.setdefault(t["market_id"], {
            "question": t["question"], "trades": 0, "pnl": 0.0, "open": 0, "side": t["side"]})
        e["trades"] += 1
        e["pnl"] = round(e["pnl"] + t["pnl"], 4)
        if t["status"] == "OPEN":
            e["open"] += 1
    by_market = sorted(by_market.values(), key=lambda x: x["pnl"], reverse=True)

    latest_scan = db.query_one("SELECT * FROM scans ORDER BY ts DESC LIMIT 1")
    latest_scan = dict(latest_scan) if latest_scan else {}
    ph = db.query_one("SELECT * FROM provider_health ORDER BY ts DESC LIMIT 1")
    ph = dict(ph) if ph else {}

    rejections = db.query(
        "SELECT reason, COUNT(*) AS c FROM rejections GROUP BY reason ORDER BY c DESC LIMIT 12")
    total_rej = sum(int(r["c"]) for r in rejections) or 1

    curve = [float(r["equity"]) for r in db.query(
        "SELECT equity FROM equity_snapshots WHERE account_id = ? ORDER BY ts", (MAIN_ACCOUNT,))]

    def count_exec(kind: str) -> int:
        row = db.query_one(
            "SELECT COUNT(*) AS c FROM orders WHERE exec_type = ? AND status='FILLED'", (kind,))
        return int(row["c"]) if row else 0

    strat_realized_row = db.query_one(
        "SELECT COALESCE(SUM(realized_pnl),0) AS s FROM positions "
        "WHERE account_id=? AND exec_type='STRATEGY' AND status='CLOSED'", (MAIN_ACCOUNT,))
    strat_realized = float(strat_realized_row["s"]) if strat_realized_row else 0.0

    return dict(
        state=st, challenge=ch, equity=equity, cash=cash, starting=starting,
        exposure=exposure, daily=daily, drawdown=dd,
        trades=trades, open_trades=open_trades, closed_trades=closed_trades,
        wins=len(wins), losses=len(losses), win_rate=win_rate, avg_pnl=avg_pnl,
        realized_total=realized_total, unrealized_total=unrealized_total,
        net_pnl=net_pnl, by_market=by_market,
        latest_scan=latest_scan, ph=ph, rejections=rejections, total_rej=total_rej,
        curve=curve, count_exec=count_exec, strat_realized=strat_realized,
        generated_at=utcnow_iso(),
    )


# --------------------------------------------------------------------------- #
# styles
# --------------------------------------------------------------------------- #
CSS = """
:root{
  --bg:#070a0f; --card:#101823; --line:#20303f; --line2:#2a3d50;
  --fg:#eef4fa; --muted:#8395ab; --ok:#2ee6a6; --warn:#ffc857;
  --bad:#ff5d6c; --accent:#4d8dff; --accent2:#8b5cff;
}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--fg);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  -webkit-text-size-adjust:100%;}
body{padding-bottom:36px}
.wrap{max-width:1220px;margin:0 auto;padding:14px}
.alerts{display:flex;flex-direction:column;gap:8px;margin-bottom:12px}
.alert{font-weight:800;font-size:15px;padding:13px 16px;border-radius:10px;
  border:1px solid rgba(255,93,108,.45);background:rgba(255,93,108,.12);color:#ffd9dd}
.alert.warn{border-color:rgba(255,200,87,.45);background:rgba(255,200,87,.1);color:#ffe9b8}
h2{font-size:12px;letter-spacing:.16em;text-transform:uppercase;color:var(--muted);
  margin:0 0 12px;font-weight:800}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(168px,1fr));gap:11px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:15px 16px}
.kpi.hl{border-color:rgba(77,141,255,.55);
  box-shadow:inset 0 0 0 1px rgba(77,141,255,.16),0 10px 34px -24px rgba(77,141,255,.95)}
.kpi-label{font-size:11.5px;letter-spacing:.13em;text-transform:uppercase;
  color:var(--muted);font-weight:800;margin-bottom:7px}
.kpi-value{font-size:clamp(25px,5.4vw,36px);font-weight:800;line-height:1.03;
  font-variant-numeric:tabular-nums}
.kpi-value.hero{font-size:clamp(38px,9.5vw,62px);letter-spacing:-.02em}
.kpi-sub{font-size:12.5px;color:var(--muted);margin-top:7px;line-height:1.35}
.pos{color:var(--ok)} .neg{color:var(--bad)} .warnc{color:var(--warn)} .accent{color:var(--accent)}
.status{font-weight:800;font-size:15px;letter-spacing:.1em;padding:7px 15px;
  border-radius:999px;border:1px solid transparent;display:inline-block}
.status.ok{color:var(--ok);background:rgba(46,230,166,.12);border-color:rgba(46,230,166,.45)}
.status.warn{color:var(--warn);background:rgba(255,200,87,.12);border-color:rgba(255,200,87,.45)}
.status.bad{color:var(--bad);background:rgba(255,93,108,.12);border-color:rgba(255,93,108,.45)}
.status.muted{color:var(--muted);background:#141d27;border-color:var(--line)}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
  padding:15px 16px;margin-top:11px}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:11px;
  margin-top:11px}
.grid2 .card{margin-top:0}
.progress{height:16px;border-radius:999px;background:#0d1620;border:1px solid var(--line);
  overflow:hidden;margin:4px 0 14px}
.progress>i{display:block;height:100%;
  background:linear-gradient(90deg,var(--accent),var(--accent2))}
table{width:100%;border-collapse:collapse}
table.kv th{text-align:left;color:var(--muted);font-weight:600;padding:8px 0;font-size:13px}
table.kv td{text-align:right;padding:8px 0;font-variant-numeric:tabular-nums;
  font-weight:800;font-size:15px}
table.data th,table.data td{padding:9px 11px;border-bottom:1px solid var(--line);
  font-size:12.5px;text-align:left;white-space:nowrap}
table.data thead th{color:var(--muted);font-weight:800;background:var(--card)}
table.data td.mkt{white-space:normal;min-width:200px;max-width:330px;font-weight:700;
  line-height:1.35}
table.data tbody tr:hover{background:rgba(255,255,255,.025)}
.scroll{overflow-x:auto}
.spark{width:100%;height:140px;display:block}
.bar{height:9px;border-radius:6px;background:#0d1620;overflow:hidden;min-width:56px}
.bar>i{display:block;height:100%;background:linear-gradient(90deg,var(--accent),var(--accent2))}
.foot{max-width:1220px;margin:0 auto;display:flex;gap:14px;align-items:center;
  flex-wrap:wrap;padding:14px}
button{background:var(--accent);color:#fff;border:0;border-radius:8px;padding:11px 20px;
  font-size:14px;font-weight:800;cursor:pointer}
label{font-size:13px;color:var(--muted)}
.muted{color:var(--muted)}
@media (max-width:520px){
  .wrap{padding:10px} .kpi{padding:13px} .card{padding:13px}
}
"""

SCRIPT = """
let t=null;
function toggleAuto(){
  const on=document.getElementById('auto').checked;
  if(t){clearInterval(t);t=null;}
  if(on){t=setInterval(()=>location.reload(),60000);}
}
"""


# --------------------------------------------------------------------------- #
# render
# --------------------------------------------------------------------------- #
def render(db, cfg, output_path: str) -> str:
    d = collect(db, cfg)
    st, ch, scan, ph = d["state"], d["challenge"], d["latest_scan"], d["ph"]

    total_hours = float(cfg.get_path("challenge.deadline_hours", 45))
    first_hours = float(cfg.get_path("challenge.first_trade_deadline_hours", 24))
    elapsed = hours_since(ch["start_utc"]) if ch.get("start_utc") else 0.0
    remaining = max(0.0, total_hours - elapsed)
    deadline_left = max(0.0, first_hours - elapsed)
    progress_pct = min(100.0, elapsed / total_hours * 100.0) if total_hours else 0.0

    returns_pct = (d["net_pnl"] / d["starting"]) if d["starting"] else 0.0
    first_trade_ok = bool(ch.get("first_trade_met"))
    first_trade_txt = (f"MET · {html.escape(str(ch.get('first_trade_type') or ''))}"
                       if first_trade_ok else
                       ("MISSED" if ch.get("deadline_alert") else "PENDING"))
    canary_status = "not run"
    if ch.get("canary_result"):
        try:
            canary_status = json.loads(ch["canary_result"]).get("status", "recorded")
        except (TypeError, json.JSONDecodeError):
            canary_status = "recorded"

    # ---- alerts ----------------------------------------------------------
    alerts = []
    if st["status"] in ("LOCKED", "DEAD"):
        alerts.append(f'<div class="alert">LIFECYCLE {html.escape(st["status"])} — '
                      f'new orders are halted. Review required.</div>')
    elif st["status"] in ("DEGRADED", "REVIEW_REQUIRED"):
        alerts.append(f'<div class="alert warn">LIFECYCLE {html.escape(st["status"])} — '
                      f'the agent is not fully healthy.</div>')
    if ch.get("deadline_alert"):
        alerts.append('<div class="alert">DEADLINE_MISSED — no real-market order and no '
                      'completed canary within 24h.</div>')
    alerts_html = f'<div class="alerts">{"".join(alerts)}</div>' if alerts else ""

    # ---- row 1: status + money -------------------------------------------
    pnl_cls = _tone(d["net_pnl"])
    row1 = (
        '<div class="kpis">'
        f'<div class="kpi hl"><div class="kpi-label">Agent status</div>'
        f'<div class="kpi-value"><span class="status {_state_class(st["status"])}">'
        f'{html.escape(st["status"])}</span></div>'
        f'<div class="kpi-sub">PAPER ONLY · REAL_TRADING=false<br>'
        f'Provider {html.escape(str(ph.get("provider") or "-"))} '
        f'({"OK" if ph.get("ok") else "DOWN"})</div></div>'
        + _kpi("Current equity", _money(d["equity"]),
               f'Starting {_money(d["starting"])} · Return '
               f'<span class="{pnl_cls}">{_pct(returns_pct, signed=True)}</span>',
               hl=True, value_cls="hero")
        + _kpi("Net P&L", _money(d["net_pnl"], signed=True),
               f'Realized {_money(d["realized_total"], signed=True)} · '
               f'Open {_money(d["unrealized_total"], signed=True)}',
               hl=True, value_cls=pnl_cls)
        + _kpi("24h P&L", _money(d["daily"], signed=True), "Since today's open",
               value_cls=_tone(d["daily"]))
        + "</div>"
    )

    # ---- row 2: trades summary -------------------------------------------
    win_rate_txt = _pct(d["win_rate"]) if d["win_rate"] is not None else "n/a"
    row2 = (
        '<div class="kpis" style="margin-top:11px">'
        + _kpi("Trades taken", _num(len(d["trades"])),
               f'{len(d["open_trades"])} open · {len(d["closed_trades"])} closed', hl=True)
        + _kpi("Winning", _num(d["wins"]), f'Losing {d["losses"]}',
               value_cls="pos" if d["wins"] else "")
        + _kpi("Win rate", win_rate_txt, f'of {d["wins"] + d["losses"]} decided',
               value_cls="pos" if (d["win_rate"] or 0) >= 0.5 else "warnc")
        + _kpi("Realized P&L", _money(d["realized_total"], signed=True), "Closed trades",
               value_cls=_tone(d["realized_total"]))
        + _kpi("Unrealized P&L", _money(d["unrealized_total"], signed=True), "Open trades",
               value_cls=_tone(d["unrealized_total"]))
        + _kpi("Avg P&L / trade", _money(d["avg_pnl"], signed=True),
               f'across {len(d["trades"])} trades', value_cls=_tone(d["avg_pnl"]))
        + "</div>"
    )

    # ---- row 3: capital --------------------------------------------------
    row3 = (
        '<div class="kpis" style="margin-top:11px">'
        + _kpi("Available cash", _money(d["cash"]), "Free to deploy")
        + _kpi("Open exposure", _money(d["exposure"]),
               f'Cap {_pct(float(cfg.get_path("risk.max_total_exposure_pct", .15)), digits=0)}')
        + _kpi("Max drawdown", _pct(d["drawdown"]),
               f'Limit {_pct(float(cfg.get_path("risk.max_drawdown_pct", .1)), digits=0)}',
               value_cls="warnc" if d["drawdown"] > 0 else "")
        + _kpi("Strategy · Exploration · Canary",
               f'{d["count_exec"]("STRATEGY")} · {d["count_exec"]("EXPLORATION")} · '
               f'{d["count_exec"]("CANARY")}', "execution categories")
        + "</div>"
    )

    # ---- challenge -------------------------------------------------------
    ft_cls = "pos" if first_trade_ok else ("neg" if ch.get("deadline_alert") else "warnc")
    dl_cls = "muted" if first_trade_ok else ("neg" if deadline_left < 4 else "warnc")
    challenge_body = (
        f'<div class="progress"><i style="width:{progress_pct:.1f}%"></i></div>'
        + _kv([
            ("Hours elapsed", f'{elapsed:.1f} / {total_hours:.0f}'),
            ("Hours remaining", f'{remaining:.1f}h'),
            ("First-trade deadline", f'<span class="{dl_cls}">{deadline_left:.1f}h left</span>'),
            ("First-trade requirement", f'<span class="{ft_cls}">{first_trade_txt}</span>'),
            ("Challenge status", html.escape(str(ch.get("status") or "PENDING"))),
            ("Strategy score (realized P&L)",
             f'<span class="{_tone(d["strat_realized"])}">'
             f'{_money(d["strat_realized"], signed=True)}</span>'),
            ("Exploration orders used",
             f'{ch.get("exploration_orders", 0)} / {cfg.get_path("risk.exploration_max_orders", 3)}'),
        ])
    )

    # ---- trades table ----------------------------------------------------
    if d["trades"]:
        rows = []
        for i, t in enumerate(d["trades"], 1):
            mark = f'{t["mark"]:.4f}' if t["mark"] is not None else "-"
            rows.append(
                "<tr><td>{}</td><td class='mkt'>{}</td><td>{}</td><td>{}</td><td>{:.2f}</td>"
                "<td>{:.4f}</td><td>{}</td><td class='{}'>{}</td><td>{}</td></tr>".format(
                    i, html.escape((t["question"] or t["market_id"])),
                    html.escape(t["side"]), html.escape(t["exec_type"]), t["quantity"],
                    t["entry_price"], mark, _tone(t["pnl"]),
                    _money(t["pnl"], signed=True), html.escape(t["status"])))
        trades_body = (
            '<div class="scroll"><table class="data"><thead><tr><th>#</th>'
            '<th>Market traded</th><th>Side</th><th>Type</th><th>Qty</th><th>Entry</th>'
            '<th>Mark / Exit</th><th>P&amp;L</th><th>Status</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>')
    else:
        trades_body = '<div class="muted">No trades yet.</div>'

    # ---- P&L by market ---------------------------------------------------
    if d["by_market"]:
        mrows = "".join(
            "<tr><td class='mkt'>{}</td><td>{}</td><td>{}</td><td class='{}'>{}</td></tr>".format(
                html.escape(m["question"] or ""), m["trades"], m["open"],
                _tone(m["pnl"]), _money(m["pnl"], signed=True))
            for m in d["by_market"])
        market_body = (
            '<div class="scroll"><table class="data"><thead><tr><th>Market</th><th>Trades</th>'
            '<th>Open</th><th>Net P&amp;L</th></tr></thead>'
            f'<tbody>{mrows}</tbody></table></div>')
    else:
        market_body = '<div class="muted">No markets traded yet.</div>'

    # ---- open positions --------------------------------------------------
    if d["open_trades"]:
        prows = "".join(
            "<tr><td class='mkt'>{}</td><td>{}</td><td>{:.2f}</td><td>{:.4f}</td><td>{}</td>"
            "<td class='{}'>{}</td></tr>".format(
                html.escape(t["question"] or t["market_id"]), html.escape(t["side"]),
                t["quantity"], t["entry_price"],
                f'{t["mark"]:.4f}' if t["mark"] is not None else "-",
                _tone(t["pnl"]), _money(t["pnl"], signed=True))
            for t in d["open_trades"])
        positions_body = (
            '<div class="scroll"><table class="data"><thead><tr><th>Market</th><th>Side</th>'
            '<th>Qty</th><th>Entry</th><th>Mark</th><th>Unrealized P&amp;L</th></tr></thead>'
            f'<tbody>{prows}</tbody></table></div>')
    else:
        positions_body = '<div class="muted">No open positions.</div>'

    # ---- funnel ----------------------------------------------------------
    funnel_body = (
        '<div class="kpis">'
        + _kpi("Discovered", _num(scan.get("discovered")))
        + _kpi("Eligible", _num(scan.get("eligible")))
        + _kpi("Analyzed", _num(scan.get("analyzed")))
        + _kpi("Signals", _num(scan.get("signals")), "passed the pipeline", value_cls="accent")
        + _kpi("Rejected", _num(scan.get("rejected")), "see reasons", value_cls="warnc")
        + _kpi("Executed", _num(scan.get("executed")), "paper orders", value_cls="pos")
        + "</div>"
    )

    # ---- rejections ------------------------------------------------------
    if d["rejections"]:
        rrows = "".join(
            "<tr><td>{}</td><td>{}</td><td style='width:34%'><div class='bar'>"
            "<i style='width:{:.1f}%'></i></div></td></tr>".format(
                html.escape(str(r["reason"])), int(r["c"]),
                int(r["c"]) / d["total_rej"] * 100.0)
            for r in d["rejections"])
        rej_body = ('<div class="scroll"><table class="data"><thead><tr><th>Reason</th>'
                    '<th>Count</th><th>Share</th></tr></thead>'
                    f'<tbody>{rrows}</tbody></table></div>')
    else:
        rej_body = '<div class="muted">No rejections recorded yet.</div>'

    # ---- system health ---------------------------------------------------
    gap = st.get("last_scan_gap_minutes")
    gap_limit = float(cfg.get_path("challenge.max_scan_gap_minutes", 30))
    sched_ok = gap is None or gap <= gap_limit
    system_body = _kv([
        ("Last successful cycle", html.escape(str(st.get("last_success_utc") or "-"))),
        ("Last scan", html.escape(str(scan.get("ts") or "never"))),
        ("API latency", f'{ph.get("latency_ms", "-")} ms'),
        ("Database health", '<span class="pos">ok</span>' if health.db_integrity_ok(db)
         else '<span class="neg">CORRUPT</span>'),
        ("Scheduler health",
         f'<span class="{"pos" if sched_ok else "neg"}">'
         f'{"ok" if sched_ok else f"gap {gap:.1f}m"}</span>'),
        ("Config version", html.escape(str(cfg.get_path("meta.config_version", "-")))),
        ("Strategy version", html.escape(str(cfg.get_path("meta.strategy_version", "-")))),
        ("Canary tests", f'{d["count_exec"]("CANARY")} · {html.escape(canary_status)}'),
    ])

    body = (
        alerts_html
        + row1 + row2 + row3
        + _card("Trades — every position, with its market and P&L", trades_body)
        + _card("P&L by market", market_body)
        + _card("Open positions", positions_body)
        + _card("Challenge · 45-hour test", challenge_body)
        + '<div class="grid2">'
        + _card("Signal funnel", funnel_body)
        + _card("Equity curve", _sparkline(d["curve"]))
        + "</div>"
        + '<div class="grid2">'
        + _card("Rejection analytics", rej_body)
        + _card("System health", system_body)
        + "</div>"
    )

    foot = (
        '<div class="foot">'
        '<button onclick="location.reload()">Refresh</button>'
        '<label><input type="checkbox" id="auto" onchange="toggleAuto()"> '
        'Auto-refresh (60s)</label>'
        f'<span class="muted">Last updated {html.escape(d["generated_at"])}</span>'
        '</div>'
    )

    title = html.escape(str(cfg.get_path("dashboard.title", "Paper Trading Terminal")))
    doc = (
        "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<title>{title}</title>\n<style>{CSS}</style>\n</head>\n<body>\n"
        + '<div class="wrap">' + body + "</div>" + foot
        + f"<script>{SCRIPT}</script>\n</body>\n</html>\n"
    )

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as fh:
        fh.write(doc)
    return output_path
