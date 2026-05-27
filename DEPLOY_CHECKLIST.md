# NovaTrade Deployment Checklist

**Use this every time you push.** Each step is verifiable in the next log batch.

---

## Files to push (7 total)

All in `/mnt/user-data/outputs/`:

- [ ] `turtle_math.py`         (NEW file — Turtle Donchian/ATR primitives)
- [ ] `binance_crypto.py`      (Turtle gates + corrected fees + BNB discount)
- [ ] `bot_with_proxy.py`      (stock Turtle gate + leaderboard dedup)
- [ ] `strategic_brain.py`     (Turtle migration + correct Claude/Grok model IDs)
- [ ] `ai_clients.py`          (Grok model fallback chain)
- [ ] `portfolio_manager.py`   (ETF universe — 15 stocks + 8 ETFs)
- [ ] `dashboard.html`         (redesigned + fees-status row)

## Railway environment variables

Set BEFORE redeploy:

- [ ] `NOVATRADE_FORCE_TURTLE_MIGRATION=1`   — flips classic playbook → Turtle on next boot, then marks migrated
- [ ] `GROK_MODEL=grok-4`                    — optional override; if unset, fallback chain runs

## After redeploy — check the next log batch

Look for these specific strings within 5 minutes of restart:

### Turtle migration ran
- [ ] `🐢 MIGRATION: claude playbook flipped to Turtle (previous archived)`
- [ ] `🐢 MIGRATION: grok playbook flipped to Turtle (previous archived)`

If you don't see these → either the env var wasn't set, or the strategy JSON files on Railway's `/data` volume are already on Turtle (idempotent — won't re-migrate).

### Grok working
- [ ] Either no `⚠️ Grok research failed` lines at all, OR
- [ ] One `⚠️ Grok research failed: ... grok-4.3 ... 404` followed by a successful response (means fallback to grok-4 worked)

If you see all 4 models fail with 404 → check your xAI console at console.x.ai → Models → see what your team has access to → set `GROK_MODEL=<that model>` in Railway env vars.

### Fees correct
- [ ] At next crypto cycle, look for `📐 ... TP floored to $X (fee-aware minimum, BNB-disc off)` OR similar — the parenthetical confirms the new logic is running
- [ ] Open `/crypto_status` endpoint in browser: should now include a `"fees"` object with `round_trip: 0.0006` (or 0.00057 with BNB)

### Turtle gates firing
At the next AI cycle (hourly), look for ONE of:
- [ ] `🐢 GATE PASS BTCUSDT: Turtle System 1 breakout — ATR=$X, 2N stop=$Y`
- [ ] `🐢 GATE REJECT BTCUSDT: Turtle active (claude), no 20d breakout`

If you see neither, the playbook isn't `strategy_type: turtle` (despite the migration). Open `/strategy/claude` and `/strategy/grok` and confirm `"strategy_type": "turtle"` is in both.

### Dashboard sane
- [ ] Open the bot URL in browser
- [ ] Top hero row: equity, day P&L, closed trades, win rate, open positions — all populated
- [ ] Health strip: 6 items (bot, turtle, claude, grok, fees, data) — all green except fees may show "warn" if no BNB
- [ ] AI Battle panel: win rate shows `50%` etc (NOT `5000%` — the old double-mult bug)
- [ ] Recent closes table: shows last 20 with no obvious duplicates

## Optional: Get the BNB fee discount working

Saves 5% on every trade fee. Worth ~$0.20–1.00/month at your current volume.

1. Go to Binance.US → Trade → buy $5 worth of BNB at market (uses 0.02% taker = $0.001 fee)
2. Wait one full crypto cycle (60 min). Logs should show:
   ```
   🔶 BNB fee discount ACTIVE: holding $X.XX BNB → effective round-trip fee 0.057%
   ```
3. The dashboard `fees` health item flips from `0.060% rt · no BNB` → `0.057% rt · 🔶 BNB on`

## Rollback procedure

If anything goes catastrophically wrong:

1. **Disable migration:** Railway → Variables → set `NOVATRADE_FORCE_TURTLE_MIGRATION=0` (or delete the var entirely)
2. **Force classic playbook:** Use the `/strategy` POST endpoint (if exposed) OR SSH into Railway and edit `/data/strategy_claude.json` and `/data/strategy_grok.json` directly — change `"strategy_type": "turtle"` back to `"classic"` and restart
3. **Revert files:** Push the previous git commit to GitHub

State is preserved across rolls — `trade_history.json` and the JSON state files survive in Railway's persistent volume.

## Honest expectations after deploy

- **Fewer crypto trades, not more.** Turtle waits for 20-day breakouts. On a quiet/oversold day like the last few logs showed, Turtle correctly does nothing. That's the system working.
- **Win rate will look LOWER than the old classic playbook initially.** Turtle's expected win rate is ~35%. Winners run, losers get cut at 2N. Watch profit factor (gross wins / gross losses), not win rate.
- **First profitable Turtle trade may take days or weeks.** Don't panic if no entries fire for the first 48 hours.
- **The fee fix unblocks trades the bot was previously rejecting.** You should see MORE trade attempts (not necessarily more fills) because the `min_profit_floor` dropped from 1.70% gross to 0.36% gross.

## What the fee fix actually changes

| Metric | Before | After (no BNB) | After (with BNB) |
|---|---|---|---|
| Maker fee | 0.00% | 0.00% | 0.00% |
| Taker fee | 0.01% (wrong) | 0.02% (actual) | 0.019% |
| Round-trip floor | 0.80% (5× actual!) | 0.06% | 0.057% |
| Min gross profit for entry | 1.70% | 0.36% | 0.36% |
| Effect | Most tight-range trades rejected | Tight-range trades clear floor | Same + 5% saved |

The bot was rejecting trades because it thought fees were 13× higher than reality. That's the main reason crypto rarely traded.
