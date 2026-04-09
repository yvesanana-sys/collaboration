# NovaTrade — Master Reference Document
*Last updated: April 7, 2026*

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

---

## Files — What Each One Does

| File | Purpose |
|------|---------|
| `bot_with_proxy.py` | Main bot — stock trading, AI sessions, Flask server, sleep/wake logic |
| `binance_crypto.py` | Crypto engine — 24/7 Binance.US trading, exit monitor, wallet reader |
| `prompt_builder.py` | AI prompt construction, adaptive memory/lessons system |
| `projection_engine.py` | 5-layer price projection model for stocks |
| `thesis_manager.py` | v3.0 — AI sleep brief writer, custom wake conditions, position thesis storage |
| `wallet_intelligence.py` | v3.0 — Cross-portfolio scanner, opportunity ranker, rotation finder |

**Files to NEVER delete from GitHub:**
- All 6 above must always be present
- `v3_patches.py` — delete this (it was docs only, not needed on Railway)

---

## Architecture — How It Works

### Sleep/Wake Cycle
AIs sleep after executing trades. Bot runs autonomously. 7 wake conditions:

1. Cash crosses active threshold
2. All positions closed + cash available
3. 2+ stop-losses fire (emergency)
4. 8:30am premarket (always runs daily)
5. SPY drops >2% suddenly (crash guard)
6. AI custom wake instructions (price triggers AI sets before sleeping)
7. **[v3.0]** Thesis conditions — AI-written per-position triggers

### v3.0 AI-Led Architecture (current)
- AI writes a **sleep brief** (JSON) before sleeping with custom wake conditions per position
- Bot monitors every 5 minutes against thesis conditions
- Bot wakes AI with rich context when conditions trigger
- **Crypto never auto-exits** without AI approval — prevents wick stop-outs
- Stocks auto-exit only at hard -10% circuit breaker if no thesis
- Bot is dumb executor — AI approves ALL trades

### Dual AI Collaboration (3 rounds)
- **Round 1**: Both AIs propose trades independently
- **Round 2**: Each AI reviews the other's proposals autonomously
- **Round 3**: Collaborative big-ticket gate (locked until $3,000 equity)

---

## Trading Rules

### Stocks (Alpaca)
| Rule | Value |
|------|-------|
| Stop loss | -10% |
| Take profit | +20% |
| Max positions | 2 |
| Daily loss limit | 5% |
| Strategy A | Fixed TP (breakout entries) |
| Strategy B | Trailing stop (momentum — NVDA always uses B) |
| Trail activates | +10% gain |
| Trail pct | 8% from peak (12% volatile stocks, 6% stable) |
| Time stop | 5 days |

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
| Order type | MARKET (converted from LIMIT to avoid PRICE_FILTER errors) |

### Crypto Tiers
| Tier | Wallet Size | Risk % | Max Positions | Universe |
|------|------------|--------|---------------|---------|
| 1 | <$100 | 45% | 1 | BTC/SOL only |
| 2 | $100-$300 | 35% | 2 | Top 5 coins |
| 3 | $300-$500 | 25% | 3 | All universe |
| 4 | $500+ | 20% | 4 | All universe |

### Autonomy Tiers (Stocks)
| Tier | Equity | Claude Budget | Grok Budget |
|------|--------|--------------|-------------|
| 1 | $150 | $25 | $25 |
| 2 | $300 | $50 | $50 |
| 3 | $600 | $100 | $100 |
| 4 | $1,200 | $200 | $200 |
| 5 | $2,000 | $300 | $300 + shorts |

---

## Crypto Universe (Binance.US)

BTC, ETH, SOL, AVAX, DOGE, LINK, ADA, DOT, ALGO, NEAR, KAVA, MATIC, ATOM, FET, UNI, PEPE, AVAX

---

## Technical Indicators Computed

RSI-14, MACD (12/26/9), EMA 9/21, SMA 20/50, Bollinger Bands, ATR-14, Volume Ratio, OBV trend

---

## Known Issues & Fixes

| Issue | Status | Fix |
|-------|--------|-----|
| TSLA Alpaca 403 | Known — bot cannot sell TSLA | Manual close required |
| AUDIO LIMIT order 400 error | Fixed — MARKET orders used | Already in code |
| Claude JSON parse fail (compressed keys) | Fixed — robust parser | In thesis_manager.py |
| Sleep brief parse fail (markdown fences) | Fixed April 7 | In thesis_manager.py |
| Flask must bind before trading loop | Fixed — `_delayed_trading_start()` | In code |
| Stale Binance cache phantom sell errors | Fixed — live balance check before every sell | In code |
| BTCUSDT "projection not viable" | Expected — range too tight, protecting capital | Normal behavior |

---

## Key Learnings

- **Infrastructure bugs caused early losses**, not strategy failures. Math on 1:2 R/R (stocks) and 1:2.5 R/R (crypto) is sound.
- **Stale API cache** causes phantom sell errors — always check live balance via `/api/v3/account` before sells.
- **LIMIT orders on crypto** cause PRICE_FILTER rejections — all sell paths use MARKET orders.
- **stepSize must be fetched dynamically** — hardcoded quantities cause order rejections.
- **Both AIs sleep together** — early architecture had Claude solo watching, which was wrong.
- **TSLA is Alpaca-restricted** — bot gets 403 on TSLA sells. Always watch manually.

---

## Rotation Mode (Crypto)

When USDT hits zero but coins are held:
1. Identify weakest holding by rotation score
2. Sell it → generates USDT
3. Immediately buy strongest breakout opportunity
4. New coin must project >1.5% gain after fees
5. Never rotate a profitable position — only rotate losers

---

## Sleep Brief System (v3.0)

Before sleeping, AI writes JSON with per-position instructions:
```json
{
  "portfolio_assessment": "...",
  "stocks": {
    "TSLA": {
      "action": "HOLD",
      "thesis": "Oversold RSI, hold above stop",
      "emergency_below": 343.61,
      "bullish_above": 360.0,
      "time_review_hrs": 24,
      "circuit_breaker": 305.0,
      "bot_approved_action": null
    }
  },
  "crypto": {
    "AVAXUSDT": {
      "action": "HOLD",
      "emergency_below": 7.5,
      "bot_approved_action": null
    }
  }
}
```

Bot checks thesis conditions every 5 minutes and wakes AI with context when triggered.

---

## API Endpoints (Railway URL)

| Endpoint | What It Returns |
|----------|----------------|
| `/status` | Full portfolio snapshot |
| `/history` | Trade history |
| `/performance` | Win rate, P&L analytics |
| `/projections` | Stock projection engine output |
| `/crypto_status` | Binance.US positions and wallet |
| `/prompt_memory` | AI lessons learned |
| `/deploy` | Push files to GitHub (POST) |

---

## Current Portfolio (as of April 7, 2026)

### Stocks
| Symbol | Entry | Current | P&L | Notes |
|--------|-------|---------|-----|-------|
| NVDA | $175.00 | ~$175 | ~+0% | Holding, MACD bullish |
| TSLA | $343.86 | ~$342 | ~-0.4% | New lower entry after averaging down from $381.79 |

### Crypto
| Symbol | Entry | Current | P&L | Notes |
|--------|-------|---------|-----|-------|
| AVAX | $8.49 | ~$8.54 | ~+0.6% | Overnight position, TP=$9.05 |
| AVAX | $8.52 | ~$8.54 | ~+0.2% | Second position from market open |

### Wallet
- USDT free: ~$10.92 (at reserve floor)
- Total crypto wallet: ~$35.40
- Stock cash: ~$24.36

---

## Pending Manual Actions

- [ ] Watch TSLA — bot cannot sell due to 403, must close manually if it breaks $336
- [ ] Claim FET staking rewards on Binance.US
- [ ] Add $65 USDT to Binance.US to unlock Tier 2 (2 positions, $35 trade size)

---

## Grok Research Sources

Twitter/X, Reddit (r/CryptoCurrency, r/CryptoMoonShots, r/Bitcoin), CoinDesk, CoinTelegraph, Decrypt, The Block, Whale Alert, Lookonchain, CoinGecko/CMC trending

---

## Development History (Sessions)

1. **v1.0** — Basic single-AI bot, basic stop/TP
2. **v1.5** — Dual-AI collaboration (Claude + Grok), 3-round system
3. **v2.0** — Crypto integration (Binance.US), sleep/wake cycles, projection engine
4. **v2.5** — Parse fixes, MARKET orders, stepSize dynamic fetch, gains tracking
5. **v3.0** — AI-led architecture: thesis_manager.py + wallet_intelligence.py, AI approves all trades, sleep brief system, cross-portfolio opportunity scanner

## Bug Fix Log

### Bug Fix — 2026-04-09
**Error:** [BEHAVIORAL] SPY price always $0.00 — indicator fetch broken (6 zero-reads/hour)
**Root Cause:** Missing initialization of projection accuracy tracking state dictionary keys in `shared_state`. The `track_projection_accuracy()` function references `shared_state["proj_total_count"]`, `shared_state["proj_hit_count"]`, and `shared_state["last_projections"]` which were never initialized, causing cascading failures in indicator/price fetch logic that depends on this state.
**Fix:** Added 4 new keys to `shared_state` dict initialization:
  - `"proj_total_count": 0` — count of projections made
  - `"proj_hit_count": 0` — count of accurate projections
  - `"proj_accuracy_pct": 0.0` — rolling accuracy percentage
  - `"last_projections": {}` — cache of last symbol projections
**File:** `bot_with_proxy.py` (lines 340–344 in shared_state initialization)
**Status:** ✅ Minimal fix applied — 4-line addition, no logic changes
