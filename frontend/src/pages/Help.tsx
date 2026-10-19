import { useEffect, useState, type MouseEvent, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { api } from "../lib/api";
import { fmtNum } from "../lib/format";
import { useAsync } from "../hooks/useAsync";
import { GO_SHORTCUTS } from "../hooks/useHotkeys";

const TOC: { id: string; title: string; sub?: { id: string; title: string }[] }[] = [
  { id: "overview", title: "Overview" },
  { id: "modes", title: "Modes & safety interlocks" },
  { id: "dashboard", title: "Dashboard & portfolio chart" },
  { id: "symbol", title: "Symbol page & order ticket", sub: [{ id: "chain", title: "Options chain" }] },
  { id: "scanner", title: "Scanner metrics", sub: [{ id: "ev", title: "EV, PoP, Kelly" }, { id: "ivrank", title: "IV rank & IV/RV" }] },
  { id: "signals", title: "Signal models & ensemble" },
  { id: "strategies", title: "Preset strategies" },
  { id: "tiers", title: "Account tiers" },
  { id: "autotrader", title: "Auto-trader" },
  { id: "risk", title: "Risk parameters" },
  { id: "llm", title: "LLM gate", sub: [{ id: "llm-log", title: "LLM Log" }] },
  { id: "feeds", title: "Feeds & data entitlements" },
  { id: "calendar", title: "Calendar" },
  { id: "backtest", title: "Backtesting, PSR & DSR" },
  { id: "shortcuts", title: "Keyboard shortcuts" },
  { id: "troubleshooting", title: "Troubleshooting" },
  { id: "disclaimer", title: "Disclaimer & credits" },
];

const ALL_IDS = TOC.flatMap((t) => [t.id, ...(t.sub?.map((s) => s.id) ?? [])]);

function scrollToId(id: string) {
  const el = document.getElementById(id);
  if (!el) return;
  el.scrollIntoView({ behavior: "smooth", block: "start" });
  history.replaceState(null, "", `#${id}`);
}

function Sec({ id, title, children }: { id: string; title: string; children: ReactNode }) {
  return (
    <section id={id} aria-labelledby={`${id}-h`}>
      <h2 id={`${id}-h`}>{title}</h2>
      {children}
    </section>
  );
}

const F = ({ children }: { children: string }) => <div className="formula">{children}</div>;

function LiveModels() {
  const models = useAsync((signal) => api.signalModels({ signal }), []);
  if (!models.data?.length) return null;
  return (
    <>
      <h3>Live model weights (from the running backend)</h3>
      <div className="table-wrap">
        <table className="tbl compact">
          <thead>
            <tr>
              <th>Model</th>
              <th>Kind</th>
              <th className="num">Horizon</th>
              <th className="num">Prior w</th>
              <th className="num">Weight</th>
              <th className="num">IC</th>
              <th className="num">IC sd</th>
              <th className="num">IC days</th>
              <th>On</th>
            </tr>
          </thead>
          <tbody>
            {models.data.map((m) => (
              <tr key={m.id}>
                <td title={m.description}>
                  <code>{m.id}</code>
                </td>
                <td>{m.kind}</td>
                <td className="num">{fmtNum(m.horizon_days, m.horizon_days < 1 ? 1 : 0)}d</td>
                <td className="num">{fmtNum(m.prior_weight, 2)}</td>
                <td className="num">{fmtNum(m.weight, 2)}</td>
                <td className="num">{m.ic === null ? "—" : fmtNum(m.ic, 3)}</td>
                <td className="num">{m.ic_sd === null ? "—" : fmtNum(m.ic_sd, 3)}</td>
                <td className="num">{m.ic_days}</td>
                <td>{m.enabled ? "yes" : "no"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

export default function Help() {
  const [active, setActive] = useState("overview");

  useEffect(() => {
    const hash = decodeURIComponent(location.hash.slice(1));
    if (hash && ALL_IDS.includes(hash)) window.setTimeout(() => document.getElementById(hash)?.scrollIntoView(), 50);
    const root = document.getElementById("main");
    const obs = new IntersectionObserver(
      (entries) => {
        const vis = entries.filter((e) => e.isIntersecting).sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
        if (vis[0]) setActive(vis[0].target.id);
      },
      { root, rootMargin: "0px 0px -70% 0px", threshold: 0 },
    );
    for (const id of ALL_IDS) {
      const el = document.getElementById(id);
      if (el) obs.observe(el);
    }
    return () => obs.disconnect();
  }, []);

  const link = (id: string) => ({
    href: `#${id}`,
    className: active === id ? "active" : "",
    onClick: (e: MouseEvent) => {
      e.preventDefault();
      scrollToId(id);
    },
  });

  return (
    <div className="page">
      <div className="page-header">
        <div>
          <h1>Help</h1>
          <div className="sub">How every part of Vantage works, the maths behind the numbers, and what to do when something looks wrong.</div>
        </div>
      </div>
      <div className="help-layout">
        <nav className="help-toc" aria-label="Help contents">
          <ol>
            {TOC.map((t) => (
              <li key={t.id}>
                <a {...link(t.id)}>{t.title}</a>
                {t.sub && (
                  <ol>
                    {t.sub.map((s) => (
                      <li key={s.id} className="sub">
                        <a {...link(s.id)}>{s.title}</a>
                      </li>
                    ))}
                  </ol>
                )}
              </li>
            ))}
          </ol>
        </nav>

        <article className="help-content">
          <Sec id="overview" title="Overview">
            <p>
              Vantage is a single-user trading workstation for an Alpaca brokerage account, running entirely on your machine. The backend (Python, <code>127.0.0.1:8765</code>) talks
              to Alpaca, computes signals, scans options, and runs the auto-trader; this browser UI shows everything live over one WebSocket.
            </p>
            <ul>
              <li>
                <strong>Dashboard</strong> — portfolio value chart, account stats, positions, open orders, auto-trader status, watchlist.
              </li>
              <li>
                <strong>Symbol</strong> — price chart, quote, order ticket, signals and ensemble forecast, options chain, news.
              </li>
              <li>
                <strong>Scanner</strong> — ranks long calls and puts by expected value under our own forecast.
              </li>
              <li>
                <strong>Feeds</strong> — live news, options flow, model signals and auto-trader decisions.
              </li>
              <li>
                <strong>Auto-trader</strong> — arming, kill switch, tier presets, risk limits and per-strategy settings.
              </li>
              <li>
                <strong>Calendar</strong>, <strong>Backtest</strong>, <strong>Settings</strong> and this page.
              </li>
            </ul>
            <p>
              All prices are in USD; all times are US/Eastern (market time). Percentages in the API are fractions; the UI shows them as %.
            </p>
          </Sec>

          <Sec id="modes" title="Modes & safety interlocks">
            <p>The current mode is always visible in the top bar badge:</p>
            <table className="tbl">
              <tbody>
                <tr>
                  <td>
                    <span className="badge mode-sim">SIM</span>
                  </td>
                  <td>Synthetic market and broker (random-walk prices with jumps, simulated fills, news and chains). No keys, no network. Good for learning the UI and testing. Never draw conclusions about strategy profitability from SIM.</td>
                </tr>
                <tr>
                  <td>
                    <span className="badge mode-paper">PAPER</span>
                  </td>
                  <td>Alpaca paper trading: real market data, fake money. Use it to forward-test strategies before risking capital.</td>
                </tr>
                <tr>
                  <td>
                    <span className="badge mode-live">LIVE</span>
                  </td>
                  <td>Your real Alpaca account. Every order uses real money. A red strip is shown across the top of every page.</td>
                </tr>
              </tbody>
            </table>
            <h3>Live-trading interlocks (all must pass)</h3>
            <ol>
              <li>
                <code>ALLOW_LIVE_TRADING=true</code> must be set in the backend <code>.env</code>, together with live API keys. Without it the Live option in Settings is disabled and
                explains why.
              </li>
              <li>
                Switching to live, and arming the auto-trader in live, requires typing <kbd>LIVE</kbd> in a confirmation dialog; the UI then sends <code>confirm: "LIVE"</code>, which the
                backend checks.
              </li>
              <li>The auto-trader always starts <strong>disarmed</strong> after a restart or a mode switch.</li>
              <li>Every order — manual or automatic, paper or live — passes the same risk manager.</li>
            </ol>
            <p>
              Switching mode stops the auto-trader, closes streams and rebuilds the broker and data services. Everything persisted is tagged with its mode, so SIM, paper and live
              data never mix. API keys exist only in the backend <code>.env</code>; the browser only ever sees booleans such as "paper keys present".
            </p>
          </Sec>

          <Sec id="dashboard" title="Dashboard & portfolio chart">
            <p>The portfolio chart is modelled on Robinhood's:</p>
            <ul>
              <li>
                The big number is your portfolio value. Below it: the change over the selected range in $ and %, with the range label (Today, Past week, …).
              </li>
              <li>
                The line is <span className="up">green</span> when the current value is at or above the range's baseline and <span className="down">orange-red</span> otherwise. The
                active range button takes the same colour.
              </li>
              <li>
                <strong>Baseline</strong>: on <strong>1D</strong> it is the previous close (shown as a dotted horizontal line); on other ranges it is the first point of the range.
              </li>
              <li>
                <strong>Scrubbing</strong>: move the mouse over the chart and the header shows the value at that moment and its change versus the baseline (the colour flips if it was
                below). A timestamp follows the cursor; leave the chart to return to the live value.
              </li>
              <li>
                On <strong>1D</strong> the x-axis covers the whole session from the start, so the line grows across the day. Pre- and after-hours segments are drawn dimmer. The chart
                streams live equity ticks (<span className="badge">LIVE</span> indicator).
              </li>
              <li>
                <strong>Zoom</strong> with the mouse wheel, <strong>pan</strong> by dragging, and <strong>double-click</strong> to reset.
              </li>
            </ul>
            <p>
              The stats strip shows buying power, cash, options buying power, day trades in the last 5 business days (for reference — FINRA eliminated the pattern-day-trader rule and its
              $25k minimum effective June 4, 2026, so there is no day-trade limit), and the account tier. Positions rows open the symbol page; <strong>Close</strong> sends a market order for 100/50/25 % after confirmation. Open
              orders can be cancelled. Watchlist prices tick live; add or remove symbols inline or in Settings.
            </p>
          </Sec>

          <Sec id="symbol" title="Symbol page & order ticket">
            <p>
              Type a symbol in the search box (press <kbd>/</kbd> to focus it) and hit <kbd>Enter</kbd>. The page shows the price chart (same Robinhood style, with a previous-close
              baseline on 1D), a toggle to candlesticks, a subtle volume pane, live bars and quotes, and ranges 1D to 5Y. In candlestick mode the header shows OHLC and volume of the
              hovered bar.
            </p>
            <h3>Order ticket</h3>
            <ul>
              <li>Buy or sell; for stocks either a number of shares or a dollar amount (fractional, DAY only).</li>
              <li>Market or limit. For limits, one-click Bid / Mid / Ask presets fill the price.</li>
              <li>Time in force DAY or GTC; "extended hours" is available for DAY limit orders on stocks.</li>
              <li>
                <strong>Review order</strong> opens a confirmation with the estimated cost (shares × price, or contracts × price × 100), the reference bid/ask, warnings (buying power,
                session, wide spreads) and the current mode badge. Nothing is sent until you confirm.
              </li>
              <li>Options orders are always DAY, whole contracts, regular hours only (Alpaca rules).</li>
            </ul>
            <h3>Signals panel</h3>
            <p>
              Left: the ensemble forecast — expected return over the horizon, probability the price is higher at the horizon, annualised volatility and the ensemble z-score — and each
              model's contribution (weight × score) as a diverging bar. Right: the latest score, confidence, horizon and measured IC of every model for this symbol, updating live.
            </p>
            <h3 id="chain">Options chain</h3>
            <p>
              Pick an expiration (defaults to the first one at least a week out). Calls are on the left, puts on the right, strikes in the middle. Columns: open interest, volume, implied
              volatility, delta, bid, mid, ask. The at-the-money strike is outlined; in-the-money cells are shaded (calls below spot, puts above). The header shows spot, days to
              expiry, ATM IV and the 1σ expected move <code>S · IV · √T</code>. Click any contract to open its chart (live mid) and an option ticket for it. "All strikes" expands
              beyond the 12 strikes either side of the money. The chain refreshes every 30 s.
            </p>
          </Sec>

          <Sec id="scanner" title="Scanner metrics">
            <p>
              The scanner evaluates long calls and puts across your universe and ranks them by how much edge they have under <em>our</em> forecast of the underlying, after costs.
              Parameters (thresholds, DTE and delta windows, liquidity filters) are generated from the backend's schema; hover the info icons for each one's meaning. Click a column
              header to sort; click a row to see the reasons; click the contract name to open it on the symbol page with the contract preselected. New scans run by the auto-trader
              appear automatically.
            </p>
            <h3 id="ev">EV, PoP and Kelly</h3>
            <p>
              The market prices options under the risk-neutral measure with implied volatility σ<sub>iv</sub>. Our forecast is a <em>physical</em> drift μ (from the signal ensemble)
              and volatility σ<sub>p</sub> (HAR-RV / GARCH blend). For a long option bought at P<sub>in</sub> (ask-side fill estimate) and exited at horizon h:
            </p>
            <F>{`S_h   = S · exp((μ − σp²/2)·h + σp·√h·z),   z ~ N(0,1)
V_h   = BlackScholes(S_h, K, T − h, r, q, σiv(h))      option value at exit
P_out = V_h − exit_cost(V_h)                           half-spread × aggression
R     = P_out / P_in − 1                               return on premium`}</F>
            <p>
              The expectation over z uses 48-node Gauss–Hermite quadrature, giving a discrete distribution of returns R<sub>i</sub> with probabilities p<sub>i</sub>. IV is assumed
              to mean-revert toward the forecast vol (time constant 30 days) and to drop after earnings (IV crush).
            </p>
            <ul>
              <li>
                <strong>EV %</strong> = Σ p<sub>i</sub> R<sub>i</sub> — expected return on the premium you pay, at the horizon, net of spread costs. +12 % means that, if the forecast
                is right on average, each $100 of premium is expected to come back as $112.
              </li>
              <li>
                <strong>PoP</strong> = Σ p<sub>i</sub> · 1[R<sub>i</sub> &gt; 0] — probability the position is worth more than you paid at the horizon (after exit costs). Not the
                same as the probability of expiring in the money.
              </li>
              <li>
                <strong>Kelly</strong> — the fraction f of equity that maximises expected log-growth Σ p<sub>j</sub> log(1 + f R<sub>j</sub>), with f ≤ 1/max(−R<sub>j</sub>) so you can
                never lose more than everything. It is 0 when EV ≤ 0. The auto-trader sizes with κ × Kelly (fractional Kelly, see risk parameters).
              </li>
              <li>
                <strong>Breakeven</strong> = K ± premium at expiry. <strong>Spread %</strong> = (ask − bid) / mid.
              </li>
              <li>
                <strong>Edge score</strong> — the composite ranking used to sort candidates and pick contracts: it rewards EV and Kelly and penalises poor liquidity and expensive
                implied vol. Compare it between candidates of the same scan rather than as an absolute number.
              </li>
            </ul>
            <p>
              Why not just PoP or IV rank? Those ignore the drift forecast, the path to exit, theta and costs. EV with a horizon combines them into one number and gives a sizing rule
              consistent with growth-optimal betting.
            </p>
            <h3 id="ivrank">IV rank and IV / RV</h3>
            <F>{`IV rank       = (IV_now − IV_min) / (IV_max − IV_min)     over the stored daily ATM-IV history (≥ 20 days, else —)
IV percentile = share of history days with IV below today
IV / RV       = σiv / σforecast                             implied vs our forecast of realised vol`}</F>
            <p>
              Low IV rank or IV/RV below 1 means options are cheap relative to their own history or to the volatility we expect — favourable for buyers. IV/RV above 1 means you pay a
              volatility premium that the move must overcome.
            </p>
          </Sec>

          <Sec id="signals" title="Signal models & ensemble">
            <p>
              Every model outputs a <strong>score</strong> in [−1, 1] (positive = bullish), computed as <code>tanh(z / 2)</code> of a standardised statistic, plus a confidence in
              [0, 1] and a horizon in days.
            </p>
            <table className="tbl compact">
              <thead>
                <tr>
                  <th>Model</th>
                  <th>Idea</th>
                  <th>Statistic</th>
                  <th>Horizon</th>
                </tr>
              </thead>
              <tbody>
                {[
                  ["tsmom", "Time-series momentum", "Vol-scaled 1/3/6/12-month returns (skipping the last 5 days), averaged", "20d"],
                  ["trend_kalman", "Kalman local-linear trend", "Slope / √var(slope)", "10d"],
                  ["meanrev_st", "Short-term reversal in an uptrend", "−z(5-day return), only above SMA200, with IBS and RSI(2)", "3d"],
                  ["breakout", "Donchian / volatility breakout", "(close − 20-day high) / ATR, volume-confirmed", "5d"],
                  ["rel_strength", "Cross-sectional momentum vs SPY", "z of 3-month excess return across the universe", "20d"],
                  ["vwap_rev", "Intraday VWAP reversion", "−(price − VWAP)/σ_intraday when Hurst < 0.5", "0.5d"],
                  ["orb", "Opening-range breakout", "Direction of 5-minute ORB × relative volume", "1d"],
                  ["vol_premium", "Implied vs forecast vol", "ln(σiv / σforecast) — not directional; the scanner uses it to prefer cheap vol", "—"],
                  ["options_flow", "Aggressive premium imbalance", "z of ask-side call premium − ask-side put premium, sweep-weighted, 1-day decay", "2d"],
                  ["ofi", "Stock order-flow imbalance", "Cont–Kukanov–Stoikov OFI over 5 minutes / depth", "0.1d"],
                  ["news_sentiment", "News sentiment", "Decayed Σ sentiment × relevance (LLM or lexicon), novelty-weighted", "2d"],
                  ["earnings_drift", "Post-earnings drift", "Earnings-day gap ÷ σ, volume-confirmed, decaying over 40 days", "20d"],
                ].map(([id, idea, stat, h]) => (
                  <tr key={id}>
                    <td>
                      <code>{id}</code>
                    </td>
                    <td>{idea}</td>
                    <td>{stat}</td>
                    <td>{h}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <h3>Ensemble and IC weighting</h3>
            <p>
              Signals are <em>calibrated, not voted</em>. Each model's information coefficient (IC: Spearman rank correlation between its scores and the realised forward returns) is
              measured online. Weights favour models that have been consistently predictive and are shrunk toward prior weights while history is short:
            </p>
            <F>{`z        = Σ_i w_i · score_i · confidence_i
w_i      ∝ max(mean IC_i, 0) / sd(IC_i)        (IC information ratio)
shrink   λ = 30 / (30 + n_obs)                  toward prior weights
E[r_h]   = IC_ens · σ_h · z_norm                 (Grinold's fundamental law; IC_ens = 0.05 until measured)
σ_h      = σp · √(h / 252)
P(up)    = N(E[r_h] / σ_h)
μ_annual = E[r_h] · 252 / h`}</F>
            <p>The expected return and P(up) on the symbol page are exactly these numbers; μ and σ<sub>p</sub> feed the scanner's EV calculation.</p>
            <LiveModels />
          </Sec>

          <Sec id="strategies" title="Preset strategies">
            <p>
              Strategies never place orders themselves: they emit trade ideas, which go through the LLM gate (if enabled for that strategy), then the risk manager, then execution. All
              thresholds are editable on the Auto-trader page.
            </p>
            <table className="tbl compact">
              <thead>
                <tr>
                  <th>Strategy</th>
                  <th>Asset / style</th>
                  <th>Entry</th>
                  <th>Exit</th>
                </tr>
              </thead>
              <tbody>
                {[
                  ["Directional Options Edge (options_edge)", "option · swing", "Ensemble |z| ≥ z_min; the scanner picks the contract with the best edge score with EV ≥ ev_min, PoP ≥ pop_min, 14–60 DTE, |Δ| 0.30–0.60, IV/RV ≤ ratio_max", "Stop −40 %, target +80 %, 25 % trailing stop after +40 %, exit at DTE ≤ 7, on forecast flip or before earnings"],
                  ["Smart-Money Flow (flow_follow)", "option · swing", "options_flow score above threshold with sweeps and an agreeing trend; LLM gate on by default", "Stop −35 %, target +100 %, 5-day time stop"],
                  ["Post-Earnings Drift (earnings_drift)", "option/equity · swing", "1–3 days after earnings: gap ≥ 1.5σ with relative volume ≥ 2; the LLM reads the headline", "20-day time stop; stop at gap fill"],
                  ["Vol-Targeted Trend (tsmom_equity)", "equity · swing", "tsmom and trend_kalman agree; sized to 15 % annual volatility per position (fractional shares)", "Signal flip or 3·ATR trailing stop"],
                  ["ETF Pullback Reversion (meanrev_etf)", "equity · swing", "SPY/QQQ/IWM/sector ETFs above SMA200 with RSI(2) < 10 or IBS < 0.2", "Close above the 5-day SMA or 5-day time stop"],
                  ["Opening-Range Breakout (orb_intraday)", "equity/option · intraday", "5-minute ORB on the top relative-volume names, direction of the first candle", "Stop at the opposite side of the range; flat by the close. Skipped only if day trades are disabled in the risk settings."],
                  ["LLM Analyst (llm_analyst)", "option · swing", "Top ensemble names; the LLM gets the full dossier and must approve", "Standard exit policy"],
                ].map(([n, a, e, x]) => (
                  <tr key={n}>
                    <td>{n}</td>
                    <td>{a}</td>
                    <td>{e}</td>
                    <td>{x}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p>
              Options exits are managed in software by the auto-trader (Alpaca does not support bracket orders for options); equities also get a broker-side stop as a backstop.
            </p>
          </Sec>

          <Sec id="tiers" title="Account tiers">
            <table className="tbl compact">
              <thead>
                <tr>
                  <th>Tier</th>
                  <th>Equity</th>
                  <th>Enabled by default</th>
                  <th>Key risk settings</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td>micro</td>
                  <td>&lt; $2,000</td>
                  <td>tsmom_equity (fractional), meanrev_etf, options_edge (only premium ≤ 10 % of equity)</td>
                  <td>max 3 positions, 5 % risk per trade, κ = 0.20, options ≤ 40 % of equity</td>
                </tr>
                <tr>
                  <td>small</td>
                  <td>$2k – $10k</td>
                  <td>+ flow_follow, earnings_drift</td>
                  <td>max 5 positions, 3 % risk per trade, κ = 0.25</td>
                </tr>
                <tr>
                  <td>growth</td>
                  <td>$10k – $25k</td>
                  <td>all, including llm_analyst and orb_intraday</td>
                  <td>max 8 positions, 2 % risk per trade, κ = 0.25</td>
                </tr>
                <tr>
                  <td>pro</td>
                  <td>≥ $25k</td>
                  <td>all strategies</td>
                  <td>max 12 positions, 1.5 % risk per trade, κ = 0.30, up to 2 positions per underlying</td>
                </tr>
              </tbody>
            </table>
            <p>
              Small accounts get a <em>higher</em> per-trade risk percentage (otherwise no contract is affordable) but fewer positions and a lower Kelly multiplier. The risk manager
              never rounds a fractional contract up: if one contract's premium exceeds the per-trade budget, the trade is skipped. Applying a preset overwrites the risk parameters;
              "Follow equity automatically" re-applies the right preset as equity crosses tier boundaries.
            </p>
          </Sec>

          <Sec id="autotrader" title="Auto-trader">
            <p>Every cycle (default 30 s during market hours):</p>
            <ol>
              <li>Refresh the account, compute the tier, check the kill switch (daily loss, drawdown, error rate).</li>
              <li>Manage open positions: each owning strategy decides exits (stop, target, trailing, time stop, DTE floor, pre-earnings, signal reversal).</li>
              <li>Generate ideas from every enabled strategy; de-duplicate, rank by edge score, drop symbols already held.</li>
              <li>LLM gate (where enabled): approve, reject, or shrink.</li>
              <li>Risk manager: size and check, or reject with a reason.</li>
              <li>Execute: limit order at mid + aggression × half-spread, re-priced up a ladder, cancelled after the timeout.</li>
            </ol>
            <p>
              Every step emits a <strong>decision</strong> (enter, exit, skip, reject, error) with a human-readable reason — see the decision log or Feeds → Decisions. With
              "Preview while disarmed", a disarmed engine still runs and logs would-be entries as <code>skip</code> decisions prefixed <code>preview (disarmed):</code>.
            </p>
            <ul>
              <li>
                <strong>Arm / Disarm</strong> — only an armed auto-trader places entries. In live mode arming requires typing LIVE.
              </li>
              <li>
                <strong>Kill switch</strong> — stops and disarms immediately; optionally <em>flattens</em> (closes all positions, cancels orders). It also trips automatically on the
                daily-loss or drawdown limit. <strong>Reset kill switch</strong> clears it; the engine stays disarmed.
              </li>
              <li>
                <strong>Run one cycle</strong> — runs a cycle now, useful to see decisions without waiting.
              </li>
              <li>Strategy cards: enable/disable, toggle the LLM gate, edit parameters, see trades / win rate / P&amp;L.</li>
            </ul>
          </Sec>

          <Sec id="risk" title="Risk parameters">
            <p>
              Hard limits checked on every order; any failure rejects the trade. Sizing: <code>contracts = floor(min(Kelly × κ × equity, risk budget, max position) / (premium × 100))</code>.
            </p>
            <table className="tbl compact">
              <tbody>
                {[
                  ["kelly_multiplier (κ)", "Fraction of full Kelly used for sizing. 0.25 (quarter Kelly) keeps about 44 % of full-Kelly growth (κ(2 − κ)) with about 6 % of its variance (κ²), and is far more forgiving of forecast error."],
                  ["max_risk_per_trade", "Maximum premium (options) or stop-distance loss (equities) per trade, as a fraction of equity."],
                  ["max_position_pct", "Maximum market value of a single position as a fraction of equity."],
                  ["max_open_positions", "Maximum number of concurrent positions."],
                  ["max_per_underlying", "Maximum positions on the same underlying (stock plus its options)."],
                  ["max_options_allocation", "Maximum total options premium held, as a fraction of equity."],
                  ["max_daily_loss", "Loss versus the previous close (fraction of equity) that trips the kill switch for the day."],
                  ["max_drawdown", "Peak-to-trough equity drawdown that trips the kill switch."],
                  ["max_trades_per_day", "Maximum new entries per day."],
                  ["min_option_volume", "Minimum contract volume today."],
                  ["min_open_interest", "Minimum contract open interest."],
                  ["max_spread_pct", "Maximum (ask − bid) / mid; wider contracts are skipped."],
                  ["allow_daytrades", "auto = unlimited day trades (PDT rule eliminated June 4, 2026), but if the broker still rejects an order under legacy PDT protection during its transition period the auto-trader pauses intraday strategies and limits same-day exits for the session; never = never open and close the same day (stop-losses still fire); always = never back off."],
                  ["entry_start_min / entry_end_min", "Entry window in minutes relative to the regular session (no new entries outside it; avoids the open/close auction noise)."],
                  ["earnings_blackout_days", "No new entries this many days before an earnings date (unless the strategy is event-driven)."],
                  ["cycle_seconds", "Seconds between auto-trader cycles."],
                  ["llm_enabled", "Global switch for the LLM gate (per-strategy toggles apply on top)."],
                  ["on_llm_failure", "What to do if the LLM times out or errors: skip the idea (default) or approve it unchanged."],
                  ["aggression", "Initial limit price = mid + aggression × half-spread (0 = mid, 1 = far side)."],
                  ["order_timeout_s", "Cancel an unfilled entry order after this many seconds."],
                ].map(([k, v]) => (
                  <tr key={k}>
                    <td>
                      <code>{k}</code>
                    </td>
                    <td>{v}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p>
              The risk manager also enforces mode/arming, market session, buying power, the day-trade policy (unlimited by default since FINRA removed the PDT rule; it backs off automatically if
              the broker rejects an order for legacy PDT reasons), and a duplicate-order guard. Tiers exist for position
              sizing — the $25k boundary no longer has any regulatory meaning.
            </p>
          </Sec>

          <Sec id="llm" title="LLM gate">
            <p>
              An optional local model (Ollama / any OpenAI-compatible server, e.g. Qwen) reviews trade ideas with a compact dossier (signals, forecast, contract metrics, news, upcoming
              events) and returns a decision, confidence, a size multiplier and a rationale.
            </p>
            <div className="callout info">
              The LLM can only <strong>veto or shrink</strong>. Its size multiplier is clamped to [0, 1]; it cannot add trades the deterministic pipeline did not propose, cannot
              increase size, and cannot override a risk rejection.
            </div>
            <p>
              Configure it in Settings (model, base URL, test button with latency). If it is unreachable, gated ideas follow <code>on_llm_failure</code> and news falls back to the
              lexicon sentiment scorer. LLM rationales appear in decisions and news items.
            </p>
            <h3 id="llm-log">LLM Log</h3>
            <p>
              The <strong>LLM Log</strong> page (sidebar, or <kbd>g</kbd> <kbd>l</kbd>) records every exchange with the model, newest first, updating live. Each row shows the
              purpose (<em>Trade gate</em>, <em>News</em>, <em>Analyst</em>, <em>Test</em>), success / failure / cached, latency, prompt→completion tokens and a one-line summary of
              the reply. Click a row to read the full exchange: the <strong>parsed result</strong> Vantage acted on, the <strong>raw reply</strong>, the <strong>input sent</strong>
              (dossiers are pretty-printed JSON) and the <strong>system prompt</strong>, each with a copy button. Filter by purpose or status, and search the full text of prompts and
              replies (e.g. a ticker). Calls are stored locally in <code>data/vantage.db</code> per trading mode; nothing leaves your machine.
            </p>
          </Sec>

          <Sec id="feeds" title="Feeds & data entitlements">
            <p>
              Feeds keeps the last 500 items of each stream, newest first, in a virtualised list. Filter by symbol, by direction (sentiment, flow direction or signal sign), and pause
              the list to read without it moving. Click flow and signal rows to open the symbol.
            </p>
            <ul>
              <li>
                <strong>News</strong> — Alpaca news with lexicon or LLM sentiment (−1 bearish … +1 bullish). Items may update once LLM enrichment finishes.
              </li>
              <li>
                <strong>Flow</strong> — options prints with side (ask = aggressive buyer), sweep badge, premium and an unusual-activity score.
              </li>
              <li>
                <strong>Signals</strong> — every model update. <strong>Decisions</strong> — every auto-trader decision; click for details.
              </li>
            </ul>
            <h3>Data entitlements</h3>
            <table className="tbl compact">
              <tbody>
                <tr>
                  <td>IEX (free stocks)</td>
                  <td>One exchange only (a few % of volume): quotes are a subset of the NBBO, so spreads look wider and prints are sparse. Liquidity filters account for it.</td>
                </tr>
                <tr>
                  <td>SIP (Algo Trader Plus)</td>
                  <td>Consolidated tape from all exchanges — accurate NBBO and full volume.</td>
                </tr>
                <tr>
                  <td>Indicative (free options)</td>
                  <td>Delayed trades and modified quotes. Real-time sweep detection is not possible, so flow falls back to snapshot-based unusual activity (volume/OI, premium, IV jumps) polled each minute.</td>
                </tr>
                <tr>
                  <td>OPRA (Algo Trader Plus)</td>
                  <td>Real-time consolidated options trades and quotes; enables true sweep and aggressor detection.</td>
                </tr>
              </tbody>
            </table>
            <p>Choose feeds in Settings; they must match your Alpaca data subscription.</p>
          </Sec>

          <Sec id="calendar" title="Calendar">
            <p>
              A month grid plus an agenda for the selected day or week. Colours: <span style={{ color: "var(--cal-earnings)" }}>earnings</span>,{" "}
              <span style={{ color: "var(--cal-fomc)" }}>FOMC</span>, <span style={{ color: "var(--cal-macro)" }}>macro</span>, <span style={{ color: "var(--cal-opex)" }}>OPEX</span>,{" "}
              <span style={{ color: "var(--cal-holiday)" }}>holidays</span>, <span style={{ color: "var(--cal-early_close)" }}>early closes</span> and{" "}
              <span style={{ color: "var(--cal-user)" }}>your events</span>. The three dots show importance. Toggle kinds with the legend, add events with the form (times are ET), and
              delete your own events. Earnings come from Nasdaq (or Finnhub with a key); FOMC dates are built in; OPEX and holidays are computed. The risk manager uses these for the
              earnings blackout and to block entries around FOMC/CPI releases.
            </p>
          </Sec>

          <Sec id="backtest" title="Backtesting, PSR & DSR">
            <p>
              The backtester replays daily (or minute) bars through the <em>same</em> strategy code as the auto-trader, filling at the next bar's open plus slippage and commission.
              Options are priced synthetically with Black-Scholes using a rolling HAR-RV vol times an IV/RV ratio — an approximation noted in the results. The equity curve is shown
              against a buy-and-hold benchmark.
            </p>
            <ul>
              <li>
                <strong>CAGR</strong>, <strong>Sharpe</strong> (annualised, √252), <strong>Sortino</strong> (downside deviation), <strong>Max drawdown</strong>, <strong>Calmar</strong> (CAGR / MaxDD),
                win rate, profit factor (gross profit / gross loss), trade count and exposure (time in the market).
              </li>
            </ul>
            <h3>Probabilistic Sharpe Ratio (PSR)</h3>
            <p>
              A Sharpe ratio from a short or fat-tailed sample is noisy. PSR is the probability that the <em>true</em> Sharpe exceeds a benchmark SR* (here 0), given the number of
              observations n, the skewness γ<sub>3</sub> and (non-excess) kurtosis γ<sub>4</sub> of returns, using the per-period Sharpe SR:
            </p>
            <F>{`PSR = N( (SR − SR*) · √(n − 1) / √(1 − γ3·SR + (γ4 − 1)/4 · SR²) )`}</F>
            <h3>Deflated Sharpe Ratio (DSR)</h3>
            <p>
              If you try many parameter sets and keep the best, its Sharpe is biased upward by luck. DSR is PSR evaluated against the Sharpe you would expect from the best of N trials of
              pure noise (N = backtests you have run for this strategy, tracked by the backend; V = variance of their Sharpes; γ = Euler–Mascheroni constant):
            </p>
            <F>{`SR* = √V · ( (1 − γ)·Φ⁻¹(1 − 1/N) + γ·Φ⁻¹(1 − 1/(N·e)) )`}</F>
            <div className="callout">
              Rule of thumb: PSR above 95 % suggests real skill over the sample; a DSR well below 50 % means the result is probably an artefact of trying many variants. Never tune on
              SIM data — the synthetic market has no exploitable structure.
            </div>
          </Sec>

          <Sec id="shortcuts" title="Keyboard shortcuts">
            <table className="tbl compact" style={{ maxWidth: 520 }}>
              <tbody>
                <tr>
                  <td>
                    <kbd>/</kbd>
                  </td>
                  <td>Focus symbol search (then <kbd>↑</kbd> <kbd>↓</kbd> and <kbd>Enter</kbd>)</td>
                </tr>
                {GO_SHORTCUTS.map((s) => (
                  <tr key={s.key}>
                    <td>
                      <kbd>g</kbd> then <kbd>{s.key}</kbd>
                    </td>
                    <td>Go to {s.label}</td>
                  </tr>
                ))}
                <tr>
                  <td>
                    <kbd>Esc</kbd>
                  </td>
                  <td>Close a dialog / leave a text field</td>
                </tr>
                <tr>
                  <td>Mouse wheel / drag / double-click</td>
                  <td>Zoom / pan / reset any chart</td>
                </tr>
              </tbody>
            </table>
          </Sec>

          <Sec id="troubleshooting" title="Troubleshooting">
            <table className="tbl compact">
              <tbody>
                {[
                  ["Red “Backend offline” banner", "Start the backend from backend/: uv run vantage (it listens on 127.0.0.1:8765). The UI retries automatically with backoff; “Retry now” forces it."],
                  ["“Live stream disconnected”", "REST works but the WebSocket dropped. It reconnects and re-subscribes automatically; charts and tables resync on reconnect."],
                  ["Live / Paper disabled in Settings", "The button explains why: missing keys in .env, or ALLOW_LIVE_TRADING not true. Restart the backend after editing .env."],
                  ["Mode switch failed banner", "The backend could not start the requested mode (bad keys, network) and fell back to SIM. Fix the cause and switch again."],
                  ["Charts empty or sparse", "On IEX, thinly traded symbols have few prints. Outside market hours 1D shows the last session. Try a longer range."],
                  ["Flow tab empty", "With the free indicative options feed, flow is snapshot-based and only appears during market hours."],
                  ["LLM “unreachable”", "Run ollama serve, pull the model (e.g. ollama pull qwen2.5), check the base URL in Settings, then press Test."],
                  ["Order rejected", "Check the toast and decision text: buying power, options approval level, broker legacy PDT protection, market closed for options, or a risk-manager limit."],
                  ["Auto-trader armed but not trading", "Look at the decision log: ideas may be skipped or rejected (entry window, liquidity, blackout, caps). Run one cycle to see fresh decisions."],
                  ["Page 404 after refresh in production", "The backend must serve index.html for unknown non-/api paths (SPA fallback) when serving frontend/dist."],
                ].map(([k, v]) => (
                  <tr key={k}>
                    <td>{k}</td>
                    <td>{v}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Sec>

          <Sec id="disclaimer" title="Disclaimer & credits">
            <div className="callout danger">
              <strong>Not financial advice.</strong> Vantage is software for your own research and execution. Its forecasts, scores and backtests can be wrong, and past or simulated
              performance does not predict future results. Trading options involves substantial risk, including the <strong>total loss of the premium paid</strong>; leverage and
              automated trading can magnify losses quickly. You are solely responsible for every order sent from this software, including orders placed by the auto-trader. Use paper
              trading first, start small, and never trade money you cannot afford to lose.
            </div>
            <p>
              Charts by{" "}
              <a href="https://www.tradingview.com/" target="_blank" rel="noopener noreferrer">
                TradingView
              </a>{" "}
              Lightweight Charts™ (Apache 2.0, Copyright © TradingView, Inc.). Market data and brokerage by Alpaca. See also <Link to="/settings">Settings</Link> for the system status.
            </p>
          </Sec>
        </article>
      </div>
    </div>
  );
}
