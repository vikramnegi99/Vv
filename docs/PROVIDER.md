# Market-data provider evaluation

The agent defines a modular `MarketProvider` interface
(`agent/providers/base.py`) and selects **one primary provider for one clearly
defined instrument class**.

## Chosen primary provider

**Polymarket public data API** — instrument class: *binary prediction markets
(YES/NO)*.

* Discovery / metadata: `https://gamma-api.polymarket.com/markets`
* Price history: `https://clob.polymarket.com/prices-history`

### Why it was chosen

1. **No credentials.** Both endpoints are public and read-only. No API key, no
   account, no wallet. This matters because the brief forbids storing any live
   trading credential.
2. **The data shape matches the strategy.** Each market carries a machine
   readable implied probability (`outcomePrices`), a real order book
   (`bestBid` / `bestAsk` / `spread`), `liquidity` and `volume`. A
   probability-edge strategy needs exactly these fields; equity or crypto
   feeds do not provide a probability to trade against.
3. **Provider-published resolution.** Markets carry a `closed` flag and final
   `outcomePrices`, so a position can be settled on a *confirmed* outcome
   instead of a transient price move.
4. **Volume ordering.** `order=volume24hr` gives a clean liquidity ranking for
   candidate selection.

### Documented limitations

* **No SLA.** The public API can be slow or return errors. The agent treats any
  failure as a provider outage: it degrades, preserves existing state, and
  never fabricates a price.
* **History depth.** `prices-history` returns a bounded window (we request a
  1-day window at 60-minute fidelity). Long-horizon history is not available
  for free.
* **Field typing.** Some list fields (`outcomes`, `outcomePrices`,
  `clobTokenIds`) are JSON-encoded strings rather than arrays; the parser
  handles both shapes defensively.
* **Resolution timing.** `closed` is set by the provider; there can be a lag
  between real-world resolution and the flag flipping. The agent only settles
  once the provider confirms.
* **Geographic / account restrictions** are irrelevant here because the agent
  never trades — it only reads public data.

## Alternatives considered

| Provider | Class | Why not primary |
|----------|-------|-----------------|
| Kalshi | Prediction markets | Requires an account/API key even for much of the data; not credential-free. |
| Manifold | Prediction markets | Play-money markets; prices are less meaningful as a real probability signal. |
| Coinbase / Kraken / Binance | Crypto spot | Excellent public APIs, but a spot price is not a probability, so the probability-edge strategy does not map onto it without a separate instrument-class adapter. |
| US equities (Stooq/Finnhub) | Equities | Same probability-mapping problem, plus rate limits on the free tiers. |

The interface makes swapping providers a one-line config change
(`provider.primary`), and adding a new instrument class means writing one
adapter against `MarketProvider`.

## Secondary provider: `mock`

`agent/providers/mock.py` is a **deterministic synthetic** provider used only
for the engine canary and the test suite. Everything it returns is labelled
`provider='mock'` and any fill from it is tagged `CANARY`. It is never
presented as market data.
