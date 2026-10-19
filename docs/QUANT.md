# Vantage — Quantitative methodology

This document is the specification for `backend/vantage/quant/`, `signals/`, `scanner/`,
`strategies/` and `execution/risk.py`. Code must match it; if you change the maths, change this
file in the same commit.

Design principles
1. **Every trade is an expected-value bet with an explicit distribution.** A strategy states a
   forecast `(μ, σ)` for the underlying over a horizon `h`; the scanner turns that into a
   distribution of option values at `h` and sizes with Kelly on that distribution.
2. **Signals are calibrated, not voted.** Each signal is standardised; weights come from its
   measured information coefficient (IC), shrunk toward a prior. `E[r] = IC · σ · z` (Grinold).
3. **Costs are first-class.** Spreads, slippage and theta are in every EV number.
4. **Overfitting is measured.** Backtests report the Probabilistic and Deflated Sharpe Ratios.
5. **Risk is bounded independently of the model** (hard caps, kill switch) because models fail.

Notation: `S` spot, `K` strike, `T` years to expiry (calendar days / 365), `r` risk-free rate
(default 0.04, settable), `q` dividend yield (default 0), `σ` volatility (annualised),
`N` standard normal CDF, `n` its PDF. Trading year = 252 days.

---

## 1. Option pricing — `quant/bs.py`

All functions are **vectorised** (numpy broadcasting over any argument) and return `np.ndarray`
(or float for scalar input — use `np.asarray` internally, return same shape).

- `bs_price(S, K, T, r, q, sigma, is_call)` — Black-Scholes-Merton.
  `d1 = [ln(S/K) + (r − q + σ²/2)T] / (σ√T)`, `d2 = d1 − σ√T`;
  call `= S e^{−qT} N(d1) − K e^{−rT} N(d2)`; put by parity. For `T ≤ 0` or `σ ≤ 0` return
  intrinsic (discounted forward intrinsic when σ=0).
- `bs_greeks(S, K, T, r, q, sigma, is_call) -> dict[str, ndarray]` with keys
  `delta, gamma, vega` (per 1.00 change in σ, i.e. ×0.01 for one vol point is the caller's job —
  **store vega per 1 vol point = raw/100**), `theta` (**per calendar day** = raw/365), `rho` (per 1 %).
- `implied_vol(price, S, K, T, r, q, is_call, tol=1e-7, max_iter=60) -> ndarray`
  Robust solver: clamp price into no-arbitrage bounds (`intrinsic ≤ price ≤ S e^{−qT}` for calls);
  initial guess Brenner-Subrahmanyam / Corrado-Miller; Newton on vega with bisection fallback
  in `[1e-4, 5.0]`. Returns `nan` where the price is outside bounds.
- `bjerksund_stensland(S, K, T, r, q, sigma, is_call)` — 2002 American approximation (used for
  puts and dividend-paying calls; for `q = 0` calls equal BSM).
- `expected_move(S, iv, T)` — `S · iv · √T` (1σ move).

## 2. Volatility — `quant/vol.py`

Range-based estimators (inputs are equal-length arrays of daily OHLC; return annualised vol of the
trailing `window`, as a float using the last `window` observations):
- `close_to_close(c, window)`, `parkinson(h, l, window)`, `garman_klass(o, h, l, c, window)`,
  `rogers_satchell(o, h, l, c, window)`, `yang_zhang(o, h, l, c, window)` (Yang-Zhang 2000,
  `k = 0.34 / (1.34 + (n+1)/(n−1))`; the default estimator — efficient and drift/gap robust).
- `ewma_vol(returns, lam=0.94)` — RiskMetrics, annualised.
- `har_rv_fit(rv) -> (b0, bd, bw, bm)` and `har_rv_forecast(rv, horizon_days) -> float`
  Corsi (2009) HAR-RV on daily realised variance `rv` (array of daily variances):
  `RV_{t+1} = b0 + bd RV_t + bw RV_t^{(5)} + bm RV_t^{(22)}`, OLS over the trailing sample
  (≥ 60 obs, else fall back to EWMA). Multi-step forecast by iterating the recursion and averaging;
  returns **annualised vol** over the horizon. Clamp to `[0.5×, 2×]` of the sample vol for stability.
- `daily_rv_from_ohlc(o, h, l, c)` — per-day variance proxy (Garman-Klass per bar) when intraday
  data are not available; `daily_rv_from_intraday(closes_by_day)` — sum of squared intraday log
  returns.
- `garch11_fit(returns) -> (omega, alpha, beta)` (Gaussian MLE, scipy `minimize`, constraints
  `α+β<1`) and `garch11_forecast(params, returns, horizon_days) -> annualised vol`.
- `iv_rank(current, history)` = `(cur − min)/(max − min)`; `iv_percentile(current, history)` =
  share of history below current. Need ≥ 20 observations, else `None`.

## 3. Statistics — `quant/stats.py`
- `zscore(x, window)` (last value), `rolling_zscore(x, window)` (array).
- `hurst_exponent(x, max_lag=50)` — variance-of-lagged-differences method.
- `ou_half_life(x)` — AR(1) on levels: `Δx = a + b x_{t−1}`, half-life `= −ln 2 / b` (inf if `b ≥ 0`).
- `sharpe(returns, periods=252)`, `sortino(returns, periods=252)`, `max_drawdown(equity) -> float`
  (positive fraction), `cagr(equity, periods_per_year)`, `calmar`.
- `probabilistic_sharpe_ratio(sr, n, skew, kurt, sr_benchmark=0.0)` — Bailey & López de Prado (2012),
  with **per-period** SR: `PSR = N( (SR − SR*) √(n−1) / √(1 − γ3 SR + (γ4−1)/4 SR²) )`
  (`kurt` is *non-excess* kurtosis, normal = 3).
- `deflated_sharpe_ratio(sr, n, skew, kurt, n_trials, sr_var)` — PSR with
  `SR* = √V · ((1−γ) Z⁻¹(1 − 1/N) + γ Z⁻¹(1 − 1/(N e)))`, `γ` = Euler-Mascheroni.
- `spearman_ic(scores, fwd_returns)` — rank correlation, `nan` if < 5 pairs.

## 4. Kelly sizing — `quant/kelly.py`
- `kelly_binary(p, win, loss)` — `f* = p/loss − (1−p)/win` for win/loss as positive fractions of stake.
- `kelly_discrete(returns, probs, max_f=1.0)` — maximise `Σ p_j log(1 + f R_j)` over
  `f ∈ [0, min(max_f, 1/max(−R_j))·0.999]` with `scipy.optimize.minimize_scalar(bounded)`.
  Return 0 if `Σ p_j R_j ≤ 0`. `R_j ≥ −1` (a long option loses at most its premium).
- `kelly_gaussian(mu, sigma)` — `μ/σ²` (continuous-time, for equities).
Live sizing always multiplies by `kelly_multiplier` κ (default 0.25 — quarter Kelly). Under a
Gaussian approximation, growth relative to full Kelly is κ(2−κ) ≈ 44 % while variance is κ² ≈ 6 %:
a small sacrifice in growth for a large cut in variance, and far more robust to over-estimated edges
(betting 2× the true Kelly fraction yields zero growth).

## 5. Option expected value under the physical measure — `quant/option_ev.py`

The market prices options under the risk-neutral measure with implied vol `σ_iv`. Our edge comes from
a different *physical* forecast: drift `μ` (annualised, from the signal ensemble) and vol `σ_p`
(HAR-RV / GARCH blend). For a long option bought at `P_in` (ask-side fill estimate) and planned exit
at horizon `h ≤ T` (years):

```
S_h = S · exp((μ − σ_p²/2) h + σ_p √h · z),          z ~ N(0,1)
V_h = bs_price(S_h, K, T − h, r, q, σ_iv(h), is_call)  (option value at exit; σ_iv(h) = IV path)
P_out = V_h − c_exit(V_h)                              (exit cost: half-spread × aggression)
R    = P_out / P_in − 1
```
`σ_iv(h)` defaults to `σ_iv + (σ_target − σ_iv)(1 − e^{−h/τ})` — mean reversion of IV toward the
forecast vol with time-constant `τ` = 30 days; around earnings it drops by the event premium
(IV crush, §6.5). Expectation over `z` uses **Gauss-Hermite quadrature** (`n = 48` nodes;
`z_i = √2 x_i`, `w_i/√π`), giving a discrete distribution `{R_i, p_i}`.

`evaluate_long_options(S, K, T, is_call, entry_price, exit_cost_frac, r, q, iv, mu, sigma_p, h,
iv_target=None, n_nodes=48) -> dict[str, ndarray]` — vectorised over contracts; returns
- `ev_pct` = `Σ p_i R_i`
- `pop` = `Σ p_i 1[R_i > 0]`
- `kelly` = `kelly_discrete(R, p)` per contract
- `value_h` = `Σ p_i V_h,i`, `prob_itm_expiry` = `N(d2)` under `(μ, σ_p)` to expiry
- `breakeven` = `K ± P_in` at expiry.

Why this is better than common retail metrics: probability-of-profit and "IV rank" alone ignore
drift, path-to-exit, theta and costs; physical-measure EV with a horizon combines them in one
number and gives a sizing rule consistent with growth-optimal betting.

## 6. Signals — `signals/library.py`

Every model outputs `Signal(score ∈ [−1, 1], confidence ∈ [0, 1], horizon_days)`. Raw statistics
are standardised to `z` and squashed: `score = tanh(z / 2)`. Positive = bullish.

| id | Idea | Statistic | Horizon | Reference |
|---|---|---|---|---|
| `tsmom` | Time-series momentum | Vol-scaled 1/3/6/12-month returns (skip last 5d), averaged | 20d | Moskowitz, Ooi, Pedersen (2012) |
| `trend_kalman` | Local-linear-trend Kalman slope | slope / √var(slope) | 10d | Harvey (1989) |
| `meanrev_st` | Short-term reversal in uptrend | −z(5-day return) gated by price > SMA200, IBS & RSI(2) | 3d | Connors; Lehmann (1990) |
| `breakout` | Donchian/vol breakout | (close − 20d high)/ATR, volume-confirmed | 5d | Turtle / Donchian |
| `rel_strength` | Cross-sectional momentum vs SPY | z of 3-month excess return across universe | 20d | Jegadeesh-Titman (1993) |
| `vwap_rev` | Intraday deviation from session VWAP | −(price − VWAP)/σ_intraday when Hurst < 0.5 | 0.5d | |
| `orb` | Opening-range breakout w/ relative volume | direction of 5-min ORB × rel. volume | 1d | Zarattini & Aziz (2023) |
| `vol_premium` | Implied vs forecast vol | `ln(σ_iv/σ_forecast)` — *not directional*; used by scanner to prefer cheap vol | — | Carr & Wu (2009) |
| `options_flow` | Aggressive premium imbalance | z of (ask-side call premium − ask-side put premium), sweep-weighted, 1-day decay | 2d | Pan & Poteshman (2006) |
| `ofi` | Order-flow imbalance (stocks) | Cont-Kukanov-Stoikov OFI over last 5 min / depth | 0.1d | Cont et al. (2014) |
| `news_sentiment` | News sentiment | decayed Σ sentiment × relevance (LLM or lexicon), novelty-weighted | 2d | Tetlock (2007) |
| `earnings_drift` | Post-earnings drift | sign/size of earnings-day gap ÷ σ, volume-confirmed, decays over 40d | 20d | Bernard & Thomas (1989) |

Ensemble (`signals/engine.py`): for symbol `s` and horizon `h`,
`z = Σ_i w_i score_i · confidence_i`, with weights `w_i ∝ max(IC̄_i, 0) / sd(IC_i)` (IC
information ratio) estimated online from logged scores vs realised forward returns, shrunk toward
prior weights with strength `λ = 30/(30 + n_obs)`. Forecast:
`E[r_h] = IC_ens · σ_h · z_norm` (Grinold's fundamental law), `σ_h = σ_p √(h/252)`,
`prob_up = N(E[r_h]/σ_h)`, IC_ens defaults to 0.05 until measured. `μ_annual = E[r_h]·252/h`.

### 6.5 Event handling
- Earnings within the option's life: IV includes an event premium. Estimate the event move from
  the term structure (`σ_event² = (σ_front² T_front − σ_back² T_front)` approximation) and apply
  IV crush after the date in `σ_iv(h)`. Strategies avoid holding through earnings unless the
  strategy is explicitly event-driven (risk param `earnings_blackout_days`).
- FOMC/CPI days: entry window blocked ±30 min around the release (config).

### 6.6 Intel signals — `intel/`, `quant/sentiment.py`

External-information models. All are low prior weight (0.4–0.5): retail chatter and rhetoric are noisy, so the online
IC tracker (§6 ensemble) decides how much they earn. None of them can place an order.

| id | Inputs | Statistic |
|---|---|---|
| `social_sentiment` | Reddit posts, Stocktwits (user-tagged bull/bear), X cashtags, ApeWisdom mention counts | `polarity = (B−R)/(B+R+3)` with `B,R` = Σ max(±s,0)·strength·engagement·decay (half-life 6 h, engagement weight `1+ln(1+e)/3`); `attention = ln((m+k)/(m₀+k))` vs the 24 h-ago count (ApeWisdom) or the 6 h item rate vs the prior 66 h rate (only once ≥ 18 h of history exist). `z = 2·polarity·gate(attention)`, gate = 0.25 when interest is fading, else `0.5+0.5·min(1, attn/ln 2)`. **Crowding guard:** `attention > ln 2 + 1` and `|polarity| > 0.6` → `z ×= 0.4` (retail extremes mean-revert) |
| `statement_shock` | Truth Social archive, Fed/SEC/White House RSS, Google News mover queries, X handles | each statement: `tilt = (P − 1.1·N)/(P+N+1.5)` from a phrase lexicon (longest match first, matched spans masked so *“pause tariffs”* is not also *“tariff”*; a negator within 12 chars flips); LLM re-scores when available (market direction × magnitude, novelty, per-ticker exposures). Applicability to a symbol: explicit mention/LLM exposure **1.0**, exposed theme (tariffs→semis/autos…) **0.7**, market-wide **0.5·β**. `z = 2.5·tanh(Σ v·w·s·a·2^(−age/3h) / 1.5)` |
| `vix_regime` | VIX, VIX9D, VIX3M, VVIX, SKEW (Yahoo; CBOE CSV fallback) | `risk_on = 0.5·(−z_Δ/3) + 0.35·clip((VIX3M/VIX−1)/0.10) + 0.15·(1−2·pct)`, `z_Δ` = 1-day log change ÷ its 1-year σ; label: **panic** (VIX ≥ 35, or z_Δ ≥ 3 with VIX ≥ 25), **stressed** (≥ 25, pct ≥ 0.85 or inverted curve), **elevated**, **calm**, **normal**. `z = 2·risk_on·β` where β = 60-day OLS beta to SPY (clipped ±3) |
| `prediction_tilt` | Polymarket (gamma API), Kalshi (events API) | markets are keyword-classified to themes (recession, fed, tariffs, shutdown, geopolitics, markets…) with an equity polarity for a higher *yes* probability; `tilt = tanh(Σ w·pol·Δp₂₄ₕ / 0.08)`, `w` = theme weight × min(1, ln(1+vol₂₄ₕ)/12). Δp is our own 24 h-ago observation when available, else the venue's field. `z = 2·tilt·β` |
| `event_vol` | same markets + VIX label | **non-directional.** `event_risk = 1 − exp(−Σ_top10 w·(0.6·4p(1−p) + 0.4·min(1,|Δp|/0.15))·e^(−days/30) / 4)`, combined as `max(event_risk, 0.7·stress(label))`. Positive score = a volatility event looks near |

`MacroState.risk_on` = weighted mean of VIX risk-on (0.5), prediction tilt (0.3) and mover-statement tilt (0.2).
Honest limitations: lexicon scoring cannot read sarcasm or context; Google News headlines are *about* a mover, not
the mover’s words (strength ×0.6, opinion pieces ×0.3); prediction-market polarity is a keyword heuristic; none of
these have backtest history, so they are evaluated forward only (paper mode + IC).

## 7. Preset strategies — `strategies/library.py`

| id | Name | Asset | Style | Entry logic | Exit logic |
|---|---|---|---|---|---|
| `options_edge` | Directional Options Edge | option | swing | Ensemble forecast `|z| ≥ z_min`; scanner picks contract maximising `edge_score` with `ev_pct ≥ ev_min`, `pop ≥ pop_min`, DTE 14–60, |Δ| 0.30–0.60, `iv/rv ≤ ratio_max` | stop −40 %, target +80 %, trail 25 % after +40 %, exit at DTE ≤ 7, forecast flip, pre-earnings |
| `flow_follow` | Smart-Money Flow | option | swing | `options_flow` score ≥ threshold with sweeps, agreeing trend; LLM gate on by default | stop −35 %, target +100 %, time stop 5 days |
| `earnings_drift` | Post-Earnings Drift | option/equity | swing | 1–3 days after earnings: gap ≥ 1.5σ with rel. volume ≥ 2, LLM reads headline | time stop 20 days, stop at gap-fill |
| `tsmom_equity` | Vol-Targeted Trend | equity | swing | `tsmom` + `trend_kalman` agree; sized to target 15 % annual vol per position (fractional) | signal flip, 3·ATR trailing stop |
| `meanrev_etf` | ETF Pullback Reversion | equity | swing | SPY/QQQ/IWM/sector ETFs above SMA200, RSI(2) < 10 or IBS < 0.2 | close > 5-day SMA or 5-day time stop |
| `orb_intraday` | Opening-Range Breakout | equity/option | intraday | 5-min ORB on top relative-volume names, direction = first candle | stop at OR opposite side, EOD flat |
| `intel_catalyst` | Sentiment & Macro Catalyst | option | swing | ≥ 2 agreeing intel signals (social / mover statements / prediction markets / VIX) + price confirmation; see §7.2 | standard exit policy + urgent exit if a mover statement turns against it |
| `llm_analyst` | LLM Analyst | option | swing | Top-N ensemble names → LLM receives full dossier and must approve | standard exit policy |

All strategies expose their thresholds as `ParamSpec`s (editable in the UI).

### 7.1 Rags to Riches (`rags_to_riches`, `strategies/rags.py`) — high-risk small-account accelerator
Goal-seeking intraday strategy for tiny balances ($20+). One best setup at a time (configurable up to 3):
opening-range breakout in the direction of the opening drive with relative volume, VWAP trend pullback,
high/low-of-day momentum on a ≥2× volume surge; QQQ-vs-VWAP tape filter (±0.1 score). Long-only
instruments: leveraged ETFs, the broker's most-active names (price ≥ `min_price`), inverse ETFs for bearish
setups, optional cheap 0–2 DTE SPY/QQQ options (|Δ| 0.15–0.45, spread ≤ 25 %, premium ≤ budget).
Sizing is explicit: `TradeIdea.target_fraction` = fraction of *current* equity (compounding), which replaces
Kelly × κ in the RiskManager while every hard cap still applies. Stops are structural, clamped to 0.5–2×
`stop_pct`; target = `reward_r` × stop; trailing after `trail_after_r`; max hold; flat before the close.
Brakes: max trades/day, spacing, daily loss stop, losing-streak stop, dollar floor. `should_halt()` stops the
strategy at the dollar goal (optionally flattening and handing over to the equity tier preset) or the floor.
`aggressiveness` presets: moderate (35 % / 3 trades / 1.5 % stop / 2R), aggressive (70 % / 6 / 2 % / 2.5R,
default), all_in (100 % / 12 / 2.5 % / 3R), or custom. The manual-only `rags` tier preset loosens global caps.

Odds estimator (`quant/growth.py`, `POST /api/tools/goal-odds`): Monte Carlo of i.i.d. trades with win
`1 + f(R·s − c)` and loss `1 − f(s(1+slip) + c)`. Anchor: with zero edge the target is hit before the stop with
probability `p0 = 1/(1+R)`; the user supplies an edge in percentage points over `p0` and its uncertainty (each
path draws its own win rate), giving P(goal before floor), P(floor), time-to-goal and the Kelly fraction
`f* = (pW − qL)/(WL)`. Realistic friction (5 bps, 15 % stop slippage) puts break-even ~3–5 points above chance,
so the strategy needs a genuine edge to grow; with none, nearly every path ends at the floor.

### 7.2 Sentiment & Macro Catalyst (`intel_catalyst`, `strategies/intel.py`)
Long calls/puts (shorter-dated, 7–35 DTE, |Δ| 0.35–0.65) when independent intel agrees **and** price confirms. Never enabled
by any tier preset; off until the user switches it on.
1. `composite = Σ w_m·confidence_m·score_m / Σ w_m` with `w` = statements 0.40, social 0.35, prediction 0.15, VIX 0.10;
   needs `|composite| ≥ 0.20`.
2. **Agreement:** ≥ `min_agree` (2) components with `|score·confidence| ≥ 0.15` in the trade direction, at least one of them
   social/statement; any component against it with `|score·confidence| ≥ 0.30` vetoes. Loud one-sided retail crowd in our own
   direction (`crowded`) vetoes.
3. **Price confirmation:** move since the prior close in the trade direction ≥ 0.3 % and ≤ 6 % (anti-chase).
4. **Regime filters only shrink or veto:** bullish entries blocked in a VIX *panic*; size ×0.5 when *stressed*; size ×0.7 when
   `event_risk ≥ 0.6`. The LLM gate is on by default (veto/shrink only); risk checks are unchanged.
5. **Exits:** shared policy (stop −35 %, target +80 %, trail, 5-day time stop, DTE ≤ 3) plus an urgent exit if
   `statement_shock` flips ≤ −0.5 against the position.

## 8. Account tiers — `strategies/presets.py`

| Tier | Equity | Enabled by default | Key risk settings |
|---|---|---|---|
| `micro` | < $2,000 | `tsmom_equity` (fractional), `meanrev_etf`, `options_edge` (only premium ≤ 10 % of equity) | max 3 positions, risk/trade 5 %, κ = 0.20, options alloc ≤ 40 % |
| `small` | $2k–$10k | + `flow_follow`, `earnings_drift` | max 5 positions, risk/trade 3 %, κ = 0.25 |
| `growth` | $10k–$25k | all incl. `llm_analyst`, `orb_intraday` | max 8 positions, risk/trade 2 %, κ = 0.25 |
| `pro` | ≥ $25k | all | max 12 positions, risk/trade 1.5 %, κ = 0.30, 2 per underlying |

Day trading: FINRA eliminated the pattern-day-trader rule (no $25k minimum, no day-trade count) effective
2026-06-04 (Regulatory Notice 26-10); intraday margin is enforced by the broker via buying power. Tiers are
purely a sizing concept. Because firms may keep legacy PDT protection until 2027-10-20, `allow_daytrades=auto`
trades freely but, after the first broker rejection mentioning PDT, pauses intraday strategies and limits
same-day exits to the legacy 3-per-5-days allowance for the rest of the session.

Small accounts get *higher* per-trade risk percentages (otherwise no contract is affordable) but
fewer concurrent positions and a lower Kelly multiplier. Option strategies pick the best contract the
account can actually size (`_best_affordable`: cheaper strikes/tenors) rather than the best overall.
Kelly is treated as an upper bound on growth-optimal size: with `min_one_contract` (default on), a
positive-edge idea that Kelly sizes below one contract may still buy exactly one — only if that one
contract fits every *hard* limit (per-trade risk budget, position cap, options allocation, buying
power). Nothing is ever rounded up past a hard limit.

## 9. Execution — `execution/engine.py`
Limit order at `mid + aggression × half_spread` (aggression default 0.25); every `reprice_s`
seconds move `+0.25 × half_spread` toward the far side up to `max_aggression`; cancel after
`order_timeout_s`. Options orders are `day`, whole contracts. Equities in small tiers use notional
(fractional) market-on-quote-protected limit orders. Exits for options are managed in software
(Alpaca does not support bracket orders for options); equities get a broker-side stop as a backstop.

## 10. Backtesting — `backtest/engine.py`
Bar-by-bar event loop on daily (or minute) bars using the **same** Strategy objects through a
backtest `StrategyContext`. Fills at next bar open + slippage (bps) + commission. Options are
priced synthetically with BSM using a rolling HAR-RV vol × an IV/RV ratio parameter (approximation
flagged in `notes`). Reported: CAGR, Sharpe, Sortino, MaxDD, Calmar, win rate, profit factor,
exposure, PSR(0), DSR (with `n_trials` = number of parameter sets the user has tried, tracked).

Sizing modes: `equal_weight` (default) gives each symbol 1/N of equity in shares (options: up to
min(1/N, 20 %) of equity in premium per trade, with the one-contract minimum) so the curve reflects
signal quality; `live` replays the auto-trader's risk settings exactly (on a small single-symbol test
this is often a few percent of equity, so the curve looks nearly flat). Each strategy declares
`backtest_support`: `full` (tsmom_equity, meanrev_etf), `synthetic` (options_edge, earnings_drift —
synthetic option prices), or `none` (flow_follow: no historical flow; orb_intraday: needs minute
bars; llm_analyst: LLM not replayed) — those are refused with the reason. Every result reports a
funnel (ideas generated → entered, top risk rejections) so a flat result is always explained.
