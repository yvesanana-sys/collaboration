"""
strategic_brain.py — Strategic AI layer for NovaTrade.

═══════════════════════════════════════════════════════════════════════
ARCHITECTURE OVERVIEW
═══════════════════════════════════════════════════════════════════════
This module implements the STRATEGIST role — the "research desk" of
the bot. Strategists DON'T trade. They:

  1. Activate on schedule (pre-market + post-close) and on-demand
  2. Read full trade history, news context, current positions
  3. Develop strategies for their tactical partner to follow
  4. Learn from past attempts (their own evolving playbook)
  5. Collaborate (Claude+Grok) on Core Reserve decisions

Each camp has its own strategist:
  • Claude-Strategist (Opus 4.7) → guides Claude-Tactician (Haiku 4.5)
  • Grok-Strategist (Grok 4.1 Fast Reasoning) → guides Grok-Tactician

The tacticians can WAKE their strategist on-demand when the situation
breaks the playbook (stop-loss breach, regime change, gap event).

Core Reserve (the long-term BTC/SPY/Cash compounder) requires consensus
from BOTH strategists to act. If they disagree, default is hold.

═══════════════════════════════════════════════════════════════════════
MODEL CONFIGURATION (swap as new models release)
═══════════════════════════════════════════════════════════════════════
"""
import json
import os
from datetime import datetime, timezone, timedelta

# ── Model configuration ─────────────────────────────────────
# Single source of truth for ALL AI model choices in NovaTrade.
# Both strategist and tactician models live here so that swapping
# to new models is a one-config edit, not a hunt through code.
#
# Wallet-tiered: as the bot's combined wallet grows, the strategist
# auto-upgrades to a more capable (and more expensive) model. The
# tactician stays on the cheap-and-fast tier — its job is pattern
# matching, not deep reasoning.
#
# To swap models when new ones release:
#   - Update "model_id" for the relevant role+tier
#   - Update cost numbers if pricing changed
#   - Optionally adjust upgrade_threshold_wallet if economics shift
MODEL_REGISTRY = {
    "strategist": {
        "claude": {
            # Default tier — used when wallet < upgrade threshold.
            # Sonnet 4.6 is "smart enough" for strategy reviews while
            # keeping the per-month cost proportional to a small wallet.
            "default": {
                "model_id":           "claude-sonnet-4-5-20251015",
                "provider":           "anthropic",
                "max_tokens":         4000,
                "input_cost_per_1m":  3.00,
                "output_cost_per_1m": 15.00,
                "notes":              "Sonnet 4.6 — strong reasoning at moderate cost",
            },
            # Premium tier — auto-activates when wallet crosses threshold.
            # At ~$60/mo for strategist work, this is justified once the
            # wallet is large enough that the % cost is small.
            "premium": {
                "model_id":           "claude-opus-4-7",
                "provider":           "anthropic",
                "max_tokens":         4000,
                "input_cost_per_1m":  15.00,
                "output_cost_per_1m": 75.00,
                "notes":              "Opus 4.7 — frontier reasoning, premium pricing",
            },
            "upgrade_threshold_wallet": 5000.0,
        },
        "grok": {
            # xAI's tiering is different from Anthropic's. Grok 4.1 Fast
            # Reasoning is purpose-built for agentic tool-calling and
            # already very capable — at $0.20/$0.50 per 1M, "premium"
            # for Grok at this price point doesn't add much value.
            # We still upgrade to grok-4 (the flagship) at the threshold.
            "default": {
                "model_id":           "grok-4-1-fast-reasoning",
                "provider":           "xai",
                "max_tokens":         4000,
                "input_cost_per_1m":  0.20,
                "output_cost_per_1m": 0.50,
                "notes":              "Grok 4.1 Fast Reasoning — agentic tool-calling, 2M ctx",
            },
            "premium": {
                "model_id":           "grok-4",
                "provider":           "xai",
                "max_tokens":         4000,
                "input_cost_per_1m":  3.00,
                "output_cost_per_1m": 15.00,
                "notes":              "Grok 4 flagship — premium reasoning",
            },
            "upgrade_threshold_wallet": 5000.0,
        },
    },
    "tactician": {
        # Tactician work is fast pattern matching — doesn't benefit
        # meaningfully from premium models. We keep the cheap-and-fast
        # tier and only swap when better cheap-tier models release.
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
                "notes":              "Same model as strategist; Grok's fast tier IS reasoning-capable",
            },
        },
    },
}


def get_active_model(role: str, ai_name: str, wallet: float = 0.0) -> dict:
    """
    Return the active model spec for a given role + AI + wallet size.
    Handles wallet-tier auto-upgrade for strategists.

    Args:
        role:     'strategist' or 'tactician'
        ai_name:  'claude' or 'grok'
        wallet:   Current combined wallet value (for tier check)

    Returns: dict with model_id, max_tokens, costs, notes.
    """
    if role not in MODEL_REGISTRY or ai_name not in MODEL_REGISTRY[role]:
        # Fallback to a safe default rather than crashing
        return {"model_id": "claude-haiku-4-5-20251001", "provider": "anthropic",
                "max_tokens": 1200, "input_cost_per_1m": 1.0, "output_cost_per_1m": 5.0,
                "notes": "fallback"}
    spec = MODEL_REGISTRY[role][ai_name]
    # Tactician only has 'default'
    if "premium" not in spec:
        return dict(spec["default"])
    # Strategist tier check
    threshold = spec.get("upgrade_threshold_wallet", float("inf"))
    if wallet >= threshold:
        return dict(spec["premium"])
    return dict(spec["default"])


# Legacy alias — strategists use this for their own self-lookup. Initialized
# at import time with wallet=0 (default tier). Refreshed each activation.
STRATEGIST_MODELS = {
    "claude": get_active_model("strategist", "claude", wallet=0.0),
    "grok":   get_active_model("strategist", "grok",   wallet=0.0),
}

# ── Activation schedule ─────────────────────────────────────
# Phase A (this deploy): module exists, plumbing wired, but NOT activating.
#   ENABLE_STRATEGIST = False keeps the system in "skeleton mode" — it
#   responds to /strategy/<ai> endpoints with current state, but doesn't
#   make any actual API calls or write strategies. This lets us verify
#   integration without spending tokens or making real strategic decisions.
#
# Phase B (next session): set ENABLE_STRATEGIST = True. Strategists go
#   live with scheduled tactical reviews. Tactician reads the strategy
#   files. Wake triggers active. Reserve management still on rules.
#
# Phase C (session after): strategists take over Core Reserve decisions.
#   Hard-rule floors stay in place as catastrophic protection.
ENABLE_STRATEGIST = False    # ⚠️ Phase A: skeleton only. Set True in Phase B.

# Daily scheduled activations (ET)
SCHEDULE = {
    "pre_market":   {"hour": 9,  "minute": 0,  "purpose": "write today's strategy"},
    "post_close":   {"hour": 16, "minute": 30, "purpose": "review + tomorrow plan (stocks)"},
    "crypto_close": {"hour": 21, "minute": 0,  "purpose": "review crypto session"},
}

# ── Wake-trigger thresholds ─────────────────────────────────
# These are the mechanical conditions that allow the tactician to wake
# the strategist mid-cycle. No AI judgment needed — these are rules.
WAKE_TRIGGERS = {
    "stop_loss_breach":          True,   # Held position broke stop
    "spy_drop_3pct_1h":          True,   # SPY down 3% in 1h (regime suspect)
    "btc_drop_3pct_1h":          True,   # BTC down 3% in 1h
    "position_gap_10pct":        True,   # Held pos gapped ±10% on news
    "consecutive_losses":        3,      # N losses in a row → wake
    "confidence_calibration":    {       # AI predicting >X but delivering <Y
        "predicted_min_pct":     60,
        "actual_max_pct":        30,
        "min_trades_to_eval":    5,
    },
}

# Cooldown: prevent wake-loops where strategist + tactician keep waking each other
WAKE_COOLDOWN_MINUTES = 30

# ── Auto-revert thresholds (Phase B) ────────────────────────
# When a strategy underperforms, revert to default. Triggered by a
# scheduled review, not mid-session.
AUTO_REVERT = {
    "min_trades_under_strategy": 5,
    "win_rate_threshold":        0.40,    # < 40% WR = bad strategy
    "pnl_threshold_usd":         -5.00,   # OR cumulative P&L < -$5
    "wr_underperformance_pp":    20,      # OR predicted-actual gap > 20pp
}

# ── Persistence ─────────────────────────────────────────────
STATE_DIR = "/data"
FALLBACK_DIR = "."

def _state_path(name: str) -> str:
    """Return the persistent state path for a given file, with fallback."""
    primary = f"{STATE_DIR}/{name}"
    if os.path.exists(STATE_DIR):
        return primary
    return f"{FALLBACK_DIR}/{name}"


# ── Strategy file schema ────────────────────────────────────
def _default_strategy(ai_name: str) -> dict:
    """The default starting strategy for a new AI."""
    return {
        "id":              f"S-{ai_name}-default",
        "name":            "Default neutral baseline",
        "version":         0,
        "active_since":    datetime.now(timezone.utc).isoformat(),
        "rules": {
            "entry_logic":             "Use indicators + news; trade what looks good",
            "max_position_pct_of_pool": 25,
            "stop_loss_pct":           8,
            "take_profit_pct":         5,
            "max_hold_hours":          12,
            "preferred_indicators":    ["RSI", "MACD", "volume_ratio"],
        },
        "rationale":           "Default starting strategy. Will evolve with experience.",
        "predicted_win_rate":  None,
        "predicted_avg_pnl_pct": None,
        "predicted_until":     None,
    }


def _default_strategy_state(ai_name: str) -> dict:
    """Initial state file for a new AI."""
    return {
        "ai_name":             ai_name,
        "model_id":            STRATEGIST_MODELS[ai_name]["model_id"],
        "current_strategy":    _default_strategy(ai_name),
        "current_performance": {
            "trades_under_this_strategy": 0,
            "wins":                        0,
            "losses":                      0,
            "actual_win_rate":             0,
            "actual_avg_pnl_pct":          0,
            "started_at":                  datetime.now(timezone.utc).isoformat(),
        },
        "strategy_history":    [],
        "last_activation":     None,
        "last_wake_call":      None,
        "total_activations":   0,
        "audit_log":           [],
    }


# ── Injected dependencies (set by bot on boot) ──────────────
log = print
ask_claude_strategist = None    # function: (prompt, system, max_tokens) -> str
ask_grok_strategist   = None
get_trade_history     = None    # function: (owner, limit) -> list of trades
get_market_context    = None    # function: () -> dict with SPY, BTC, VIX, etc.
record_trade          = None    # for logging strategist-induced trades
get_wallet_value      = None    # function: () -> float (combined wallet for tier)

def _set_context(log_fn=None,
                 ask_claude_strategist_fn=None,
                 ask_grok_strategist_fn=None,
                 get_trade_history_fn=None,
                 get_market_context_fn=None,
                 record_trade_fn=None,
                 get_wallet_fn=None):
    """Inject runtime dependencies."""
    global log, ask_claude_strategist, ask_grok_strategist
    global get_trade_history, get_market_context, record_trade, get_wallet_value
    if log_fn:                     log = log_fn
    if ask_claude_strategist_fn:   ask_claude_strategist = ask_claude_strategist_fn
    if ask_grok_strategist_fn:     ask_grok_strategist   = ask_grok_strategist_fn
    if get_trade_history_fn:       get_trade_history     = get_trade_history_fn
    if get_market_context_fn:      get_market_context    = get_market_context_fn
    if record_trade_fn:            record_trade          = record_trade_fn
    if get_wallet_fn:              get_wallet_value      = get_wallet_fn


def _refresh_model_registry():
    """
    Update the cached STRATEGIST_MODELS dict based on current wallet.
    Called at the start of each activation so wallet-tier upgrades
    take effect without a restart.
    """
    global STRATEGIST_MODELS
    wallet = 0.0
    try:
        if get_wallet_value:
            wallet = float(get_wallet_value() or 0)
    except Exception:
        pass
    STRATEGIST_MODELS = {
        "claude": get_active_model("strategist", "claude", wallet=wallet),
        "grok":   get_active_model("strategist", "grok",   wallet=wallet),
    }
    return wallet


# ── State load/save ─────────────────────────────────────────
_state_cache = {}

def load_strategy(ai_name: str) -> dict:
    """Load strategy state for a specific AI from /data volume."""
    if ai_name in _state_cache:
        return _state_cache[ai_name]
    path = _state_path(f"strategy_{ai_name}.json")
    try:
        with open(path) as f:
            state = json.load(f)
            # Forward-compat: ensure all expected fields exist
            default = _default_strategy_state(ai_name)
            for k in default:
                if k not in state:
                    state[k] = default[k]
            _state_cache[ai_name] = state
            return state
    except FileNotFoundError:
        state = _default_strategy_state(ai_name)
        _state_cache[ai_name] = state
        save_strategy(ai_name)
        return state
    except Exception as e:
        log(f"⚠️ Strategy load failed for {ai_name}: {e}")
        return _default_strategy_state(ai_name)


def save_strategy(ai_name: str) -> bool:
    """Persist strategy state."""
    if ai_name not in _state_cache:
        return False
    path = _state_path(f"strategy_{ai_name}.json")
    try:
        with open(path, "w") as f:
            json.dump(_state_cache[ai_name], f, default=str, indent=2)
        return True
    except Exception as e:
        log(f"⚠️ Strategy save failed for {ai_name}: {e}")
        return False


def _audit(ai_name: str, event_type: str, message: str, **extra):
    """Append an audit event to the AI's strategy log."""
    state = load_strategy(ai_name)
    state.setdefault("audit_log", []).append({
        "ts":      datetime.now(timezone.utc).isoformat(),
        "type":    event_type,
        "message": message,
        **extra,
    })
    if len(state["audit_log"]) > 200:
        state["audit_log"] = state["audit_log"][-200:]
    save_strategy(ai_name)
    log(f"🧠 STRATEGIST [{ai_name}]: {message}")


# ── Strategist prompt construction ──────────────────────────
def _build_strategist_prompt(ai_name: str, purpose: str,
                              trade_history: list, market_context: dict) -> str:
    """
    Build the strategist's input prompt. This is the "research desk
    briefing" that the strategist reads to decide on strategy.
    """
    state = load_strategy(ai_name)
    current = state["current_strategy"]
    perf = state["current_performance"]
    rival_name = "Grok" if ai_name == "claude" else "Claude"

    # Format trade history compactly
    trade_lines = []
    for t in trade_history[-30:]:
        sym = (t.get("symbol") or "?").replace("USDT", "")
        action = t.get("action", "?")
        if action == "buy":
            trade_lines.append(f"  BUY  {sym} @ ${t.get('price', 0):.4f} = ${t.get('notional', 0):.2f}")
        else:
            pnl = t.get("pnl_usd", 0) or 0
            pnl_pct = t.get("pnl_pct", 0) or 0
            icon = "✅" if pnl > 0 else "❌"
            trade_lines.append(f"  {icon} {action.upper()} {sym} @ ${t.get('price', 0):.4f} → P&L ${pnl:+.2f} ({pnl_pct:+.1f}%)")

    trades_block = "\n".join(trade_lines) if trade_lines else "  (no trades under your stewardship yet)"

    # Format market context
    mc_lines = []
    for k, v in (market_context or {}).items():
        mc_lines.append(f"  {k}: {v}")
    mc_block = "\n".join(mc_lines) if mc_lines else "  (no market context available)"

    # Strategy history summary
    hist_lines = []
    for h in state.get("strategy_history", [])[-5:]:
        hist_lines.append(
            f"  • [{h.get('id', '?')}] '{h.get('name', '?')}' — "
            f"{h.get('total_trades', 0)} trades, "
            f"{h.get('final_win_rate', 0)}% WR, "
            f"${h.get('final_pnl_usd', 0):+.2f} P&L → {h.get('outcome', '?')}"
        )
    hist_block = "\n".join(hist_lines) if hist_lines else "  (no prior strategies)"

    return f"""You are {ai_name.upper()}-STRATEGIST. You do not trade. Your job is to develop trading strategy for {ai_name.upper()}-TACTICIAN, who executes the trades.

═══ ACTIVATION CONTEXT ═══
Purpose: {purpose}
Time:    {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}
Activation: scheduled / on-demand / wake-call

═══ YOUR CURRENT STRATEGY ═══
ID:           {current.get('id')}
Name:         {current.get('name')}
Active since: {current.get('active_since')}
Rules:        {json.dumps(current.get('rules', {}), indent=2)}
Predicted:    {current.get('predicted_win_rate', '?')}% WR, {current.get('predicted_avg_pnl_pct', '?')}% avg P&L

═══ PERFORMANCE UNDER THIS STRATEGY ═══
Trades:       {perf.get('trades_under_this_strategy', 0)}
Wins:         {perf.get('wins', 0)}
Losses:       {perf.get('losses', 0)}
Actual WR:    {perf.get('actual_win_rate', 0)}%
Actual P&L:   {perf.get('actual_avg_pnl_pct', 0):+.2f}% avg

═══ RECENT TRADES (your tactician's last 30) ═══
{trades_block}

═══ MARKET CONTEXT (right now) ═══
{mc_block}

═══ YOUR PRIOR STRATEGIES ═══
{hist_block}

═══ COMPETITIVE CONTEXT ═══
You are competing against {rival_name}-Strategist. Both of you guide
your respective tacticians. Your trades are tracked separately.
Maximum profit minus fees wins.

═══ YOUR TASK ═══
Review your situation and respond with a structured decision:

1. KEEP current strategy (no change)
2. MODIFY current strategy (small parameter tweaks)
3. REPLACE with new strategy

Reply ONLY with valid JSON in this exact schema:
{{
  "decision": "keep" | "modify" | "replace",
  "rationale": "Why you made this choice (be evidence-based, cite trades).",
  "new_strategy": {{
    "name": "Short descriptive name",
    "rules": {{
      "entry_logic": "When to buy in plain language",
      "exit_logic": "When to sell",
      "max_position_pct_of_pool": 25,
      "stop_loss_pct": 8,
      "take_profit_pct": 5,
      "max_hold_hours": 12,
      "preferred_indicators": ["RSI", "MACD"],
      "preferred_symbols": ["any USDT pair you focus on"]
    }},
    "predicted_win_rate": 65,
    "predicted_avg_pnl_pct": 3.0
  }}
}}

Notes:
- For "keep" decision, set new_strategy: null
- For "modify", new_strategy is the FULL new spec (not a delta)
- Rules must respect hard limits: max_position_pct ≤ 50, stop_loss between 3-15%, max_hold ≤ 168
- Be specific. Vague strategies don't perform.
- Your predicted WR will be checked against actual. Underperforming
  predictions trigger auto-revert.
"""


def parse_strategist_response(raw: str) -> dict:
    """Parse strategist JSON response with multiple fallback layers."""
    if not raw:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        # Try the global parser if available
        try:
            from ai_clients import parse_json as _parse_json
            result = _parse_json(raw)
            if result and isinstance(result, dict) and "decision" in result:
                return result
        except Exception:
            pass

        # Fallback: naive parse
        import re
        clean = re.sub(r'```\w*', '', str(raw)).replace('```','').strip()
        s = clean.find('{')
        e = clean.rfind('}') + 1
        if s >= 0 and e > s:
            return json.loads(clean[s:e])
    except Exception as e:
        log(f"⚠️ Strategist response parse failed: {e}")
    return None


# ── Strategy validation (hard limits enforcement) ───────────
def validate_strategy(proposed: dict) -> tuple[bool, str]:
    """
    Validate a proposed new strategy against hard limits that the
    AI cannot override regardless of what it proposes.

    Returns (is_valid, reason_if_invalid).
    """
    if not isinstance(proposed, dict):
        return False, "not a dict"
    if "rules" not in proposed:
        return False, "missing 'rules' field"

    rules = proposed["rules"]

    # Hard limit: position size
    max_pos = rules.get("max_position_pct_of_pool", 0)
    try:
        max_pos = float(max_pos)
    except (TypeError, ValueError):
        return False, "max_position_pct_of_pool not numeric"
    if max_pos <= 0 or max_pos > 50:
        return False, f"max_position_pct_of_pool ({max_pos}) outside [0, 50]"

    # Hard limit: stop loss must exist and be reasonable
    sl = rules.get("stop_loss_pct", 0)
    try:
        sl = float(sl)
    except (TypeError, ValueError):
        return False, "stop_loss_pct not numeric"
    if sl < 3 or sl > 15:
        return False, f"stop_loss_pct ({sl}) outside [3, 15]"

    # Hard limit: max hold reasonable
    mh = rules.get("max_hold_hours", 0)
    try:
        mh = float(mh)
    except (TypeError, ValueError):
        return False, "max_hold_hours not numeric"
    if mh <= 0 or mh > 168:
        return False, f"max_hold_hours ({mh}) outside (0, 168]"

    # Hard limit: take profit reasonable
    tp = rules.get("take_profit_pct", 0)
    try:
        tp = float(tp)
    except (TypeError, ValueError):
        return False, "take_profit_pct not numeric"
    if tp < 1 or tp > 100:
        return False, f"take_profit_pct ({tp}) outside [1, 100]"

    # Banned phrases anywhere in the strategy
    banned = ["ignore previous", "world's best", "guaranteed", "never lose",
              "bypass", "core_reserve", "core reserve"]
    full_text = json.dumps(proposed, default=str).lower()
    for phrase in banned:
        if phrase.lower() in full_text:
            return False, f"banned phrase: '{phrase}'"

    # Predicted WR sanity check
    pwr = proposed.get("predicted_win_rate", 50)
    try:
        pwr = float(pwr)
    except (TypeError, ValueError):
        pwr = 50
    if pwr > 90:
        return False, f"predicted_win_rate ({pwr}) unrealistic — over 90%"

    return True, ""


# ── Strategy application ────────────────────────────────────
def apply_strategy_decision(ai_name: str, decision: dict) -> bool:
    """
    Apply a strategist's decision: keep, modify, or replace strategy.
    Returns True if applied successfully, False if rejected.
    """
    state = load_strategy(ai_name)
    decision_type = (decision or {}).get("decision", "keep")
    rationale = (decision or {}).get("rationale", "")

    if decision_type == "keep":
        _audit(ai_name, "STRATEGY_KEPT",
               f"Strategist kept current strategy: {state['current_strategy'].get('name', '?')}",
               rationale=rationale)
        return True

    new_strategy = decision.get("new_strategy")
    if not new_strategy:
        _audit(ai_name, "STRATEGY_DECISION_INVALID",
               f"Decision was '{decision_type}' but new_strategy missing — keeping current")
        return False

    # Validate against hard limits
    is_valid, reason = validate_strategy(new_strategy)
    if not is_valid:
        _audit(ai_name, "STRATEGY_REJECTED",
               f"Proposed strategy rejected: {reason}",
               rejected_strategy=new_strategy.get("name", "?"))
        return False

    # Archive current strategy to history
    current = state["current_strategy"]
    perf = state["current_performance"]
    history_entry = {
        "id":             current.get("id"),
        "name":           current.get("name"),
        "version":        current.get("version", 0),
        "active_from":    current.get("active_since"),
        "active_until":   datetime.now(timezone.utc).isoformat(),
        "total_trades":   perf.get("trades_under_this_strategy", 0),
        "wins":           perf.get("wins", 0),
        "losses":         perf.get("losses", 0),
        "final_win_rate": perf.get("actual_win_rate", 0),
        "final_pnl_usd":  perf.get("actual_avg_pnl_pct", 0),  # TODO: track cum USD too
        "outcome":        f"replaced_by_strategist ({decision_type})",
        "predicted_wr":   current.get("predicted_win_rate"),
        "rationale":      rationale,
    }
    state.setdefault("strategy_history", []).append(history_entry)
    if len(state["strategy_history"]) > 50:
        state["strategy_history"] = state["strategy_history"][-50:]

    # Build the new strategy (assigned an ID and version bump)
    new_version = current.get("version", 0) + 1
    new_id = f"S-{ai_name}-{new_version}"
    new_strategy_full = {
        "id":                    new_id,
        "name":                  new_strategy.get("name", "Unnamed"),
        "version":               new_version,
        "active_since":          datetime.now(timezone.utc).isoformat(),
        "rules":                 new_strategy.get("rules", {}),
        "rationale":             rationale,
        "predicted_win_rate":    new_strategy.get("predicted_win_rate"),
        "predicted_avg_pnl_pct": new_strategy.get("predicted_avg_pnl_pct"),
    }
    state["current_strategy"] = new_strategy_full
    state["current_performance"] = {
        "trades_under_this_strategy": 0,
        "wins":                        0,
        "losses":                      0,
        "actual_win_rate":             0,
        "actual_avg_pnl_pct":          0,
        "started_at":                  datetime.now(timezone.utc).isoformat(),
    }

    _audit(ai_name, "STRATEGY_REPLACED",
           f"New strategy '{new_strategy_full['name']}' (id={new_id}) "
           f"with predicted {new_strategy_full.get('predicted_win_rate', '?')}% WR",
           old_strategy=current.get("name"),
           new_strategy=new_strategy_full["name"],
           rationale=rationale[:200])
    save_strategy(ai_name)
    return True


# ── Main activation entry point ─────────────────────────────
def activate_strategist(ai_name: str, purpose: str = "scheduled review") -> dict:
    """
    Main entry point — activate a strategist to review and propose strategy.

    Returns a dict with the outcome:
      {"applied": bool, "decision": str, "strategy_name": str, "cost_usd": float}
    """
    if not ENABLE_STRATEGIST:
        return {"applied": False, "reason": "strategist disabled"}

    # Refresh model registry — picks up wallet-tier upgrade if applicable
    wallet = _refresh_model_registry()

    if ai_name not in STRATEGIST_MODELS:
        return {"applied": False, "reason": f"unknown AI '{ai_name}'"}

    # Cooldown check
    state = load_strategy(ai_name)
    last_activation_iso = state.get("last_activation")
    if last_activation_iso:
        try:
            last_activation = datetime.fromisoformat(last_activation_iso.replace("Z", "+00:00"))
            cooldown_until = last_activation + timedelta(minutes=WAKE_COOLDOWN_MINUTES)
            if datetime.now(timezone.utc) < cooldown_until:
                return {"applied": False,
                        "reason": f"cooldown active until {cooldown_until.isoformat()}"}
        except Exception:
            pass

    # Gather inputs
    history = []
    market = {}
    try:
        if get_trade_history:
            history = get_trade_history(owner=ai_name, limit=30)
        if get_market_context:
            market = get_market_context()
    except Exception as e:
        log(f"⚠️ Strategist {ai_name}: failed to gather inputs: {e}")

    # Build the prompt
    prompt = _build_strategist_prompt(ai_name, purpose, history, market)

    # Call the appropriate strategist model
    raw_response = None
    try:
        if ai_name == "claude" and ask_claude_strategist:
            raw_response = ask_claude_strategist(prompt)
        elif ai_name == "grok" and ask_grok_strategist:
            raw_response = ask_grok_strategist(prompt)
        else:
            return {"applied": False, "reason": f"strategist function not wired for {ai_name}"}
    except Exception as e:
        _audit(ai_name, "ACTIVATION_FAILED", f"API error: {e}", purpose=purpose)
        return {"applied": False, "reason": f"API error: {e}"}

    # Parse and apply
    decision = parse_strategist_response(raw_response)
    if not decision:
        _audit(ai_name, "ACTIVATION_PARSE_FAILED",
               f"Could not parse strategist response (raw: {str(raw_response)[:200]})",
               purpose=purpose)
        return {"applied": False, "reason": "parse failed"}

    applied = apply_strategy_decision(ai_name, decision)

    # Update activation timestamp
    state = load_strategy(ai_name)
    state["last_activation"] = datetime.now(timezone.utc).isoformat()
    state["total_activations"] = state.get("total_activations", 0) + 1
    save_strategy(ai_name)

    return {
        "applied":       applied,
        "decision":      decision.get("decision", "?"),
        "strategy_name": state["current_strategy"].get("name"),
        "purpose":       purpose,
    }


# ── Wake-trigger detection (called from tactician/main loop) ─
def check_wake_triggers(ai_name: str, context: dict) -> tuple[bool, str]:
    """
    Evaluate whether the tactician should wake the strategist NOW.
    Called every cycle. context contains current market state and
    performance flags.

    Returns (should_wake, reason).
    """
    state = load_strategy(ai_name)

    # Cooldown check
    last_wake_iso = state.get("last_wake_call")
    if last_wake_iso:
        try:
            last_wake = datetime.fromisoformat(last_wake_iso.replace("Z", "+00:00"))
            if datetime.now(timezone.utc) - last_wake < timedelta(minutes=WAKE_COOLDOWN_MINUTES):
                return False, "cooldown"
        except Exception:
            pass

    # Hard wake triggers (no AI judgment)
    if context.get("stop_loss_breach"):
        return True, "stop_loss_breach"
    if context.get("spy_drop_3pct_1h"):
        return True, "spy_drop_3pct_1h"
    if context.get("btc_drop_3pct_1h"):
        return True, "btc_drop_3pct_1h"
    if context.get("position_gap_10pct"):
        return True, "position_gap_10pct"

    # Consecutive losses
    consecutive = context.get("consecutive_losses", 0)
    if consecutive >= WAKE_TRIGGERS["consecutive_losses"]:
        return True, f"consecutive_losses_{consecutive}"

    # Confidence calibration
    cc = WAKE_TRIGGERS["confidence_calibration"]
    pred_wr = context.get("predicted_win_rate", 0)
    actual_wr = context.get("actual_win_rate_recent", 100)
    trades_evaluated = context.get("trades_recent", 0)
    if (trades_evaluated >= cc["min_trades_to_eval"] and
        pred_wr >= cc["predicted_min_pct"] and
        actual_wr <= cc["actual_max_pct"]):
        return True, f"confidence_calibration_miss"

    return False, ""


# ── Status snapshot for dashboard ───────────────────────────
def get_full_status() -> dict:
    """Snapshot for /strategy endpoint."""
    # Refresh model registry from current wallet (so /strategy reflects
    # the actual model that would be used right now)
    wallet = _refresh_model_registry()

    # Compute upgrade headroom for visibility on the dashboard
    upgrade_info = {}
    for ai in ("claude", "grok"):
        spec      = MODEL_REGISTRY["strategist"][ai]
        threshold = spec.get("upgrade_threshold_wallet", 0)
        is_premium = wallet >= threshold
        upgrade_info[ai] = {
            "tier":              "premium" if is_premium else "default",
            "threshold_wallet":  threshold,
            "wallet_now":        round(wallet, 2),
            "remaining":         round(max(0, threshold - wallet), 2),
        }

    out = {
        "enabled":          ENABLE_STRATEGIST,
        "phase":            "A — plumbing only" if not ENABLE_STRATEGIST else "B/C — active",
        "models":           STRATEGIST_MODELS,
        "model_registry":   MODEL_REGISTRY,
        "upgrade_info":     upgrade_info,
        "schedule":         SCHEDULE,
        "wake_triggers":    WAKE_TRIGGERS,
        "wake_cooldown":    WAKE_COOLDOWN_MINUTES,
        "auto_revert":      AUTO_REVERT,
        "ais":              {},
    }
    for ai in ("claude", "grok"):
        state = load_strategy(ai)
        out["ais"][ai] = {
            "model":              state.get("model_id"),
            "active_model":       STRATEGIST_MODELS[ai].get("model_id"),
            "current_strategy":   state.get("current_strategy", {}),
            "current_performance":state.get("current_performance", {}),
            "strategy_history":   state.get("strategy_history", [])[-5:],
            "last_activation":    state.get("last_activation"),
            "last_wake_call":     state.get("last_wake_call"),
            "total_activations":  state.get("total_activations", 0),
            "recent_audit":       state.get("audit_log", [])[-10:],
        }
    return out
