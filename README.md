# IBKR week-trading bot — and an honest quant research log

A fully automated long-only trading bot for US equities. It runs on the
Interactive Brokers API (`ib_insync`) with yfinance market data, Telegram
alerts and a local web cockpit. On top of the bot sits a quantitative
research effort whose main output is a set of **negative results, proven
properly**.

> **Headline finding.** After ~40 signals were tested out of sample, the
> strategy's entries do **not** beat random entries with the same exits
> (placebo rank 62–68 %; 95 % would be needed). The backtested returns come
> mostly from holding volatile trending stocks in a bull market. The live
> paper test that will decide is **pre-registered** (see below).

*The detailed report and the protocol are written in French.*

## Current strategy ("breakout semaine")

- **Entries:** 1-hour breakout on completed bars, daily trend filter from the
  previous close, VIX < 22.
- **Exits:** stop −10 %, target +25 %, forced exit after 5 trading sessions.
- **Positions:** one at a time, sized in +25 % equity steps.
- **Universe:** 8 liquid, volatile US stocks, selected by a mechanical rule
  rather than by past P&L.
- **Guards:** PDT rule counter, drawdown circuit breaker, earnings blackout.

## Pre-registered live test

The verdict protocol was written and frozen **on 2026-09-30, before the first
paper trade**: [`docs/protocole-verdict-bot.md`](docs/protocole-verdict-bot.md).

- **Metric:** sum of net per-trade returns, independent of position size.
- **Benchmark:** 1000 random bots, each drawing exactly N entries over the
  same period, with the same tickers, exits and costs, and no entry filter.
- **Rank used:** the lower of the paper rank and the same-period backtest
  rank, which neutralises execution luck.
- **Decisions:**
  - stop if the rank is below 50 % at 30 trades ;
  - "edge demonstrated" only if it is at least 95 % at 60 trades ;
  - "inconclusive" is accepted in advance.
- **Frozen inputs:** SHA-256 hashes of the code and config are listed in the
  protocol. [`scripts/track_week_strategy.py`](scripts/track_week_strategy.py)
  applies it and logs every check to `scripts/track_log.jsonl`.

## What the research established

Full write-up: [`docs/reports/recherche-quant-2026-09-29.md`](docs/reports/recherche-quant-2026-09-29.md).

| question | answer (out of sample, recent 18 months as judge) |
|---|---|
| Do the original entry signals predict anything? | 82 % of them (the "structure" path) have zero excess return at every horizon. Only breakouts show a small intraday excess. |
| Does day trading survive costs at a $1,000 account? | No. Breakout at the close, 60-min ORB, overnight drift and intraday momentum are all ≤ 0 after the $0.35 minimum fee. |
| Do extra indicators help? | No. 15 pre-registered features, ridge and gradient boosting: in-sample ρ up to 0.36, out of sample ≈ 0.02. |
| Alternative data? | IBKR implied volatility, FINRA short volume, analyst actions, earnings surprises, BTC lead-lag: nothing robust. |
| Other universes or automatic ticker rotation? | No rule beats a fixed universe robustly. Picking tickers by past P&L went from +1586 $ in train to −146 $ in test. |
| Calendar anomalies (turn of month, pre-holiday), 30 years | Real historically, but no longer significant after publication. |

## Methodology highlights

- **Event-driven backtest with honest fills**: next-bar-open entries,
  gap-aware stops, real IBKR fee schedule, spread. Two shortcuts that
  inflated results by $2,674 were found and removed.
- **Robustness**: train/test splits, month-by-month walk-forward universe
  selection, 20 perturbations per variant (path dependence), parameter
  sensitivity.
- **Placebo tests matched on the number of trades**. A placebo that was
  itself biased was caught and fixed: a control window near the event
  captures the move that defines the signal.
- **Pre-registered hypotheses** with a declared direction, awareness of
  multiple testing, and a refusal to search until something passes by
  chance.
- **Production = backtest**: the live decision code reproduces the backtest
  on 100 % of 644 sampled bars, and a negative control shows the test can
  detect a mismatch.
- **Tests**:
  - [`scripts/test_week_strategy.py`](scripts/test_week_strategy.py): 78 checks ;
  - [`scripts/test_execution_safety.py`](scripts/test_execution_safety.py): 52 checks ;
  - both use stubs, with no broker connection.

## Repository layout

| path | content |
|---|---|
| `main.py` | the bot: scan loop, decision rules, risk guards, journal, Telegram (a single large file, historically grown) |
| `ibkr_execution.py`, `broker_hooks.py` | order execution, brackets, fills, reconciliation |
| `cockpit_web.py` | local live dashboard |
| `scripts/research_*.py` | the research: engine, strategies, placebo, features, sources, universes, calendar… |
| `scripts/*_week_strategy.py` | tests, parity check, live dry-run, verdict tracker |
| `examples/*.env.example` | configuration with every secret replaced by `CHANGE_ME` |
| `docs/` | report, verdict protocol, setup and deployment notes |

## Quick start

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
copy examples\base.env.example .env
copy examples\ibkr_paper_profile.env.example .env.ibkr_paper
# fill the CHANGE_ME values in .env

.\.venv\Scripts\python.exe scripts\test_week_strategy.py      # unit tests, no broker needed
.\.venv\Scripts\python.exe scripts\dryrun_week_strategy.py    # what would the bot decide right now (no orders)
.\.venv\Scripts\python.exe scripts\research_strategies.py     # strategy comparison (downloads data)
.\.venv\Scripts\python.exe main.py --env-file .env.ibkr_paper --no-portfolio-prompt   # run (IB Gateway required)
```

## How this was built

This project was developed with an AI pair-programmer (Claude, by Anthropic).

- **My part**: I set the goals and constraints (week-trading horizon,
  account size, risk tolerance) and made the trading decisions (strategy
  choice, position sizing, risk limits). I also had the verdict protocol
  reviewed in a separate session before freezing it.
- **Claude's part**: most of the code and the analyses, under my direction.

Every result in the report can be reproduced from `scripts/`.

## Limitations and disclaimer

- The backtests cover ~3 years of hourly data in a strong bull market. The
  8 tickers were chosen with hindsight, and yfinance data can be revised.
- No real-money track record yet. The paper verdict is pending.
- **This is a personal research project, not investment advice.** Trading
  involves the risk of losing money.
