# NovaTrade — Master Reference Document
*Last updated: May 8, 2026*

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
| `bot_with_proxy.py` | Main bot — orchestrator, Flask server, sleep/wake logic, route handlers |
| `binance_crypto.py` | Crypto engine — 24/7 Binance.US trading, exit monitor, wallet reader, AI competition logic |
| `prompt_builder.py` | AI prompt construction, persistent memory/lessons system, AI personas, brain stats |
| `portfolio_manager.py` | Trade history persistence, Binance fill sync, reserve scaling, gain metrics, performance analytics |
| `sleep_manager.py` | AI sleep/wake state, custom wake conditions, restriction cleanup |
| `pdt_manager.py` | Pattern Day Trade tracking, intraday buy logging, hold council |
| `ai_clients.py` | Claude/Grok HTTP clients, JSON parser with truncation recovery, response normalizer |
| `market_data.py` | OHLCV bars, technical indicators (RSI/MACD/EMA/BB/ATR), SPY trend |
| `intelligence.py` | Market news, politician trades, dark pool flow, Twitter/X intel |
| `projection_engine.py` | 5-layer price projection model for stocks |
| `thesis_manager.py` | v3.0 — AI sleep brief writer, custom wake conditions, position thesis storage |
| `wallet_intelligence.py` | v3.0 — Cross-portfolio scanner, opportunity ranker, rotation finder |
| `core_reserve.py` | **[v3.1]** Long-term wealth compounder — BTC/SPY/Cash, walled off from tactical AIs, rule-based watcher with 4 contingencies |
| `ai_evolution.py` | **[v3.1.1]** Tier system for AI self-evolution — equal capability baseline, earns prompt customization through proven P&L |
| `strategic_brain.py` | **[v3.1.3 — Phase B ACTIVE]** Strategist layer with **Living Playbook** system — strategists write comprehensive standing orders that the bot + tacticians follow autonomously between scheduled activations. Conditional rule executor (zero AI cost). Stop-loss gate. Sentiment-aware. Self-defined wake conditions. Hard system limits enforce R/R ≥ 1:1. |
| `github_deploy.py` | Push files to GitHub from `/deploy` endpoint |
| `dashboard.html` | Live web dashboard — battle card, brain panel, core reserve, performance, history |

**Files to NEVER delete from GitHub:**
- All 14 `.py` modules above must always be present
- `dashboard.html` for the web UI
- `requirements.txt` for Railway dependencies
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

### AI Competition Mode (v3.1.1 — current)
Claude and Grok are **equal-capability autonomous traders** with separate capital pools, separate P&L tracking, and a head-to-head leaderboard. No specialty roles — both have full access to indicators, news, sentiment, on-chain data, and history. Their strategies emerge from earned P&L, not from our prejudgment.

- **Pool split:** Available USDT divided 50/50 between Claude and Grok at the start of each cycle (`CLAUDE_POOL_PCT`, `GROK_POOL_PCT` in `binance_crypto.py`).
- **Identical baseline prompts:** Both AIs receive the same system prompt with only the name swapped. No "technical analyst" vs "social/news" assignment. The earlier specialty baking has been removed from `prompt_builder.py` and `binance_crypto.py`.
- **Rivalry context injected each cycle:** Each AI's prompt includes concrete current standings ("You are LEADING/TRAILING by $X" or "tied at zero"), recent record (W/L), and anti-tilt guidance ("chasing losses is the #1 account killer"). This makes the leaderboard a real-time motivator without inducing revenge trades.
- **No more "shared" merge:** When both AIs pick the same coin, the higher-confidence proposal wins the slot. The losing AI doesn't get charged but also doesn't double-up. Each fill is tagged with its true `owner`.
- **Each AI sees only its own pool** as buying power. They cannot starve each other.
- **Per-AI position cap:** If Claude's slice runs dry mid-cycle, it skips the slot and Grok takes the next pick.
- **Leaderboard:** `/leaderboard` endpoint and dashboard battle card show each AI's wins/losses, win rate, total realized P&L, and current open positions. The leader is determined by realized P&L on closed crypto trades only.
- **Kill switch:** Set `ENABLE_AI_COMPETITION = False` to revert to the legacy shared-pool merge.

### AI Evolution Tier System (v3.1.1 — Pass A foundation)

Both AIs unlock the ability to modify their own prompts by accumulating closed-trade P&L. They start at Tier 0 (default neutral prompt) and earn customization tiers through proven performance. Lives in `ai_evolution.py`.**Tier ladder (must satisfy BOTH conditions to qualify):**

| Tier | Name | Min trades | Min P&L | Token cap | What unlocks |
|---|---|---|---|---|---|
| 0 | Probation | 0 | $0 | 800 | Default neutral prompt |
| 1 | Apprentice | 5 | $0 | 1,000 | 1-paragraph style notes |
| 2 | Journeyman | 15 | $5 | 1,500 | 2-paragraph strategy preferences |
| 3 | Strategist | 30 | $50 | 2,000 | 3-paragraph trading philosophy + indicator preferences |
| 4 | Autonomous | 100 | $500 | 3,000 | Full prompt rewriting + custom indicator combinations |

**Hard limits (NEVER editable, regardless of tier):**
- Max position size (existing wallet rules)
- Wallet reserve rule
- Stop-loss enforcement
- Fee floor on exits
- Core Reserve isolation
- Maximum prompt token cap (per tier)
- JSON schema requirement
- Banned-phrase regex (e.g. "ignore previous instructions", "world's best", "guaranteed", "never lose")

**Auto-revert protection (Pass B):** If a custom prompt has 5+ trades and win rate drops below 40% over the last 10 trades, soft revert removes the most recent change. After 3 consecutive soft reverts, hard revert to Tier 0 — AI must re-earn promotion.

**State persistence:** `/data/ai_evolution.json` — current tier per AI, custom prompt history, audit log of every promotion/proposal/revert (capped at 100 events per AI).

**Pass A (currently shipped):** Foundation only.
- Tier framework + persistent state
- Identical neutral prompts for both AIs
- Rivalry context injection
- `/evolution` endpoint exposing current tier and progress to next
- Dashboard role labels show tier + progress ("Tier 0 (Probation) · next: 5 more trades")

**Pass B (next session):** Self-modification loop.
- AIs proposing prompt changes every N cycles
- Validation layer enforcing token caps + banned phrases
- Soft-launch period (first 5 trades after promotion are advisory)
- Auto-revert mechanism
- Dashboard tier panel with proposal history

### Strategic Brain — Living Playbook Architecture (v3.1.3 — Phase B ACTIVE)

**Phase B is live as of v3.1.3.** The architecture has evolved from "occasional strategy review" to a **living playbook** system: strategists write comprehensive standing orders covering every situation the bot may encounter, then go offline. The bot and tacticians follow the playbook autonomously between activations.

**Strategists** (Claude + Grok strategists) — *don't trade*. They:
- Activate **3× daily on schedule** (pre-market 9am ET, post-close 4:30pm ET, crypto-close 9pm ET) plus on-demand wake triggers
- Read full trade history + **live sentiment + news + market regime context**
- Write a comprehensive **playbook** with conditional responses for every situation
- Define their own **wake conditions** — when to be recalled vs handled autonomously
- Write **tactician training notes** injected into every tactician prompt
- Use **smarter, more expensive models** because the work is high-leverage
- Collaborate (Claude + Grok) on Core Reserve decisions — both must agree to act

**Tacticians** (Claude + Grok tacticians) — *execute trades*. They:
- See the current playbook in **every prompt** (injected via `get_playbook_summary()`)
- Follow strategist's standing orders — entry/exit rules, conditional responses
- Can wake their strategist mid-day if a playbook condition fires
- Use **fast, cheap models** because the work is pattern matching

**Bot (executor)** — runs the playbook autonomously. Every cycle:
- Calls `execute_playbook()` — checks all conditional rules with **zero AI cost**
- Applies directives: block entries, halt altcoins, reduce position size, widen stops, etc.
- Wakes strategist only when playbook says to (not on every dip or single stop)

#### The Living Playbook — Conditional Standing Orders

Each playbook contains both normal parameters AND conditional responses the bot executes autonomously:

**Normal parameters** — entry logic, exit logic, stop_loss_pct, take_profit_pct, max_hold_hours, preferred indicators/symbols, max position size, max concurrent positions, min confidence, trail settings.

**Conditional responses** (bot executes without AI call):
- `on_stop_loss` — pause new entries for X minutes, log reason
- `on_two_stops_same_session` — go defensive, reduce position size, longer pause
- `on_three_consecutive_losses` — halt new entries, **wake strategist**
- `on_winning_streak_3` — hold size discipline (don't oversize on a streak)
- `on_btc_drops_3pct_1h` — halt altcoin entries, hold existing
- `on_btc_drops_5pct_1h` — close weakest position, **wake strategist**
- `on_spy_drops_2pct` — halt new stock entries
- `on_sentiment_extreme_fear` — widen stops 25% to avoid noise stops
- `on_sentiment_extreme_greed` — tighten TP 25% to take profits faster

**Self-defined wake conditions** — the playbook itself specifies when to recall the strategist:
- `consecutive_losses` (default 3)
- `session_drawdown_pct` (default 15%)
- `days_without_trade` (default 2)
- `btc_regime_flip` / `spy_regime_flip` (true/false)
- `win_rate_below_pct` (default 35% — needs 10+ trades)
- `predicted_vs_actual_gap` (default 25 percentage points)

**Hard system limits** — the bot enforces these regardless of what the playbook says:
- `consecutive_loss_gate`: 5 (forces wake, blocks entries)
- `drawdown_halt_pct`: 20% (forces wake, blocks entries)
- `stop_loss_pct`: 3–15% (validator rejects out-of-range)
- `take_profit_pct` ≥ `stop_loss_pct` (R/R ≥ 1:1 enforced)
- `max_position_pct_of_pool`: ≤ 50%
- `min_confidence`: ≥ 50%
- `max_hold_hours`: ≤ 168 (1 week)

#### Wallet-tiered model registry

The `MODEL_REGISTRY` in `strategic_brain.py` is the single source of truth for ALL model choices. Wallet-aware auto-upgrade:

| Role | AI | Default tier (wallet < $5K) | Premium tier (wallet ≥ $5K) |
|---|---|---|---|
| Strategist | Claude | Sonnet 4.6 ($3/$15 per 1M) | Opus 4.7 ($15/$75 per 1M) |
| Strategist | Grok | Grok 4.1 Fast Reasoning ($0.20/$0.50) | Grok 4 ($3/$15) |
| Tactician | Claude | Haiku 4.5 ($1/$5) | (no upgrade — speed matters more) |
| Tactician | Grok | Grok 4.1 Fast Reasoning ($0.20/$0.50) | (no upgrade — same as default) |

#### Stop-Loss Gate — How Stops Are Handled

When any stop fires, `handle_stop_loss_event()` runs **immediately** (no AI call):
1. Increments `shared_state["consecutive_losses"]` and `shared_state["stops_fired_today"]`
2. Reads the playbook's `on_stop_loss` rule — applies pause minutes, blocks entries
3. If `consecutive_losses >= 3` → triggers strategist wake with sentiment context
4. If `consecutive_losses >= 5` → HARD GATE (system override regardless of playbook)

The strategist, when woken by a stop, gets the full sentiment + news + execution log of which conditional rules fired. It writes an updated playbook based on that — not just the stop in isolation.

#### Cooldown — Preventing Wake Loops

`WAKE_COOLDOWN_MINUTES = 120` (system limit). Even if the playbook says wake, the strategist won't re-activate within 2 hours. This protects against ping-pong between strategist and tactician during fast markets, and gives the playbook time to actually run before being replaced.

#### Three-phase rollout — current status

- **Phase A (v3.1.2):** Foundation only. Plumbing wired, dormant. ✅ Complete.
- **Phase B (v3.1.3 — current):** Living playbook ACTIVE. Strategists write standing orders, bot executes them autonomously, sentiment context wired, R/R enforced ≥ 1:1, dual-AI playbook gates new entries via risk_gate.
- **Phase C (next):** Strategists take over Core Reserve decisions (collaborative — both must agree). Hard-rule floors stay in place as catastrophic protection.

#### Persistence

`/data/strategy_claude.json` and `/data/strategy_grok.json` — per-AI strategy state including full playbook, current performance vs prediction, strategy history, **playbook execution log** (which conditional rules fired and when), audit log of every activation. Survives redeploys.

### Wallet-Scaling Reserve Rule (v3.1)
Both stock and crypto sides honor a unified reserve that scales with combined wallet value:

| Combined wallet | Reserve % | Tradeable % |
|---|---|---|
| < $1,000 | 0% (full freedom) | 100% |
| $1,000 – $1,999 | 10% | 90% |
| $2,000 – $2,999 | 11% | 89% |
| ... | ... +1% per $1,000 ... | ... |
| $20,000 – $20,999 | 29% | 71% |
| $21,000+ | 30% (cap) | 70% |

`get_wallet_reserve_pct(combined_wallet)` lives in `binance_crypto.py` and is the single source of truth — both `binance_crypto` (USDT pool sizing) and `portfolio_manager` (stock trading pool) call it. Below $1k the AIs trade 100% of available capital; above $1k the reserve ramps up to protect accumulated gains.

### Core Reserve (v3.1) — Long-term Compounder
A walled-off long-term wealth layer that activates at $1,000 combined wallet. **Tactical AIs cannot see it or trade against it.** Lives entirely in `core_reserve.py`.

**Allocation:**
- 50% BTC (Binance.US)
- 30% SPY (Alpaca)
- 20% USDT cash (Binance.US — emergency liquidity + dry powder)

**Funding:** As wallet crosses each $1,000 threshold, the reserve grows by the incremental %. Money is split per allocation. Once deposited, the BTC and SPY positions are intended to compound long-term.

**Four contingency triggers** (run hourly, rule-based, no AI calls):

1. **Defensive trim** — BTC -20% or SPY -15% over 7 days → sell 30% of position (72h cooldown)
2. **Opportunity buy** — BTC -30% from ATH or SPY -20% AND RSI < 30 → deploy 50% of cash slice (1-week cooldown)
3. **Take-profit trim** — BTC +50% or SPY +30% from entry → trim 20% to cash (1-week cooldown)
4. **Drift rebalance** — any slice drifts >15% from target → rebalance to target weights (30-day cooldown)

**Persistence:** Full state saved to `/data/core_reserve.json` — entry prices, ATH per slice, last-trigger timestamps, audit log of all events (capped at 200).

**Trade tagging:** All Core Reserve orders tagged with `owner="core_reserve"` so they appear in trade history but never pollute the Claude vs Grok leaderboard.

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

## Persistent Storage (Railway `/data` volume)

All learning, history, and state survives redeploys via the Railway volume mount at `/data`:

| File | Purpose | Module |
|---|---|---|
| `/data/trade_history.json` | Every buy/sell ever executed (stocks + crypto), tagged by owner | `portfolio_manager.py` |
| `/data/binance_trade_history.json` | Binance.US fill history from `/api/v3/myTrades` (last 6 months) | `portfolio_manager.py` |
| `/data/ai_memory.json` | **AI learned lessons** — symbol win rates, persona stats, regime performance | `prompt_builder.py` |
| `/data/shared_state.json` | AI sleep state, cash thresholds, gain baselines, allocations | `portfolio_manager.py` |
| `/data/sleep_state.json` | Sleep/wake timing | `sleep_manager.py` |
| `/data/core_reserve.json` | **[v3.1]** Core Reserve state — entries, ATH, triggers, audit log | `core_reserve.py` |
| `/data/repair_log.json` | Self-repair history from Claude Code | `bot_with_proxy.py` |

### AI Learning Loop

1. Trade closes → `prompt_builder.on_trade_closed()` → `memory.record_outcome()` → auto-saves to `/data/ai_memory.json`
2. Next AI cycle → `memory.format_for_prompt()` injects "LEARNED FROM PAST TRADES:" into both Claude and Grok prompts (up to 4 most-relevant lessons by symbol/situation/regime match)
3. AIs see overall win rate, per-symbol performance, per-AI persona stats, bear-market warnings, situation-specific guidance

### Boot-Time Backfill (v3.1)

On first boot after deploying the v3.1 patches, `_replay_trade_history_into_memory()` does three things:
1. Replays stored stock closes back into AI memory (if any)
2. **One-time Binance history backfill** — converts pre-existing Binance fills into synthetic lessons via FIFO buy/sell matching, so the AIs start with a meaningful baseline rather than cold-starting at zero
3. Logs full status: `🧠 AI Memory ready: N lessons | M closes (X% win rate) | Y symbols tracked`

The `backfilled` flag in `ai_memory.json` ensures this only runs once per fresh deploy.

---

## Known Issues & Fixes

| Issue | Status | Fix |
|-------|--------|-----|
| TSLA Alpaca 403 | Known — bot cannot sell TSLA | Manual close required |
| AUDIO LIMIT order 400 error | Fixed — MARKET orders used | Already in code |
| Claude JSON parse fail (compressed keys / truncation) | **Fixed v3.1** — bracket-balancing recovery + abbreviated key map | `ai_clients.py` |
| Sleep brief parse fail (markdown fences) | Fixed April 7 | In `thesis_manager.py` |
| Flask must bind before trading loop | Fixed — `_delayed_trading_start()` | In code |
| Stale Binance cache phantom sell errors | Fixed — live balance check before every sell | In code |
| BTCUSDT "projection not viable" | Expected — range too tight, protecting capital | Normal behavior |
| ETH phantom sell order stuck unfilled | Fixed April 24 — `place_crypto_sell` defaults MARKET | `binance_crypto.py` |
| AI sell decisions re-stacking limit orders | Fixed April 24 — cancel open orders + refresh wallet before sell | `binance_crypto.py` |
| AI returns bare asset name (`UNI`, `ETH`) → 400 | Fixed April 24 — auto-append `USDT` if no quote suffix | `binance_crypto.py` |
| Buy `INSUFFICIENT_BALANCE` on full-notional MARKET orders | Fixed April 24 — 0.5% safety buffer in Method 2 | `binance_crypto.py` |
| Ghost positions spam exit-monitor every cycle | Fixed April 25 — pre-check + DUST_BALANCE handling | `binance_crypto.py` |
| **Sleep manager `ZoneInfo` not imported** → AIs forcibly asleep | **Fixed v3.1** — added missing import + tuple-return guarantee | `sleep_manager.py` |
| **Autonomous monitor `cannot unpack non-iterable NoneType`** | **Fixed v3.1** — `check_wake_conditions` always returns tuple | `sleep_manager.py` |
| **Binance history sync returning 0 trades** | **Fixed v3.1** — `signed=True` was missing on `/api/v3/myTrades` | `portfolio_manager.py` |
| **`round_trip_fee = 0.0002` (40× too low)** | **Fixed v3.1** — set to 0.008 (Binance.US 0.40% × 2 sides) | `binance_crypto.py` |
| **ETH dumped at -1.38% on time_exit despite below fee floor** | **Fixed v3.1** — fee-floor guard extends hold up to `hard_max_hold_hours=72` | `binance_crypto.py` |
| **Stock side ignored wallet-scaling reserve rule** | **Fixed v3.1** — `portfolio_manager.get_trading_pool()` calls `get_wallet_reserve_pct()` | `portfolio_manager.py` |
| **Dust ($<$1) cluttering snapshot logs and AI prompts** | **Fixed v3.1** — `MIN_DISPLAY_VALUE = $1` filter + dust summary | `binance_crypto.py`, `bot_with_proxy.py` |

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

Base URL: `https://collaboration-production-cba3.up.railway.app`

### Core endpoints
| Endpoint | What It Returns |
|----------|----------------|
| `/health` | Liveness probe (always 200) |
| `/dashboard` (or `/`) | Live web dashboard |
| `/stats` | Full portfolio snapshot — equity, cash, positions, AI sleep state |
| `/crypto_status` | Binance.US positions, wallet, USDT free, day P&L |
| `/history` | Recent trade history (closes) — filter: `?symbol=X`, `?owner=claude` |
| `/performance` | Win rate, P&L analytics, by_symbol/by_AI/by_strategy breakdowns |
| `/projection` | Stock projection engine output (5-layer model) |

### v3.1 endpoints
| Endpoint | What It Returns |
|----------|----------------|
| `/leaderboard` | **Claude vs Grok head-to-head** — wins/losses, win rate, total P&L, recent crypto closes, current leader, pool split, reserve info |
| `/memory` | **AI Brain stats** — total lessons, win rate, top symbols, AI personas, market regimes, recent learned lessons |
| `/core_reserve` | **Long-term compounder status** — BTC/SPY/cash split, target allocation, P&L vs contributions, ATH per slice, recent contingency events |
| `/evolution` | **AI tier system** — current tier per AI (Claude/Grok), trades/P&L stats, distance to next tier, audit log |
| `/strategy` | **[v3.1.2]** Strategic Brain overview — both strategists' status, model registry, upgrade thresholds, schedule, wake triggers |
| `/strategy/<ai>` | **[v3.1.2]** Per-AI strategy file — current strategy with rules and rationale, performance vs prediction, history. Use `claude` or `grok` |
| `/binance_history` | Binance fill history from `/api/v3/myTrades` (last 6 months) |
| `/prompt_memory` | Legacy view of prompt memory state |

### Operational
| Endpoint | What It Returns |
|----------|----------------|
| `/storage` | Volume disk usage on `/data` |
| `/repair_log` | Self-repair history from Claude Code |
| `/repair_status` | Active/pending repair jobs |
| `/pdt` | Pattern Day Trade status |
| `/liquidate` | Emergency close all positions (POST) |
| `/deploy` | Push files to GitHub (POST) |

---

## Current Portfolio (as of April 28, 2026)

### Stocks (equity ~$53.28, cash ~$21.28)
| Symbol | Entry | Current | P&L | Notes |
|--------|-------|---------|-----|-------|
| AMZN | $262.53 | ~$259.56 | -1.13% | Grok pick, strat A |
| GOOGL | $350.25 | ~$349.72 | -0.15% | Claude pick, strat A |
| NVDA | $209.19 | ~$210.82 | +0.78% | Claude pick, strat B |

3 positions × ~$10.65 each = ~$32 deployed. Cash holds the rest. Stock side is at `max_positions=3` cap — once v3.1 deploys with the unified reserve rule, the cap is the constraint, not the reserve.

### Crypto (wallet ~$57.68, USDT free ~$5.65)
| Symbol | Entry | Current | P&L | Held | Notes |
|--------|-------|---------|-----|------|-------|
| ALGOUSDT | $0.1119 | ~$0.1140 | +1.88% | ~1.5h | Claude pick, near TP $0.1148 |
| FETUSDT | $0.1977 | ~$0.1965 | -0.61% | ~1.5h | Bot-tracked, holding |
| ATOMUSDT | $1.938 | ~$1.961 | +1.19% | ~1.5h | Bot-tracked, near TP $1.9819 |

### Wallet dust (13 coins ~$0.62 — hidden from logs and AI prompts post-v3.1)
ETH residual, NEAR, LTC, DOGE, UNI, AVAX, ATOM remnant, SOL, KAVA, RVN, SHIB, DOT, AUDIO — all under $1 threshold.

### Combined wallet ~$110.96
Below the $1,000 Core Reserve activation threshold. Phase 1.5 module loaded but inactive — dashboard shows "Locked — $889 to activation."

---

## Pending Manual Actions

- [ ] **Deploy v3.1.3 to Railway** (6 files: `strategic_brain.py`, `bot_with_proxy.py`, `binance_crypto.py`, `prompt_builder.py`, `sleep_manager.py`, `risk_gate.py`, `dashboard.html`)
- [ ] **Monitor first strategist activation** in Railway logs — verify both Claude and Grok strategists respond cleanly at 9am ET pre-market call
- [ ] **Verify `/coin_performance` endpoint** is being polled by new dashboard (Coin Health panel)
- [x] **Wire sentiment data into shared_state** — DONE (sentiment pipeline). Grok crypto intel cycle now publishes `market_sentiment` + news/social/whale summaries via `_publish_intel_sentiment()`; sleep brief publishes `market_sentiment`/`sentiment_notes`; `get_1h_changes()` tracks `btc_change_1h`/`spy_change_1h`; fear/greed cached in `shared_state["fear_greed"]`. All flow into the strategist prompt via `_strategist_market_context()`.
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
6. **v3.1** — Modularization complete. AI Competition mode (separate Claude/Grok pools + leaderboard). Wallet-scaling reserve rule unified across stocks + crypto. Persistent AI memory with Binance backfill. Core Reserve long-term compounder (BTC/SPY/Cash) with rule-based contingency watcher. Sleep manager + JSON parser bug fixes. Dust cleanup throughout. New `/leaderboard`, `/memory`, `/core_reserve` endpoints + dashboard panels.
7. **v3.1.1** — Equal capability for both AIs (no more specialty roles). AI Evolution tier system (Pass A foundation): both AIs at Tier 0 with identical neutral prompts and rivalry context. Grok upgraded to reasoning variant at same price. SPY 404 spam fixed (Alpaca data domain). Grok JSON parse failures fixed via autowrap parser + neutral prompts. New `/evolution` endpoint + tier-aware dashboard labels.
8. **v3.1.2** — Strategic Brain foundation (Phase A). New `strategic_brain.py` module with wallet-tiered model registry (Sonnet 4.6 → Opus 4.7 at $5K wallet), twice-daily activation schedule, five hard wake triggers, three-phase rollout plan. Phase A ships plumbing only — strategists are wired but DORMANT. New `/strategy` and `/strategy/<ai>` endpoints. New "🧭 Strategic Brain" dashboard panel. Foundation for Phase B activation in next session.
9. **v3.1.3** — *(current)* **Strategic Brain Phase B ACTIVE — Living Playbook system.** Strategists now write comprehensive standing orders the bot follows autonomously. Conditional rules executor with zero AI cost. Stop-loss gate integrated with playbook on both crypto and stock paths. Sentiment-aware strategist prompts. Self-defined wake conditions (playbook decides when to recall the strategist). R/R ≥ 1:1 enforced at validator. `consecutive_losses`/`wins` tracking added to shared_state. 3× daily scheduled activations live (9am, 4:30pm, 9pm ET). Cooldown raised to 2 hours. Risk gate now blocks entries based on playbook directives.

---

## Roadmap (Upcoming)

### Phase C — Core Reserve Handover (next priority)
- Strategists take over Core Reserve allocation decisions (collaborative — both must agree)
- Hard-rule floors STAY in place as catastrophic protection
- Weekly scheduled reviews + reserve-specific wake triggers
- BTC -50% from entry over 30+ days = automatic 50% trim regardless of strategist input

### Phase B' — AI Evolution Pass B (parallel track, when ~10-15 trades exist)
- Tactician self-modification loop (the original Pass B from v3.1.1)
- Validation layer enforces tier token caps + banned phrases
- Auto-revert on win-rate drop
- Dashboard tier panel with proposal/revert audit trail

### Sentiment Pipeline Enrichment — ✅ SHIPPED
- Grok's news/social/whale intel now populates `shared_state["market_sentiment"]`, `["latest_news_summary"]`, `["latest_social_summary"]`, `["latest_whale_summary"]` every crypto AI cycle (`CryptoTrader._publish_intel_sentiment` in `binance_crypto.py`); the stock sleep brief also publishes its `market_sentiment`/`sentiment_notes`
- Fear/greed index (alternative.me, already fetched daily) is now cached in `shared_state["fear_greed"]` and refetched live at each strategist activation
- BTC/SPY 1h change tracking via `market_data.get_1h_changes()` (5-min cache) → `shared_state["btc_change_1h"]` / `["spy_change_1h"]`
- All of the above is appended to the strategist prompt through `_strategist_market_context()` in `bot_with_proxy.py`

### Phase 1.6 — Smaller wins
- **Dust convert** — integrate Binance.US `/sapi/v1/asset/dust` to sweep <$10 holdings into USDT/BTC. Frees up otherwise stuck capital.
- **Convert API** — use Binance.US OTC convert endpoints for crypto-to-crypto swaps in Rotation Mode. 0% fees vs current 0.80% round-trip = significant fee savings on every rotation.

### Phase 2 (further out)
- **AI-negotiated pool split** — replace fixed 50/50 with each AI proposing its own % each cycle, system reconciles based on recent performance.
- **Profit-minus-fees as exit driver** — make every exit decision compute `expected_pnl - round_trip_fees - slippage_estimate`, only fire if positive (or stop-loss).

### Phase 4 (architectural shift)
- **Full AI tool-calling autonomy** — agent loop where AIs request whichever indicators / data / asset class they want via tool calls. Moves from "prompt-and-parse" to true agentic architecture. Multiple-session refactor.

---

## Bug Fix Log

### Bug Fix — 2026-06-03
**Error:** Claude/Grok proposing crypto USDT pairs in stock execution cycle
**Fix:** Added `is_crypto_ticker()` validator function and `STOCK_EXEC_FORBIDDEN_PATTERNS` list to filter crypto stablecoins (USDT, USDC, BUSD, TUSD) before Alpaca order submission. Prevents malformed "Skip USDT" errors when AI recommends BTC/ETH crosses during market analysis spillover.
**File:** bot_with_proxy.py
**Status:** ✅ PR opened


### 🚀 v3.1.4 Release — 2026-05-08 — Turtle Trend-Following System (Long-Term Growth)

**Strategic pivot from scalping to trend-following.** After analysis showed the 8% stop / 8% TP scalping strategy was running at 1:1 R/R with break-even at 50% WR — leaving zero margin for error — the system has been re-architected around **Richard Dennis's Turtle Trading System** for long-term growth.

**Why Turtle**: The math is asymmetric and well-validated. Turtle accepts a 30-40% win rate as normal, but profits come from large winners (4-8N) covering many small 2N losses. Mathematical expectancy: ~+0.75% per trade, ~25-30% annual growth in trending markets, mechanical discipline.

**Files modified (4):**

- **`projection_engine.py`** — Added `compute_donchian()`, `compute_turtle_signal()`, and `compute_turtle_position_size()`. Donchian channels return prior 10/20/55-day highs/lows for entry/exit signals. ATR (already computed) provides volatility for stops and sizing.

- **`strategic_brain.py`** — Major upgrade:
  - New `TURTLE_SYSTEM_CONFIG` constants
  - New `_default_strategy_turtle()` template generates a complete Turtle playbook
  - Default playbook for both AIs is now Turtle (was scalping)
  - Validator extended to accept ATR-based strategies (`strategy_type="turtle"`)
  - Turtle validator enforces: `stop_loss_atr_multiple` 1.0-4.0, `risk_pct_per_trade` 0-3.0, `entry_donchian_period` 5-200, `exit_donchian_period` 5-100, exit < entry period
  - Strategist prompt completely rewritten to teach Turtle principles and explicitly forbid abandoning the system on losing streaks
  - Conditional rules `on_stop_loss`, `on_two_stops_same_session`, `on_three_consecutive_losses` all default to `no_pause` action — Turtle doesn't pause after losses
  - Wake conditions tightened: `consecutive_losses=6` (was 3), `min_trades_before_eval=30`, `win_rate_below_pct=15` (was 35)
  - `execute_playbook()` now respects `no_pause` action (doesn't block entries on losing streaks for Turtle)

- **`binance_crypto.py`** — New Turtle helpers:
  - `get_daily_bars()` fetches daily klines for Turtle signals
  - `turtle_check_entry()` returns eligibility, entry level, ATR, calculated 2N stop
  - `turtle_check_exit()` returns Donchian breakdown OR 2N stop check
  - `turtle_position_size()` returns 1%-risk position sizing
  - `CryptoPosition.__init__` extended with `strategy_type`, `turtle_system`, `atr_at_entry`, `stop_price_override` params
  - `CryptoPosition.should_turtle_exit()` method runs daily Donchian check
  - Exit monitor now checks Turtle exit BEFORE classic TP (Turtle has no fixed TP)
  - Both `CryptoPosition()` instantiation sites detect current playbook and apply Turtle metadata when applicable

- **`bot_with_proxy.py`** — Stock-side will inherit Turtle behavior automatically once `compute_turtle_signal` is wired into stock entry logic (next iteration). The `/bars` endpoint already serves the daily OHLC data needed.

**Turtle Mechanics Recap:**

| Component | Value |
|---|---|
| Entry signal | Daily close > 20-day Donchian high (System 1) |
| Exit signal | Daily close < 10-day Donchian low (System 1) |
| Hard stop | Entry - (2 × ATR) |
| Position size | 1% of equity / (2 × ATR) |
| Max concurrent | 4 positions |
| Expected win rate | 30-40% |
| Avg winner | 4-8N (200-400% of risk) |
| Avg loser | 2N (capped at 1% of equity) |
| Hold horizon | Days to weeks (until Donchian breakdown) |
| Strategy abandonment | Only after 30+ trades AND <15% WR AND no winners >4N |

**Critical Discipline Rule**: The strategist is explicitly instructed in its prompt to NEVER abandon Turtle after losing streaks. This is the historical failure mode of every Turtle trader who didn't survive — they couldn't sit through drawdowns. The bot has no such psychological pressure.

**Status:** ✅ All 4 files compile, smoke-tested. Default playbook for both AIs on next boot will be Turtle System 1.

**Stock-side Turtle ADDED in v3.1.5 (2026-05-10):** `bot_with_proxy.py` now has `stock_turtle_check_entry()`, `stock_turtle_check_exit()`, `stock_turtle_position_size()`, `is_turtle_active_for_stocks()` helpers. New Strategy "T" supported alongside A and B in both AI-awake and autonomous exit monitors. When Turtle is active in the playbook, stock entries auto-detect 20-day Donchian breakouts and apply 2N ATR stops. Donchian breakdown (10-day low) triggers exit. No fixed TP, no time stop — pure trend-following.

---

### 🚀 v3.1.3 Release — 2026-05-05 — Strategic Brain Phase B (Living Playbook ACTIVE)

**The strategist is now alive.** This release activates Phase B of the Strategic Brain with a fundamental design upgrade: instead of writing brief strategy summaries for occasional review, the strategist now writes **comprehensive living playbooks** — standing orders the bot and tacticians follow autonomously between activations. This trains the bot to handle situations without constant AI calls.

**The core insight that drove this design:** A trader's emotional risk (moving stops, revenge trading after losses) doesn't apply to a bot — but the *intelligence* a human gains from pausing to analyze sentiment after a stop *does* apply. The living playbook captures that intelligence preemptively: the strategist anticipates situations, writes responses, and the bot executes those responses without needing to wake the strategist every time.

**Files modified (6):**
- `strategic_brain.py` — Full rewrite. Living playbook system with conditional rules executor, stop-loss gate, sentiment-aware prompts, self-defined wake conditions, R/R ≥ 1:1 enforcement.
- `bot_with_proxy.py` — Added `consecutive_losses` and `consecutive_wins` to `shared_state`. New `_strategist_sentiment_context()` function feeding sentiment + news + BTC/SPY change to strategist. Scheduled activations wired at pre-market (9am ET), post-close (4:30pm ET), crypto-close (9pm ET). `execute_playbook()` runs every crypto cycle. Stop-loss gates wired on both stock exit paths (AI-awake and autonomous).
- `binance_crypto.py` — Stop-loss gate calls `handle_stop_loss_event()` immediately when crypto stops fire. Wins reset `consecutive_losses` and increment `consecutive_wins`. `record_trade_result()` updates strategy performance counters every close.
- `prompt_builder.py` — `build_r1()` and `build_crypto_context()` accept `ai_name` parameter and inject `get_playbook_summary()` at the top of every tactician prompt. Tacticians now always see their strategist's standing orders.
- `sleep_manager.py` — Resets `consecutive_losses` and `consecutive_wins` at both sleep reset points so each session starts clean.
- `risk_gate.py` — `can_open_new_positions()` now checks both AIs' playbooks first via `execute_playbook()`. If either playbook says `block_new_entries`, the gate blocks before any other rule runs.

**The Living Playbook schema** — every strategy now contains:
- Normal trading parameters (entry/exit logic, SL/TP, trail, hold time, position size, indicators)
- **Conditional responses** — `on_stop_loss`, `on_two_stops_same_session`, `on_three_consecutive_losses`, `on_winning_streak_3`, `on_btc_drops_3pct_1h`, `on_btc_drops_5pct_1h`, `on_spy_drops_2pct`, `on_sentiment_extreme_fear`, `on_sentiment_extreme_greed` — each with action, parameters, log_reason, optional `wake_strategist` flag
- **Self-defined wake conditions** — the playbook specifies when to recall the strategist (consecutive losses, drawdown threshold, regime flips, win rate floor, prediction gap)
- **Tactician training notes** — plain-language coaching injected into every tactician prompt

**Hard system limits added** (cannot be overridden by playbook):
- `consecutive_loss_gate`: 5 (forces wake + block)
- `drawdown_halt_pct`: 20% (forces wake + block)
- R/R ratio enforced: `take_profit_pct >= stop_loss_pct` validator rejects 1:1 or worse
- `min_confidence` floor: 50%

**Strategist prompt enrichment** — the strategist now receives:
- Full sentiment block (overall, fear/greed, BTC 1h/24h, news summary, social summary, whale activity)
- Playbook execution log (which conditional rules fired since last activation)
- Wake reason if triggered mid-cycle (vs scheduled)
- Current playbook performance (predicted vs actual WR)

**Cooldown** raised from 30 minutes to **120 minutes** (`WAKE_COOLDOWN_MINUTES`) — the strategist is meant to write durable playbooks, not be reactive. 2 hours between activations is enough for the bot to genuinely test the playbook before the next review.

**Backward compatibility:** `load_strategy()` auto-migrates old strategy files by injecting any missing conditional rule keys from the default playbook. No manual data migration needed.

**Status:** ✅ All 6 files compile, smoke-tested. `ENABLE_STRATEGIST = True` is now the default.

**Next:** Bake for 1-2 days, monitor playbook execution log, then move to Phase C (Core Reserve handover).

---

### 🚀 v3.1.2 Release — 2026-04-30 — Strategic Brain Foundation (Phase A)

This release lays the foundation for the **two-tier AI architecture**: strategists who develop trading playbooks, and tacticians who execute them. Phase A ships the plumbing only — the strategists are wired but DORMANT (`ENABLE_STRATEGIST = False`). This deploys safely alongside Pass A (v3.1.1) without changing any actual trading behavior.

**New files:**
- `strategic_brain.py` (~830 lines) — full strategist module with model registry, schedule, wake triggers, validation, and persistence

**Architecture decisions documented in this release:**
- **Wallet-tiered model registry** — single config block in `strategic_brain.py:MODEL_REGISTRY` controls ALL AI model choices across the bot. Strategists auto-upgrade from Sonnet 4.6 → Opus 4.7 when combined wallet crosses $5,000. Grok strategists auto-upgrade from Grok 4.1 Fast Reasoning → Grok 4 at the same threshold. Tacticians stay on cheap-and-fast tier (Haiku 4.5 / Grok 4.1 Fast Reasoning) regardless of wallet — speed matters more for them than reasoning depth.
- **Twice-daily scheduled activations** — pre-market (9:00 AM ET), post-close stocks (4:30 PM ET), crypto session (9:00 PM ET). Plus on-demand wakes from tactician.
- **Five hard wake triggers** — stop-loss breach, SPY/BTC -3% in 1h, position gap ±10%, 3 consecutive losses, confidence-calibration miss (predicted ≥60% WR, actual <30% over 5+ trades).
- **30-minute cooldown** between wakes to prevent ping-pong loops between strategist and tactician.
- **Per-camp strategies, collaborative reserve** — Claude-Strategist serves Claude-Tactician, Grok-Strategist serves Grok-Tactician (each develops their own unique playbook). For Core Reserve decisions, both strategists must reach consensus to act. Disagreement = hold.
- **Three-phase rollout** — Phase A (this release: foundation, dormant), Phase B (activation: scheduled reviews + tactician integration), Phase C (reserve handover).

**Plumbing wired in `bot_with_proxy.py`:**
- Module imports with `try/except` (graceful degradation if file missing)
- Six injected dependencies: `log`, `ask_claude_strategist`, `ask_grok_strategist`, `get_trade_history`, `get_market_context`, `record_trade`, `get_wallet_value`
- Strategist API wrappers (`_ask_claude_strategist`, `_ask_grok_strategist`) read the model registry per-call so wallet-tier upgrades take effect without restart
- Boot log surfaces active strategist models and DORMANT/ACTIVE status

**New endpoints:**
- `/strategy` — overview: both strategists, model registry, upgrade info, schedule, triggers
- `/strategy/<claude|grok>` — per-AI strategy file (current strategy, performance, history, audit log)

**Dashboard:**
- New "🧭 Strategic Brain — Research Desk" panel between AI Battle and Core Reserve
- Shows ACTIVE/DORMANT status pill, per-AI cards with active model, wallet-tier upgrade headroom, and current strategy (Phase B onward)
- Color-coded: Claude side blue gradient, Grok side purple gradient

**Status:** ✅ All 9 files compile, model resolution verified at boundary cases ($0, $4999, $5000, $50000), `ENABLE_STRATEGIST = False` confirmed default in Phase A. No trading behavior changes — purely structural addition.

**Next:** Phase B activates the strategists. Estimated 3-5 days after Pass A is verified clean.

---

### 🚀 v3.1.1 Release — 2026-04-29 — Equal Capability + Pass A Tier System + 24h Bug Fixes

This release ships THREE things together: 24-hour bug fixes from Apr 29 logs, a model-tier upgrade for Grok, and Pass A of the AI Evolution tier system.

**Bug fixes from 5h log analysis:**
1. **SPY 404 spam** — `core_reserve.py` was hitting `https://api.alpaca.markets/v2/stocks/SPY/trades/latest` (54 occurrences in 5h). Alpaca market data lives on a **different domain**: `https://data.alpaca.markets`. Trading API and Data API are separate domains with separate paths. Fixed by adding a `stock_price_fn` injection slot. `bot_with_proxy.py` now provides a proper SPY fetcher using `DATA_URL` + `quotes/latest` (already proven elsewhere in the bot) with bars-fallback for off-hours.
2. **Grok JSON parse failures** — `_parse_crypto_resp` in `binance_crypto.py` was a naive parser. Grok returned flat trade-shaped responses with abbreviated keys like `{"sn":"DOGEUSDT","mt":"...","pt":"$0.1021","cc":74,"bw":17.08,"st":"buy"}` — missing the `crypto_trades` wrapper entirely. Replaced with 4-layer parser: (1) naive parse on raw, (2) autowrap on RAW (catches Grok abbrevs sn/pt/cc/bw/st via field aliases like SYMBOL_ALIASES, ACTION_ALIASES, NOTIONAL_ALIASES), (3) hardened global parser with abbrev expansion, (4) best-effort fallback. Tested against actual log strings.

**Model upgrades:**
3. **Grok model swap** — `grok-4-1-fast-non-reasoning` → `grok-4-1-fast-reasoning`. **Same price** ($0.20/$0.50 per 1M tokens) but reasoning variant pauses to think before responding, reducing JSON shape drift. The non-reasoning variant was aggressively compressing keys when under length pressure (4.4M vs 7.9M test tokens — well below average) which caused the schema-drift parse failures.
4. **max_tokens bump** — Both `ask_claude` and `ask_grok` defaults raised from 1200 → 2400. Reasoning models use internal tokens before output; 1200 was too tight. Negligible cost increase, prevents truncation entirely.

**Equal Capability + Tier System (Pass A):**
5. **Specialty role baking removed** — Three places stripped:
   - `prompt_builder.py:build_claude_system/build_grok_system` — both AIs now use `_build_neutral_system(self_name, rival_name)` which produces identical prompts with only the names swapped. Removed "disciplined quantitative trader" / "momentum trader with Twitter/X access" baking.
   - `binance_crypto.py:2837-2844` — removed "Focus on crypto 2-3 day momentum" / "Use Twitter/X crypto sentiment" overrides. Both AIs get full data access; specialties emerge from earned P&L.
   - Removed instruction to "Use abbreviated keys (sn/mt/pt/cc/bw)" — this was actively causing the Grok parse failures by encouraging key compression.
6. **Rivalry context injection** — Each cycle, `binance_crypto.py` now calls `ai_evolution.build_rivalry_context()` to inject concrete current standings into each AI's prompt: "You are LEADING/TRAILING by $X" or "tied at zero", recent W/L record, and explicit anti-tilt guidance.
7. **`ai_evolution.py` foundation** — New module (~360 lines) with 5-tier ladder, persistent state at `/data/ai_evolution.json`, banned-phrase list, audit log helpers. Pass A: framework only — `validate_proposed_prompt()` and `get_custom_prompt_addition()` are stubbed for Pass B.
8. **`/evolution` endpoint** — Returns full tier status per AI: current tier, progress to next, eligibility, prompt token cap.
9. **Dashboard role labels** — `Technical analysis manager` and `Social / news intelligence` replaced with dynamic tier labels: "Autonomous trader · Tier 0 (Probation) · next: 5 more trades". Color-coded by tier (gray → teal → blue → purple → green).

**Why this all ships together:** The model swap fixes parse failures the autowrap was patching around. The neutral prompts remove the abbreviated-key instruction that was causing the same parse failures from the prompt side. Together they should drop JSON parse errors to near-zero. Pass A foundations the future self-evolution work without enabling it yet — both AIs continue to use neutral prompts in this release.

**Files touched:** `core_reserve.py` (SPY fetcher), `bot_with_proxy.py` (SPY price function + ai_evolution import + /evolution endpoint), `binance_crypto.py` (autowrap parser + neutral prompts + rivalry inject), `ai_clients.py` (model + max_tokens), `prompt_builder.py` (neutral system builders), `ai_evolution.py` (NEW), `dashboard.html` (tier labels + renderEvolution).

**Status:** ✅ All files compile, autowrap tested against actual failing log strings, tier math verified across boundary cases.

---

### 🚀 v3.1 Release — 2026-04-28 — Phase 1 + 1.5 Major Update

Comprehensive update touching 8 files with 7 critical fixes plus 3 major features. Sequence of events:

1. **Logs analysis** revealed two errors firing every 5 minutes for 90+ minutes — `ZoneInfo not defined` in `sleep_manager.py` cascading into `cannot unpack non-iterable NoneType object` in the autonomous monitor.
2. **Root cause:** missing `from zoneinfo import ZoneInfo` in `sleep_manager.py`, plus broken control flow in `check_wake_conditions` exception handler that returned `None` instead of a `(bool, str)` tuple.
3. **Impact:** AIs were forcibly stuck asleep — `Wake triggers active: 3` showed up but never acted on because the check itself crashed before returning.

**Phase 1 fixes (this release):**
1. **Sleep manager** — added `ZoneInfo` import, rewrote exception handler to always return tuple, extracted orphaned cleanup into safe helper `_cleanup_stale_restrictions()`
2. **JSON parser** — bracket-balancing truncation recovery in `ai_clients.py:parse_json()`. Tested against mid-string, mid-array, abbreviated-key, and well-formed inputs.
3. **Binance history sync** — `signed=True` was missing on `/api/v3/myTrades` calls in `portfolio_manager.py`. Without HMAC signing, every symbol's history fetch silently returned 0 trades. Diagnostic logging added for first 3 errors so future failures aren't invisible.
4. **Fee floor** — `round_trip_fee` was `0.0002` (0.02%, 40× too low). Set to `0.008` (Binance.US 0.40% maker/taker × 2 sides). The fee-floor exit guard was working but checking against a fee 40× too low, letting losing exits through.
5. **Time-exit guard** — `should_time_exit` now extends hold up to `hard_max_hold_hours = 72` (instead of dumping at 24h regardless). When position is underwater AND below fee floor, log `⏳ time_exit skipped — extending hold`. Prevents the kind of dump we saw on ETH at -1.38%.
6. **Wallet-scaling reserve unification** — `portfolio_manager.get_trading_pool()` now calls `binance_crypto.get_wallet_reserve_pct(combined_wallet)` instead of hardcoded `0.15`. Stocks and crypto now share the same reserve rule: `<$1k → 0%`, `$1k → 10%`, `+1% per $1k`, cap at 30% at $21k+. Below $1k the AIs trade with full freedom.
7. **Dust filtering** — `MIN_DISPLAY_VALUE = $1.00` and `MIN_TRADABLE_VALUE = $10.00` constants. Hides dust from cycle logs, AI prompts (saves ~30% tokens), and dashboard wallet display. Dust summary line shows `🧹 Dust: 13 coins ~$0.62 — below $1.00 threshold`.

**Phase 1 features:**
- **AI Competition** — Claude and Grok now have separate USDT pools (50/50 split), separate P&L tracking, head-to-head leaderboard. No more "shared" merge — winning proposals stay tagged with their true `owner`. Kill switch: `ENABLE_AI_COMPETITION = False`.
- **AI Memory + Backfill** — `prompt_builder.PromptMemory` now backfills lessons from existing Binance fill history via FIFO buy/sell matching. AIs no longer cold-start at zero. New `/memory` endpoint exposes full brain state. Dashboard "🧠 AI Brain" panel shows lessons, top symbols, win rates per AI, market regime stats.
- **Boot status logging** — every boot now logs `🧠 AI Memory ready: N lessons | M closes (X% win rate) | Y symbols tracked | saved to /data/ai_memory.json`.

**Phase 1.5 — Core Reserve (NEW MODULE):**
- New `core_reserve.py` (~800 lines) — long-term wealth compounder, walled off from tactical AIs.
- 50% BTC / 30% SPY / 20% USDT cash, activates at $1,000 combined wallet.
- 4 contingency triggers (defensive trim, opportunity buy, take-profit trim, drift rebalance) — all rule-based, no AI calls, hourly cadence.
- State persists to `/data/core_reserve.json` with full audit trail of events (capped at 200).
- Trades tagged `owner="core_reserve"` so they don't pollute the Claude vs Grok leaderboard.
- New `/core_reserve` endpoint and dashboard panel surface activation status, slice composition, P&L vs total contributions, recent contingency events.

**Files touched:** `core_reserve.py` (new), `bot_with_proxy.py`, `binance_crypto.py`, `portfolio_manager.py`, `sleep_manager.py`, `ai_clients.py`, `prompt_builder.py`, `dashboard.html`

**Status:** ✅ All files syntax-validated, backfill tested with synthetic data (FIFO matching across partial fills), reserve math verified across boundary cases.

---

### 🔧 Bug Fix — 2026-04-25 — Ghost Position Cleanup (LTC dust)
**Symptom:** `[CRYPTO]    💡 LTCUSDT: requested sell qty 0.35 > live balance 0.00134000` appearing in every 5-min snapshot log; bot's internal `self.positions["LTCUSDT"]` showed `qty=0.35` and `+1.27% P&L` while wallet only held `0.00134 LTC` (~$0.07 dust).

**Root cause:** LTC was sold outside the bot at some point — manual sale on Binance.US, prior partial liquidation, or similar. `self.positions` tracker was never cleared. At hour 25 the time-exit fired (`max_hold_hours=24`), `_execute_exit` called `place_crypto_sell(0.35)`, the live-balance safety check truncated qty to `0.00134`, and either `_round_qty_step` zeroed it or Binance would have rejected on `MIN_NOTIONAL`. The `_execute_exit` ghost-cleanup path only handled exact `ZERO_BALANCE` — dust ($0.07) wasn't zero, so the cleanup never fired and the cycle repeated every 5 minutes indefinitely. Also impacted: log noise, fake P&L in snapshots, potential false stop-loss/TP "fires" against a phantom position.

**Fix (4 edits, all in `binance_crypto.py`):**
1. `place_crypto_sell` returns new `DUST_BALANCE` error when `live_balance × current_price < $1.50` (well below Binance's $10 min notional).
2. `place_crypto_stop` mirrors the same check.
3. `run_exit_monitor` adds a pre-loop ghost-check: at the top of every per-symbol iteration, fetch live balance and price; if `qty == 0` or value `< $1.50`, silently `del self.positions[symbol]` and `continue`. This kills the per-cycle log spam permanently.
4. `_execute_exit` ghost-cleanup expanded from just `ZERO_BALANCE` to a tuple `("ZERO_BALANCE", "DUST_BALANCE", "QTY_ROUNDED_TO_ZERO")` — any of these now clear the tracker and return success.

**Threshold rationale:** $1.50 was chosen because Binance min notional is $10 — anything below that can't be sold anyway. Setting the dust threshold at $1.50 leaves a comfortable buffer for price ticks and avoids accidentally clearing a legitimate small position that's in temporary drawdown.

**Files:** `binance_crypto.py` (4 edits, one file)
**Status:** ✅ Syntax-validated

---

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
