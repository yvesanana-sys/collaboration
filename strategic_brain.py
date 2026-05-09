"""
strategic_brain.py — Strategic AI layer for NovaTrade.

ARCHITECTURE: STRATEGIST = field commander writing LIVING PLAYBOOKS.

The strategist wakes infrequently (3x daily schedule + playbook-defined
conditions), writes comprehensive standing orders, then goes back to sleep.
The bot + tacticians follow the playbook autonomously. The strategist is
only recalled when the playbook's own conditions say so.

This trains the bot over time rather than creating dependence on constant
AI calls.
"""
import json
import os
from datetime import datetime, timezone, timedelta

# ── Model registry ───────────────────────────────────────────
MODEL_REGISTRY = {
    "strategist": {
        "claude": {
            "default": {
                "model_id":           "claude-sonnet-4-5-20251015",
                "provider":           "anthropic",
                "max_tokens":         6000,
                "input_cost_per_1m":  3.00,
                "output_cost_per_1m": 15.00,
                "notes":              "Sonnet 4.6 — strong playbook reasoning",
            },
            "premium": {
                "model_id":           "claude-opus-4-7",
                "provider":           "anthropic",
                "max_tokens":         6000,
                "input_cost_per_1m":  15.00,
                "output_cost_per_1m": 75.00,
                "notes":              "Opus 4.7 — frontier reasoning at $5k+ wallet",
            },
            "upgrade_threshold_wallet": 5000.0,
        },
        "grok": {
            "default": {
                "model_id":           "grok-4-1-fast-reasoning",
                "provider":           "xai",
                "max_tokens":         6000,
                "input_cost_per_1m":  0.20,
                "output_cost_per_1m": 0.50,
                "notes":              "Grok 4.1 Fast Reasoning — agentic, 2M ctx",
            },
            "premium": {
                "model_id":           "grok-4",
                "provider":           "xai",
                "max_tokens":         6000,
                "input_cost_per_1m":  3.00,
                "output_cost_per_1m": 15.00,
                "notes":              "Grok 4 flagship — premium reasoning",
            },
            "upgrade_threshold_wallet": 5000.0,
        },
    },
    "tactician": {
        "claude": {
            "default": {
                "model_id":           "claude-haiku-4-5-20251001",
                "provider":           "anthropic",
                "max_tokens":         2400,
                "input_cost_per_1m":  1.00,
                "output_cost_per_1m": 5.00,
                "notes":              "Haiku 4.5 — fast, structured-output reliable",
            },
        },
        "grok": {
            "default": {
                "model_id":           "grok-4-1-fast-reasoning",
                "provider":           "xai",
                "max_tokens":         2400,
                "input_cost_per_1m":  0.20,
                "output_cost_per_1m": 0.50,
                "notes":              "Grok fast — reasoning-capable at low cost",
            },
        },
    },
}


def get_active_model(role, ai_name, wallet=0.0):
    if role not in MODEL_REGISTRY or ai_name not in MODEL_REGISTRY[role]:
        return {"model_id": "claude-haiku-4-5-20251001", "provider": "anthropic",
                "max_tokens": 1200, "input_cost_per_1m": 1.0,
                "output_cost_per_1m": 5.0, "notes": "fallback"}
    spec = MODEL_REGISTRY[role][ai_name]
    if "premium" not in spec:
        return dict(spec["default"])
    threshold = spec.get("upgrade_threshold_wallet", float("inf"))
    return dict(spec["premium"] if wallet >= threshold else spec["default"])


STRATEGIST_MODELS = {
    "claude": get_active_model("strategist", "claude", 0.0),
    "grok":   get_active_model("strategist", "grok",   0.0),
}

# ── Phase + schedule ─────────────────────────────────────────
ENABLE_STRATEGIST = True   # Phase B: ACTIVE

SCHEDULE = {
    "pre_market":   {"hour": 9,  "minute": 0,  "purpose": "pre-market playbook — write today's standing orders"},
    "post_close":   {"hour": 16, "minute": 30, "purpose": "post-close review — update playbook for tomorrow"},
    "crypto_close": {"hour": 21, "minute": 0,  "purpose": "crypto session review — refine crypto playbook"},
}

# Hard cooldown — system-enforced regardless of playbook settings
WAKE_COOLDOWN_MINUTES = 120

# ── Hard limits — bot enforces, AI cannot override ───────────
HARD_LIMITS = {
    "max_position_pct_of_pool": 50,
    "stop_loss_pct_min":         3,
    "stop_loss_pct_max":        15,
    "take_profit_pct_min":       1,
    "take_profit_pct_max":     100,
    "max_hold_hours":          168,
    "min_confidence":           50,
    "drawdown_halt_pct":        20,
    "consecutive_loss_gate":     5,
}

# ══════════════════════════════════════════════════════════════
# TURTLE TRADING SYSTEM CONFIGURATION
# Richard Dennis's mechanical trend-following rules adapted for
# crypto + stocks. Long-only (no shorts on Binance.US/Alpaca cash).
# ══════════════════════════════════════════════════════════════
TURTLE_SYSTEM_CONFIG = {
    "enabled":                  True,    # Enable Turtle as a strategy option
    "default_system":           1,       # 1=fast (20/10), 2=slow (55/20)
    "risk_pct_per_trade":       1.0,     # Risk 1% of equity per trade
    "stop_loss_atr_multiple":   2.0,     # Stop at entry - (2 × N)
    "max_concurrent_positions": 4,       # Diversification limit (vs Turtle's 12)
    "max_pyramid_units":        1,       # 1 = no pyramid (Phase 1). Increase to 4 in Phase 2.
    "min_atr_multiple_target":  3.0,     # Don't bother with breakouts where 3N target < 2% gain
    "anti_chop_filter":         True,    # Skip breakout if ATR has been falling for 5+ days (range-bound)
    "btc_macro_filter":         True,    # Skip crypto entries if BTC -3% in 24h
    "spy_macro_filter":         True,    # Skip stock entries if SPY -2% in last day
    "min_trades_before_review": 30,      # Don't abandon Turtle until 30 trades evaluated
    "min_winrate_to_continue":  0.20,    # Below 20% WR over 30+ trades = strategist may revise
    "min_avg_win_atr_multiple": 4.0,     # Require avg winner ≥ 4N to confirm system working
}


def _default_strategy_turtle(ai_name, asset_class="crypto"):
    """
    Turtle Trend-Following playbook — Richard Dennis's mechanical system.
    Long-term growth via catching trends, cutting losers fast, riding winners.

    KEY DIFFERENCES FROM CLASSIC PLAYBOOK:
    - Stop is ATR-based (2N below entry), not percentage-based
    - Exit is Donchian breakdown (10-day low for System 1)
    - Position sized for 1% risk, not pool percentage
    - Tactician notes are STRICTLY mechanical — no prediction allowed

    The strategist will NOT abandon this playbook on losing streaks.
    Turtle is designed to lose 60-70% of trades; profitability comes
    from large winners. Override only after 30+ trades AND sub-20% WR.
    """
    is_crypto = asset_class == "crypto"
    return {
        "id":           f"S-{ai_name}-turtle-{asset_class}",
        "name":         f"Turtle Trend-Follow System 1 ({asset_class.title()})",
        "version":      0,
        "strategy_type": "turtle",   # ← marker so executor knows which exit logic to use
        "active_since": datetime.now(timezone.utc).isoformat(),
        "rules": {
            # ── Entry ────────────────────────────────────────
            "entry_logic": (
                "MECHANICAL ONLY. Enter long when daily close > 20-day Donchian high "
                "(System 1). Skip on strong macro down-days (BTC -3% / SPY -2%). "
                "DO NOT predict — take EVERY breakout that meets criteria."
            ),
            "exit_logic": (
                "Exit when daily close < 10-day Donchian low (System 1). "
                "OR: hard stop at entry - (2 × ATR), whichever fires first. "
                "DO NOT exit early on profit. Let winners run to the breakdown signal."
            ),
            "preferred_indicators":     ["donchian_20", "donchian_10", "atr_20"],
            "preferred_symbols":        [],     # Trade any symbol meeting criteria
            "min_confidence":           60,     # Lower than classic — system is mechanical
            "max_position_pct_of_pool": 25,     # Backup cap; ATR sizing is primary
            "max_concurrent_positions": 4,

            # ── ATR-based stops/sizing (Turtle hallmark) ────
            "stop_loss_pct":            null_marker(),  # see _validate_strategy
            "take_profit_pct":          null_marker(),
            "stop_loss_atr_multiple":   2.0,    # Stop at entry - (2 × N)
            "exit_donchian_period":     10,     # System 1 exit
            "entry_donchian_period":    20,     # System 1 entry
            "risk_pct_per_trade":       1.0,    # Always 1% of equity

            # ── Hold / time ──────────────────────────────────
            "max_hold_hours":           168,    # 1 week per leg, then re-evaluate
            "trail_activate_pct":       0,      # NOT USED — Turtle uses Donchian, not trail
            "trail_pct":                0,

            # ── Conditional responses ────────────────────────
            "on_stop_loss": {
                "action": "no_pause",   # ← KEY DIFFERENCE: Turtle doesn't pause after stops
                "pause_minutes": 0,
                "log_reason": "Turtle stop fired — IMMEDIATELY re-eligible. Losses are part of the system.",
            },
            "on_two_stops_same_session": {
                "action": "no_pause",   # ← Same: don't pause
                "reduce_size_pct": 0,   # No size reduction
                "pause_minutes": 0,
                "log_reason": "2 stops same session is NORMAL for Turtle. Continue mechanically.",
            },
            "on_three_consecutive_losses": {
                "action": "no_pause",   # ← Critical: 3 losses is NORMAL
                "log_reason": "3 consecutive losses is expected. The next big winner pays for them.",
                "wake_strategist": False,
            },
            "on_six_consecutive_losses": {
                "action": "wake_strategist_only",   # 6 in a row → review (still don't pause)
                "log_reason": "6 losses in a row — strategist reviews if anti-chop filter needs adjusting.",
                "wake_strategist": True,
            },
            "on_winning_streak_3": {
                "action": "hold_current_size",
                "log_reason": "Streak — maintain discipline, no oversize.",
            },
            "on_btc_drops_3pct_1h": {
                "action": "halt_altcoin_entries" if is_crypto else "ignore",
                "log_reason": "BTC -3% — pause new crypto entries. Existing positions held.",
            },
            "on_btc_drops_5pct_1h": {
                "action": "halt_altcoin_entries",
                "log_reason": "BTC -5% — halt entries. Existing stops will fire if breached.",
                "wake_strategist": False,   # Turtle stops handle this
            },
            "on_spy_drops_2pct": {
                "action": "halt_stock_entries",
                "log_reason": "SPY -2% — pause new stock entries.",
            },
            "on_sentiment_extreme_fear": {
                "action": "no_change",   # ← KEY: Turtle ignores sentiment, follows price
                "stop_multiplier": 1.0,
                "log_reason": "Sentiment ignored. Turtle follows price, not opinion.",
            },
            "on_sentiment_extreme_greed": {
                "action": "no_change",
                "tp_multiplier": 1.0,
                "log_reason": "Sentiment ignored. Let winners run via Donchian exit.",
            },

            # ── Wake conditions (very conservative) ─────────
            "wake_strategist_if": {
                "consecutive_losses":       6,    # Up from 3 — Turtle expects losses
                "session_drawdown_pct":    20,    # Up from 15
                "days_without_trade":       7,    # No breakout for a week
                "btc_regime_flip":       True,
                "spy_regime_flip":       True,
                "win_rate_below_pct":      15,    # Down from 35 — Turtle WR is 30-40%
                "predicted_vs_actual_gap": 50,    # Up from 25 — Turtle has high variance
                "min_trades_before_eval":  30,    # Don't review until 30 trades
            },

            # ── Tactician training notes — STRICT discipline ─
            "tactician_notes": (
                "TURTLE TREND-FOLLOWING SYSTEM. RULES OVER INSTINCT.\n"
                "1. ENTER long when current price > 20-day Donchian high. NO EXCEPTIONS.\n"
                "2. EXIT when current price < 10-day Donchian low OR stop hits at entry-2N.\n"
                "3. NEVER take profit early. Winners must run to the Donchian exit.\n"
                "4. NEVER skip a valid breakout because it 'feels too high'. The system catches "
                "trends precisely by entering after the move started.\n"
                "5. NEVER predict tops or bottoms. Wait for the signal.\n"
                "6. EXPECT to lose on 60-70% of trades. The 30-40% winners pay for everything.\n"
                "7. Risk EXACTLY 1% of equity per trade — no more, no less.\n"
                "8. After a stop loss: IMMEDIATELY eligible for next breakout. Do not 'cool off'.\n"
                "9. After a winner: IMMEDIATELY eligible. Don't 'wait for pullback'.\n"
                "10. DISCIPLINE IS THE EDGE. The system works only if followed mechanically."
            ),
        },
        "rationale":             "Turtle Trend-Follow System 1 — Richard Dennis methodology adapted for spot-only crypto/stock.",
        "predicted_win_rate":    35,     # Realistic Turtle expectation
        "predicted_avg_pnl_pct": 4.0,    # Avg winner ~ 4N, avg loser ~ 2N
        "predicted_until":       None,
    }


def null_marker():
    """Marker value indicating ATR-based stop is in use (no fixed pct)."""
    return None  # Validator handles this case specially

AUTO_REVERT = {
    "min_trades_under_strategy": 5,
    "win_rate_threshold":        0.40,
    "pnl_threshold_usd":        -5.00,
    "wr_underperformance_pp":    20,
}

STATE_DIR    = "/data"
FALLBACK_DIR = "."

def _state_path(name):
    p = f"{STATE_DIR}/{name}"
    return p if os.path.exists(STATE_DIR) else f"{FALLBACK_DIR}/{name}"


# ── Default playbook ─────────────────────────────────────────
def _default_strategy(ai_name):
    return {
        "id":           f"S-{ai_name}-default",
        "name":         "Default Conservative Baseline",
        "version":      0,
        "active_since": datetime.now(timezone.utc).isoformat(),
        "rules": {
            "entry_logic":              "RSI > 55 + volume spike 1.5x + MACD positive. Min confidence 65.",
            "exit_logic":               "Fixed TP or trailing stop, whichever fires first.",
            "preferred_indicators":     ["RSI", "MACD", "volume_ratio", "EMA_9_21"],
            "preferred_symbols":        [],
            "min_confidence":           65,
            "max_position_pct_of_pool": 25,
            "max_concurrent_positions": 2,
            "stop_loss_pct":            8,
            "take_profit_pct":          16,
            "max_hold_hours":           24,
            "trail_activate_pct":       3,
            "trail_pct":                2.5,
            # Conditional responses — bot executes these autonomously
            "on_stop_loss": {
                "action": "pause_entries", "pause_minutes": 60,
                "log_reason": "Stop fired — cooling off 60m before re-entry",
            },
            "on_two_stops_same_session": {
                "action": "go_defensive", "reduce_size_pct": 50, "pause_minutes": 120,
                "log_reason": "2 stops same session — defensive mode 120m",
            },
            "on_three_consecutive_losses": {
                "action": "halt_new_entries", "wake_strategist": True,
                "log_reason": "3 consecutive losses — halt entries, wake strategist",
            },
            "on_winning_streak_3": {
                "action": "hold_current_size",
                "log_reason": "Winning streak — maintain discipline, no size increase",
            },
            "on_btc_drops_3pct_1h": {
                "action": "halt_altcoin_entries",
                "log_reason": "BTC -3% in 1h — halt altcoin entries",
            },
            "on_btc_drops_5pct_1h": {
                "action": "close_weakest_position", "wake_strategist": True,
                "log_reason": "BTC -5% in 1h — close weakest, wake strategist",
            },
            "on_spy_drops_2pct": {
                "action": "halt_stock_entries",
                "log_reason": "SPY -2% — halt new stock entries",
            },
            "on_sentiment_extreme_fear": {
                "action": "widen_stop", "stop_multiplier": 1.25,
                "log_reason": "Extreme fear — widening stops to avoid noise",
            },
            "on_sentiment_extreme_greed": {
                "action": "tighten_tp", "tp_multiplier": 0.75,
                "log_reason": "Extreme greed — taking profits faster",
            },
            # When to wake the strategist — defined by the playbook itself
            "wake_strategist_if": {
                "consecutive_losses":       3,
                "session_drawdown_pct":    15,
                "days_without_trade":       2,
                "btc_regime_flip":       True,
                "spy_regime_flip":       True,
                "win_rate_below_pct":      35,
                "predicted_vs_actual_gap": 25,
            },
            # Training notes injected into tactician prompt each cycle
            "tactician_notes": (
                "Focus on volume-confirmed breakouts. Avoid low-volume pumps. "
                "Skip the trade when in doubt — missed opportunities beat unnecessary losses. "
                "Never chase a coin that already moved >5% in the past hour."
            ),
        },
        "rationale":             "Default starting playbook. Strategist refines on first activation.",
        "predicted_win_rate":    None,
        "predicted_avg_pnl_pct": None,
        "predicted_until":       None,
    }


def _default_strategy_state(ai_name):
    # Default to TURTLE strategy — long-term trend-following, mechanical discipline
    return {
        "ai_name":               ai_name,
        "model_id":              STRATEGIST_MODELS[ai_name]["model_id"],
        "current_strategy":      _default_strategy_turtle(ai_name, asset_class="crypto"),
        "current_performance":   {
            "trades_under_this_strategy": 0, "wins": 0, "losses": 0,
            "actual_win_rate": 0, "actual_avg_pnl_pct": 0,
            "started_at": datetime.now(timezone.utc).isoformat(),
        },
        "playbook_execution_log": [],
        "strategy_history":       [],
        "last_activation":        None,
        "last_wake_call":         None,
        "last_wake_reason":       None,
        "total_activations":      0,
        "audit_log":              [],
    }


# ── Injected dependencies ────────────────────────────────────
log                   = print
ask_claude_strategist = None
ask_grok_strategist   = None
get_trade_history     = None
get_market_context    = None
get_sentiment_context = None   # NEW — injects news+social summary
record_trade          = None
get_wallet_value      = None


def _set_context(log_fn=None, ask_claude_strategist_fn=None,
                 ask_grok_strategist_fn=None, get_trade_history_fn=None,
                 get_market_context_fn=None, get_sentiment_context_fn=None,
                 record_trade_fn=None, get_wallet_fn=None):
    global log, ask_claude_strategist, ask_grok_strategist
    global get_trade_history, get_market_context, get_sentiment_context
    global record_trade, get_wallet_value
    if log_fn:                    log                   = log_fn
    if ask_claude_strategist_fn:  ask_claude_strategist = ask_claude_strategist_fn
    if ask_grok_strategist_fn:    ask_grok_strategist   = ask_grok_strategist_fn
    if get_trade_history_fn:      get_trade_history     = get_trade_history_fn
    if get_market_context_fn:     get_market_context    = get_market_context_fn
    if get_sentiment_context_fn:  get_sentiment_context = get_sentiment_context_fn
    if record_trade_fn:           record_trade          = record_trade_fn
    if get_wallet_fn:             get_wallet_value      = get_wallet_fn


def _refresh_model_registry():
    global STRATEGIST_MODELS
    wallet = 0.0
    try:
        if get_wallet_value:
            wallet = float(get_wallet_value() or 0)
    except Exception:
        pass
    STRATEGIST_MODELS = {
        "claude": get_active_model("strategist", "claude", wallet),
        "grok":   get_active_model("strategist", "grok",   wallet),
    }
    return wallet


# ── State load/save ──────────────────────────────────────────
_state_cache = {}

def load_strategy(ai_name):
    if ai_name in _state_cache:
        return _state_cache[ai_name]
    path = _state_path(f"strategy_{ai_name}.json")
    try:
        with open(path) as f:
            state = json.load(f)
        default = _default_strategy_state(ai_name)
        for k in default:
            if k not in state:
                state[k] = default[k]
        # Forward-compat: ensure new conditional rule keys exist
        rules = state.get("current_strategy", {}).get("rules", {})
        default_rules = _default_strategy(ai_name)["rules"]
        for key in ["on_stop_loss", "on_two_stops_same_session",
                    "on_three_consecutive_losses", "on_winning_streak_3",
                    "on_btc_drops_3pct_1h", "on_btc_drops_5pct_1h",
                    "on_spy_drops_2pct", "on_sentiment_extreme_fear",
                    "on_sentiment_extreme_greed", "wake_strategist_if",
                    "tactician_notes", "trail_activate_pct", "trail_pct",
                    "max_concurrent_positions", "min_confidence"]:
            if key not in rules:
                rules[key] = default_rules.get(key)
        _state_cache[ai_name] = state
        return state
    except FileNotFoundError:
        state = _default_strategy_state(ai_name)
        _state_cache[ai_name] = state
        save_strategy(ai_name)
        return state
    except Exception as e:
        log(f"warning strategy load failed for {ai_name}: {e}")
        return _default_strategy_state(ai_name)


def save_strategy(ai_name):
    if ai_name not in _state_cache:
        return False
    path = _state_path(f"strategy_{ai_name}.json")
    try:
        with open(path, "w") as f:
            json.dump(_state_cache[ai_name], f, default=str, indent=2)
        return True
    except Exception as e:
        log(f"warning strategy save failed for {ai_name}: {e}")
        return False


def _audit(ai_name, event_type, message, **extra):
    state = load_strategy(ai_name)
    state.setdefault("audit_log", []).append({
        "ts": datetime.now(timezone.utc).isoformat(),
        "type": event_type, "message": message, **extra,
    })
    if len(state["audit_log"]) > 200:
        state["audit_log"] = state["audit_log"][-200:]
    save_strategy(ai_name)
    log(f"STRATEGIST [{ai_name}]: {message}")


# ════════════════════════════════════════════════════════════
# PLAYBOOK EXECUTOR — called every cycle, zero AI cost
# ════════════════════════════════════════════════════════════

def execute_playbook(ai_name, cycle_context):
    """
    Evaluate current cycle against playbook conditional rules.
    Returns directives the bot applies this cycle — no AI call.

    cycle_context keys:
      stops_fired_session   int
      consecutive_losses    int
      winning_streak        int
      btc_change_1h         float  (negative = down)
      spy_change_1h         float
      sentiment             str    extreme_fear|fear|neutral|greed|extreme_greed
      session_drawdown_pct  float
      minutes_since_stop    int|None
      session_win_rate_pct  float
      session_trade_count   int
    """
    state  = load_strategy(ai_name)
    rules  = state.get("current_strategy", {}).get("rules", {})
    result = {
        "block_new_entries":        False,
        "block_altcoins":           False,
        "block_stocks":             False,
        "position_size_multiplier": 1.0,
        "stop_loss_multiplier":     1.0,
        "tp_multiplier":            1.0,
        "close_weakest":            False,
        "wake_strategist":          False,
        "wake_reason":              "",
        "active_rules":             [],
        "tactician_notes":          rules.get("tactician_notes", ""),
    }

    stops      = int(cycle_context.get("stops_fired_session", 0))
    consec     = int(cycle_context.get("consecutive_losses", 0))
    streak     = int(cycle_context.get("winning_streak", 0))
    btc_chg    = float(cycle_context.get("btc_change_1h", 0.0))
    spy_chg    = float(cycle_context.get("spy_change_1h", 0.0))
    sentiment  = str(cycle_context.get("sentiment", "neutral")).lower()
    drawdown   = float(cycle_context.get("session_drawdown_pct", 0.0))
    mins_sl    = cycle_context.get("minutes_since_stop")
    sess_wr    = float(cycle_context.get("session_win_rate_pct", 100))
    sess_trades= int(cycle_context.get("session_trade_count", 0))

    # ── HARD SYSTEM LIMITS (always checked first) ─────────────
    if consec >= HARD_LIMITS["consecutive_loss_gate"]:
        result.update({"block_new_entries": True, "wake_strategist": True,
                       "wake_reason": f"HARD LIMIT: {consec} consecutive losses"})
        result["active_rules"].append("hard_consecutive_loss_gate")
        return result   # Hard gate — return immediately

    if drawdown >= HARD_LIMITS["drawdown_halt_pct"]:
        result.update({"block_new_entries": True, "wake_strategist": True,
                       "wake_reason": f"HARD LIMIT: drawdown {drawdown:.1f}%"})
        result["active_rules"].append("hard_drawdown_halt")
        return result

    # ── PLAYBOOK CONDITIONALS ─────────────────────────────────
    wc = rules.get("wake_strategist_if", {})

    # Single stop — pause entries
    if stops == 1 and mins_sl is not None:
        r = rules.get("on_stop_loss", {})
        pause = int(r.get("pause_minutes", 60))
        if mins_sl < pause:
            result["block_new_entries"] = True
            result["active_rules"].append(f"on_stop_loss (pause {pause - mins_sl:.0f}m left)")

    # Two+ stops — defensive
    if stops >= 2:
        r = rules.get("on_two_stops_same_session", {})
        pause  = int(r.get("pause_minutes", 120))
        reduce = float(r.get("reduce_size_pct", 50))
        if mins_sl is not None and mins_sl < pause:
            result["block_new_entries"]        = True
            result["position_size_multiplier"] = (100 - reduce) / 100
            result["active_rules"].append(f"on_two_stops_same_session")

    # Consecutive losses
    wake_at_consec = int(wc.get("consecutive_losses", 3))
    if consec >= wake_at_consec:
        r = rules.get("on_three_consecutive_losses", {})
        action = r.get("action", "halt_new_entries")
        # Turtle uses "no_pause" — don't block entries on losing streaks
        if action != "no_pause":
            result["block_new_entries"] = True
        if r.get("wake_strategist", True) and not result["wake_strategist"]:
            result["wake_strategist"] = True
            result["wake_reason"]     = f"playbook: {consec} consecutive losses"
        result["active_rules"].append("on_three_consecutive_losses")

    # BTC drops
    if btc_chg <= -3.0:
        result["block_altcoins"] = True
        result["active_rules"].append(f"on_btc_drops_3pct (BTC {btc_chg:.1f}%)")
    if btc_chg <= -5.0:
        r = rules.get("on_btc_drops_5pct_1h", {})
        if r.get("action") == "close_weakest_position":
            result["close_weakest"] = True
        if r.get("wake_strategist", True) and not result["wake_strategist"]:
            result["wake_strategist"] = True
            result["wake_reason"]     = f"playbook: BTC {btc_chg:.1f}% in 1h"
        result["active_rules"].append(f"on_btc_drops_5pct (BTC {btc_chg:.1f}%)")

    # SPY drops
    if spy_chg <= -2.0:
        result["block_stocks"] = True
        result["active_rules"].append(f"on_spy_drops_2pct (SPY {spy_chg:.1f}%)")

    # Sentiment
    if sentiment == "extreme_fear":
        r = rules.get("on_sentiment_extreme_fear", {})
        result["stop_loss_multiplier"] = float(r.get("stop_multiplier", 1.25))
        result["active_rules"].append("on_sentiment_extreme_fear")
    if sentiment == "extreme_greed":
        r = rules.get("on_sentiment_extreme_greed", {})
        result["tp_multiplier"] = float(r.get("tp_multiplier", 0.75))
        result["active_rules"].append("on_sentiment_extreme_greed")

    # Playbook-defined win rate floor
    min_wr = float(wc.get("win_rate_below_pct", 35))
    if sess_trades >= 10 and sess_wr < min_wr and not result["wake_strategist"]:
        result["wake_strategist"] = True
        result["wake_reason"]     = f"playbook: WR {sess_wr:.0f}% < {min_wr:.0f}% floor after {sess_trades} trades"

    # Drawdown threshold from playbook (softer than hard limit)
    pb_drawdown = float(wc.get("session_drawdown_pct", 15))
    if drawdown >= pb_drawdown and not result["wake_strategist"]:
        result["wake_strategist"] = True
        result["wake_reason"]     = f"playbook: drawdown {drawdown:.1f}%"

    # Log fired rules
    if result["active_rules"]:
        try:
            state.setdefault("playbook_execution_log", []).append({
                "ts":          datetime.now(timezone.utc).isoformat(),
                "rules_fired": result["active_rules"],
                "wake":        result["wake_strategist"],
                "wake_reason": result.get("wake_reason", ""),
            })
            if len(state["playbook_execution_log"]) > 100:
                state["playbook_execution_log"] = state["playbook_execution_log"][-100:]
            save_strategy(ai_name)
        except Exception:
            pass

    return result


def get_playbook_summary(ai_name):
    """Compact plaintext playbook summary for tactician prompt injection."""
    state = load_strategy(ai_name)
    strat = state.get("current_strategy", {})
    rules = strat.get("rules", {})
    ver   = strat.get("version", 0)
    name  = strat.get("name", "Default")
    since = (strat.get("active_since") or "")[:10]
    lines = [
        f"STRATEGIST PLAYBOOK v{ver} — {name} (since {since})",
        f"Entry: {rules.get('entry_logic', 'standard')}",
        (f"Exit:  SL={rules.get('stop_loss_pct', 8)}% | "
         f"TP={rules.get('take_profit_pct', 16)}% | "
         f"Hold max {rules.get('max_hold_hours', 24)}h"),
        (f"Size:  Max {rules.get('max_position_pct_of_pool', 25)}% pool | "
         f"Max {rules.get('max_concurrent_positions', 2)} positions | "
         f"Min conf {rules.get('min_confidence', 65)}%"),
    ]
    notes = rules.get("tactician_notes", "")
    if notes:
        lines.append(f"Coach: {notes[:200]}")
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════
# STOP-LOSS GATE — called immediately when any stop fires
# ════════════════════════════════════════════════════════════

def handle_stop_loss_event(ai_name, symbol, pnl_pct,
                           stops_today, consecutive_losses,
                           cycle_context=None):
    """
    Enforces playbook gate logic after a stop fires.
    Returns directives dict — no AI call unless playbook says wake.
    """
    state  = load_strategy(ai_name)
    rules  = state.get("current_strategy", {}).get("rules", {})
    pname  = state.get("current_strategy", {}).get("name", "?")

    result = {
        "gate_entries":    False,
        "gate_minutes":    0,
        "reduce_size":     False,
        "size_multiplier": 1.0,
        "wake_strategist": False,
        "wake_reason":     "",
        "log_message":     "",
    }

    log(f"STOP-LOSS GATE [{ai_name}]: {symbol} {pnl_pct:+.1f}% | "
        f"stops_today={stops_today} consec={consecutive_losses} playbook='{pname}'")

    if stops_today == 1:
        r     = rules.get("on_stop_loss", {})
        pause = int(r.get("pause_minutes", 60))
        result.update({"gate_entries": True, "gate_minutes": pause,
                       "log_message": r.get("log_reason", f"Stop fired — pause {pause}m")})

    elif stops_today >= 2:
        r      = rules.get("on_two_stops_same_session", {})
        pause  = int(r.get("pause_minutes", 120))
        reduce = float(r.get("reduce_size_pct", 50))
        result.update({
            "gate_entries":    True,  "gate_minutes":    pause,
            "reduce_size":     True,  "size_multiplier": (100 - reduce) / 100,
            "log_message":     r.get("log_reason", f"2 stops — defensive {pause}m"),
        })

    wake_at = int(rules.get("wake_strategist_if", {}).get("consecutive_losses", 3))
    if consecutive_losses >= wake_at:
        r = rules.get("on_three_consecutive_losses", {})
        result["gate_entries"] = True
        if r.get("wake_strategist", True):
            result.update({
                "wake_strategist": True,
                "wake_reason": (f"{consecutive_losses} consecutive losses — "
                                f"stop on {symbol} ({pnl_pct:+.1f}%)"),
            })
        result["log_message"] = r.get("log_reason", "Consecutive losses — halting")

    # Hard gate override
    if consecutive_losses >= HARD_LIMITS["consecutive_loss_gate"]:
        result.update({
            "gate_entries": True, "gate_minutes": 240,
            "wake_strategist": True,
            "wake_reason": f"HARD GATE: {consecutive_losses} consecutive losses",
            "log_message": "HARD GATE — strategist woken",
        })

    # Log to execution log
    try:
        state.setdefault("playbook_execution_log", []).append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "rules_fired": ["stop_loss_gate"],
            "symbol": symbol, "pnl_pct": pnl_pct,
            "stops_today": stops_today, "consecutive": consecutive_losses,
            "wake": result["wake_strategist"],
            "wake_reason": result.get("wake_reason", ""),
        })
        if len(state["playbook_execution_log"]) > 100:
            state["playbook_execution_log"] = state["playbook_execution_log"][-100:]
        save_strategy(ai_name)
    except Exception:
        pass

    log(f"   Gate: block={result['gate_entries']} {result['gate_minutes']}m | "
        f"size_mult={result['size_multiplier']:.1f} | wake={result['wake_strategist']}")
    return result


# ════════════════════════════════════════════════════════════
# STRATEGIST PROMPT BUILDER
# ════════════════════════════════════════════════════════════

def _build_strategist_prompt(ai_name, purpose, trade_history,
                              market_context, sentiment_context=None,
                              wake_reason=""):
    state      = load_strategy(ai_name)
    current    = state["current_strategy"]
    perf       = state["current_performance"]
    rival_name = "Grok" if ai_name == "claude" else "Claude"

    # Trade history
    tlines = []
    for t in (trade_history or [])[-30:]:
        sym    = (t.get("symbol") or "?").replace("USDT", "")
        action = t.get("action", "?")
        if action == "buy":
            tlines.append(f"  BUY {sym} @{t.get('price',0):.4f} "
                          f"${t.get('notional',0):.2f} conf={t.get('confidence','?')}%")
        else:
            pnl    = t.get("pnl_usd", 0) or 0
            pp     = t.get("pnl_pct", 0) or 0
            reason = (t.get("reason") or t.get("exit_reason") or "?")[:28]
            icon   = "WIN" if pnl > 0 else "LOSS"
            tlines.append(f"  {icon} {action.upper()} {sym} @{t.get('price',0):.4f} "
                          f"P&L ${pnl:+.2f} ({pp*100 if pp < 1 else pp:+.1f}%) | {reason}")
    trades_block = "\n".join(tlines) or "  (no trades yet)"

    # Market context
    mc_block = "\n".join(f"  {k}: {v}" for k, v in (market_context or {}).items()) \
               or "  (no market data)"

    # Sentiment context
    sc = sentiment_context or {}
    sent_block = (
        f"  Overall: {sc.get('overall', 'neutral')}\n"
        f"  Fear/Greed: {sc.get('fear_greed_label', 'unknown')} ({sc.get('fear_greed_score', '?')}/100)\n"
        f"  BTC 1h: {sc.get('btc_change_1h', '?')}%  24h: {sc.get('btc_change_24h', '?')}%\n"
        f"  News: {sc.get('news_summary', 'none')}\n"
        f"  Social: {sc.get('social_summary', 'none')}\n"
        f"  Whales: {sc.get('whale_summary', 'none')}"
    )

    # Playbook execution log
    exec_log = state.get("playbook_execution_log", [])[-10:]
    if exec_log:
        elines = [f"  [{e.get('ts','')[:16]}] {', '.join(e.get('rules_fired',[]))}"
                  f"{' -> WAKE: '+e['wake_reason'] if e.get('wake') else ''}"
                  for e in exec_log]
        exec_block = "\n".join(elines)
    else:
        exec_block = "  (no conditional rules fired yet)"

    # Prior strategies
    hist_lines = [
        f"  [{h.get('id','?')}] '{h.get('name','?')}' — "
        f"{h.get('total_trades',0)} trades WR={h.get('final_win_rate',0):.0f}% "
        f"P&L=${h.get('final_pnl_usd',0):+.2f} -> {h.get('outcome','?')}"
        for h in state.get("strategy_history", [])[-5:]
    ]
    hist_block = "\n".join(hist_lines) or "  (no prior playbooks)"

    wake_block = f"\nWAKE TRIGGER: {wake_reason}" if wake_reason else ""

    return f"""You are {ai_name.upper()}-STRATEGIST — field commander for {ai_name.upper()}-TACTICIAN.

You do NOT trade. You write STANDING ORDERS — a comprehensive playbook that the tactician
and bot follow autonomously until you are recalled. Train the bot to handle situations
without needing you constantly. Your playbook must cover every scenario.{wake_block}

═══ THE BOT IS RUNNING THE TURTLE TRADING SYSTEM ═══
This is RICHARD DENNIS'S TURTLE TREND-FOLLOWING SYSTEM, adapted for spot-only crypto and stocks.
The system is designed for LONG-TERM GROWTH via catching big trends and cutting losers fast.

CORE PRINCIPLES (DO NOT VIOLATE):
1. Mechanical entries on Donchian breakouts (System 1: 20-day high; System 2: 55-day high)
2. Mechanical exits on Donchian breakdowns (System 1: 10-day low; System 2: 20-day low)
3. Hard stops at entry - (2 × ATR), no exceptions
4. Risk EXACTLY 1% of equity per trade
5. EXPECT 60-70% loss rate. Profitability comes from large winners, not high win rate.
6. NEVER abandon the system after losing streaks — that's how Turtle traders fail
7. Discipline > prediction. The system catches trends BECAUSE it's mechanical.

WHEN TO ACTUALLY REVISE THE TURTLE PLAYBOOK:
✓ AFTER 30+ trades, if win rate < 20% AND no winners > 4N achieved → adjust filters
✓ If anti-chop filter is missing too many true breakouts → loosen it
✓ If macro filter (BTC -3% / SPY -2%) is too restrictive → adjust thresholds
✗ DO NOT revise after 3, 6, or even 10 losses — that's NORMAL for Turtle
✗ DO NOT switch to scalping/mean-reversion because Turtle is "in drawdown"
✗ DO NOT add fixed take-profits — the whole point is letting winners run

ACTIVATION: {purpose}  |  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}

=== CURRENT PLAYBOOK ===
ID: {current.get('id')} | Version {current.get('version',0)} | Type: {current.get('strategy_type', 'classic')}
Name: {current.get('name')}
Since: {current.get('active_since','')[:16]}
Rules:
{json.dumps(current.get('rules',{}), indent=2)}

=== PERFORMANCE UNDER THIS PLAYBOOK ===
Trades: {perf.get('trades_under_this_strategy',0)} | Wins: {perf.get('wins',0)} | Losses: {perf.get('losses',0)}
Win Rate: {perf.get('actual_win_rate',0):.0f}% (predicted: {current.get('predicted_win_rate','?')}%)
Avg P&L: {perf.get('actual_avg_pnl_pct',0):+.2f}%
Note: For Turtle, 30-40% WR is EXPECTED. Below 20% over 30+ trades is concerning.

=== PLAYBOOK EXECUTION LOG (what conditional rules fired) ===
{exec_block}

=== RECENT TRADES ===
{trades_block}

=== MARKET CONTEXT ===
{mc_block}

=== SENTIMENT AND NEWS (Turtle ignores sentiment but useful for risk awareness) ===
{sent_block}

=== PRIOR PLAYBOOKS ===
{hist_block}

=== COMPETITIVE CONTEXT ===
You compete against {rival_name}-Strategist. Realized P&L on closed trades is the score.
Both AIs are running Turtle. Discipline differentials matter more than entry tweaks.

=== YOUR TASK ===
Write a LIVING TURTLE PLAYBOOK — comprehensive standing orders for trend-following.
The bot follows this autonomously. You are NOT called for every stop or dip.
Define PRECISELY when to recall you (hint: not often — 30+ trades evaluation window).

HARD LIMITS (validator will reject violations):
  stop_loss_atr_multiple: 1.0 - 4.0 (Turtle uses 2.0)
  risk_pct_per_trade: 0 - 3.0 (Turtle uses 1.0)
  entry_donchian_period: 5 - 200 (System 1: 20, System 2: 55)
  exit_donchian_period: 5 - 100 (System 1: 10, System 2: 20)
  exit_donchian_period MUST be < entry_donchian_period
  max_position_pct_of_pool: <= {HARD_LIMITS['max_position_pct_of_pool']}%
  max_hold_hours: <= {HARD_LIMITS['max_hold_hours']}
  min_confidence: >= {HARD_LIMITS['min_confidence']}

Reply ONLY with valid JSON:
{{
  "decision": "keep|modify|replace",
  "rationale": "Evidence-based. Cite specific trades. Defend continuing Turtle.",
  "new_strategy": {{
    "name": "Turtle [variant name]",
    "strategy_type": "turtle",
    "rules": {{
      "entry_logic": "MECHANICAL: enter on Donchian breakout. NO prediction.",
      "exit_logic": "MECHANICAL: exit on Donchian breakdown OR 2N stop.",
      "preferred_indicators": ["donchian_20","donchian_10","atr_20"],
      "preferred_symbols": [],
      "min_confidence": 60,
      "max_position_pct_of_pool": 25,
      "max_concurrent_positions": 4,
      "stop_loss_pct": null,
      "take_profit_pct": null,
      "stop_loss_atr_multiple": 2.0,
      "exit_donchian_period": 10,
      "entry_donchian_period": 20,
      "risk_pct_per_trade": 1.0,
      "max_hold_hours": 168,
      "trail_activate_pct": 0,
      "trail_pct": 0,
      "on_stop_loss": {{"action":"no_pause","pause_minutes":0,"log_reason":"Turtle: losses are NORMAL"}},
      "on_two_stops_same_session": {{"action":"no_pause","reduce_size_pct":0,"pause_minutes":0,"log_reason":"NORMAL"}},
      "on_three_consecutive_losses": {{"action":"no_pause","log_reason":"3 losses NORMAL for Turtle","wake_strategist":false}},
      "on_six_consecutive_losses": {{"action":"wake_strategist_only","log_reason":"review filters","wake_strategist":true}},
      "on_winning_streak_3": {{"action":"hold_current_size","log_reason":"discipline"}},
      "on_btc_drops_3pct_1h": {{"action":"halt_altcoin_entries","log_reason":"BTC weakness"}},
      "on_btc_drops_5pct_1h": {{"action":"halt_altcoin_entries","log_reason":"BTC severe","wake_strategist":false}},
      "on_spy_drops_2pct": {{"action":"halt_stock_entries","log_reason":"SPY weakness"}},
      "on_sentiment_extreme_fear": {{"action":"no_change","stop_multiplier":1.0,"log_reason":"Turtle ignores sentiment"}},
      "on_sentiment_extreme_greed": {{"action":"no_change","tp_multiplier":1.0,"log_reason":"Let winners run"}},
      "wake_strategist_if": {{
        "consecutive_losses": 6,
        "session_drawdown_pct": 20,
        "days_without_trade": 7,
        "btc_regime_flip": true,
        "spy_regime_flip": true,
        "win_rate_below_pct": 15,
        "predicted_vs_actual_gap": 50,
        "min_trades_before_eval": 30
      }},
      "tactician_notes": "[STRICT mechanical Turtle discipline rules — see template]"
    }},
    "predicted_win_rate": 35,
    "predicted_avg_pnl_pct": 4.0
  }}
}}

For "keep": new_strategy = null.
"""


# ════════════════════════════════════════════════════════════
# VALIDATION
# ════════════════════════════════════════════════════════════

def validate_strategy(proposed):
    if not isinstance(proposed, dict):
        return False, "not a dict"
    if "rules" not in proposed:
        return False, "missing rules field"
    rules = proposed["rules"]

    # ── Turtle (ATR-based) strategies use different validation ─
    strategy_type = proposed.get("strategy_type", "classic")
    is_turtle = strategy_type == "turtle"

    def _float(key, default=0):
        try:
            return float(rules.get(key, default))
        except (TypeError, ValueError):
            return None

    max_pos = _float("max_position_pct_of_pool")
    if max_pos is None or max_pos <= 0 or max_pos > HARD_LIMITS["max_position_pct_of_pool"]:
        return False, f"max_position_pct_of_pool out of range [0,{HARD_LIMITS['max_position_pct_of_pool']}]"

    if is_turtle:
        # ── Turtle validation: ATR-based stops/exits ─────────
        atr_mult = _float("stop_loss_atr_multiple")
        if atr_mult is None or atr_mult < 1.0 or atr_mult > 4.0:
            return False, f"stop_loss_atr_multiple ({atr_mult}) outside [1.0, 4.0]"

        risk_pct = _float("risk_pct_per_trade")
        if risk_pct is None or risk_pct <= 0 or risk_pct > 3.0:
            return False, f"risk_pct_per_trade ({risk_pct}) outside (0, 3.0]"

        entry_period = _float("entry_donchian_period")
        if entry_period is None or entry_period < 5 or entry_period > 200:
            return False, f"entry_donchian_period ({entry_period}) outside [5, 200]"

        exit_period = _float("exit_donchian_period")
        if exit_period is None or exit_period < 5 or exit_period > 100:
            return False, f"exit_donchian_period ({exit_period}) outside [5, 100]"

        if exit_period >= entry_period:
            return False, f"exit_donchian_period ({exit_period}) must be < entry_donchian_period ({entry_period})"
    else:
        # ── Classic playbook validation ───────────────────────
        sl = _float("stop_loss_pct")
        if sl is None or sl < HARD_LIMITS["stop_loss_pct_min"] or sl > HARD_LIMITS["stop_loss_pct_max"]:
            return False, f"stop_loss_pct out of range [{HARD_LIMITS['stop_loss_pct_min']},{HARD_LIMITS['stop_loss_pct_max']}]"

        tp = _float("take_profit_pct")
        if tp is None or tp < HARD_LIMITS["take_profit_pct_min"] or tp > HARD_LIMITS["take_profit_pct_max"]:
            return False, f"take_profit_pct out of range [{HARD_LIMITS['take_profit_pct_min']},{HARD_LIMITS['take_profit_pct_max']}]"
        if tp < sl:
            return False, f"take_profit_pct ({tp}) < stop_loss_pct ({sl}) — R/R < 1:1 rejected"

    mh = _float("max_hold_hours")
    if mh is None or mh <= 0 or mh > HARD_LIMITS["max_hold_hours"]:
        return False, f"max_hold_hours out of range (0,{HARD_LIMITS['max_hold_hours']}]"

    mc = _float("min_confidence")
    if mc is None or mc < HARD_LIMITS["min_confidence"]:
        return False, f"min_confidence below floor {HARD_LIMITS['min_confidence']}"

    banned = ["ignore previous", "world's best", "guaranteed", "never lose",
              "bypass", "core_reserve", "override hard limit"]
    full   = json.dumps(proposed, default=str).lower()
    for phrase in banned:
        if phrase in full:
            return False, f"banned phrase: '{phrase}'"

    pwr = proposed.get("predicted_win_rate", 50)
    try:
        pwr = float(pwr)
    except Exception:
        pwr = 50
    if pwr > 90:
        return False, f"predicted_win_rate {pwr} unrealistic"

    return True, ""


# ════════════════════════════════════════════════════════════
# PARSE + APPLY
# ════════════════════════════════════════════════════════════

def parse_strategist_response(raw):
    if not raw:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        try:
            from ai_clients import parse_json as _pj
            r = _pj(raw)
            if r and isinstance(r, dict) and "decision" in r:
                return r
        except Exception:
            pass
        import re
        clean = re.sub(r'```\w*', '', str(raw)).replace('```', '').strip()
        s = clean.find('{')
        e = clean.rfind('}') + 1
        if s >= 0 and e > s:
            return json.loads(clean[s:e])
    except Exception as err:
        log(f"warning strategist parse failed: {err}")
    return None


def apply_strategy_decision(ai_name, decision):
    state         = load_strategy(ai_name)
    decision_type = (decision or {}).get("decision", "keep")
    rationale     = (decision or {}).get("rationale", "")

    if decision_type == "keep":
        _audit(ai_name, "STRATEGY_KEPT",
               f"Playbook kept: {state['current_strategy'].get('name','?')}",
               rationale=rationale)
        return True

    new_strategy = decision.get("new_strategy")
    if not new_strategy:
        _audit(ai_name, "STRATEGY_DECISION_INVALID", f"Decision '{decision_type}' but no new_strategy")
        return False

    is_valid, reason = validate_strategy(new_strategy)
    if not is_valid:
        _audit(ai_name, "STRATEGY_REJECTED", f"Playbook rejected: {reason}",
               rejected=new_strategy.get("name", "?"))
        return False

    # Archive current
    current = state["current_strategy"]
    perf    = state["current_performance"]
    state.setdefault("strategy_history", []).append({
        "id":             current.get("id"),
        "name":           current.get("name"),
        "version":        current.get("version", 0),
        "active_from":    current.get("active_since"),
        "active_until":   datetime.now(timezone.utc).isoformat(),
        "total_trades":   perf.get("trades_under_this_strategy", 0),
        "wins":           perf.get("wins", 0),
        "losses":         perf.get("losses", 0),
        "final_win_rate": perf.get("actual_win_rate", 0),
        "final_pnl_usd":  perf.get("actual_avg_pnl_pct", 0),
        "outcome":        f"replaced ({decision_type})",
        "predicted_wr":   current.get("predicted_win_rate"),
        "rationale":      rationale[:300],
    })
    if len(state["strategy_history"]) > 50:
        state["strategy_history"] = state["strategy_history"][-50:]

    new_ver = current.get("version", 0) + 1
    new_id  = f"S-{ai_name}-{new_ver}"
    state["current_strategy"] = {
        "id":                    new_id,
        "name":                  new_strategy.get("name", "Unnamed"),
        "version":               new_ver,
        "active_since":          datetime.now(timezone.utc).isoformat(),
        "rules":                 new_strategy.get("rules", {}),
        "rationale":             rationale,
        "predicted_win_rate":    new_strategy.get("predicted_win_rate"),
        "predicted_avg_pnl_pct": new_strategy.get("predicted_avg_pnl_pct"),
    }
    state["current_performance"] = {
        "trades_under_this_strategy": 0, "wins": 0, "losses": 0,
        "actual_win_rate": 0, "actual_avg_pnl_pct": 0,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    state["playbook_execution_log"] = []   # Fresh start for new playbook

    _audit(ai_name, "STRATEGY_REPLACED",
           f"New playbook '{new_strategy.get('name','?')}' v{new_ver} — "
           f"predicted {new_strategy.get('predicted_win_rate','?')}% WR",
           old=current.get("name"), new=new_strategy.get("name"),
           rationale=rationale[:200])
    save_strategy(ai_name)
    return True


# ════════════════════════════════════════════════════════════
# WAKE TRIGGER CHECK (no AI call)
# ════════════════════════════════════════════════════════════

def check_wake_triggers(ai_name, context):
    """Returns (should_wake, reason). Called every cycle — zero AI cost."""
    state = load_strategy(ai_name)
    last_iso = state.get("last_wake_call")
    if last_iso:
        try:
            last = datetime.fromisoformat(last_iso.replace("Z", "+00:00"))
            if datetime.now(timezone.utc) - last < timedelta(minutes=WAKE_COOLDOWN_MINUTES):
                return False, "cooldown"
        except Exception:
            pass
    result = execute_playbook(ai_name, context)
    if result.get("wake_strategist"):
        return True, result.get("wake_reason", "playbook condition")
    return False, ""


# ════════════════════════════════════════════════════════════
# MAIN ACTIVATION ENTRY POINT
# ════════════════════════════════════════════════════════════

def activate_strategist(ai_name, purpose="scheduled review",
                        wake_reason="", cycle_context=None):
    """
    Activate strategist to read context and write/update the living playbook.
    Returns {"applied": bool, "decision": str, "strategy_name": str}
    """
    if not ENABLE_STRATEGIST:
        return {"applied": False, "reason": "strategist disabled"}

    wallet = _refresh_model_registry()

    if ai_name not in STRATEGIST_MODELS:
        return {"applied": False, "reason": f"unknown AI '{ai_name}'"}

    # Cooldown
    state = load_strategy(ai_name)
    last_act_iso = state.get("last_activation")
    if last_act_iso:
        try:
            last_act = datetime.fromisoformat(last_act_iso.replace("Z", "+00:00"))
            cooldown = last_act + timedelta(minutes=WAKE_COOLDOWN_MINUTES)
            if datetime.now(timezone.utc) < cooldown:
                return {"applied": False,
                        "reason": f"cooldown until {cooldown.strftime('%H:%M UTC')}"}
        except Exception:
            pass

    log(f"STRATEGIST [{ai_name}] activating: {purpose}"
        + (f" | wake: {wake_reason}" if wake_reason else ""))

    # Gather all context
    history   = []
    market    = {}
    sentiment = {}
    try:
        if get_trade_history:
            history = get_trade_history(owner=ai_name, limit=50)
        if get_market_context:
            market = get_market_context()
        if get_sentiment_context:
            sentiment = get_sentiment_context()
    except Exception as e:
        log(f"warning strategist context gather failed for {ai_name}: {e}")

    prompt = _build_strategist_prompt(
        ai_name, purpose, history, market,
        sentiment_context=sentiment,
        wake_reason=wake_reason,
    )

    # Call AI model
    raw = None
    try:
        if ai_name == "claude" and ask_claude_strategist:
            raw = ask_claude_strategist(prompt)
        elif ai_name == "grok" and ask_grok_strategist:
            raw = ask_grok_strategist(prompt)
        else:
            return {"applied": False, "reason": f"strategist not wired for {ai_name}"}
    except Exception as e:
        _audit(ai_name, "ACTIVATION_FAILED", f"API error: {e}", purpose=purpose)
        return {"applied": False, "reason": f"API error: {e}"}

    decision = parse_strategist_response(raw)
    if not decision:
        _audit(ai_name, "ACTIVATION_PARSE_FAILED",
               f"Parse failed (raw: {str(raw)[:200]})", purpose=purpose)
        return {"applied": False, "reason": "parse failed"}

    applied = apply_strategy_decision(ai_name, decision)

    state = load_strategy(ai_name)
    state["last_activation"]   = datetime.now(timezone.utc).isoformat()
    state["last_wake_call"]    = datetime.now(timezone.utc).isoformat()
    state["last_wake_reason"]  = wake_reason or purpose
    state["total_activations"] = state.get("total_activations", 0) + 1
    save_strategy(ai_name)

    strat_name = state["current_strategy"].get("name", "?")
    log(f"STRATEGIST [{ai_name}] done — decision={decision.get('decision','?')} "
        f"applied={applied} playbook='{strat_name}'")

    return {"applied": applied, "decision": decision.get("decision", "?"),
            "strategy_name": strat_name, "purpose": purpose,
            "wake_reason": wake_reason}


# ════════════════════════════════════════════════════════════
# PERFORMANCE TRACKING
# ════════════════════════════════════════════════════════════

def record_trade_result(ai_name, won, pnl_pct):
    """Update strategy performance counters after a trade closes."""
    try:
        state = load_strategy(ai_name)
        perf  = state.get("current_performance", {})
        total = perf.get("trades_under_this_strategy", 0) + 1
        wins  = perf.get("wins", 0) + (1 if won else 0)
        prev_avg = perf.get("actual_avg_pnl_pct", 0)
        perf.update({
            "trades_under_this_strategy": total,
            "wins":                        wins,
            "losses":                      total - wins,
            "actual_win_rate":             round(wins / total * 100, 1),
            "actual_avg_pnl_pct":          round((prev_avg * (total-1) + pnl_pct) / total, 2),
        })
        state["current_performance"] = perf
        save_strategy(ai_name)
    except Exception as e:
        log(f"warning record_trade_result [{ai_name}]: {e}")


# ════════════════════════════════════════════════════════════
# DASHBOARD STATUS
# ════════════════════════════════════════════════════════════

def get_full_status():
    wallet = _refresh_model_registry()
    upgrade_info = {}
    for ai in ("claude", "grok"):
        spec  = MODEL_REGISTRY["strategist"][ai]
        thresh = spec.get("upgrade_threshold_wallet", 0)
        upgrade_info[ai] = {
            "tier":             "premium" if wallet >= thresh else "default",
            "threshold_wallet": thresh,
            "wallet_now":       round(wallet, 2),
            "remaining":        round(max(0, thresh - wallet), 2),
        }
    out = {
        "enabled":       ENABLE_STRATEGIST,
        "phase":         "B — ACTIVE (living playbook)" if ENABLE_STRATEGIST else "A — skeleton",
        "models":        STRATEGIST_MODELS,
        "upgrade_info":  upgrade_info,
        "schedule":      SCHEDULE,
        "hard_limits":   HARD_LIMITS,
        "cooldown_mins": WAKE_COOLDOWN_MINUTES,
        "auto_revert":   AUTO_REVERT,
        "ais":           {},
    }
    for ai in ("claude", "grok"):
        state = load_strategy(ai)
        strat = state.get("current_strategy", {})
        rules = strat.get("rules", {})
        out["ais"][ai] = {
            "active_model":           STRATEGIST_MODELS[ai].get("model_id"),
            "current_strategy":       strat,
            "current_performance":    state.get("current_performance", {}),
            "strategy_history":       state.get("strategy_history", [])[-5:],
            "last_activation":        state.get("last_activation"),
            "last_wake_reason":       state.get("last_wake_reason"),
            "total_activations":      state.get("total_activations", 0),
            "playbook_execution_log": state.get("playbook_execution_log", [])[-10:],
            "recent_audit":           state.get("audit_log", [])[-10:],
            "wake_conditions":        rules.get("wake_strategist_if", {}),
            "tactician_notes":        rules.get("tactician_notes", ""),
        }
    return out
