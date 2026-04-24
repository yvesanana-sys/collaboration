# NovaTrade — Master Reference Document
*Last updated: April 24, 2026*

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
| Order type | MARKET default for all sells (LIMIT available via `force_limit=True`) |

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
| ETH phantom sell order stuck unfilled (`filled=0.0000`) | Fixed April 24 — `place_crypto_sell` defaulted to LIMIT at price × 1.0015; now defaults to MARKET | `binance_crypto.py` |
| AI sell decisions re-stacking limit orders every cycle | Fixed April 24 — cancel open orders + refresh wallet before placing new sell | `binance_crypto.py` |
| AI returns bare asset name (`UNI`, `ETH`) → 400 Bad Request | Fixed April 24 — auto-append `USDT` if symbol lacks quote suffix | `binance_crypto.py` |
| Buy `INSUFFICIENT_BALANCE` on full-notional MARKET orders | Fixed April 24 — 0.5% safety buffer in Method 2 fallback; stricter Method 1 success detection via `orderId` | `binance_crypto.py` |

---

## Key Learnings

- **Infrastructure bugs caused early losses**, not strategy failures. Math on 1:2 R/R (stocks) and 1:2.5 R/R (crypto) is sound.
- **Stale API cache** causes phantom sell errors — always check live balance via `/api/v3/account` before sells.
- **LIMIT orders on crypto cause two failure modes** — PRICE_FILTER rejections if price is off-tick, and unfilled resting orders if price is above market. `place_crypto_sell()` now defaults to MARKET; LIMIT requires explicit `force_limit=True`.
- **Always cancel existing open orders for a symbol before placing a new sell.** Without this, prior unfilled limits leave balance locked (`🔒` in wallet), and the new order either fails on LOT_SIZE or stacks above market. After cancel, wait ~500ms and re-read wallet so the freed balance is usable.
- **stepSize must be fetched dynamically** — hardcoded quantities cause order rejections.
- **Both AIs sleep together** — early architecture had Claude solo watching, which was wrong.
- **TSLA is Alpaca-restricted** — bot gets 403 on TSLA sells. Always watch manually.
- **Trust the logs over the master doc when they disagree.** If a log shows `SELL ... filled=0.0000` reappearing across snapshots, that's a stale-limit problem, not a normal pending order.

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

## Current Portfolio (as of April 24, 2026 — ~08:24 ET snapshot)

### Stocks (equity ~$53.22, cash ~$20.32, day P&L +$1.21 / +2.33%)
| Symbol | Entry | Current | P&L | Notes |
|--------|-------|---------|-----|-------|
| AMD  | $289.79 | ~$343.56 | +18.55% ($+1.91) | Grok pick, strat A |
| META | $627.80 | ~$663.23 | +5.64%  ($+0.56) | Grok pick, strat A |
| NVDA | $199.85 | ~$201.24 | +0.70%  ($+0.07) | Grok pick, strat A |

### Crypto (wallet ~$72.96, USDT free ~$13.96, day realized P&L $0.00)
| Symbol | Entry | Current | P&L | Notes |
|--------|-------|---------|-----|-------|
| AUDIOUSDT | $0.0203 | $0.02259 | +11.28% | Bot-tracked, stop $0.022328, TP $0.0254, held ~13h |

### Wallet holdings (non-tradeable / small)
ETH 0.0062 ($14.48), UNI 4.15 ($13.59), KAVA 7.09 ($0.44), NEAR, ALGO, DOGE, LTC, AVAX, SOL, FET, RVN, SHIB, DOT — most under $0.50 dust.

### Health note
Last observed bug: stale SELL ETHUSDT limit order at $2333.02 (0.0061 ETH) sitting unfilled and re-appearing in snapshots. Root cause fixed April 24 (MARKET default + cancel-before-sell). One-time manual cancel of the stuck order on Binance.US required once to release locked balance.

---

## Pending Manual Actions

- [ ] **Cancel stuck SELL ETHUSDT limit order** (0.0061 @ $2333.02) on Binance.US — one-time, to release locked balance before the April 24 patch runs
- [ ] **Deploy updated `binance_crypto.py`** to Railway (MARKET default + cancel-before-sell — April 24 patch)
- [ ] Watch TSLA if it returns — bot cannot sell due to Alpaca 403, must close manually
- [ ] Claim FET staking rewards on Binance.US (~small USDT amount)
- [ ] Add funding to Binance.US to unlock Tier 2 when ready

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

---

## Bug Fix Log

### Bug Fix — 2026-04-24 (Auto-repair)
**Error:** NameError — `name 'st' is not defined` in `get_crypto_24h_stats()` function signature
**Root cause:** Incomplete function signature — parameter type hint was cut off mid-word (`st` instead of `str`)
**Fix:** Completed the function signature from `def get_crypto_24h_stats(symbol: st` → `def get_crypto_24h_stats(symbol: str) -> dict:`
**File:** binance_crypto.py
**Impact:** Module would not import; any code calling `get_crypto_24h_stats()` would crash with NameError at module load time
**Status:** ✅ Fixed — file is now syntactically valid and complete


### 🔧 Bug Fix — 2026-04-24 (pt 2) — Symbol Normalization + Buy Safety Buffer
**Errors (observed live after first patch deployed):**
1. `❌ Sell error for UNI: ... symbol=UNI&side=SELL ...` → 400 Bad Request. Claude's `sell_decisions` returned `"symbol": "UNI"` (bare asset) instead of `"UNIUSDT"`. Binance has no `UNI` pair → rejection.
2. `❌ Order failed for KAVAUSDT: ... quantity=238.0 ...` → 400 Bad Request. `place_crypto_buy` Method 2 fallback computed qty exactly at balance (`238 × $0.05936 ≈ $14.13 free`), and a tiny price tick between price-fetch and market-fill pushed actual cost over available → `INSUFFICIENT_BALANCE`.

**Root cause:**
1. `sell_decisions` parsing at line ~2770 used AI-returned symbol verbatim. AI output is inconsistent — sometimes `"UNIUSDT"`, sometimes just `"UNI"`. Same issue applies to buy paths if AI ever returns bare assets there.
2. `place_crypto_buy` Method 2 used full `notional_usdt / price` with `math.floor`. At the exact boundary (qty × price == free balance), any positive price slippage triggers rejection. Also, Method 1 success check (`"error" not in str(result).lower()`) could false-negative on valid orderIds that contain "error" elsewhere in response, and false-positive on Binance error responses.

**Fix:**
1. Symbol normalization at three entry points (`sell_decisions` parse, `crypto_trades` parse, `execute_from_r1`): if symbol doesn't end in `USDT`/`USDC`/`BUSD`/`USD`, append `USDT`. Also `.upper().strip()` for good measure.
2. `place_crypto_buy` Method 1: success detection now checks `isinstance(result, dict) and result.get("orderId")` — explicit, can't false-anything.
3. `place_crypto_buy` Method 2: apply `effective_notional = notional_usdt * 0.995` (0.5% safety buffer) before qty computation. Also re-validates buffered qty still meets $10 min_notional. Better to slightly under-spend than get rejected.

**Files:** `binance_crypto.py` (4 edits in one file)
**Status:** ✅ Syntax-validated

---

### 🔧 Bug Fix — 2026-04-24 — ETH Phantom Sell Order / Stacked Limits
**Error:** `SELL ETHUSDT 0.0061 @ $2333.020000 filled=0.0000` appearing in every 5-min snapshot; 0.0047 ETH shown as 🔒 locked in wallet; new limit placed each AI cycle on top of existing unfilled ones.

**Root cause (two compounding bugs):**
1. `place_crypto_sell()` defaulted to **LIMIT** order at `current_price * 1.0015` despite the master doc claiming all sell paths use MARKET. Resting limits above market sit unfilled forever (or until a wick).
2. The AI sell-decisions path and rotation pre-sell path did **not cancel existing open orders** before placing a new sell. The `_execute_exit` path did cancel first, but it only runs for bot-tracked `self.positions` — wallet holdings like ETH (not tracked by the bot) never got their stale orders cleared. Result: every AI cycle that voted to sell ETH re-placed another unfilled limit, and the locked balance from the prior order blocked the new one from using the full free qty.

**Fix:**
1. `place_crypto_sell(symbol, qty, limit_price=None, force_limit=False)` — now defaults to MARKET. Keeps the live-balance check and `ZERO_BALANCE` / `QTY_ROUNDED_TO_ZERO` safety returns. LIMIT path only fires when caller explicitly passes `force_limit=True`.
2. AI sell-decisions block (~line 2785): before selling, call `get_open_crypto_orders(sym)`, cancel each via `cancel_crypto_order`, sleep 500ms, re-read `get_full_wallet()` so freed balance is picked up. Then just call `place_crypto_sell(sym, qty)` with no price.
3. Rotation pre-sell block (~line 2990): same cancel-before-sell pattern.
4. `_execute_exit()`: removed dead `sell_price` computation and fee-aware floor logic (MARKET orders ignore any price passed). Replaced with an informational log warning when exiting below `entry + fees + 0.5%`, so loss exits are still visible but the code doesn't pretend to control the fill.

**Files:** `binance_crypto.py` (3 edits in one file)
**Manual step before deploy:** cancel the stuck SELL ETHUSDT 0.0061 @ $2333.02 order on Binance.US to release locked balance.
**Status:** ✅ Syntax-validated, ready to push

---

### Bug Fix — 2026-04-22
**Error:** NameError — `get_crypto_24h_stats` function signature incomplete (truncated at parameter list)
**Root Cause:** File was truncated mid-function definition at line containing `def get_crypto_24h_stats(symbol: st` — parameter type hint and entire function body missing
**Fix:** Completed the function signature `def get_crypto_24h_stats(symbol: str) -> dict:` and added full function body to return 24h stats dictionary from Binance API
**File:** binance_crypto.py
**Status:** ✅ PR opened


### 🌍 Discovery Mode Enabled — 2026-04-22 12:57 UTC — FULL MARKET ACCESS
**Type:** Configuration + prompt change
**File changed:** `binance_crypto.py`
**Rationale:** Previously bot scanned top 10 gainers across full Binance.US market but could only BUY coins from the restricted tier list (8 coins at Tier 1). Now Discovery Mode is enabled — AI can buy ANY coin from the market scan if confidence ≥ 70%.

**What changed:**

1. **Tier coin restrictions REMOVED** — all tiers now have `"coins": None`
   - Tier 1-4 all allow full market access
   - AI picks winners from the top 10 scan regardless of equity tier

2. **Strategy prompt aligned with aggressive scalping**
   - Old: "30-80% profit target" (conflicted with 8% TP)
   - New: "8% take-profit, bank wins fast, redeploy capital"

3. **TASK section updated**
   - Added: "DISCOVERY: ★ NEW coins trending in market scan with strong setup? → BUY"
   - Changed "hold to 50-80% target" → "hold 3%+ with momentum, trail protects"
   - Changed "down 15%+" → "down 5%+" (matches new tighter stop)

**Safety remains:**
- Max 3 positions at Tier 1 (limits exposure)
- 30% risk per trade max (damage cap)
- Still needs volume > $500K (avoids illiquid rug pulls)
- Still needs confidence ≥ 60% baseline (70% for discoveries)
- Global 40% drawdown pause unchanged

**Expected behavior:**
- Bot scans all USDT pairs on Binance.US every 5 min
- AI sees top 10 movers with ★ NEW tags on non-universe coins
- If PEPE is +25% with volume spike → AI can recommend buy
- Bot executes without tier restriction blocking it

---



### 🪙 Crypto Tier Expansion — 2026-04-22 12:35 UTC — MATCH STOCK AGGRESSIVE PROFILE
**Type:** Configuration change
**File changed:** `binance_crypto.py`
**Rationale:** Crypto tiers were too restrictive at small equity (BTC/SOL only, 1 position). Bot had 3 crypto holdings already but could only open new positions in 2 coins. This blocked the aggressive scanning strategy. Expanded to match stock tier philosophy — more scanning opportunities, controlled position sizing.

**Crypto tier changes:**

| Tier | Equity | Before (coins) | After (coins) | Max positions | Risk |
|------|--------|----------------|---------------|---------------|------|
| 1 | $0-$150 | BTC, SOL (2) | BTC, ETH, SOL, AVAX, LINK, DOGE, XRP, ADA (8) | 1 → 3 | 45% → 30% |
| 2 | $150-$300 | BTC, ETH, SOL (3) | + DOT, MATIC, NEAR, FET (12) | 2 → 3 | 35% → 25% |
| 3 | $300-$600 | + AVAX, ADA (5) | + LTC, ATOM, ALGO, UNI, SHIB (17) | 2 → 4 | 25% → 20% |
| 4 | $600+ | Full universe | Full universe (23 coins) | 3 → 5 | 18% → 15% |

**Risk per trade scaled down** since scanning more coins:
- More opportunities means bot picks better setups
- Smaller per-position risk keeps losses manageable
- R/R still favorable: 8% TP vs -8% stop = 1:1 at matched win rate

**Why this matters:**
- Previous: 3 existing crypto holdings managed but only BTC/SOL scanned for new buys
- Now: 8 coins actively scanned for new entries at Tier 1
- Bot can rotate between diverse setups instead of forcing BTC/SOL buys
- More data points for pattern learning

---



### 📈 Tier Expansion — 2026-04-21 17:48 UTC — BROADER STOCK SCANNING
**Type:** Configuration change
**File changed:** `portfolio_manager.py`
**Rationale:** Expanded stock tiers to scan more opportunities while keeping position sizing controlled. Supports aggressive scalping strategy with more setups to choose from.

**Tier changes:**

| Tier | Equity | Before (focus) | After (focus) | Max positions |
|------|--------|----------------|---------------|---------------|
| 1 | $0-$150 | TSLA, NVDA (2) | TSLA, NVDA, AMD, META, PLTR, COIN, SOFI, RKLB (8) | 1 → 3 |
| 2 | $150-$300 | TSLA, NVDA, AMD (3) | + MSTR, AMZN (10) | 2 → 3 |
| 3 | $300-$600 | TSLA, NVDA, AMD, META, PLTR (5) | + COIN, SOFI, RKLB, MSTR, AMZN, GOOGL, AAPL, MSFT, NFLX (14) | 2 → 4 |
| 4 | $600+ | Full universe | Full universe | 3 → 5 |

**Risk adjustments (scaled down since scanning more):**
- Tier 1: 35% → 30% risk per position
- Tier 2: 30% → 25% risk per position
- Tier 3: 25% → 20% risk per position
- Tier 4: 20% → 15% risk per position

**Volatile stock list expanded:**
Added PLTR, NFLX to volatile list (get 4% trail instead of 2% trail)

**Expected impact:**
- 4x more stocks scanned each cycle at current equity
- 3x more simultaneous positions possible
- Better diversification of trade setups while keeping concentrated sizing
- More data points for bot learning and self-repair stress testing

---



### 🎯 Strategy Shift — 2026-04-21 17:37 UTC — AGGRESSIVE SCALPING PROFILE
**Type:** Configuration change (not a bug fix)
**Files changed:** `binance_crypto.py`, `portfolio_manager.py`
**Rationale:** Moved from swing trading (big wins, long holds) to aggressive scalping (many small wins, fast turnover). Stress-tests auto-repair system with higher trade volume.

**Crypto changes (binance_crypto.py):**
| Parameter | Before | After | Effect |
|-----------|--------|-------|--------|
| stop_loss_pct | -20% | -8% | Smaller losses |
| take_profit_pct | +50% | +8% | Bank wins 6x faster |
| trail_activate_pct | +30% | +3% | Trailing kicks in 10x sooner |
| trail_pct | 40% | 2.5% | Lock 97.5% of gain (was 60%) |
| max_hold_hours | 72h | 24h | Exit stale trades 3x faster |
| min_confidence | 65 | 60 | More entries |
| max_positions | 2 | 3 | More simultaneous trades |

**Stock changes (portfolio_manager.py):**
| Parameter | Before | After | Effect |
|-----------|--------|-------|--------|
| exit_A_take_profit | +20% | +8% | 2.5x faster profit-taking |
| exit_A_stop_loss | -10% | -5% | Tighter risk control |
| exit_B_trail_default | 8% | 2.5% | Tighter trail |
| exit_B_trail_activates | +10% | +4% | Trailing activates 2.5x sooner |
| exit_B_time_stop_days | 5 | 3 | Free capital faster |
| min_confidence | 75 | 70 | More entries |
| max_positions | 2 | 3 | More simultaneous trades |

**R/R math:**
- Stocks: 8% TP / 5% stop = **1.6:1 win/loss ratio** (wins bigger than losses)
- Crypto: 8% TP / 8% stop = **1:1** (requires 55%+ win rate to profit)

**Expected trade volume increase:**
- Stocks: ~2x more entries (lower confidence + higher position count)
- Crypto: ~3x more entries (lower confidence + higher positions + faster turnover)
- Total: ~2.5x more trades = more stress on self-repair system

**Monitoring:**
- Win rate — critical, needs 55%+ on crypto, 45%+ on stocks
- Fee drag — at 0.02% round-trip, 100 trades = 2% drag
- Self-repair activation rate — expecting more edge-case bugs
- Compound growth rate — compare vs. prior moderate profile

---



*Auto-updated by self_repair.py and claude_code_trigger.py after every repair.*  
*Format: severity | file | result | duration | what was fixed*

<!-- New entries added automatically above this line -->

---

## Claude Code Repair System

NovaTrade uses a 3-layer autonomous repair system:

**Layer 1 — self_repair.py** (in-process)
- Scans every log line in real time
- Classifies: WARN / ERROR / CRITICAL
- Fixes known patterns via GitHub API (syntax patches, single-line fixes)
- Escalates to Claude Code after 3 failures or immediately on CRITICAL

**Layer 2 — claude_code_trigger.py** (escalation)
- Wakes Claude Code SSH service via Railway API
- Writes repair job to /data/repair_queue.json with full context
- Logs all repair activity to /data/repair_log.json permanently
- Updates this master doc after every completed repair

**Layer 3 — Claude Code SSH** (Railway service)
- Runs repair_agent.sh on boot
- Reads all 18 files for full codebase context
- Writes fix → tests syntax → pushes to GitHub
- Verifies fix via /health polling for 120 seconds
- Reverts immediately if fix causes new crash (max 3 attempts)
- Suspends itself when done to save Railway credits

**Sunday 3am ET** — scheduled weekly maintenance:
- Full audit of all 18 files
- Missing shared_state keys, datetime guards, circular imports
- Silent exception handlers, hardcoded values
- Results logged here automatically

**Volume files:**
- `/data/repair_log.json` — full repair history
- `/data/repair_queue.json` — current/pending repair job
- `/data/repair_state.json` — idle / active / maintenance
