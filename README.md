# Vantage

A locally hosted, quantitative trading workstation for **Alpaca**:

- **Options scanner** for long calls and puts, ranked by *physical-measure expected value* at your
  planned exit horizon (net of spread and theta), probability of profit and Kelly sizing.
- **Signal engine** fusing 17 models — momentum, Kalman trend, short-term reversal, breakouts,
  relative strength, VWAP reversion, opening-range breakout, options order flow (sweeps,
  aggressor side), stock order-flow imbalance, news sentiment (LLM or lexicon), post-event drift and
  the volatility risk premium, plus the **Intel** models below — into an IC-weighted ensemble forecast.
- **Intel** (Intel page): VIX regime and term structure, statements by market movers (Trump via a Truth Social
  archive feed, the Fed, White House, SEC, plus configurable people), Reddit / Stocktwits / X sentiment and
  attention, and Polymarket / Kalshi prediction markets → five signal models, a market-regime card, a top-bar
  VIX chip and the optional **Sentiment & Macro Catalyst** strategy (needs ≥ 2 agreeing sources + price
  confirmation). Only public/official APIs are used; X and Reddit's own API need keys (see `.env.example`).
- **Auto-trader** with nine preset strategies, account-size tiers ($500 → $25k+), fractional
  Kelly sizing, hard risk limits, daily-loss and drawdown kill switches, smart limit
  order execution, and a full decision log explaining every action.
- **Local LLM gate** (Ollama / Qwen or any OpenAI-compatible server): reviews trade dossiers and can
  veto or shrink trades — never enlarge them — and turns news into structured signals.
- **Browser UI** with Robinhood-style portfolio chart (1D baseline, scrubbing, ranges, zoom),
  stock/ETF and option charts, live feeds (news, flow, signals, decisions), calendar (earnings,
  FOMC, OPEX, holidays), backtester with Probabilistic/Deflated Sharpe, and in-app Help.
- **SIM / PAPER / LIVE** modes. SIM runs a synthetic market with no keys at all.

> Not financial advice. Options can lose 100 % of the premium. Test in SIM and PAPER first.

## Quick start (Windows)

Prerequisites: [uv](https://docs.astral.sh/uv/), Node 20+, optionally [Ollama](https://ollama.com)
with a Qwen model (`ollama pull qwen3.6:35b-a3b`).

```powershell
copy .env.example .env          # add Alpaca paper keys to trade paper; leave blank for SIM
cd backend; uv sync; cd ..
cd frontend; npm install; cd ..
./scripts/dev.ps1               # backend :8765 + UI http://localhost:5173
```

Single-process mode: `cd frontend; npm run build`, then `cd ../backend; uv run vantage` and open
http://127.0.0.1:8765.

## Going live
1. Put live keys in `.env` **and** set `ALLOW_LIVE_TRADING=true`, restart.
2. Settings → Mode → LIVE, type `LIVE` to confirm.
3. The auto-trader always starts **disarmed**; arming in live mode needs a second `LIVE` confirmation.

## Docs
- [CLAUDE.md](CLAUDE.md) — working agreement for AI agents / contributors
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — system design
- [docs/API.md](docs/API.md) — REST + WebSocket contract
- [docs/QUANT.md](docs/QUANT.md) — the mathematics behind signals, scanner, sizing and strategies

## Tests
```bash
cd backend && uv run pytest -q
cd frontend && npm run build
```
