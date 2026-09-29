
        fails = shared_state["failed_sells"].get(symbol, 0) + 1
        shared_state["failed_sells"][symbol] = fails
        if fails >= 3:
            if "restricted_positions" not in shared_state:
                shared_state["restricted_positions"] = set()
            shared_state["restricted_positions"].add(symbol)
            log(f"🔒 {symbol} marked RESTRICTED after {fails} failed attempts — close manually in Alpaca")
            shared_state["failed_sells"].pop(symbol, None)
    return False

def check_exit_conditions(positions, equity):
    """
    Strategy-aware exit system.
    Each position uses whichever strategy was assigned at entry:
    Strategy A: Fixed 7% take-profit + 4% stop + no time limit
    Strategy B: Trailing stop + 4% hard stop + 3-day time stop
    """
    today = datetime.now().strftime("%Y-%m-%d")
    quick_tp = get_quick_take_profit_pct(equity)

    for pos in positions:
        symbol       = pos["symbol"]
        pnl_pct      = float(pos["unrealized_plpc"])
        pnl_usd      = float(pos["unrealized_pl"])
        current_price = float(pos["current_price"])
        owner        = "Claude" if symbol in shared_state["claude_positions"] else "Grok"

        # Get exit config for this position
        exit_cfg  = shared_state["position_exits"].get(symbol, {})
        strategy  = exit_cfg.get("strategy", "A")
        entry_price = exit_cfg.get("entry_price", current_price)
        entry_date  = exit_cfg.get("entry_date", today)
        trail_pct   = exit_cfg.get("trail_pct", RULES["exit_B_trail_default"])

        # ── UNIVERSAL: Hard stop-loss (both strategies) ───────
        if pnl_pct <= -RULES["exit_A_stop_loss"]:
            log(f"🛑 [{owner}] STOP LOSS {symbol} ({pnl_pct*100:.1f}%) strategy={strategy}")
            if smart_sell(symbol, "stop loss", pos):
                record_trade("stop_loss", symbol, pos.get("qty"), current_price,
                             float(pos.get("market_value", 0)), owner.lower(),
                             reason="stop loss triggered", pnl_usd=pnl_usd,
                             pnl_pct=pnl_pct, strategy=strategy,
                             entry_price=entry_price)
                shared_state["claude_positions"] = [s for s in shared_state["claude_positions"] if s != symbol]
                shared_state["grok_positions"]   = [s for s in shared_state["grok_positions"]   if s != symbol]
                shared_state["position_exits"].pop(symbol, None)
            continue

        # ── UNIVERSAL: Quick take-profit, all strategies ──────
        # Bank any real gain instead of waiting for the strategy-specific
        # 8% target — maximize round-trips and keep cash working.
        # Applies to Turtle (strategy T) too now — every tier has a
        # tp_pct (see portfolio_manager.py stock_tiers), so this always
        # fires before the A/B/T-specific logic below gets a chance to
        # hold out for a bigger, slower move.
        if quick_tp is not None and pnl_pct >= quick_tp:
            log(f"⚡ [{owner}] QUICK TP {symbol} +{pnl_pct*100:.1f}% >= {quick_tp*100:.1f}% "
                f"(tier: equity ${equity:.0f}) | +${pnl_usd:.2f}")
            if smart_sell(symbol, "quick take-profit (small-account velocity)", pos):
                record_trade("take_profit", symbol, pos.get("qty"), current_price,
                             float(pos.get("market_value", 0)), owner.lower(),
                             reason=f"quick take-profit (tier tp={quick_tp*100:.1f}%)",
                             pnl_usd=pnl_usd, pnl_pct=pnl_pct, strategy=strategy,
                             entry_price=entry_price)
                shared_state["claude_positions"] = [s for s in shared_state["claude_positions"] if s != symbol]
                shared_state["grok_positions"]   = [s for s in shared_state["grok_positions"]   if s != symbol]
                shared_state["position_exits"].pop(symbol, None)
            continue

        # ── STRATEGY T: Turtle (2N ATR stop + Donchian breakdown) ─
        # Turtle bypasses A/B entirely. The ONLY two exit conditions are:
        #   1. Price hits the 2N stop set at entry (a 1-unit loss)
        #   2. Price closes below the 10-day Donchian low (or 20-day for System 2)
        # No fixed TP. No trailing. No time stop.
        if strategy == "T":
            t_atr    = exit_cfg.get("atr_at_entry")
            t_system = exit_cfg.get("turtle_system", 1)
            if t_atr and t_atr > 0:
                try:
                    t_exit = stock_turtle_check_exit(symbol, entry_price, t_atr, system=t_system)
                except Exception as _te:
                    log(f"   ⚠️ [T] {symbol}: exit check failed: {_te}")
                    t_exit = {"should_exit": False, "reason": "exit check error"}

                if t_exit.get("should_exit"):
                    reason = t_exit.get("reason", "turtle exit")
                    log(f"🐢 [T] [{owner}] TURTLE EXIT {symbol} {pnl_pct*100:+.1f}% — {reason}")
                    if smart_sell(symbol, f"turtle exit — {reason}", pos):
                        # Distinguish stop vs trend-end for recording
                        is_stop = "2N stop" in reason
                        record_trade("stop_loss" if is_stop else "take_profit",
                                     symbol, pos.get("qty"), current_price,
                                     float(pos.get("market_value", 0)), owner.lower(),
                                     reason=f"turtle {reason}",
                                     pnl_usd=pnl_usd, pnl_pct=pnl_pct, strategy="T",
                                     entry_price=entry_price)
                        shared_state["claude_positions"] = [s for s in shared_state["claude_positions"] if s != symbol]
                        shared_state["grok_positions"]   = [s for s in shared_state["grok_positions"]   if s != symbol]
                        shared_state["position_exits"].pop(symbol, None)
                else:
                    # Show current 2N stop level for log clarity
                    stop2n = entry_price - (2 * t_atr)
                    log(f"   🐢 [T] {symbol}: {pnl_pct*100:+.2f}% | 2N stop=${stop2n:.2f} | holding")
            else:
                # ATR missing — degrade to a simple 8% stop, don't get stuck
                if pnl_pct <= -0.08:
                    log(f"🛑 [T-FALLBACK] {symbol} {pnl_pct*100:.1f}% — no ATR, using 8% fallback stop")
                    if smart_sell(symbol, "turtle fallback stop (no ATR)", pos):
                        record_trade("stop_loss", symbol, pos.get("qty"), current_price,
                                     float(pos.get("market_value", 0)), owner.lower(),
                                     reason="turtle fallback stop", pnl_usd=pnl_usd,
                                     pnl_pct=pnl_pct, strategy="T",
                                     entry_price=entry_price)
                        shared_state["claude_positions"] = [s for s in shared_state["claude_positions"] if s != symbol]
                        shared_state["grok_positions"]   = [s for s in shared_state["grok_positions"]   if s != symbol]
                        shared_state["position_exits"].pop(symbol, None)
                else:
                    log(f"   🐢 [T] {symbol}: {pnl_pct*100:+.2f}% (no ATR — fallback monitoring)")
            continue   # Don't fall through to A/B

        # ── STRATEGY A: Dynamic projection take-profit (proj_get_exit_guidance) ─
        if strategy == "A":
            # Try projection_engine dynamic TP first
            should_proj_exit = False
            proj_exit_price  = 0.0
            proj_reason      = ""
            try:
                bars_tp = get_bars(symbol, days=10)
                ind_tp  = compute_indicators(bars_tp) if bars_tp else None
                if ind_tp:
                    guidance = proj_get_exit_guidance(
                        symbol, bars_tp, ind_tp,
                        entry_price, current_price, pnl_pct
                    )
                    if guidance.get("conf", 0) >= 55:
                        should_proj_exit = guidance["should_exit"]
                        proj_exit_price  = guidance["exit_price"]
                        proj_reason      = guidance["reason"]
                        # Also honour projection stop level
                        if current_price <= guidance["stop_price"] and pnl_pct < 0:
                            log(f"🛑 [A-PROJ] [{owner}] PROJ STOP {symbol} "
                                f"below proj_low stop=${guidance['stop_price']:.2f}")
                            if smart_sell(symbol, f"proj stop {guidance['stop_price']}", pos):
                                record_trade("stop_loss", symbol, pos.get("qty"), current_price,
                                             float(pos.get("market_value", 0)), owner.lower(),
                                             reason=f"strategy A proj stop — {proj_reason}",
                                             pnl_usd=pnl_usd, pnl_pct=pnl_pct, strategy="A-proj",
                                             entry_price=entry_price)
                                shared_state["claude_positions"] = [s for s in shared_state["claude_positions"] if s != symbol]
                                shared_state["grok_positions"]   = [s for s in shared_state["grok_positions"]   if s != symbol]
                                shared_state["position_exits"].pop(symbol, None)
                            continue
            except Exception:
                pass  # Fall through to fixed TP

            if should_proj_exit and proj_exit_price > 0:
                log(f"🎯 [A-PROJ] [{owner}] DYNAMIC TP {symbol} "
                    f"+{pnl_pct*100:.1f}% | target=${proj_exit_price:.2f} | {proj_reason}")
                if smart_sell(symbol, f"strategy A dynamic TP — {proj_reason}", pos):
                    record_trade("take_profit", symbol, pos.get("qty"), current_price,
                                 float(pos.get("market_value", 0)), owner.lower(),
                                 reason=f"strategy A dynamic TP — {proj_reason}",
                                 pnl_usd=pnl_usd, pnl_pct=pnl_pct, strategy="A-proj",
                                 entry_price=entry_price)
                    shared_state["claude_positions"] = [s for s in shared_state["claude_positions"] if s != symbol]
                    shared_state["grok_positions"]   = [s for s in shared_state["grok_positions"]   if s != symbol]
                    shared_state["position_exits"].pop(symbol, None)
            elif pnl_pct >= RULES["exit_A_take_profit"]:
                # Fixed fallback: original 7% take-profit
                log(f"🎯 [A] [{owner}] FIXED TP {symbol} +{pnl_pct*100:.1f}% >= {RULES['exit_A_take_profit']*100:.0f}% | +${pnl_usd:.2f}")
                if smart_sell(symbol, "strategy A take-profit", pos):
                    record_trade("take_profit", symbol, pos.get("qty"), current_price,
                                 float(pos.get("market_value", 0)), owner.lower(),
                                 reason="strategy A fixed take-profit", pnl_usd=pnl_usd,
                                 pnl_pct=pnl_pct, strategy="A",
                                 entry_price=entry_price)
                    shared_state["claude_positions"] = [s for s in shared_state["claude_positions"] if s != symbol]
                    shared_state["grok_positions"]   = [s for s in shared_state["grok_positions"]   if s != symbol]
                    shared_state["position_exits"].pop(symbol, None)
            else:
                log(f"   [A] {symbol}: {pnl_pct*100:+.2f}% → target {RULES['exit_A_take_profit']*100:.0f}% | holding")

        # ── STRATEGY B: Trailing stop + time stop ────────────
        elif strategy == "B":
            # Update peak price
            if current_price > exit_cfg.get("peak_price", entry_price):
                old_peak = exit_cfg.get("peak_price", entry_price)
                shared_state["position_exits"][symbol]["peak_price"] = current_price
                log(f"   [B] {symbol}: New peak ${current_price:.2f} (was ${old_peak:.2f}) | trailing stop = ${current_price*(1-trail_pct):.2f}")

            peak_price     = shared_state["position_exits"][symbol].get("peak_price", current_price)
            trail_stop     = peak_price * (1 - trail_pct)
            profit_at_peak = (peak_price - entry_price) / entry_price

            # Trailing activates only once position hits trail_activates threshold
            trail_active = profit_at_peak >= RULES["exit_B_trail_activates"]

            # ── Fee-aware floor: trailing stop never drops below entry + fees ──
            min_exit_price = min_profitable_exit(entry_price)
            if trail_active and trail_stop < min_exit_price:
                trail_stop = min_exit_price
                log(f"   [B] {symbol}: trail stop floored to ${min_exit_price:.2f} (entry + fees + 0.5%)")

            if trail_active and current_price <= trail_stop:
                log(f"🎯 [B] [{owner}] TRAILING STOP {symbol} | "
                    f"peak=${peak_price:.2f} trail=${trail_stop:.2f} current=${current_price:.2f} | "
                    f"+{pnl_pct*100:.1f}% | +${pnl_usd:.2f}")
                if smart_sell(symbol, f"strategy B trailing stop (peak ${peak_price:.2f})", pos):
                    record_trade("trail_stop", symbol, pos.get("qty"), current_price,
                                 float(pos.get("market_value", 0)), owner.lower(),
                                 reason=f"strategy B trailing stop peak=${peak_price:.2f}",
                                 pnl_usd=pnl_usd, pnl_pct=pnl_pct, strategy="B",
                                 entry_price=entry_price)
                    shared_state["claude_positions"] = [s for s in shared_state["claude_positions"] if s != symbol]
                    shared_state["grok_positions"]   = [s for s in shared_state["grok_positions"]   if s != symbol]
                    shared_state["position_exits"].pop(symbol, None)

            # Time stop — sell if stuck after N days
            elif RULES["exit_B_time_stop_days"]:
                try:
                    days_held = (datetime.now() - datetime.strptime(entry_date, "%Y-%m-%d")).days
                    if days_held >= RULES["exit_B_time_stop_days"] and pnl_pct < RULES["exit_B_trail_activates"]:
                        log(f"⏰ [B] [{owner}] TIME STOP {symbol} | "
                            f"{days_held} days held, only {pnl_pct*100:+.2f}% — freeing capital")
                        if smart_sell(symbol, f"strategy B time stop ({days_held} days)", pos):
                            record_trade("time_stop", symbol, pos.get("qty"), current_price,
                                         float(pos.get("market_value", 0)), owner.lower(),
                                         reason=f"strategy B time stop {days_held} days held",
                                         pnl_usd=pnl_usd, pnl_pct=pnl_pct, strategy="B",
                                         entry_price=entry_price)
                            shared_state["claude_positions"] = [s for s in shared_state["claude_positions"] if s != symbol]
                            shared_state["grok_positions"]   = [s for s in shared_state["grok_positions"]   if s != symbol]
                            shared_state["position_exits"].pop(symbol, None)
                    else:
                        status = f"trailing active, peak=${peak_price:.2f} stop=${trail_stop:.2f}" if trail_active else f"waiting for +3% to activate trail (currently {pnl_pct*100:+.2f}%)"
                        log(f"   [B] {symbol}: {status} | {days_held}d held")
                except Exception: pass

# ── Decision Engine (Claude primary, Grok support/review) ────

def collaborative_session(equity, cash, positions, pos_symbols, open_count,
                          chart_section, news, market_ctx, features, pool):

    pos_details = [
        f"  {p['symbol']}: entry=${float(p['avg_entry_price']):.2f} "
        f"now=${float(p['current_price']):.2f} "
        f"P&L={round(float(p['unrealized_plpc'])*100,2)}% "
        f"owner={'Claude' if p['symbol'] in shared_state['claude_positions'] else 'Grok'}"
        for p in positions
    ]

    can_short    = features.get("can_short", False)
    short_note   = "SHORT SELLING ENABLED" if can_short else f"Short locked (${features.get('until_short',2000):.0f} away)"

    # ── Stock tier ────────────────────────────────────────────
    tier         = get_stock_tier(equity)
    tier_focus   = tier.get("focus") or RULES["universe"]
    tier_risk    = tier["risk_pct"]
    trade_budget = round(equity * tier_risk, 2)

    tier_note = (
        f"\n📊 STOCK TIER: {tier['note']}"
        f"\n   Trade budget: {tier_risk*100:.0f}% = ${trade_budget:.2f} per position"
        f"\n   Focus stocks: {', '.join(tier_focus[:5])}"
        f"\n   Swing targets: Stop={RULES['stop_loss_pct']*100:.0f}% | TP={RULES['take_profit_pct']*100:.0f}% | Trail activates at +{RULES['exit_B_trail_activates']*100:.0f}%"
    )

    # ── Breakout scan across universe ────────────────────────
    breakout_stocks = []
    try:
        for sym in tier_focus[:8]:
            proj = shared_state.get("last_projections", {}).get(sym, {})
            ind  = proj.get("indicators", {}) if proj else {}
            if ind.get("breakout_signal") == "BULLISH_BREAKOUT":
                breakout_stocks.append(f"{sym} 🚀")
    except Exception:
        pass
    breakout_note = (f"\n🚀 BREAKOUT STOCKS NOW: {', '.join(breakout_stocks)}"
                     if breakout_stocks else
                     "\n(No confirmed breakouts this cycle — scan for dip entries)")

    # PDT warning for AI
    day_trades_used = shared_state.get("day_trade_count", 0)
    intraday_buys   = shared_state.get("intraday_buys", {})
    pdt_note = ""
    if equity < 25000:
        pdt_note = f"\n⚠️ PDT RULE: Account < $25k → max 3 day trades per 5 days ({day_trades_used}/3 used today)"
        if intraday_buys:
            pdt_note += f"\n   Stocks bought today (selling = day trade): {list(intraday_buys.keys())}"
            pdt_note += f"\n   AVOID selling these today unless stop-loss triggered"
    short_note = short_note + tier_note + breakout_note + pdt_note

    # Get full intelligence for this cycle
    pol_text, pol_trades = get_politician_trades()
    pol_signals  = analyze_politician_signals(pol_trades, chart_section)
    inv_text, inv_holdings = get_top_investor_portfolios()
    gainers      = get_biggest_gainers()
    ipos         = get_recent_ipos()
    smart_money  = analyze_smart_money(pol_signals, inv_holdings, gainers)
    pol_mimick   = pol_signals.get("top_mimick", [])
    gainer_syms  = [g["symbol"] for g in gainers if g.get("in_universe")]
    ipo_syms     = [i["symbol"] for i in ipos[:5]]
    hot_ipos     = [i["symbol"] for i in ipos if abs(i.get("mom_5d", 0)) > 5]
    triple_syms  = smart_money.get("triple_confirmation", [])
    top_collab   = smart_money.get("top_collab", [])

    if triple_syms:
        log(f"🔥 Triple confirmation this cycle: {triple_syms}")
    if gainer_syms:
        log(f"📈 Big gainers for collaborative: {gainer_syms}")
    if ipo_syms:
        log(f"🆕 IPOs in play: {ipo_syms}")

    # ── Sub-$5 + under-$25 opportunities: scan + Grok research ──
    # Deliberately separate from RULES["universe"] (which stays
    # "no OTC/penny tickers") — these are explicit, higher-risk
    # opportunistic channels Claude sees as extra context, not a
    # change to the core watchlist.
    penny_candidates = []
    wider_candidates = []
    penny_research   = ""
    try:
        penny_candidates = get_penny_stock_movers()
    except Exception as pe:
        log(f"⚠️ Penny stock scan failed: {pe}")
    try:
        wider_candidates = get_under_25_movers()
    except Exception as we:
        log(f"⚠️ Under-$25 scan failed: {we}")

    # Cache latest scans for the dashboard — informational only, not
    # re-read by the trading logic itself.
    shared_state["last_penny_candidates"] = penny_candidates
    shared_state["last_wider_candidates"] = wider_candidates
    shared_state["last_opportunity_scan_at"] = datetime.now().isoformat()

    # One combined Grok research call covers both lists (dedup by
    # symbol) — no reason to spend two AI calls on overlapping names.
    research_candidates = list({c["symbol"]: c for c in (penny_candidates + wider_candidates)}.values())
    if research_candidates and shared_state.get("grok_healthy", True):
        try:
            log(f"🔴 Grok researching {len(research_candidates)} sub-$25 mover(s) on X/news...")
            penny_research = ask_grok_guarded(
                prompt_builder.build_penny_research_prompt(research_candidates),
                prompt_builder.build_penny_research_system(),
            )
            if penny_research:
                log(f"🔴 Opportunity research: {len(penny_research)} chars returned")
        except Exception as pre:
            log(f"⚠️ Opportunity research failed: {pre}")
            penny_research = ""

    # Crypto trading has been split out of the stock decision cycle —
    # see binance_crypto.py's standalone entrypoint. No crypto context
    # is built or piggybacked onto the stock R1 call any more.

    # ── Round 1: Claude proposes (sole decision-maker) ─────────
    # ── Adaptive prompt — situation-aware, projection-informed, memory-injected ──
    r1_prompt, situation_mode = prompt_builder.build_r1(
        equity          = equity,
        cash            = cash,
        positions       = positions,
        pos_details     = pos_details,
        pool            = pool,
        chart_section   = chart_section,
        news            = news,
        market_ctx      = market_ctx,
        pol_text        = pol_text,
        pol_mimick      = pol_mimick,
        gainers         = gainers,
        ipos            = ipos,
        hot_ipos        = hot_ipos,
        triple_syms     = triple_syms,
        top_collab      = top_collab,
        inv_text        = inv_text,
        short_note      = short_note,
        spy_trend       = shared_state.get("spy_trend", "neutral"),
        features        = features,
        projections     = shared_state.get("last_projections", {}),
        crypto_context  = "",
        penny_stocks    = penny_candidates,
        penny_research  = penny_research,
        wider_stocks    = wider_candidates,
    )
    log(f"🧠 Prompt mode: {situation_mode.upper().replace('_',' ')}")

    log("🔵 Round 1 — Claude proposing (primary decision-maker)...")

    c_ok = shared_state["claude_healthy"]
    if not c_ok:
        log("⚠️ Claude unhealthy — no trade this cycle (Grok is support-only, never trades solo)")
        return [], False, {}

    claude_r1 = safe_ask_claude(r1_prompt, prompt_builder.build_claude_system())
    if not claude_r1:
        log("⚠️ Claude Round 1 failed — holding")
        return [], False, {}

    log(f"🔵 Claude: '{claude_r1.get('strategy_name','')}' | {len(claude_r1.get('proposed_trades',[]))} trades")

    # ── Round 2 — Grok reviews Claude's proposal (support role only) ──
    # Grok no longer trades its own fund or proposes independent trades;
    # it's a second-opinion / risk-check on Claude's picks.
    c_trades = [(t.get("symbol"),t.get("confidence"),t.get("direction","long"))
                for t in (claude_r1 or {}).get("proposed_trades",[])]

    g_ok = shared_state.get("grok_healthy", True)
    grok_review = None
    if g_ok and c_trades:
        log("🔴 Round 2 — Grok reviewing Claude's proposal (support role)...")
        g_review_prompt = f"""Claude is proposing these trades this cycle: {c_trades}.
You are Grok, acting as a SUPPORT / second-opinion risk-check — you do NOT trade your own fund.
Use X/Twitter sentiment and news to flag risk on each symbol.
JSON only: {{"reviewed":[{{"symbol":"NVDA","verdict":"confirm|caution|veto","note":"<12w>"}}]}}"""
        grok_review = ask_with_retry(ask_grok_guarded, g_review_prompt,
            "You are Grok, a support/risk-check reviewer only — not an independent trader. ONLY valid JSON under 400 chars.")
        if grok_review:
            log(f"🔴 Grok review: {len(grok_review.get('reviewed',[]))} trade(s) reviewed")
    elif not g_ok:
        log("⚠️ Grok unhealthy — proceeding on Claude's proposal alone")

    vetoed = set()
    for r in (grok_review or {}).get("reviewed", []):
        verdict = str(r.get("verdict", "")).lower()
        sym     = r.get("symbol")
        if verdict == "veto" and sym:
            vetoed.add(sym)
            log(f"🔴 Grok VETO {sym}: {r.get('note','')[:60]} — skipping")
        elif verdict == "caution" and sym:
            log(f"🟡 Grok caution on {sym}: {r.get('note','')[:60]}")

    # ── Round 3 — Claude confirms its best trades ──────────────
    log("🔵 Round 3 — Claude confirming best trades...")
    c_review_prompt = f"""Your proposed trades: {c_trades}. Grok's risk review: {(grok_review or {}).get('reviewed', [])}.
Your budget: ${pool['claude']:.2f}. Confirm your best 1-2 trades (owner=claude).
Min $8. Confidence 80%+.
JSON: {{"refined_trades":[{{"action":"buy|sell","symbol":"NVDA","notional_usd":15.0,"confidence":85,"f":"flags","r":"<8w>","owner":"claude"}}]}}"""

    # Re-check health here (not c_ok from before Round 1) — a
    # credits_exhausted failure in Round 1 flips claude_healthy to False
    # immediately, and we don't want Round 3 to hit the same dead API.
    claude_r2 = (ask_with_retry(ask_claude_guarded, c_review_prompt,
        "You are Claude confirming your proposed trades. ONLY valid JSON under 500 chars.")
        if shared_state.get("claude_healthy", True) else None)

    if claude_r2: log(f"🔵 Claude confirmed: {len(claude_r2.get('refined_trades',[]))} trades")

    c_ref = (claude_r2 or {}).get("refined_trades", (claude_r1 or {}).get("proposed_trades",[])[:2])
    final_trades = [t for t in c_ref if t.get("symbol") not in vetoed]
    for t in final_trades:
        t["owner"] = "claude"
        t.setdefault("fee_estimate", estimate_fees(float(t.get("notional_usd", 0) or 0)))

    total_alloc = sum(t.get("notional_usd", 0) for t in final_trades)
    log(f"🎯 Final plan: {len(final_trades)} Claude trade(s), Grok-reviewed | ${total_alloc:.2f} to deploy | Cash: ${cash:.2f}")

    for t in final_trades:
        log(f"   [CLAUDE] {t.get('action','?').upper()} {t.get('symbol','?')} "
            f"${t.get('notional_usd',0):.2f} conf={t.get('confidence','?')}% "
            f"fee≈${t.get('fee_estimate',0):.3f}")

    # Update bearish watchlist
    for sym in (claude_r1 or {}).get("bearish_watchlist", []):
        if sym not in shared_state["bearish_watchlist"]:
            shared_state["bearish_watchlist"].append(sym)
    if shared_state["bearish_watchlist"]:
        log(f"📋 Bearish watchlist: {shared_state['bearish_watchlist']}")

    autonomy_unlocked = equity >= 150
    return final_trades, autonomy_unlocked, {"joint_message": f"{len(final_trades)} Claude trade(s), Grok-reviewed"}

def execute_trades(final_trades, cash, pos_symbols, open_count, final_plan, features):
    remaining_cash = cash
    new_positions  = open_count
    can_short      = features.get("can_short", False)

    if final_plan.get("autonomy_unlocked"):
        shared_state["autonomy_mode"] = True
        for sym in final_plan.get("claude_autonomous_stocks",[]):
            if sym not in shared_state["claude_positions"]:
                shared_state["claude_positions"].append(sym)
        for sym in final_plan.get("grok_autonomous_stocks",[]):
            if sym not in shared_state["grok_positions"]:
                shared_state["grok_positions"].append(sym)

    for trade in final_trades:
        action   = trade.get("action","hold").lower()
        symbol   = trade.get("symbol")
        notional = float(trade.get("notional_usd", 0))
        conf     = trade.get("confidence", 0)
        owner    = trade.get("owner", "shared")
        fee_est  = trade.get("fee_estimate", estimate_fees(notional))

        if not symbol: continue
        if conf < RULES["min_confidence"]:
            log(f"⚠️ Skip {symbol} — conf {conf}% < {RULES['min_confidence']}%")
            continue

        # ── Symbol validation ─────────────────────────────────
        # Block placeholder/example symbols from JSON templates
