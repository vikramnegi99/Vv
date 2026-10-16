# Autonomous PAPER-ONLY Trading Agent (v2)

An autonomous, **paper-only** trading agent that scans prediction markets,
estimates probabilities, simulates orders, manages positions, tracks
performance and publishes a mobile-friendly research dashboard.

> **REAL_TRADING is permanently disabled.** There is no code path that reaches a
> live order endpoint, a broker, a wallet, or a real credential. Orders are
> simulated against public market data.

---

## What it does

Every scan cycle runs this pipeline:

```
market data -> validation -> features -> probability estimate -> signal
-> edge check -> transaction-cost check -> risk check -> paper execution
-> position monitoring -> P&L -> dashboard
```

One cycle runs roughly every 10 minutes via GitHub Actions. State is committed
back to the repository so the $1,000 virtual bankroll and the full decision
history survive across completely separate scheduler executions.

## The 45-hour challenge

| Hour | Behaviour |
|------|-----------|
| 0    | Initialise the $1,000 bankroll once, stamp versions, verify DB + engine |
| 12   | If no strategy trade yet, enable the **bounded exploration** policy |
| 24   | Checkpoint. If no real-market order and no canary, raise `DEADLINE_MISSED` |
| 45   | Final report. The bankroll is **never** reset |

Three execution categories are kept strictly separate in accounting and labels:

* **STRATEGY** – a trade from the normal signal pipeline after every gate passes.
* **EXPLORATION** – a small, bounded probe (max **$2**/order, max **3** in the
  challenge) used only when no strategy trade has occurred by hour 12.
* **CANARY** – a deterministic synthetic pipeline test on an **isolated** test
  account. It never touches the main bankroll, P&L or win rate.

## Architecture

```
agent/
  config.py        single source of truth for every threshold (config/config.yaml)
  db.py            SQLite schema, migrations, pre-migration backups, DAO helpers
  models.py        Market / PricePoint / Decision dataclasses
  providers/       read-only market-data providers
    base.py        the MarketProvider interface
    polymarket.py  primary provider (public Polymarket data API)
    mock.py        deterministic synthetic provider (canary + tests)
  features.py      transparent feature engineering
  estimator.py     baseline probability estimator + confidence
  strategy.py      signal / edge / cost pipeline + rejection reason codes
  risk.py          hard risk limits (the final gate before every fill)
  execution.py     simulated fills, fees, slippage, settlement
  portfolio.py     accounts, positions, equity, P&L, drawdown
  challenge.py     45h / 24h challenge state machine
  health.py        lifecycle states, heartbeat, fail-safe detection
  dashboard.py     self-contained dark mobile dashboard (no CDN)
  report.py        status + final report
  cli.py           command line entrypoint
  main.py          the orchestrator (one cycle)
tests/             57 unit + integration tests
config/config.yaml all thresholds
.github/workflows/ scan.yml (scheduler) + tests.yml (CI)
docs/              generated dashboard (index.html) + report.md
data/state.db      persisted state (committed)
```

## Risk limits (conservative paper defaults)

| Limit | Value |
|-------|-------|
| Max position | 1% of current equity |
| Max total open exposure | 15% of current equity |
| Max daily loss | 3% of the day's starting equity |
| Max drawdown | 10% -> orders **LOCKED** pending review |
| Exploration stake | $2 per order |
| Exploration orders | 3 across the challenge |
| Max open positions | 10 |
| Tradeable price band | 0.05 – 0.95 implied probability |

Near-certain markets (implied probability outside 0.05–0.95) are treated as
*effectively resolved* and are not traded: the provider's `closed` flag lags,
mean-reversion at the extremes is spurious, and the bid/ask spread on the
resulting huge contract count is punitive. This rule was added after the first
live scan (see `docs/report.md`) surfaced exactly that failure mode.

The agent never raises these automatically, and never forces a trade to hit a
deadline or a trade count.

## Lifecycle states

`RUNNING` -> `DEGRADED` -> `REVIEW_REQUIRED` -> `LOCKED` -> `DEAD`

`DEAD` is reserved for fatal conditions (bankroll bankruptcy, unrecoverable
state corruption). Strategy inactivity raises `REVIEW_REQUIRED`, never `DEAD`.

## Running it locally

```bash
pip install -r requirements.txt

# run one cycle against the real public provider
python -m agent.cli scan --mode auto

# deterministic canary end-to-end
python -m agent.cli canary

# re-render the dashboard
python -m agent.cli dashboard

# machine-readable status / human report
python -m agent.cli status
python -m agent.cli report

# tests
python -m pytest -q
```

## Dashboard

`docs/index.html` is regenerated on every scan and served via GitHub Pages.
It is a single self-contained file: inline CSS, a small amount of vanilla JS,
inline SVG charts, **no external CDN dependencies**, and no horizontal overflow
on a phone. Every figure comes from the stored database.

## Safety

* `safety.real_trading` must be `false`; `load_config()` refuses to start otherwise.
* No private keys, seed phrases, exchange or withdrawal credentials are ever
  requested, stored or used.
* No live exchange or prediction-market order endpoint is ever called.
* Rejected or invalid orders are never recorded as executed trades.
* The canary account is isolated from the main bankroll.

## Limitations

* The baseline estimator is a transparent heuristic, **not** a proven edge.
  No profitability claim is made without statistically meaningful evidence.
* Polymarket public data has no SLA; when it is unavailable the agent degrades
  and runs a canary rather than fabricating prices.
* Settlement depends on the provider publishing a confirmed outcome.
* GitHub Actions cron is best-effort and may be delayed under load.
