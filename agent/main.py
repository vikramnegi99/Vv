"""Agent orchestrator: one scan cycle end to end.

    market data -> validation -> features -> estimate -> signal -> edge check
    -> cost check -> risk check -> paper execution -> position monitoring -> P&L

Also runs the deterministic canary when no real-market instrument is available.
"""
from __future__ import annotations

import json
import traceback
from typing import Optional

from . import health, portfolio
from .challenge import CANARY_ACCOUNT, MAIN_ACCOUNT, ChallengeManager
from .db import DB
from .execution import ExecutionEngine
from .models import Market
from .providers import build_provider
from .providers.mock import canary_scenario
from .risk import RiskEngine
from .strategy import evaluate_market
from .util import new_id, parse_iso, utcnow, utcnow_iso


def _first_failed_stage(stage_results: dict) -> str:
    for key, val in stage_results.items():
        if val is False:
            return key
    return "unknown"


class Agent:
    def __init__(self, cfg, db: DB, provider=None):
        self.cfg = cfg
        self.db = db
        self.provider = provider or build_provider(cfg)
        self.challenge = ChallengeManager(cfg, db)
        self.exec = ExecutionEngine(cfg, db)
        self.risk = RiskEngine(cfg)
        self.strategy_version = cfg.get_path("meta.strategy_version", "baseline")

    # ------------------------------------------------------------------ scan
    def run_cycle(self, mode: str = "auto") -> dict:
        summary: dict = {"mode": mode, "notes": [], "executed": [], "canary": None}
        self.challenge.ensure_initialized(self.provider.name)
        health.init_state(self.db)

        scan_id = new_id("scan_")
        funnel = dict(discovered=0, eligible=0, candidates=0, analyzed=0,
                      signals=0, rejected=0, risk_approved=0, executed=0)

        # -- provider health -------------------------------------------------
        ph = self.provider.health()
        self.db.execute(
            "INSERT INTO provider_health(ts, provider, ok, latency_ms, error, stale, eligible_markets)"
            " VALUES (?,?,?,?,?,?,?)",
            (utcnow_iso(), self.provider.name, int(ph.ok), ph.latency_ms, ph.error, 0,
             ph.eligible_markets),
        )
        health.bump(self.db, health._KEY_FAILS, reset=ph.ok)
        prev = health.get_state(self.db)
        gap_minutes = None
        if prev.get("last_cycle_utc"):
            try:
                gap_minutes = (utcnow() - parse_iso(prev["last_cycle_utc"])).total_seconds() / 60.0
            except (ValueError, TypeError):
                gap_minutes = None
        health.record_heartbeat(self.db, success=ph.ok, api_latency_ms=ph.latency_ms,
                                gap_minutes=gap_minutes)
        summary["provider_ok"] = ph.ok

        markets: list[Market] = []
        error: Optional[str] = None
        if ph.ok and mode != "canary":
            try:
                limit = int(self.cfg.get_path(f"provider.{self.provider.name}.max_markets", 60))
                markets = self.provider.discover_markets(limit)
            except Exception as exc:  # noqa: BLE001
                error = f"discover failed: {type(exc).__name__}: {exc}"
                markets = []

        # -- strategy scan ---------------------------------------------------
        signals = []
        quotes: dict = {}
        if markets:
            funnel["discovered"] = len(markets)
            eligible = [m for m in markets if self._eligible(m)]
            funnel["eligible"] = len(eligible)
            max_analyze = int(self.cfg.get_path(f"provider.{self.provider.name}.max_analyze", 25))
            candidates = sorted(eligible, key=lambda m: (m.liquidity or 0.0), reverse=True)[:max_analyze]
            funnel["candidates"] = len(candidates)

            for m in candidates:
                quotes[m.id] = m.yes_price
                self._snapshot(scan_id, m)
                try:
                    hist = self.provider.get_price_history(m.id)
                except Exception:  # noqa: BLE001
                    hist = []
                decision = evaluate_market(m, hist, self.cfg, "STRATEGY")
                self._record_decision(scan_id, decision)
                funnel["analyzed"] += 1
                if decision.decision == "TRADE":
                    signals.append((m, decision))
                else:
                    funnel["rejected"] += 1
                    self.db.execute(
                        "INSERT INTO rejections(scan_id, ts, market_id, stage, reason) VALUES (?,?,?,?,?)",
                        (scan_id, utcnow_iso(), m.id,
                         _first_failed_stage(decision.stage_results), decision.reason),
                    )

        funnel["signals"] = len(signals)
        signals.sort(key=lambda t: t[1].edge or 0.0, reverse=True)
        cap = int(self.cfg.get_path("strategy.max_trades_per_scan", 3))

        # refresh quotes for open positions not seen this scan (accurate equity)
        self._refresh_open_quotes(quotes)

        for m, decision in signals[:cap]:
            res = self.exec.execute(MAIN_ACCOUNT, m, decision.direction, "STRATEGY",
                                    self.strategy_version, scan_id, decision.reason, quotes)
            if res.get("ok"):
                funnel["risk_approved"] += 1
                funnel["executed"] += 1
                summary["executed"].append(res)
                self.challenge.mark_first_trade("STRATEGY")
            else:
                self.db.execute(
                    "INSERT INTO rejections(scan_id, ts, market_id, stage, reason) VALUES (?,?,?,?,?)",
                    (scan_id, utcnow_iso(), m.id, "risk", res.get("reason", "REJECTED")),
                )

        # -- exploration (bounded) ------------------------------------------
        if funnel["executed"] == 0 and self.challenge.exploration_allowed(funnel["eligible"]):
            self._try_exploration(scan_id, quotes, funnel, summary)

        # -- canary fallback -------------------------------------------------
        if mode == "canary" or (not ph.ok) or (funnel["eligible"] == 0):
            summary["canary"] = self.run_canary(reason=error or (
                "provider unavailable" if not ph.ok else "no eligible real-market instrument"))

        # -- position maintenance -------------------------------------------
        self._refresh_open_quotes(quotes)
        self.exec.mark_to_market(MAIN_ACCOUNT, quotes)
        settled = self._settle_resolved(quotes)
        if settled:
            summary["settled"] = settled
        snapshot = portfolio.record_snapshot(self.db, MAIN_ACCOUNT, quotes)
        summary["equity"] = snapshot["equity"]

        # -- checkpoints & health -------------------------------------------
        summary["notes"].extend(self.challenge.run_checkpoints(funnel))
        self._update_health(ph, quotes, snapshot)

        self.db.execute(
            "INSERT OR REPLACE INTO scans(id, ts, provider, status, discovered, eligible, candidates,"
            " analyzed, signals, rejected, risk_approved, executed, latency_ms, error)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (scan_id, utcnow_iso(), self.provider.name,
             "CANARY" if summary.get("canary") else ("OK" if not error else "DEGRADED"),
             funnel["discovered"], funnel["eligible"], funnel["candidates"], funnel["analyzed"],
             funnel["signals"], funnel["rejected"], funnel["risk_approved"], funnel["executed"],
             ph.latency_ms, error),
        )
        summary["funnel"] = funnel
        summary["scan_id"] = scan_id
        return summary

    # ------------------------------------------------------------- helpers
    def _eligible(self, m: Market) -> bool:
        return bool(m.active) and not m.closed and m.yes_price is not None and 0.0 < m.yes_price < 1.0

    def _snapshot(self, scan_id: str, m: Market) -> None:
        self.db.execute(
            "INSERT INTO market_snapshots(scan_id, ts, market_id, question, price, bid, ask, spread,"
            " liquidity, volume, end_date, payload) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (scan_id, utcnow_iso(), m.id, m.question, m.yes_price, m.bid, m.ask, m.spread,
             m.liquidity, m.volume, m.end_date, json.dumps(m.raw)),
        )

    def _record_decision(self, scan_id: str, d) -> None:
        self.db.execute(
            "INSERT INTO decisions(scan_id, ts, market_id, question, market_prob, est_prob, edge,"
            " confidence, decision, direction, features, data_ts, reason, risk_flags, stage_results,"
            " exec_type) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (scan_id, utcnow_iso(), d.market_id, d.question, d.market_prob, d.est_prob, d.edge,
             d.confidence, d.decision, d.direction, json.dumps(d.features), d.data_ts, d.reason,
             json.dumps(d.risk_flags), json.dumps(d.stage_results), d.exec_type),
        )

    def _refresh_open_quotes(self, quotes: dict) -> None:
        for pos in portfolio.open_positions(self.db, MAIN_ACCOUNT):
            if pos["market_id"] in quotes:
                continue
            try:
                q = self.provider.get_latest_quote(pos["market_id"])
                if q is not None and q.yes_price is not None:
                    quotes[pos["market_id"]] = q.yes_price
            except Exception:  # noqa: BLE001
                continue

    def _try_exploration(self, scan_id, quotes, funnel, summary) -> None:
        open_ids = {p["market_id"] for p in portfolio.open_positions(self.db, MAIN_ACCOUNT)}
        try:
            limit = int(self.cfg.get_path(f"provider.{self.provider.name}.max_markets", 60))
            markets = self.provider.discover_markets(limit)
        except Exception:  # noqa: BLE001
            return
        eligible = [m for m in markets if self._eligible(m) and m.id not in open_ids]
        eligible.sort(key=lambda m: (m.liquidity or 0.0), reverse=True)
        for m in eligible[:10]:
            try:
                hist = self.provider.get_price_history(m.id)
            except Exception:  # noqa: BLE001
                hist = []
            decision = evaluate_market(m, hist, self.cfg, "EXPLORATION")
            self._record_decision(scan_id, decision)
            if decision.decision != "TRADE":
                continue
            quotes[m.id] = m.yes_price
            res = self.exec.execute(MAIN_ACCOUNT, m, decision.direction, "EXPLORATION",
                                    self.strategy_version, scan_id,
                                    "exploration: " + decision.reason, quotes)
            if res.get("ok"):
                funnel["risk_approved"] += 1
                funnel["executed"] += 1
                summary["executed"].append(res)
                self.challenge.bump_exploration_orders()
                self.challenge.mark_first_trade("EXPLORATION")
                summary["notes"].append(f"EXPLORATION order placed on {m.id}")
            break

    def _settle_resolved(self, quotes: dict) -> list:
        resolved: dict = {}
        for pos in portfolio.open_positions(self.db, MAIN_ACCOUNT):
            mid = pos["market_id"]
            try:
                q = self.provider.get_latest_quote(mid)
            except Exception:  # noqa: BLE001
                continue
            if q is not None and q.resolved and q.resolved_outcome in ("YES", "NO"):
                resolved[mid] = q.resolved_outcome
                quotes[mid] = 1.0 if q.resolved_outcome == "YES" else 0.0
        return self.exec.settle(MAIN_ACCOUNT, resolved) if resolved else []

    def _update_health(self, ph, quotes, snapshot) -> None:
        eq = snapshot["equity"]
        bankrupt = eq <= 0.01
        corruption = not health.db_integrity_ok(self.db)
        risk_locked = self.risk.is_locked(self.db, MAIN_ACCOUNT)
        inactivity = health.get_counter(self.db, health._KEY_INACTIVE)
        stale = False
        for pos in portfolio.open_positions(self.db, MAIN_ACCOUNT):
            if pos["current_price"] is None:
                stale = True
        state, reason = health.detect_state(
            self.db, self.cfg, provider_ok=ph.ok, stale=stale, risk_locked=risk_locked,
            inactivity_scans=inactivity, bankrupt=bankrupt, corruption=corruption)
        health.transition(self.db, state, reason)

    # ------------------------------------------------------------- canary
    def run_canary(self, reason: str = "manual") -> dict:
        """Deterministic synthetic pipeline test on the ISOLATED canary account.

        Exercises: validation -> features -> estimate -> signal -> edge/cost
        checks -> risk approval -> simulated execution -> position -> settlement.
        It NEVER touches the main bankroll.
        """
        market, hist = canary_scenario(self.cfg)
        decision = evaluate_market(market, hist, self.cfg, "STRATEGY")
        quotes = {market.id: market.yes_price}
        canary_prices = {market.id: market.yes_price}

        if decision.decision != "TRADE":
            result = {"status": "NO_SIGNAL", "reason": decision.reason,
                      "stage_results": decision.stage_results}
            self.challenge.record_canary(json.dumps(result))
            return result

        # Size against the CANARY account only.
        eq = portfolio.equity(self.db, CANARY_ACCOUNT, canary_prices)
        qty = self.exec.size(eq, self.exec.entry_price(market, decision.direction), "CANARY")
        res = self.exec.execute(CANARY_ACCOUNT, market, decision.direction, "CANARY",
                                self.strategy_version, "canary", "canary pipeline check",
                                canary_prices, quantity=qty)
        if not res.get("ok"):
            result = {"status": "EXEC_FAILED", "reason": res.get("reason")}
            self.challenge.record_canary(json.dumps(result))
            return result

        # Confirm settlement through the same settlement path.
        settled = self.exec.settle(CANARY_ACCOUNT, {market.id: "YES"})
        result = {
            "status": "PASSED",
            "reason": reason,
            "market_id": market.id,
            "direction": decision.direction,
            "edge": decision.edge,
            "confidence": decision.confidence,
            "fill_price": res["fill_price"],
            "quantity": res["quantity"],
            "fee": res["fee"],
            "slippage": res["slippage"],
            "settled": settled,
            "labelled": "TEST/CANARY (synthetic, isolated account)",
        }
        self.challenge.record_canary(json.dumps(result))
        return result
