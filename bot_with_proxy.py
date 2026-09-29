                cancel_stock_orders(p["symbol"], "(before low-cash TP close)")
                alpaca("DELETE", f"/v2/positions/{p['symbol']}")
                record_trade("take_profit", p["symbol"], None, p["price"], p["value"],
                             p["owner"].lower(), reason="low-cash auto take-profit",
                             pnl_usd=p["pnl_usd"], pnl_pct=p["pnl_pct"]/100)
                shared_state["claude_positions"] = [s for s in shared_state["claude_positions"] if s != p["symbol"]]
                shared_state["grok_positions"]   = [s for s in shared_state["grok_positions"]   if s != p["symbol"]]
                log(f"✅ SOLD {p['symbol']} — profit locked")
                sold_something = True
            except Exception as e:
                log(f"❌ Sell {p['symbol']}: {e}")

        elif p["pnl_pct"] <= -RULES["stop_loss_pct"] * 100:
            log(f"🛑 [{p['owner']}] Auto stop-loss: {p['symbol']} at {p['pnl_pct']:.1f}%")
            try:
                cancel_stock_orders(p["symbol"], "(before low-cash SL close)")
                alpaca("DELETE", f"/v2/positions/{p['symbol']}")
                record_trade("stop_loss", p["symbol"], None, p["price"], p["value"],
                             p["owner"].lower(), reason="low-cash auto stop-loss",
                             pnl_usd=p["pnl_usd"], pnl_pct=p["pnl_pct"]/100)
                shared_state["claude_positions"] = [s for s in shared_state["claude_positions"] if s != p["symbol"]]
                shared_state["grok_positions"]   = [s for s in shared_state["grok_positions"]   if s != p["symbol"]]
                log(f"✅ SOLD {p['symbol']} — loss cut")
                sold_something = True
            except Exception as e:
                log(f"❌ Sell {p['symbol']}: {e}")

    # Claude decides whether to sell; Grok's agreement (or not) is logged
    # as supporting context only, and is never required to act.
    if c_sell != "NONE" and c_sell in pos_symbols and not sold_something:
        grok_note = ("Grok agrees" if c_sell == g_sell
                     else f"Grok says {g_sell or 'HOLD'} (advisory only)")
        log(f"🔵 Claude decision: SELL {c_sell} to free up cash ({grok_note})")
        log(f"   Claude reason: {(claude_decision or {}).get('sell_reason','')}")
        try:
            cancel_stock_orders(c_sell, "(before low-cash sell)")
            alpaca("DELETE", f"/v2/positions/{c_sell}")
            sold_pos = next((p for p in pos_details if p["symbol"] == c_sell), {})
            record_trade("sell", c_sell, None, sold_pos.get("price"), sold_pos.get("value"),
                         sold_pos.get("owner","shared").lower(),
                         reason=f"low-cash: Claude decision — {(claude_decision or {}).get('sell_reason','')}",
                         pnl_usd=sold_pos.get("pnl_usd"), pnl_pct=(sold_pos.get("pnl_pct",0)/100 if sold_pos.get("pnl_pct") else None))
            shared_state["claude_positions"] = [s for s in shared_state["claude_positions"] if s != c_sell]
            shared_state["grok_positions"]   = [s for s in shared_state["grok_positions"]   if s != c_sell]
            log(f"✅ SOLD {c_sell} — cash freed up for better opportunity")
            sold_something = True
        except Exception as e:
            log(f"❌ Sell {c_sell}: {e}")

    elif c_action == "hold":
        log(f"🔵 Claude decision: HOLD all positions — not worth selling yet")
        if g_action != "hold":
            log(f"   (Grok's read was: {g_action} {g_sell} — advisory only)")
        log(f"   Best position: {max(pos_details, key=lambda x: x['pnl_pct'])['symbol'] if pos_details else 'none'}")

    # ── NEXT STRATEGY LOG ───────────────────────────────────
    c_next = (claude_decision or {}).get("next_buy_target", "")
    g_next = (grok_decision   or {}).get("next_buy_target", "")

    log(f"📋 NEXT BUY TARGET (ready when cash available):")
    if c_next: log(f"   🔵 Claude (decision):  {c_next} — {(claude_decision or {}).get('next_buy_reason','')[:80]}")
    if g_next: log(f"   🔴 Grok (advisory):    {g_next} — {(grok_decision   or {}).get('next_buy_reason','')[:80]}")

    if c_next:
        shared_state["next_buy_target"] = c_next

    log("=" * 50)
    log(f"💸 Low cash cycle complete | Cash: ${cash:.2f} | Positions: {len(positions)}")
    log("=" * 50)

# ── Boot sequence — load persistent data from volume ─────────
# Runs after all modules imported, before trading loop starts
trade_history[:] = _load_trade_history()   # Load into existing list (keeps reference)
_load_shared_state()                        # Restore equity baselines
_load_sleep_state()                         # Restore AI sleep/wake state

def _recover_missing_buy_records():
    """
    If trade_history is empty but Alpaca has open positions,
    inject buy records so history is accurate when positions eventually close.
    Runs once on boot in background thread.
    """
    try:
        if len(trade_history) > 0:
            return  # Already have records — no recovery needed
        positions = alpaca_get("/v2/positions")
        if not positions:
            return
        from datetime import datetime, timezone
        recovered = 0
        for p in positions:
            sym   = p.get("symbol", "")
            qty   = float(p.get("qty", 0))
            entry = float(p.get("avg_entry_price", 0))
            cost  = float(p.get("cost_basis", 0)) or entry * qty
            owner = ("claude" if sym in shared_state.get("claude_positions", [])
                     else "grok" if sym in shared_state.get("grok_positions", [])
                     else "grok")
            if sym and qty > 0 and entry > 0:
                trade_history.append({
                    "action":    "buy",
                    "symbol":    sym,
                    "qty":       qty,
                    "price":     entry,
                    "notional":  round(cost, 2),
                    "owner":     owner,
                    "time":      datetime.now(timezone.utc).isoformat(),
                    "time_et":   datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "reason":    "recovered from Alpaca on boot",
                    "recovered": True,
                })
                log(f"📋 Recovered buy record: {sym} {qty:.4f} shares @ ${entry:.2f} [{owner}]")
                recovered += 1
        if recovered > 0:
            _save_trade_history(trade_history)
            log(f"✅ Boot recovery: {recovered} buy records restored from Alpaca")
    except Exception as e:
        log(f"⚠️ Buy record recovery failed: {e}")

threading.Thread(target=_recover_missing_buy_records, daemon=True).start()

# ── Late injection: sleep + PDT + portfolio (need functions defined after log()) ──
# portfolio_manager needs trade_history, alpaca, prompt_builder
_portfolio_manager._set_context(
    log_fn             = log,
    shared_state_ref   = shared_state,
    trade_history_ref  = trade_history,
    rules              = RULES,
    alpaca_fn          = alpaca,
    prompt_builder_ref = prompt_builder,
    binance_get_fn     = binance_crypto.binance_get if hasattr(binance_crypto, "binance_get") else None,
)
# Boot replay — seeds AI memory from trade history (needs portfolio_manager injected)
try:
    _replay_trade_history_into_memory()
except Exception:
    pass

# Sync Binance trade history from exchange — fetch last 6 months on first boot
# Runs in background thread so it doesn't delay startup. Triggers AI
# memory backfill once the fresh history is on disk.
def _boot_binance_sync():
    try:
        sync_binance_history()
        # After sync completes, replay history again — this catches any new
        # trades the sync brought in and runs the backfill once memory has
        # the latest Binance data on disk.
        try:
            _replay_trade_history_into_memory()
        except Exception as re:
            log(f"⚠️ Post-sync replay failed: {re}")
    except Exception as e:
        log(f"⚠️ Binance history sync failed: {e}")
threading.Thread(target=_boot_binance_sync, daemon=True).start()
# sleep_manager needs get_cash_thresholds (defined ~line 2640)
_sleep_manager._set_context(log, shared_state,
                             get_cash_thresholds_fn = get_cash_thresholds,
                             get_spy_trend_fn       = get_spy_trend,
                             save_state_fn_ref      = _save_all_persistent_state)
_pdt_manager._set_context(
    log_fn                = log,
    shared_state_ref      = shared_state,
    rules                 = RULES,
    alpaca_fn             = alpaca,
    ask_claude_fn         = ask_claude_guarded,
    ask_grok_fn           = ask_grok_guarded,
    parse_json_fn         = parse_json,
    smart_sell_fn         = smart_sell,
    record_trade_fn       = record_trade,
    get_bars_fn           = get_bars,
    compute_indicators_fn = compute_indicators,
)

# ── Core Reserve context wiring ──────────────────────────────
# The reserve module is fully isolated — receives only what it needs
# to fetch prices and place orders on its own behalf. The tactical
# AIs cannot reach into core_reserve's state at all.
if HAVE_CORE_RESERVE:
    try:
        # Stock-price fetcher for Core Reserve. Uses DATA_URL (different domain
        # from trading API). Returns 0.0 on failure — caller handles it.
        # Falls back to bars endpoint if quotes is unavailable (e.g. weekends).
        def _core_reserve_stock_price(symbol: str) -> float:
            try:
                snap_url = f"{DATA_URL}/v2/stocks/{symbol}/quotes/latest"
                headers  = {"APCA-API-KEY-ID": ALPACA_KEY,
                            "APCA-API-SECRET-KEY": ALPACA_SECRET}
                r = requests.get(snap_url, headers=headers, timeout=5)
                if r.ok:
                    quote = r.json().get("quote", {})
                    bid = float(quote.get("bp", 0))
                    ask = float(quote.get("ap", 0))
                    if bid > 0 and ask > 0:
                        return round((bid + ask) / 2, 2)
                    if ask > 0:
                        return ask
                # Fallback: latest bar close (works pre-market / after-hours)
                bars = get_bars(symbol, days=1)
                if bars and len(bars) > 0:
                    last = bars[-1]
                    if isinstance(last, dict) and last.get("c"):
                        return float(last["c"])
            except Exception:
                pass
            return 0.0

        core_reserve._set_context(
            log_fn          = log,
            binance_get_fn  = binance_crypto.binance_get,
            binance_post_fn = binance_crypto.binance_post,
            alpaca_fn       = alpaca,
            wallet_fn       = binance_crypto.get_full_wallet,
            record_trade_fn = record_trade,
            stock_price_fn  = _core_reserve_stock_price,
        )
        log(f"🏦 Core Reserve module loaded — activation threshold ${core_reserve.ACTIVATION_THRESHOLD:.0f}")
    except Exception as _cre:
        log(f"⚠️ Core Reserve init failed: {_cre}")

# ── Strategic Brain context wiring (Phase A: plumbing only) ──────────
# The strategic brain receives the same dependencies needed to do its job
# when activated in Phase B. In Phase A, ENABLE_STRATEGIST=False keeps
# all of this dormant — endpoints respond with state, but no AI calls.
if HAVE_STRATEGIC_BRAIN:
    try:
        # Wallet getter — used by strategic_brain to auto-upgrade strategist
        # model tier when wallet crosses $5,000 threshold.
        def _strategist_wallet() -> float:
            try:
                acct = alpaca("GET", "/v2/account") or {}
                stock_eq = float(acct.get("equity", 0) or 0)
                wallet   = binance_crypto.get_full_wallet() or {}
                crypto_v = float(wallet.get("total_value", 0) or 0)
                return stock_eq + crypto_v
            except Exception:
                return 0.0

        # Trade history getter — strategist reads only its own AI's trades
        def _strategist_trade_history(owner: str = None, limit: int = 30) -> list:
            try:
                # Latest closed trades from the persistent trade history,
                # filtered by owner (claude/grok/core_reserve)
                from portfolio_manager import trade_history as _th
                if not _th:
                    return []
                # Filter to closes only (sell-side actions with pnl_usd populated)
                exit_actions = {"sell", "stop_loss", "take_profit",
                                "trail_stop", "time_stop"}
                results = []
                for t in reversed(_th):
                    if t.get("action") not in exit_actions:
                        continue
                    if owner and t.get("owner") != owner:
                        continue
                    results.append(t)
                    if len(results) >= limit:
                        break
                return results
            except Exception as e:
                log(f"⚠️ Strategist trade history fetch failed: {e}")
                return []

        # Market context — what's happening macro that the strategist should
        # consider when writing strategy. SPY, BTC, VIX, and current positions.
        def _strategist_market_context() -> dict:
            ctx = {}
            try:
                # SPY price
                snap_url = f"{DATA_URL}/v2/stocks/SPY/quotes/latest"
                headers  = {"APCA-API-KEY-ID": ALPACA_KEY,
                            "APCA-API-SECRET-KEY": ALPACA_SECRET}
                r = requests.get(snap_url, headers=headers, timeout=5)
                if r.ok:
                    q = r.json().get("quote", {})
                    bid = float(q.get("bp", 0)); ask = float(q.get("ap", 0))
                    if bid > 0 and ask > 0:
                        ctx["spy_price"] = round((bid + ask) / 2, 2)
                # BTC price
                btc_r = binance_crypto.binance_get(
                    "/api/v3/ticker/price", {"symbol": "BTCUSDT"})
                if btc_r and "price" in btc_r:
                    ctx["btc_price"] = float(btc_r["price"])
                # Combined wallet
                ctx["combined_wallet"] = _strategist_wallet()
                # Open positions count
                try:
                    pos = alpaca("GET", "/v2/positions") or []
                    ctx["stock_positions"] = len(pos) if isinstance(pos, list) else 0
                except Exception:
                    ctx["stock_positions"] = 0
                try:
                    crypto_pos = (binance_crypto.get_full_wallet() or {}).get("tradeable", [])
                    ctx["crypto_positions"] = len(crypto_pos)
                except Exception:
                    ctx["crypto_positions"] = 0
            except Exception as e:
                log(f"⚠️ Strategist market context fetch failed: {e}")
            return ctx

        # Strategist API wrappers — these read the model registry per-call
        # so wallet-tier upgrades take effect automatically. In Phase A,
        # these are NOT called (ENABLE_STRATEGIST is False). They exist so
        # the wiring is verified and Phase B is a 1-line activation.
        def _ask_claude_strategist(prompt: str, system: str = "", max_tokens: int = 4000) -> str:
            spec = strategic_brain.get_active_model("strategist", "claude",
                                                   wallet=_strategist_wallet())
            with httpx.Client(timeout=120) as http:
                res = http.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={"x-api-key": ANTHROPIC_KEY,
                             "anthropic-version": "2023-06-01",
                             "content-type": "application/json"},
                    json={"model": spec["model_id"],
                          "max_tokens": min(max_tokens, spec.get("max_tokens", 4000)),
                          "system": system or "You are a strategic trading AI. Output valid JSON only.",
                          "messages": [{"role": "user", "content": prompt}]},
                )
                if not res.is_success:
                    raise Exception(f"{res.status_code}: {res.text}")
                return res.json()["content"][0]["text"]

        def _ask_grok_strategist(prompt: str, system: str = "", max_tokens: int = 4000) -> str:
            # Build fallback chain: spec first, then known-available models.
            # If GROK_MODEL env var is set, that overrides everything.
            import os
            spec = strategic_brain.get_active_model("strategist", "grok",
                                                   wallet=_strategist_wallet())
            override = os.environ.get("GROK_MODEL", "").strip()
            if override:
                candidates = [override]
            else:
                # Try the spec's model first, then fall back through models
                # known available on the team (console.x.ai).
                candidates = [spec["model_id"]]
                for alt in ["grok-4.20-0309-reasoning",
                            "grok-4.20-0309-non-reasoning",
                            "grok-4.3",
                            "grok-build-0.1"]:
                    if alt not in candidates:
                        candidates.append(alt)
            # Reuse a cached working model first if we have one (saves 404s)
            cached = getattr(_ask_grok_strategist, "_working_model", None)
            if cached:
                candidates = [cached] + [c for c in candidates if c != cached]

            last_err = None
            with httpx.Client(timeout=120) as http:
                for model_id in candidates:
                    try:
                        res = http.post(
                            "https://api.x.ai/v1/chat/completions",
                            headers={"Authorization": f"Bearer {GROK_KEY}",
                                     "Content-Type": "application/json"},
                            json={"model": model_id,
                                  "max_tokens": min(max_tokens, spec.get("max_tokens", 4000)),
                                  "messages": [
                                      {"role": "system", "content": system or "You are a strategic trading AI. Output valid JSON only."},
                                      {"role": "user",   "content": prompt},
                                  ]},
                        )
                        if res.is_success:
                            _ask_grok_strategist._working_model = model_id
                            return res.json()["choices"][0]["message"]["content"]
                        if res.status_code == 404:
                            last_err = f"{model_id} 404"
                            continue
                        raise Exception(f"{res.status_code}: {res.text}")
                    except httpx.HTTPError as e:
                        last_err = f"{model_id}: {e}"
                        continue
            raise Exception(f"All Grok strategist models failed. Last: {last_err}. "
                            f"Set GROK_MODEL env var to a model your team has access to.")

        strategic_brain._set_context(
            log_fn                    = log,
            ask_claude_strategist_fn  = _ask_claude_strategist,
            ask_grok_strategist_fn    = _ask_grok_strategist,
            get_trade_history_fn      = _strategist_trade_history,
            get_market_context_fn     = _strategist_market_context,
            record_trade_fn           = record_trade,
            get_wallet_fn             = _strategist_wallet,
        )
        # Surface the active model spec for visibility
        wallet_now = _strategist_wallet()
        c_spec = strategic_brain.get_active_model("strategist", "claude", wallet=wallet_now)
        g_spec = strategic_brain.get_active_model("strategist", "grok",   wallet=wallet_now)
        active = "ACTIVE" if strategic_brain.ENABLE_STRATEGIST else "DORMANT (Phase A)"
        log(f"🧭 Strategic Brain {active} — wallet ${wallet_now:.2f}")
        log(f"   Claude-Strategist: {c_spec['model_id']} "
            f"(${c_spec['input_cost_per_1m']:.2f}/{c_spec['output_cost_per_1m']:.2f} per 1M)")
        log(f"   Grok-Strategist:   {g_spec['model_id']} "
            f"(${g_spec['input_cost_per_1m']:.2f}/{g_spec['output_cost_per_1m']:.2f} per 1M)")
    except Exception as _sbe:
        log(f"⚠️ Strategic Brain init failed: {_sbe}")



# [_replay_trade_history_into_memory → portfolio_manager.py]
def run_cycle():
    log("── 🤝 Collaboration Cycle ──")
    if not is_market_open():
        log("Market closed."); return

    account   = alpaca("GET", "/v2/account")
    equity    = float(account["equity"])
    cash      = float(account["cash"])
    features  = check_account_features(account, equity)
    pool      = get_trading_pool(equity)

    log(f"💰 REAL Equity: ${equity:.2f} | Cash: ${cash:.2f} | P&L: ${equity-RULES['total_budget']:+.2f}")

    # Update month/year rollover and display gains summary
    update_gain_metrics(equity)
    log(format_gains(equity))

    # ── Execute any pending PDT hold councils ─────────────────
    # These were queued when PDT blocked a sell — now AIs are awake
    pending_councils = {k: v for k, v in shared_state.items()
                        if k.startswith("pdt_council_pending_")}
    for key, council in list(pending_councils.items()):
        sym = council.get("symbol", "")
        pos = council.get("pos", {})
        log(f"📊 Running PDT hold council for {sym} (queued from earlier)...")
        try:
            plan = run_pdt_hold_council(sym, pos, ask_claude_guarded, ask_grok_guarded)
            if plan:
                log(f"   ✅ Council complete: hold {plan.get('hold_days')}d "
                    f"exit=${plan.get('exit_target')} stop=${plan.get('stop_price')}")
        except Exception as e:
            log(f"   ⚠️ Council failed: {e}")
        del shared_state[key]

    # ── Reassess holds with new data if price surged ──────────
    needs_reassess = {k: v for k, v in shared_state.items()
                      if k.startswith("pdt_hold_") and v.get("needs_reassess")}
    for key, plan in list(needs_reassess.items()):
        sym = plan.get("symbol", "")
        log(f"📊 PDT SURGE REASSESS: {sym} price moved significantly — re-running council...")
        try:
            positions = {p["symbol"]: p for p in alpaca("GET", "/v2/positions")}
            if sym in positions:
                new_plan = run_pdt_hold_council(sym, positions[sym], ask_claude_guarded, ask_grok_guarded)
                if new_plan:
                    log(f"   ✅ Updated plan: hold {new_plan.get('hold_days')}d "
                        f"exit=${new_plan.get('exit_target')}")
            else:
                del shared_state[key]  # Position gone
        except Exception as e:
            log(f"   ⚠️ Reassess failed: {e}")
            shared_state[key]["needs_reassess"] = False

    # Check for tier upgrades
    tier_upgraded, tier_data = check_autonomy_tier(equity)
    autonomy = get_autonomy_status(equity)
    if not shared_state["autonomy_mode"]:
        log(f"🎯 Autonomy progress: {autonomy['progress_pct']}% — need ${autonomy['needed']:.2f} for Tier 1 (${autonomy['next_fund']} each AI)")
    else:
        log(f"🔓 Tier {shared_state['autonomy_tier']}: Claude=${shared_state['claude_auto_fund']:.2f} | Grok=${shared_state['grok_auto_fund']:.2f}")
        if autonomy.get("needed", 0) > 0:
            log(f"🎯 Next tier: ${autonomy['needed']:.2f} away — {autonomy.get('next_description','')}")

    if pool["autonomy_active"]:
        log(f"💼 Tier {pool['tier']}: Claude=${pool['claude']:.2f} (sole decision-maker) | Reserve=${pool['reserve']:.2f}")
    else:
        log(f"💼 Pool: ${pool['trading']:.2f} (Claude-managed) | Reserve=${pool['reserve']:.2f} (safe)")

    # Daily loss limit — compare to today's starting equity, not all-time budget
    # Use equity at market open (stored in shared_state) as the baseline
    # This prevents false triggers from cumulative losses across days
    day_start_equity = shared_state.get("day_start_equity", equity)
    if day_start_equity <= 0:
        day_start_equity = equity
    loss_pct = (day_start_equity - equity) / day_start_equity if day_start_equity > equity else 0
    if loss_pct >= RULES["daily_loss_limit_pct"]:
        log(f"🛑 Daily loss limit {loss_pct*100:.1f}% — STOPPING today. "
            f"(start=${day_start_equity:.2f} now=${equity:.2f})")
        return

    positions   = alpaca("GET", "/v2/positions")
    pos_symbols = [p["symbol"] for p in positions]
    open_count  = len(positions)
    log(f"Positions ({open_count}): {pos_symbols or 'none'}")

    track_pnl(positions)
    log(f"📊 Today: Claude ${shared_state['claude_daily_pnl']:+.2f} | Grok ${shared_state['grok_daily_pnl']:+.2f}")

    check_exit_conditions(positions, equity)
    positions   = alpaca("GET", "/v2/positions")
    pos_symbols = [p["symbol"] for p in positions]
    open_count  = len(positions)

    # ── Broker-side stop reconciliation ──────────────────────
    # Every long position gets a resting stop at Alpaca; dangling
    # stops (position gone) are cancelled. Runs every cycle.
    ensure_protective_stops(positions)

    # ── AI HEALTH CHECK ─────────────────────────────────────
    c_ok, g_ok, failover_mode = check_ai_health()

    # ── DYNAMIC CASH THRESHOLDS ──────────────────────────────
