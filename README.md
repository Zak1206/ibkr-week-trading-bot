# IBKR week-trading bot

A Python bot that trades US stocks on a weekly horizon through the Interactive
Brokers API. I built it to answer one question: does the entry signal actually
beat chance, or does it just look good in a backtest?

The detailed report and the verdict protocol are in French, in `docs/`.

## The strategy

Buy a stock when it breaks out on hourly bars, if its daily trend is up and the
VIX is below 22. Sell at -10 %, +25 % or after 5 trading days, whichever comes
first. One position at a time, on 8 liquid, volatile US stocks.

## Backtest results

Oct 2023 to Sep 2026, $1,000 account, real IBKR fees.

| | annual return | max drawdown |
|---|---|---|
| This bot | +47 % | -29 % |
| Random entries, same exits | +40 % | -34 % |
| SPY | +25 % | -19 % |

The bot beats the index. But a bot that enters at random with the same exits
does almost as well, so most of the gain comes from holding volatile stocks in
a strong bull market, and those 8 stocks were picked with hindsight. In
backtest, my entries beat 62 to 68 % of random bots. A real edge would need 95 %.

## The live test

On 30 Sep 2026, before the first paper trade, I wrote down how the bot would be
judged and froze the code with SHA-256 hashes:
[`docs/protocole-verdict-bot.md`](docs/protocole-verdict-bot.md).

The bot is ranked against 1,000 random bots trading the same stocks over the
same period, with the same exits and costs.

- At 30 trades: if it ranks in the bottom half, I stop.
- At 60 trades: top 5 % means a real edge. Anything in between is
  inconclusive, and I accepted that outcome in advance.

## What didn't work

I tested about 40 ideas before keeping this one. Full write-up:
[`docs/reports/recherche-quant-2026-09-29.md`](docs/reports/recherche-quant-2026-09-29.md).

- Day trading with $1,000: the $0.35 minimum fee per order kills every
  intraday strategy I tried. That's why the bot holds for days.
- Machine learning: good in sample, nothing out of sample. Too many features
  for too few trades.
- Alternative data (implied volatility, short volume, analyst ratings):
  nothing that held up.
- Picking stocks by past performance: it worked in training and lost money in
  testing.

## How I tested it

- Realistic fills: entry at the next bar's open, stops that account for
  overnight gaps, real fees and spread. An earlier version had two shortcuts
  that inflated the results; I found and removed them.
- Walk-forward: the stock list is rebuilt each month from past data only.
- Placebo tests: the same number of trades at random times on the same stocks,
  to check whether the signal does better than noise.
- Parity check: the live code makes the same decisions as the backtest on
  644 out of 644 sampled bars.

## Repository

- `main.py`: the bot (scan loop, rules, risk guards, Telegram alerts)
- `ibkr_execution.py`, `broker_hooks.py`: order execution
- `cockpit_web.py`: local dashboard
- `scripts/research_*.py`: the research
- `scripts/*_week_strategy.py`: tests, parity check, dry run, verdict tracker
- `examples/*.env.example`: config, every secret replaced by `CHANGE_ME`
- `docs/`: report, protocol, setup notes

## Quick start

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
copy examples\base.env.example .env
copy examples\ibkr_paper_profile.env.example .env.ibkr_paper
# fill the CHANGE_ME values in .env

.\.venv\Scripts\python.exe scripts\test_week_strategy.py      # unit tests, no broker needed
.\.venv\Scripts\python.exe scripts\dryrun_week_strategy.py    # what the bot would do right now (no orders)
.\.venv\Scripts\python.exe main.py --env-file .env.ibkr_paper --no-portfolio-prompt   # run (IB Gateway required)
```

## Notes

Code written with an AI coding assistant under my direction. The goals, the
trading decisions and the verdict protocol are mine.

About 3 years of data, all in a bull market. No real money yet. This is a
personal research project, not investment advice.
