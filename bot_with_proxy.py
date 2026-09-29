    return {
        "chart_section": chart_section,
        "news":          news,
        "market_ctx":    market_ctx,
        "pol_text":      pol_text,
        "pol_trades":    pol_trades,
        "pol_signals":   pol_signals,
        "inv_text":      inv_text,
        "inv_holdings":  inv_holdings,
        "gainers":       gainers,
        "ipos":          ipos,
        "smart_money":   smart_money,
    }

# [get_news_context → moved to market_data.py / intelligence.py]
# [get_fear_greed_index → moved to market_data.py / intelligence.py]
# [get_earnings_calendar → moved to market_data.py / intelligence.py]
# [_trim_trade_history_to_6months → portfolio_manager.py]
def estimate_fees(notional):
    return round(max(notional * 0.0000278, 0.01) + min(notional * 0.000145, 7.27), 4)

def min_profitable_exit(entry_price: float, fee_pct: float = 0.0003,
                         min_profit_pct: float = 0.005) -> float:
    """Calculate minimum sell price that covers fees and slippage."""
    return round(entry_price * (1 + fee_pct + min_profit_pct), 2)

# ── AI Calls ─────────────────────────────────────────────
# [ask_claude → moved to ai_clients.py]
# [ask_grok → moved to ai_clients.py]
# [clean_json_str → moved to ai_clients.py]
# [_expand_r1_keys → moved to ai_clients.py]
# [parse_json → moved to ai_clients.py]
# [ask_with_retry → moved to ai_clients.py]
def is_market_open():
    return alpaca("GET", "/v2/clock").get("is_open", False)

# [get_market_mode → moved to market_data.py / intelligence.py]
def get_trail_pct(symbol):
    """Get volatility-adjusted trailing stop percentage for a stock"""
    if symbol in RULES["volatile_stocks"]:
        return RULES["exit_B_trail_volatile"]   # 8% for volatile
    elif symbol in RULES["stable_stocks"]:
        return RULES["exit_B_trail_stable"]     # 3% for stable
    return RULES["exit_B_trail_default"]        # 5% default

def stock_turtle_check_entry(symbol: str, system: int = 1) -> dict:
    """
    Check if a STOCK is a valid Turtle entry RIGHT NOW.
    Fetches daily bars via existing get_bars helper.
    Returns same shape as binance_crypto.turtle_check_entry().
    """
    try:
        from turtle_math import compute_turtle_signal
    except ImportError as e:
        return {"eligible": False, "reason": f"turtle_math import failed: {e}",
                "entry_level": None, "stop_price": None, "atr": None, "system": system}
    try:
        # get_bars is imported at top of bot_with_proxy.py from market_data.
        # 90 days of daily bars: plenty for 55-day Donchian + ATR(20).
        bars = get_bars(symbol, days=90)
    except Exception as e:
        return {"eligible": False, "reason": f"bar fetch failed: {e}",
                "entry_level": None, "stop_price": None, "atr": None, "system": system}
    if not bars or len(bars) < 56:
        return {"eligible": False, "reason": f"insufficient history ({len(bars) if bars else 0}/56 bars)",
                "entry_level": None, "stop_price": None, "atr": None, "system": system}
    sig = compute_turtle_signal(bars, system=system)
    if sig is None:
        return {"eligible": False, "reason": "could not compute signal",
                "entry_level": None, "stop_price": None, "atr": None, "system": system}
    period = 20 if system == 1 else 55
    if sig["entry_signal"]:
        return {"eligible": True,
                "reason": f"{period}d breakout: ${sig['current_close']:.2f} > ${sig['entry_level']:.2f}",
                "entry_level": sig["entry_level"], "stop_price": sig["stop_price"],
                "atr": sig["atr"], "system": system,
                "donchian_high": sig["entry_level"]}
    return {"eligible": False,
            "reason": f"no {period}d breakout: ${sig['current_close']:.2f} ≤ ${sig['entry_level']:.2f}",
            "entry_level": sig["entry_level"], "stop_price": sig["stop_price"],
            "atr": sig["atr"], "system": system,
            "donchian_high": sig["entry_level"]}


def stock_turtle_check_exit(symbol: str, entry_price: float, atr_at_entry: float,
                           system: int = 1) -> dict:
    """Check if a Turtle stock position should be closed now."""
    try:
        from turtle_math import should_turtle_exit
    except ImportError as e:
        return {"should_exit": False, "reason": f"turtle_math import failed: {e}",
                "exit_level": None}
    try:
        bars = get_bars(symbol, days=90)
    except Exception as e:
        return {"should_exit": False, "reason": f"bar fetch failed: {e}", "exit_level": None}
    if not bars:
        return {"should_exit": False, "reason": "no bars", "exit_level": None}
    return should_turtle_exit(bars, entry_price, atr_at_entry, system=system)


def is_turtle_active_for_stocks() -> bool:
    """
    Returns True iff EITHER AI's STOCK playbook has strategy_type='turtle'.
    Stocks are shared between Claude and Grok, so we activate Turtle on
    stocks whenever either AI's stock playbook says so. Reads the
    per-asset-class playbook — the crypto playbook (mean-reversion)
    must never influence how stocks pick trades.
    """
    try:
        import strategic_brain as _sb
        for ai_name in ("claude", "grok"):
            try:
                cs = _sb.load_strategy_for(ai_name, "stock") or {}
                if cs.get("strategy_type") == "turtle":
                    return True
            except Exception:
                continue
        return False
    except Exception:
        return False


def assign_exit_strategy(symbol, strategy, entry_price, confidence=80, rationale="",
                        atr_at_entry=None, donchian_high=None, system=1):
    """
    Assign exit strategy to a position when it's opened.
    Strategy A = fixed take-profit (fast trades, news-driven)
    Strategy B = trailing stop (momentum/trend plays, let winners run)
    Strategy T = Turtle (2N ATR stop + Donchian breakdown exit, no TP)
    """
    trail_pct = get_trail_pct(symbol)
    cfg = {
        "strategy":    strategy,
        "entry_price": entry_price,
        "peak_price":  entry_price,   # Tracks highest price seen
        "entry_date":  datetime.now().strftime("%Y-%m-%d"),
        "trail_pct":   trail_pct,
        "confidence":  confidence,
        "rationale":   rationale[:100],
    }
    if strategy == "T":
        cfg["atr_at_entry"]  = atr_at_entry
        cfg["donchian_high"] = donchian_high
        cfg["turtle_system"] = system
        if atr_at_entry:
            stop_price = round(entry_price - (2 * atr_at_entry), 4)
            cfg["stop_price"] = stop_price
            log(f"📋 {symbol} exit strategy: T (Turtle System {system}, 2N stop ${stop_price:.2f}, ATR=${atr_at_entry:.2f}) — {rationale[:60]}")
        else:
            log(f"📋 {symbol} exit strategy: T (Turtle, no ATR recorded) — {rationale[:60]}")
    elif strategy == "A":
        log(f"📋 {symbol} exit strategy: A (fixed {RULES['exit_A_take_profit']*100:.0f}% TP) — {rationale[:60]}")
    else:
        log(f"📋 {symbol} exit strategy: B (trailing {trail_pct*100:.0f}% stop, {RULES['exit_B_time_stop_days']}d time) — {rationale[:60]}")
    shared_state["position_exits"][symbol] = cfg

def decide_exit_strategy_solo(symbol, trade_data, bars, ind):
    """
    Single AI decides exit strategy autonomously.
    Called when one AI is making an autonomous trade.
    Claude uses technical signals, Grok uses momentum signals.
    """
    # Heuristic rules (fast, no API call needed for autonomous trades)
    # Read from new compact "flags" field — or legacy "signals" list for back-compat
    flags_raw   = trade_data.get("f") or trade_data.get("flags") or ""
    signals_raw = trade_data.get("signals", [])
    # Merge both into one lowercase string for keyword matching
    signals_str = (flags_raw + " " + " ".join(signals_raw)).lower()

    # Strategy B signals (trailing — let it run)
    b_signals = [
        ind and ind.get("mom_5d", 0) and abs(ind["mom_5d"]) > 3,  # Strong momentum
        "ipo"        in signals_str,   # IPO momentum
        "momentum"   in signals_str,   # Momentum play
        "breakout"   in signals_str,   # Breakout
        ind and ind.get("vol_ratio", 1) > 1.5,                    # High volume
    ]

    # Strategy A signals (fixed — take profit quickly)
    a_signals = [
        "news"       in signals_str,   # News-driven (can reverse fast)
        "politician" in signals_str,   # Politician signal
        "earnings"   in signals_str,   # Earnings play
    ]

    b_count = sum(1 for s in b_signals if s)
    a_count = sum(1 for s in a_signals if s)

    if b_count >= 2:
        return "B", f"momentum signals ({b_count} B-signals) → let it run"
    elif a_count >= 2:
        return "A", f"news/event driven ({a_count} A-signals) → take quick profit"
    else:
        # Default: high confidence = B (trust the signal), low = A (take what you can)
        conf = trade_data.get("confidence", 80)
        if conf >= 88:
            return "B", f"high confidence {conf}% → trailing stop"
        else:
            return "A", f"moderate confidence {conf}% → fixed take-profit"

# [get_spy_trend → moved to market_data.py / intelligence.py]
# [record_intraday_buy → pdt_manager.py]
# [is_day_trade → pdt_manager.py]
# [get_stock_tier → pdt_manager.py]
# [reset_intraday_buys_if_new_day → pdt_manager.py]
# [check_pdt_safe → pdt_manager.py]
# [run_pdt_hold_council → pdt_manager.py]
# [_pdt_fallback_plan → pdt_manager.py]
# [check_pdt_hold_plans → pdt_manager.py]
# [get_pdt_decision → pdt_manager.py]
# [get_pdt_status → pdt_manager.py]

# ══════════════════════════════════════════════════════════════
# BROKER-SIDE PROTECTIVE STOPS (P0b)
# Every long stock position gets a real resting STOP order at
# Alpaca so downside protection survives bot restarts/outages.
# A resting stop HOLDS the shares — every sell/close path must
# cancel it first (see cancel_stock_orders calls in exit paths).
# ══════════════════════════════════════════════════════════════

def _is_option_symbol(symbol):
    """OCC option symbols end in C/P + 8-digit strike (e.g. AAPL240119C00190000)."""
    return len(symbol) > 12 and symbol[-9] in ("C", "P") and symbol[-8:].isdigit()

def get_open_stock_orders(symbol=None):
    """List open Alpaca orders, optionally filtered to one symbol."""
    try:
        path = "/v2/orders?status=open&limit=500"
        if symbol:
            path += f"&symbols={symbol}"
        return alpaca("GET", path) or []
    except Exception as e:
        log(f"⚠️ [STOP] open-orders fetch failed ({symbol or 'all'}): {e}")
        return []

def cancel_stock_orders(symbol, why=""):
    """Cancel ALL open orders for a symbol. Returns count cancelled.
    Never raises — safe to call unconditionally before any sell/close."""
    n = 0
    for o in get_open_stock_orders(symbol):
        try:
            alpaca("DELETE", f"/v2/orders/{o['id']}")
            log(f"   [STOP] Cancelled {o.get('type','?')} {o.get('side','?')} "
                f"order for {symbol} (id={str(o.get('id',''))[:8]}...) {why}")
            n += 1
        except Exception as ce:
            log(f"   ⚠️ [STOP] Cancel failed {symbol} {str(o.get('id',''))[:8]}: {ce}")
    return n

def compute_protective_stop_price(symbol, entry_price):
    """Stop level mirroring the software exit logic: Turtle 2N stop if
    assigned in position_exits, else the universal hard stop."""
    cfg = shared_state.get("position_exits", {}).get(symbol, {})
    sp = cfg.get("stop_price")
    if sp and sp > 0:
        return sp
    return entry_price * (1 - RULES["exit_A_stop_loss"])

def place_stock_protective_stop(symbol, qty, stop_price):
    """Submit a resting STOP sell at Alpaca. Tries GTC first, falls back
    to DAY (fractional GTC support varies). Non-fatal on failure — the
    software stop monitor stays active either way. Returns order or None."""
    if _is_option_symbol(symbol):
        log(f"   [STOP] {symbol} looks like an option — software-managed only")
        return None
    if qty != int(qty):
        log(f"   [STOP] {symbol} qty={qty} is fractional — Alpaca rejects stop "
            f"orders on fractional share quantities, software stop still active")
        return None
    # Alpaca price increments: $0.01 at/above $1, $0.0001 below
    stop_price = round(stop_price, 2) if stop_price >= 1 else round(stop_price, 4)
    if stop_price <= 0 or qty <= 0:
        log(f"   [STOP] invalid stop for {symbol} (qty={qty}, stop={stop_price}) — skipped")
        return None
    for tif in ("gtc", "day"):
        try:
            order = alpaca("POST", "/v2/orders", {
                "symbol": symbol, "qty": str(qty),
                "side": "sell", "type": "stop",
                "stop_price": str(stop_price),
                "time_in_force": tif,
            })
            log(f"   [STOP] Broker stop resting for {symbol}: {qty} @ ${stop_price} "
                f"({tif.upper()}, order {str(order.get('id',''))[:8]}...)")
            return order
        except Exception as se:
            log(f"   [STOP] {tif.upper()} stop rejected for {symbol}: {str(se)[:120]}")
    log(f"   [STOP] NOT placed for {symbol} -- software stop still active")
    return None

def _arm_stop_after_buy(order, symbol, fallback_entry=0):
    """After a BUY submits, wait briefly for the fill and place the broker
    stop. Unfilled orders are picked up by ensure_protective_stops later."""
    try:
        time.sleep(2)
        od = alpaca("GET", f"/v2/orders/{order['id']}")
        filled_qty = float(od.get("filled_qty") or 0)
        if od.get("status") == "filled" and filled_qty > 0:
            entry = float(od.get("filled_avg_price") or 0) or fallback_entry
            if entry > 0:
                place_stock_protective_stop(
                    symbol, filled_qty,
                    compute_protective_stop_price(symbol, entry))
                return
        log(f"   [STOP] {symbol} buy not filled yet ({od.get('status','?')}) — "
            f"stop deferred to reconciler")
    except Exception as e:
        log(f"   [STOP] arm-after-buy error {symbol}: {e} — reconciler will cover")

def ensure_protective_stops(positions):
    """Reconciler — runs every cycle. (1) Cancels DANGLING stop orders
    (symbol no longer held) so a triggered stray stop can never sell
    shares we don't have. (2) Places a missing stop for any long stock
    position without one (catches late limit fills, restarts, manual
    cancels). (3) Cleans tracker state for positions that vanished —
    stop filled at the broker while the bot was down."""
    try:
        open_orders = get_open_stock_orders()
        held = {p["symbol"]: p for p in positions}

        # 1. Dangling stop sells → cancel
        for o in open_orders:
            if "stop" in (o.get("type") or "") and o.get("side") == "sell" \
                    and o.get("symbol") not in held:
                try:
                    alpaca("DELETE", f"/v2/orders/{o['id']}")
                    log(f"   [STOP] Cancelled DANGLING stop for {o.get('symbol')} "
                        f"(no position — exit/stop already filled)")
                except Exception as ce:
                    log(f"   ⚠️ [STOP] Dangling cancel failed {o.get('symbol')}: {ce}")

        # 2. Unprotected longs → place stop
        stop_syms = {o.get("symbol") for o in open_orders
                     if "stop" in (o.get("type") or "") and o.get("side") == "sell"}
        sell_syms = {o.get("symbol") for o in open_orders if o.get("side") == "sell"}
        for sym, p in held.items():
            if _is_option_symbol(sym):
                continue
            qty = float(p.get("qty", 0) or 0)
            if qty <= 0:
                continue  # shorts stay software-managed for now
            if sym in stop_syms:
                continue  # already protected
            if sym in sell_syms:
                continue  # an exit sell is already working — never double-sell
            qty_avail = float(p.get("qty_available", qty) or 0)
            if qty_avail <= 0:
                log(f"   [STOP] {sym}: no available qty (held by other orders) — skipped")
                continue
            entry = float(p.get("avg_entry_price", 0) or 0)
            if entry <= 0:
                continue
            place_stock_protective_stop(sym, qty_avail,
                                        compute_protective_stop_price(sym, entry))

        # 3. Vanished positions → clean trackers
        open_syms = {o.get("symbol") for o in open_orders}
        for sym in list(shared_state.get("position_exits", {}).keys()):
            if sym not in held and sym not in open_syms:
                log(f"   [STOP] {sym} tracked but no position/orders — "
                    f"broker stop likely filled; cleaning trackers")
                shared_state["position_exits"].pop(sym, None)
                shared_state["claude_positions"] = [s for s in shared_state["claude_positions"] if s != sym]
                shared_state["grok_positions"]   = [s for s in shared_state["grok_positions"]   if s != sym]
    except Exception as e:
        log(f"⚠️ ensure_protective_stops error: {e}")

def smart_sell(symbol, reason, pos):
    """Execute a smart limit sell, fall back to market order.
    Checks PDT rule + uses projections to decide hold-overnight vs sell."""
    # ── PDT projection-based decision ────────────────────────
    try:
        account       = alpaca("GET", "/v2/account")
        equity        = float(account.get("equity", 55))
        current_price = float(pos.get("current_price", 0)) or \
                        float(pos.get("avg_entry_price", 0))
        entry_price   = float(pos.get("avg_entry_price", 0))
        projections   = shared_state.get("last_projections", {})

        pdt = get_pdt_decision(symbol, equity, current_price,
                               entry_price, projections)

        if pdt["action"] == "hold_overnight":
            log(f"🌙 PDT HOLD: {pdt['reason']}")
            log(f"   Day trades: {pdt['pdt_used']}/3 used | "
                f"Proj: {pdt['proj_bias'].upper()} | "
                f"Tomorrow: {pdt.get('expected_tomorrow', 'N/A')}")

            # Run hold council if not already planned for this symbol
            plan_key = f"pdt_hold_{symbol}"
            if plan_key not in shared_state:
                log(f"   🤝 Triggering PDT hold council for {symbol}...")
                # Council runs in background — AIs will be called
                shared_state[f"pdt_council_pending_{symbol}"] = {
                    "symbol": symbol, "pos": pos, "reason": reason
                }
            else:
                existing = shared_state[plan_key]
                log(f"   📋 Existing hold plan: exit=${existing.get('exit_target')} "
                    f"in {existing.get('hold_days')}d | "
                    f"stop=${existing.get('stop_price')}")

            # Update stop if tighter
            if pdt.get("new_stop") and symbol in shared_state.get("position_exits", {}):
                old_stop = shared_state["position_exits"][symbol].get("stop_price", 0)
                new_stop = pdt["new_stop"]
                if new_stop > old_stop:
                    shared_state["position_exits"][symbol]["stop_price"] = new_stop
                    log(f"   🛡️ Trail stop updated: ${old_stop} → ${new_stop}")
            return False

        # Override: sell despite PDT (losing + bearish)
        if pdt.get("override"):
            log(f"⚠️ PDT OVERRIDE: {pdt['reason']}")

        # PDT-safe or override → count it if it's a day trade
        if is_day_trade(symbol):
            used = shared_state.get("day_trade_count", 0)
            shared_state["day_trade_count"] = used + 1
            today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
            shared_state.setdefault("day_trade_dates", []).append(today)
            shared_state["day_trade_dates"] = shared_state["day_trade_dates"][-5:]
            log(f"📋 PDT: day trade #{used+1}/3 — {symbol}")

    except Exception as e:
        log(f"⚠️ PDT check error: {e} — proceeding with sell")

    # ── Cancel any resting broker stop first — it HOLDS the shares and
    # would block every sell method below (and could double-sell later).
    cancel_stock_orders(symbol, "(before exit sell)")

    last_err = None
    # Method 1: Limit sell at mid-price
    try:
        snap_url = f"{DATA_URL}/v2/stocks/{symbol}/quotes/latest"
        headers  = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}
        snap_res = requests.get(snap_url, headers=headers, timeout=5)
        if snap_res.ok:
            quote = snap_res.json().get("quote", {})
            bid   = float(quote.get("bp", 0))
            ask   = float(quote.get("ap", 0))
            if bid > 0 and ask > 0:
                sell_price = round((bid + ask) / 2, 2)
                qty = pos.get("qty", pos.get("qty_available", "1"))
                alpaca("POST", "/v2/orders", {
                    "symbol": symbol, "qty": str(qty),
                    "side": "sell", "type": "limit",
                    "limit_price": str(sell_price),
                    "time_in_force": "day",
                })
                log(f"✅ LIMIT SELL {symbol} {qty} @ ${sell_price} — {reason}")
                shared_state.get("failed_sells", {}).pop(symbol, None)
                return True
    except Exception as e:
        last_err = str(e)
        log(f"   ⚠️ Method 1 (limit) failed: {last_err[:80]}")
    # Method 2: Market via DELETE
    try:
        alpaca("DELETE", f"/v2/positions/{symbol}")
        log(f"✅ MARKET SELL {symbol} (DELETE) — {reason}")
        shared_state.get("failed_sells", {}).pop(symbol, None)
        return True
    except Exception as e:
        last_err = str(e)
        log(f"   ⚠️ Method 2 (DELETE) failed: {last_err[:80]}")
    # Method 3: Market via POST
    try:
        qty = pos.get("qty", pos.get("qty_available", "1"))
        alpaca("POST", "/v2/orders", {
            "symbol": symbol, "qty": str(qty),
            "side": "sell", "type": "market", "time_in_force": "day",
        })
        log(f"✅ MARKET SELL {symbol} (POST) — {reason}")
        shared_state.get("failed_sells", {}).pop(symbol, None)
        return True
    except Exception as e:
        last_err = str(e)
        log(f"   ⚠️ Method 3 (POST market) failed: {last_err[:80]}")
    # Method 4: Notional sell
    try:
        market_val = float(pos.get("market_value", 0))
        if market_val > 0:
            alpaca("POST", "/v2/orders", {
                "symbol": symbol,
                "notional": str(round(market_val, 2)),
                "side": "sell", "type": "market", "time_in_force": "day",
            })
            log(f"✅ NOTIONAL SELL {symbol} ${market_val:.2f} — {reason}")
            shared_state.get("failed_sells", {}).pop(symbol, None)
            return True
    except Exception as e:
        last_err = str(e)
        log(f"   ⚠️ Method 4 (notional) failed: {last_err[:80]}")
    # All methods failed — mark restricted after 3 attempts
    log(f"❌ ALL SELL METHODS FAILED for {symbol}: {last_err}")
    if "403" in str(last_err) or "Forbidden" in str(last_err):
        if "failed_sells" not in shared_state:
            shared_state["failed_sells"] = {}
