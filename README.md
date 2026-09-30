# IBKR week-trading bot

A fully automated week-trading bot for US equities, running on the
Interactive Brokers API (`ib_insync`) with yfinance data, Telegram alerts and
a live web cockpit. It was built around one question: **is the edge real, or
is it luck?** That question drove every design choice.

*The detailed report and the protocol are written in French.*

## Results so far (backtest)

Same window (2023-10-31 → 2026-09-28), $1,000 account, real IBKR fees and
spread, median of 12 randomised runs:

| | annual return (CAGR) | Sharpe | max drawdown | last 18 months: gain / max drawdown |
|---|---|---|---|---|
| **This bot** (fixed $1,000 position) | **+47 %** | **1.58** | −29 % | **+879 $ / −18 %** |
| Random-entry bot, same exits | +40 % | 1.05 | −34 % | +659 $ / −36 % |
| SPY, buy & hold | +25 % | 1.52 | −19 % | +387 $ / −12 % |
| QQQ, buy & hold | +30 % | 1.38 | −23 % | +569 $ / −13 % |

**Read honestly:**
- The bot beats both indices and a random-entry bot with the same exits. It
  has the best risk-adjusted return of the group, and over the last 18 months
  it had **half the drawdown** of the random bot.
- **The open question** is how much of that comes from the entry signal
  itself, and how much from holding volatile trending stocks in a strong
  market. In backtest, the entries beat 62–68 % of random bots. That is
  promising, but below the 95 % a real edge would require.
- This is exactly what the live test below will settle, under rules fixed in
  advance.

The paper account sizes positions in +25 % equity steps. Backtest of that
variant: +76 %/yr, with a max drawdown of −48 %.

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

## What the research ruled out

Most trading ideas do not survive honest testing. Each idea below was
tested out of sample, with the most recent 18 months as the judge, and
rejected. That is what kept the strategy simple. Full write-up:
[`docs/reports/recherche-quant-2026-09-29.md`](docs/reports/recherche-quant-2026-09-29.md).

| idea tested | result |
|---|---|
| the original multi-indicator entry score | 82 % of its signals (the "structure" path) had zero excess return at every horizon. Only breakouts were kept. |
| day trading with a $1,000 account | Breakout sold at the close, 60-min ORB, overnight drift, intraday momentum: all ≤ 0 after the $0.35 minimum fee. Hence week trading. |
| more indicators, machine learning | 15 pre-registered features, ridge and gradient boosting: in-sample ρ up to 0.36, out of sample ≈ 0.02. Classic overfitting, rejected. |
| alternative data | IBKR implied volatility, FINRA short volume, analyst actions, earnings surprises, BTC lead-lag: nothing robust. |
| other universes, automatic ticker rotation | No rule beat a fixed universe robustly. Picking tickers by past P&L went from +1586 $ in train to −146 $ in test. |
| calendar anomalies (turn of month, pre-holiday), 30 years | Real historically, but no longer significant after publication. |

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
