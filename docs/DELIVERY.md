# Final delivery report

Autonomous PAPER-ONLY trading agent v2. `REAL_TRADING` is permanently `false`.

## 1. Architecture and files

```
agent/
  config.py       single source of truth for every threshold (config/config.yaml)
  db.py           SQLite schema, migrations, pre-migration backups, DAO helpers
  models.py       Market / PricePoint / Decision dataclasses
  providers/      base.py (interface), polymarket.py (primary), mock.py (canary/tests)
  features.py     transparent feature engineering
  estimator.py    baseline probability estimator + confidence
  strategy.py     signal / edge / cost pipeline + rejection reason codes
  risk.py         hard risk limits (final gate before every fill)
  execution.py    simulated fills, fees, slippage, settlement
  portfolio.py    accounts, positions, equity, P&L, drawdown
  challenge.py    45h / 24h challenge state machine
  health.py       lifecycle states, heartbeat, fail-safe detection
  dashboard.py    self-contained dark mobile dashboard (no CDN)
  report.py       status + final report
  cli.py          command line entrypoint
  main.py         the orchestrator (one cycle)
tests/            59 unit + integration tests
config/config.yaml
.github/workflows/scan.yml (scheduler) + tests.yml (CI)
docs/             index.html (dashboard), report.md, PROVIDER.md, DELIVERY.md
data/state.db     persisted state (committed by the scheduler)
```

## 2. Selected market provider and limitations

**Polymarket public data API** — binary prediction markets (YES/NO).
Chosen because it is credential-free, exposes an implied probability plus a real
bid/ask/spread, and publishes resolution. Full evaluation and limitations:
`docs/PROVIDER.md`. Key limitations: no SLA; bounded history window; some list
fields are JSON-encoded strings; resolution flag can lag.

## 3. Starting bankroll verification

`MAIN` account is created exactly once with `starting_balance = $1000.00` and
`cash = $1000.00`. `test_starting_capital_is_exactly_1000` asserts this.
Re-initialising (a restart) does not reset the balance
(`test_no_automatic_bankroll_reset`).

## 4. First-trade deadline behaviour

At hour 24 the checkpoint verifies that either a real-market strategy/exploration
order was recorded or a canary completed. If neither, `deadline_alert = 1`
(`DEADLINE_MISSED`). Tests: `test_24h_deadline_alert_when_no_trade`,
`test_24h_satisfied_by_strategy_trade`, `test_24h_satisfied_by_completed_canary`.

## 5. Exploration policy and risk limits

Exploration unlocks only after hour 12, only if no strategy trade exists, and
only with eligible markets. It uses a documented relaxed edge/confidence bar but
every data-quality, liquidity, cost and risk gate still applies. Hard caps:
**$2 per order**, **3 orders** in the challenge, no repeat while a position is
open. Risk limits: position 1% of equity, total exposure 15%, daily loss 3%,
drawdown 10% -> LOCKED, max 10 open positions, tradeable price band 0.05–0.95.

## 6. Canary test result

Deterministic synthetic scenario run through the real pipeline
(validation -> estimate -> signal -> edge/cost -> risk -> execution -> position
-> settlement) on the **isolated** `CANARY` account:

```
status: PASSED   direction: YES   edge: 0.13855   confidence: 0.9142
fill_price: 0.41041  quantity: 24.39  fee: 0.02002  slippage: 0.01
settled: outcome YES  realized_pnl: 14.36008
```

The canary never touches the main bankroll
(`test_canary_never_touches_main_bankroll`). It is labelled TEST/CANARY and is
never used to claim that live-market trading works or that the strategy is
profitable.

## 7. Actual live-data scan result

Real Polymarket data, first dispatch run (2026-10-16T16:49:42Z):

```
discovered: 60   eligible: 60   candidates: 25   analyzed: 25
signals: 2   rejected: 23   risk_approved: 2   executed: 2
rejections: INSUFFICIENT_EDGE: 23
```

The two STRATEGY orders filled and are open. Marking to mid against an entry at
the ask produced an unrealized loss; investigating it surfaced a genuine strategy
flaw (mean-reversion "edges" on near-certain markets). A price-band rule was
added (strategy v2) and a second live scan confirmed the fix:

```
signals: 0   rejected: 25   executed: 0
rejections: INSUFFICIENT_EDGE: 40   MARKET_NEAR_RESOLVED: 8
```

## 8. Test count and results

**59 tests, all passing**, run three times locally and once in CI
(`.github/workflows/tests.yml`, conclusion: success). Coverage includes starting
capital, bankroll persistence, 24h/45h logic, strategy approval, exploration
limits, invalid-data holds, canary isolation, rejection logging, position
accounting, fees/slippage, resolution and P&L, risk limits, provider failure,
scheduler recovery, dashboard rendering, duplicate prevention and no-reset.

## 9. Dashboard URL

https://vikramnegi99.github.io/Vv/  (GitHub Pages build status: **built**)

## 10. Workflow scheduling

`.github/workflows/scan.yml` — `cron: "*/10 * * * *"` plus `workflow_dispatch`.
A `concurrency` group prevents overlapping runs. Each run scans, renders the
dashboard, writes `docs/report.md` and commits the state DB back to `main`, so
state survives completely separate executions. A missed/delayed run is detected
via the heartbeat gap and moves the agent to `DEGRADED`.

## 11. Remaining limitations

* The baseline estimator is a transparent heuristic, **not** a proven edge.
  No profitability claim is made without statistically meaningful evidence.
* Two positions opened by strategy v1 remain open until their markets resolve;
  they are left in place rather than silently discarded.
* Polymarket public data has no SLA; the agent degrades and runs a canary rather
  than fabricating prices.
* Settlement depends on the provider publishing a confirmed outcome.
* GitHub Actions cron is best-effort and may be delayed under load.

## 12. Starting and monitoring the 45-hour challenge

The challenge is already active (started 2026-10-16T16:49:34Z).

* It runs itself every ~10 minutes via the scheduled workflow — no action needed.
* Dashboard: https://vikramnegi99.github.io/Vv/
* Report: https://github.com/vikramnegi99/Vv/blob/main/docs/report.md
* Force a scan now: Actions -> paper-scan -> Run workflow.
* Inspect state: `python -m agent.cli status` / `python -m agent.cli report`.
* To stop: disable the `paper-scan` workflow in the Actions tab.
