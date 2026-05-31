# Repo Cleanup Summary

## What changed

**Removed (moved to `_archive/`) — 3 files, ~1,279 lines, ~54KB:**
- `risk_gate.py` — Macro circuit breaker (BTC <200-EMA, weekend, drawdown). Useful in theory, but its rules would have **blocked your recent wins** (AUDIO +15%, SOL still open). Premature for current bot behaviour.
- `coin_performance.py` — Per-coin P&L memory for AI prompts. Useful, but would shrink an already-narrow trade universe.
- `exchange_rules.py` — Per-coin min notional / tick size cache. Pure plumbing, ~$0.50/year value.

All three were **complete, working code that nothing imported**. Archived (not deleted) so you can recover them if you want to wire them in later.

**Deduplicated:**
- 4 Turtle math primitives (`compute_atr`, `compute_donchian`, `compute_turtle_signal`, `compute_turtle_position_size`) lived in BOTH `projection_engine.py` AND `turtle_math.py` with different implementations. Removed from `projection_engine.py` — single source of truth now in `turtle_math.py`. `projection_engine` imports `compute_atr` from `turtle_math` internally for its 5-layer projections.

**Added to self_repair monitoring:**
- `strategic_brain.py` (Turtle migration + AI model registry)
- `turtle_math.py` (Donchian/ATR primitives)

Both were critical to trading logic but not in the auto-repair file list. Now they are.

## Active files (19)

| File | Purpose | Used by |
|---|---|---|
| `bot_with_proxy.py` | Main entry point | — |
| `binance_crypto.py` | Crypto trading engine | bot |
| `strategic_brain.py` | Strategist AI + Turtle playbook | bot, binance_crypto |
| `turtle_math.py` | Donchian/ATR primitives | bot, binance_crypto, projection_engine |
| `projection_engine.py` | 5-layer daily price projections | bot, market_data |
| `ai_clients.py` | Claude + Grok API callers w/ fallback | bot, binance_crypto |
| `ai_evolution.py` | AI strategy learning over time | bot, binance_crypto |
| `portfolio_manager.py` | Tier system, universe, trade history | bot |
| `market_data.py` | Alpaca quotes, bars, indicators | bot |
| `prompt_builder.py` | AI prompt construction + memory | bot |
| `intelligence.py` | News/sentiment context | bot |
| `wallet_intelligence.py` | Cross-asset wallet analysis | bot |
| `thesis_manager.py` | Position thesis tracking | bot |
| `core_reserve.py` | Long-term SPY/BTC compounder | bot |
| `pdt_manager.py` | Pattern day trader compliance | bot |
| `sleep_manager.py` | Bot wake/sleep cycle | bot |
| `self_repair.py` | Auto-repair via Claude Code | bot |
| `claude_code_trigger.py` | Spawns Claude Code sessions | bot, self_repair |
| `github_deploy.py` | GitHub push helpers | bot, self_repair |

## Verification results

- ✅ All 19 files compile clean
- ✅ No imports of removed modules anywhere
- ✅ Turtle math single source — primitives importable from `turtle_math`
- ✅ `projection_engine.get_projection()` still produces output (uses ATR from `turtle_math`)
- ✅ Strategic brain model registry has correct IDs (Grok 4.20 reasoning/non-reasoning)
- ✅ `ai_clients.ask_grok` fallback chain has 4 working models
- ✅ `self_repair.PATCHABLE_FILES` includes all critical files
- ✅ CryptoPosition Turtle metadata works end-to-end

## Deploy notes

1. **Push the 19 active files** to GitHub. The 3 archived files in `_archive/` are NOT pushed — they're a local backup.
2. **Old files on Railway will not auto-delete.** Either:
   - SSH into Railway and `rm risk_gate.py coin_performance.py exchange_rules.py`, OR
   - Leave them; they're not imported anywhere so they'll just be dead bytes on disk
3. **Env vars unchanged** — same `NOVATRADE_FORCE_TURTLE_MIGRATION=1` and optional `GROK_MODEL=...`.
4. **No new dependencies** — same `requirements.txt`.

## What I deliberately did NOT do

- **Did not wire in `risk_gate.py`** — its macro filters would have blocked your recent wins. Maybe revisit if you start losing on broad market moves.
- **Did not wire in `coin_performance.py`** — your problem isn't repeating bad trades, it's trading too rarely. Adding a "stop trying losing coins" filter shrinks an already-narrow universe.
- **Did not wire in `exchange_rules.py`** — value too low for integration risk right now.
- **Did not touch `wallet_intelligence.py` / `thesis_manager.py`** despite low usage — they're class-based modules where one entry point is normal pattern. Working as designed.

If trading behavior changes and one of the archived modules becomes worth wiring, the code is intact and recoverable from `_archive/`.
