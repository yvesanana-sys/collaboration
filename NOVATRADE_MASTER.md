# NovaTrade — Master Reference Document
*Last updated: April 10, 2026*

---

## What Is NovaTrade?

An AI-powered algorithmic trading bot that trades stocks via **Alpaca** and crypto via **Binance.US** simultaneously. Two AI models act as collaborative "managers" — Claude Haiku handles technical analysis, Grok-mini handles social/news intelligence. The bot is the "worker" that executes their instructions autonomously between sessions.

---

## Infrastructure

| Item | Detail |
|------|--------|
| GitHub repo | `yvesanana-sys/collaboration` |
| Deployment | Railway (`collaboration-production-cba3.up.railway.app`) |
| Language | Python (Flask web server) |
| Port | 8080 (must bind before trading loop starts) |
| Stock API | Alpaca live trading |
| Crypto API | Binance.US |
| AI Models | Claude Haiku (technical) + Grok-mini (social/news) |
| Persistent Storage | Railway Volume at `/data` (500MB, survives redeploys) |
| Trade History | `/data/trade_history.json` — rolling 6-month window |

---

## Modular Architecture — Files

NovaTrade uses a clean modular architecture. `bot_with_proxy.py` is the orchestrator — it imports from focused modules. Each module is self-contained and receives dependencies via `_set_context()` injection at startup.

### Core Modules

| File | Lines | Purpose |
|------|-------|---------|
| `bot_with_proxy.py` | ~4,500 | Orchestrator — Flask server, trading loop, buy/sell execution, collaboration sessions |
| `binance_crypto.py` | ~3,300 | Crypto engine — 24/7 Binance.US trading, exit monitor, wallet reader |
| `prompt_builder.py` | ~1,050 | AI prompt construction, adaptive memory/lessons system |
| `projection_engine.py` | ~845 | 5-layer price projection model for stocks |

### Extracted Modules (Phase 1-3 Refactor)

| File | Lines | Purpose |
|------|-------|---------|
| `market_data.py` | ~800 | All market data fetching — indicators, news, Fear & Greed, earnings calendar, SPY trend, gainers, IPOs |
| `intelligence.py` | ~390 | Smart money signals — SEC EDGAR Form 4, politician trades, top investor portfolios |
| `github_deploy.py` | ~155 | GitHub API — push files, trigger Railway redeploy |
| `ai_clients.py` | ~300 | Claude & Grok API wrappers, JSON parsing, health checks, retry logic |
| `sleep_manager.py` | ~210 | AI sleep/wake system — controls when AIs rest, monitors wake conditions |
| `pdt_manager.py` | ~580 | Pattern Day Trader rule management — tracks intraday buys, AI hold councils |

### Support Files

| File | Purpose |
|------|---------|
| `thesis_manager.py` | AI sleep brief writer, custom wake conditions, position thesis storage |
| `wallet_intelligence.py` | Cross-portfolio scanner, opportunity ranker, rotation finder |
| `self_repair.py` | Behavioral anomaly detection, auto-repair with syntax validation, auto-merge |
| `dashboard.html` | Web dashboard UI |
| `NOVATRADE_MASTER.md` | This document |

**Files that must always be present in GitHub:**
All files listed above. If any are missing Railway will crash on boot.

---

## Injection Architecture

Modules are self-contained — they receive dependencies from `bot_with_proxy.py` at startup via `_set_context()`. This prevents circular imports and keeps each module testable independently.

```
Startup sequence (order matters):

1. bot_with_proxy.py starts
2. All modules imported at top
3. log() defined (~line 1085)
4. _market_data._set_context(RULES, log, shared_state)     ← early
5. _github_deploy._set_context(log)                         ← early
6. _ai_clients._set_context(log, shared_state)              ← early
7. _intelligence._set_context(RULES, log, ask_grok, parse_json)  ← early
8. _sleep_manager._set_context(log, shared_state,
       get_cash_thresholds, get_spy_trend)                  ← early
9. ask_grok() and parse_json() defined (~line 1750)
10. _pdt_manager._set_context(log, shared_state, RULES,
        alpaca, ask_claude, ask_grok, parse_json,
        smart_sell, record_trade, get_bars,
        compute_indicators)                                  ← LATE (before run_cycle)
11. run_cycle() and trading_loop() start
```

---

## Data Schemas

### shared_state (dict) — global bot state
```
ai_sleeping: bool          — whether AIs are currently sleeping
sleep_reason: str          — why AIs went to sleep
last_sleep_time: str       — ISO timestamp of last sleep
wake_reason: str           — what triggered last wake
ai_wake_instructions: list — per-position instructions from AI brief
last_cash: float           — cash at last cycle (for threshold crossing detection)
day_start_equity: float    — equity at market open (for daily P&L)
week_start_equity: float   — equity at week start
month_start_equity: float  — equity at month start
year_start_equity: float   — equity at year start
claude_positions: list     — symbols Claude owns
grok_positions: list       — symbols Grok owns
claude_allocation: float   — Claude's capital budget
grok_allocation: float     — Grok's capital budget
last_projections: dict     — cached projections from projection_engine
spy_cache: tuple           — (trend, price, change_pct, sma50)
restricted_positions: set  — symbols bot cannot trade (failed sells, 403s)
intraday_buys: dict        — tracks same-day buys for PDT rule
day_trade_count: int       — number of day trades used today
crypto_day_pnl: float      — crypto P&L for today
crypto_week_pnl: float     — crypto P&L for the week
```

### trade_history entry (dict) — written to /data/trade_history.json
```
time: str          — ISO timestamp (UTC)
time_et: str       — human-readable ET time
action: str        — buy | sell | stop_loss | take_profit | trail_stop | time_stop
symbol: str        — ticker symbol
qty: float         — number of shares/coins
price: float       — execution price
notional: float    — total dollar value
owner: str         — claude | grok | shared | bot
confidence: int    — AI confidence score (0-100)
reason: str        — why the trade was made
entry_price: float — original entry price (for exits)
pnl_usd: float     — realized P&L in dollars
pnl_pct: float     — realized P&L as decimal (0.05 = 5%)
strategy: str      — A | B | crypto | autopilot
spy_trend: str     — bull | bear | neutral at time of trade
equity_after: float — total equity after trade
```

### Key Return Types
```
get_spy_trend()             → (trend: str, price: float, change_pct: float, sma50: float)
get_politician_trades()     → (pol_text: str, actionable: list[dict])
analyze_smart_money()       → {triple_confirmation, top_collab, buy_pressure}
get_full_market_intelligence() → {chart_section, news, market_ctx, pol_text,
                                   pol_trades, pol_signals, inv_text,
                                   inv_holdings, gainers, ipos, smart_money}
check_pdt_safe()            → (safe: bool, reason: str)
ai_sleep()                  → None (sets shared_state)
```

---

## Phase 1 Features (Added April 10, 2026)

| Feature | Source | Frequency | What It Does |
|---------|--------|-----------|-------------|
| Fear & Greed Index | `api.alternative.me/fng/` | Every collaboration cycle | Crypto market sentiment 0-100. Extreme fear = best buy zone |
| Earnings Calendar | Alpaca news API | Every collaboration cycle | Detects earnings risk on current positions — AIs avoid buying pre-earnings |
| Crypto Funding Rates | Binance API (free) | Every crypto cycle | High positive funding = crowded longs = reversal risk |
| Rolling 6-month History Trim | Internal | Daily at reset | Keeps trade history relevant — drops patterns from different market regimes |

---

## Architecture — How It Works

### Sleep/Wake Cycle
AIs sleep after executing trades. Bot runs autonomously. Wake conditions:

1. Cash crosses active threshold
2. All positions closed + cash available
3. 2+ stop-losses fire (emergency)
4. 8:30am premarket (always runs daily)
5. SPY drops >2% suddenly (crash guard)
6. AI custom wake instructions (price triggers AI sets before sleeping)
7. Thesis conditions — AI-written per-position triggers

### Dual AI Collaboration (3 rounds)
- **Round 1**: Both AIs propose trades independently
- **Round 2**: Each AI reviews the other's proposals
- **Round 3**: Collaborative big-ticket gate (locked until $3,000 equity)

### Self-Repair System
`self_repair.py` monitors 7 behavioral patterns:
- `ai_wake`, `ai_brief`, `collaboration_cycle` — frequency guards
- `spy_zero` — data feed failures
- `stale_order` — unfilled Binance orders
- `crypto_parse_fail` — JSON parse failures
- `max_positions_skip` — position limit blocks

When anomaly detected:
- Confidence ≥ 80% + syntax clean → **auto-merge to main** (no PR needed)
- Confidence 60-79% or syntax fails → opens PR for manual review

---

## Trading Rules

### Stocks (Alpaca)
| Rule | Value |
|------|-------|
| Stop loss | -10% |
| Take profit | +20% |
| Max positions | 2 |
| Daily loss limit | 5% |
| Strategy A | Fixed TP (news/breakout entries) |
| Strategy B | Trailing stop (momentum — NVDA always uses B) |
| Trail activates | +10% gain |
| Trail pct | 8% from peak (12% volatile, 6% stable) |
| Time stop | 5 days |
| PDT protection | Tracks intraday buys, AI council on borderline cases |

### Crypto (Binance.US)
| Rule | Value |
|------|-------|
| Stop loss | ~20% below entry |
| Take profit | ~25-30% above entry |
| Max positions | 1 (Tier 1) |
| Max hold | 72 hours |
| Min profit (after fees) | 1.5% |
| Trade size | 45% of wallet (Tier 1) |
| Reserve | $10 USDT always kept |
| Order type | MARKET (avoids PRICE_FILTER errors) |
| Stale order cancel | BUY >30min, SELL >60min auto-cancelled |

### Autonomy Tiers (Stocks)
| Tier | Equity | Claude Budget | Grok Budget |
|------|--------|--------------|-------------|
| 1 | $150 | $25 | $25 |
| 2 | $300 | $50 | $50 |
| 3 | $600 | $100 | $100 |
| 4 | $1,200 | $200 | $200 |
| 5 | $2,000 | $300 | $300 + shorts |

---

## Intelligence Sources

### Stocks
- **SEC EDGAR Form 4** — Official congressional insider trades (free, no API key, real-time)
- **Top Investor Portfolios** — Buffett, Ackman, Burry, Lynch, Dalio (via Grok web search)
- **Smart Money Analysis** — Triple confirmation signals from politician + investor data
- **Earnings Calendar** — Alpaca news API earnings detection
- **Biggest Gainers + Recent IPOs** — Alpaca market data
- **Fear & Greed Index** — alternative.me (free)

### Crypto (Grok searches every cycle)
- Twitter/X — coin tickers + influencers (Kaleo, PlanB, Altcoin Daily, Ansem)
- Reddit — r/CryptoCurrency, r/CryptoMoonShots, r/Bitcoin, r/ethtrader
- News — CoinDesk, CoinTelegraph, Decrypt, The Block
- Whale trackers — Whale Alert, Lookonchain
- Exchange listings — Binance, Coinbase, Kraken announcements
- Macro — BTC ETF flows, Fed/rate news, SEC crypto regulation
- Funding rates — Binance perpetuals (free, already connected)
- Per-holding FUD/catalyst check + hidden gem scan

---

## Technical Indicators Computed

RSI-14, MACD (12/26/9), EMA 9/21, SMA 20/50, Bollinger Bands, ATR-14, Volume Ratio, OBV trend, VWAP, Breakout signals, Intraday indicators (5-min bars)

---

## API Endpoints (Railway URL)

| Endpoint | What It Returns |
|----------|----------------|
| `/health` | Bot status |
| `/storage` | Volume mount status + trade history file info |
| `/stats` | Full portfolio snapshot |
| `/history` | Trade history from volume |
| `/performance` | Win rate, P&L analytics |
| `/projections` | Stock projection engine output |
| `/crypto_status` | Binance.US positions and wallet |
| `/prompt_memory` | AI lessons learned |
| `/repair_status` | Self-repair behavioral monitor status |
| `/pdt_status` | PDT rule status and intraday buy tracker |
| `/dashboard` | Web dashboard UI |
| `/deploy` | Push files to GitHub (POST) |

---

## Known Issues & Fixes

| Issue | Status | Fix |
|-------|--------|-----|
| TSLA Alpaca 403 | Known — bot cannot sell TSLA | Manual close required |
| Capitol Trades unreachable | Fixed April 10 | Replaced with SEC EDGAR Form 4 |
| AUDIO LIMIT order stuck | Fixed April 10 | MARKET orders + stale cancel |
| Claude JSON parse fail | Fixed — robust parser | In thesis_manager.py |
| Flask must bind before trading loop | Fixed | `_delayed_trading_start()` |
| Stale Binance cache phantom sells | Fixed | Live balance check before every sell |
| run_exit_monitor missing record_trade | Fixed April 10 | All exits now record to history |
| ZoneInfo missing from market_data | Fixed April 10 | Added to module imports |
| _intelligence in ai_clients clash | Fixed April 10 | Injection only in bot orchestrator |
| get_cash_thresholds not defined | Fixed April 10 | sleep_manager injection moved to late block (after function defined) |
| get_spy_trend not in market_data module | Fixed April 10 | get_spy_trend imported from market_data — available early |

---

## Key Learnings

- **Infrastructure bugs caused early losses**, not strategy failures. Math on 1:2 R/R (stocks) and 1:2.5 R/R (crypto) is sound.
- **Injection order matters** — functions must be defined before passed as references to modules. Early injections (after log()) can only use functions already defined. Late injections (before run_cycle) can use everything.
- **Never put cross-module injections inside extracted modules** — caused `_intelligence` NameError.
- **Each extracted module needs ALL its own imports** — ZoneInfo, json, etc. not inherited from bot.
- **Stale API cache causes phantom sell errors** — always check live balance before every sell.
- **LIMIT orders on crypto cause PRICE_FILTER rejections** — all sell paths use MARKET orders.
- **stepSize must be fetched dynamically** — hardcoded quantities cause order rejections.
- **Both AIs sleep together** — solo watch mode was wrong architecture.
- **TSLA is Alpaca-restricted** — bot gets 403 on TSLA sells. Always watch manually.
- **Self-repair auto-merge needs syntax validation** — merging broken code crashed bot.

---

## Rotation Mode (Crypto)

When USDT hits zero but coins are held:
1. Identify weakest holding by rotation score
2. Sell it → generates USDT
3. Immediately buy strongest breakout opportunity
4. New coin must project >1.5% gain after fees
5. Never rotate a profitable position — only rotate losers

---

## Sleep Brief System

Before sleeping, AI writes JSON with per-position instructions:
```json
{
  "portfolio_assessment": "...",
  "stocks": {
    "NVDA": {
      "action": "HOLD",
      "thesis": "Momentum building, RSI healthy",
      "emergency_below": 157.50,
      "bullish_above": 200.0,
      "time_review_hrs": 24,
      "circuit_breaker": 157.50,
      "bot_approved_action": null
    }
  },
  "crypto": {
    "DOTUSDT": {
      "action": "HOLD",
      "emergency_below": 1.18,
      "bot_approved_action": null
    }
  }
}
```

Bot checks thesis conditions every 5 minutes and wakes AI with context when triggered.

---

## Roadmap

### Phase 4 — Portfolio Manager (next)
Extract `portfolio_manager.py` from bot:
- `get_trading_pool`, `update_gain_metrics`, `record_trade`
- `rebalance_allocations`, `format_gains`, `track_pnl`

### Phase 5 — Trade Executor (last, highest risk)
Extract `trade_executor.py`:
- `smart_sell`, `execute_trades`, `collaborative_session`
- `run_autopilot`, `run_low_cash_cycle`, `execute_watchlist`

### Future Features
- **Pattern Engine** — detect 15-20 bull/bear technical patterns + candlestick patterns
- **AI-Driven Feature Selection** — each AI picks its own data sources per cycle (check & balance)
- **Trade History Learning** — after 50-100 closed trades, AIs extract winning patterns
- **Market Regime Detection** — VIX + breadth + SPY distance for regime-aware sizing
- **Correlation Matrix** — avoid holding two highly correlated stocks simultaneously
- **News Sentiment Scoring** — -1 to +1 sentiment score injected into AI context

---

## Development History

| Version | Date | What Changed |
|---------|------|-------------|
| v1.0 | Early 2026 | Basic single-AI bot, basic stop/TP |
| v1.5 | Early 2026 | Dual-AI collaboration (Claude + Grok), 3-round system |
| v2.0 | Early 2026 | Crypto integration (Binance.US), sleep/wake cycles, projection engine |
| v2.5 | Early 2026 | Parse fixes, MARKET orders, stepSize dynamic fetch, gains tracking |
| v3.0 | April 7 | AI-led architecture, thesis_manager, wallet_intelligence, sleep brief system |
| v3.1 | April 10 | Modular refactor (Phase 1-3), SEC EDGAR, Phase 1 features, all exit paths record |
