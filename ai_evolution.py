"""
ai_evolution.py — AI Self-Evolution Tier System
═══════════════════════════════════════════════════════════════════════
Each tactical AI (Claude, Grok) earns the right to modify its own
prompt by accumulating closed-trade P&L. They start at Tier 0 with
identical neutral prompts and unlock customization tiers as they prove
themselves. This module owns:

  • Tier eligibility math (based on trade count + realized P&L)
  • Persistent state per AI (current tier, custom prompt additions)
  • Token budgeting per tier (hard ceilings)
  • Auto-revert when performance degrades
  • Audit trail of every prompt evolution

Pass A (THIS PHASE): Foundation only. Tier 0 framework, neutral
prompts, rivalry context injection, dashboard surfacing. AIs CANNOT
yet propose changes to themselves — that's Pass B.

Pass B (NEXT PHASE): Self-modification loop with validation,
soft-launch, and auto-revert.
═══════════════════════════════════════════════════════════════════════
"""
import json
import os
from datetime import datetime, timezone

# ── Tier definitions ─────────────────────────────────────────
# Each tier requires CUMULATIVE conditions: trade count AND P&L.
# Tiers are earned, never assigned. Both AIs start at Tier 0.

TIERS = {
    0: {
        "name":             "Probation",
        "min_trades":       0,
        "min_pnl_usd":      -float("inf"),
        "prompt_token_cap": 800,
        "can_modify":       False,
        "description":      "Default starting state. Standard prompt.",
    },
    1: {
        "name":             "Apprentice",
        "min_trades":       5,
        "min_pnl_usd":      0.0,    # Just need to not be net-down
        "prompt_token_cap": 1000,
        "can_modify":       "style_notes",   # 1 paragraph max
        "description":      "Can add style notes (1 paragraph).",
    },
    2: {
        "name":             "Journeyman",
        "min_trades":       15,
        "min_pnl_usd":      5.0,
        "prompt_token_cap": 1500,
        "can_modify":       "preferences",   # 2 paragraphs, can name indicators
        "description":      "Can edit strategy preferences (2 paragraphs).",
    },
    3: {
        "name":             "Strategist",
        "min_trades":       30,
        "min_pnl_usd":      50.0,
        "prompt_token_cap": 2000,
        "can_modify":       "philosophy",    # Full philosophy block
        "description":      "Can write personal trading philosophy.",
    },
    4: {
        "name":             "Autonomous",
        "min_trades":       100,
        "min_pnl_usd":      500.0,
        "prompt_token_cap": 3000,
        "can_modify":       "full",          # Custom indicator combos
        "description":      "Full prompt autonomy with custom indicators.",
    },
}

# ── Hard limits that CANNOT be overridden by any tier ─────────
# These are validated against every proposed prompt change in Pass B.
# In Pass A they're just documented — AIs can't propose changes yet.
ALWAYS_BANNED_PHRASES = [
    "ignore previous instructions",
    "ignore the rules",
    "you have unlimited",
    "no stop loss",
    "no fees",
    "guaranteed profit",
    "world's best",
    "never lose",
    "always win",
    "core reserve",       # Forbidden topic — AIs can't see/touch reserve
    "btc reserve",
    "spy reserve",
    "wallet reserve",
    "skip reserve",
    "bypass reserve",
]

# Hard performance threshold — if win rate drops below this over the last
# 10 trades, the AI's most recent prompt change is reverted (soft revert).
# After 3 consecutive bad changes, we revert all the way back to Tier 0.
AUTO_REVERT_WINRATE_THRESHOLD = 0.40
AUTO_REVERT_MIN_TRADES         = 5    # Need this many trades on a prompt before judging it
SOFT_REVERT_BEFORE_HARD        = 3    # Soft reverts before going back to Tier 0

# Persistent state file (Railway /data volume — survives redeploys)
STATE_FILE = "/data/ai_evolution.json"
FALLBACK_STATE_FILE = "./ai_evolution.json"


# ── State management ─────────────────────────────────────────
def _default_state():
    """Empty state. Both AIs start at Tier 0."""
    base_ai_state = {
        "current_tier":          0,
        "tier_unlocked_at":      None,
        "custom_prompt":         "",          # AI's earned additions (Tier 1+)
        "prompt_version":        0,            # Increments on every change
        "consecutive_soft_reverts": 0,
        "evolution_history":     [],           # Full audit trail
        "prompt_versions":       [],           # Past prompts for comparison
    }
    return {
        "claude": dict(base_ai_state),
        "grok":   dict(base_ai_state),
        "schema_version": 1,
        "created_iso":    datetime.now(timezone.utc).isoformat(),
    }


_state = None


def _load_state():
    """Load AI evolution state from /data volume."""
    global _state
    if _state is not None:
        return _state
    for path in [STATE_FILE, FALLBACK_STATE_FILE]:
        try:
            with open(path) as f:
                _state = json.load(f)
                # Defensive: ensure both AI keys exist (handles older state files)
                _default = _default_state()
                for ai in ("claude", "grok"):
                    if ai not in _state:
                        _state[ai] = dict(_default[ai])
                    else:
                        for k, v in _default[ai].items():
                            if k not in _state[ai]:
                                _state[ai][k] = v
                return _state
        except FileNotFoundError:
            continue
        except Exception:
            continue
    _state = _default_state()
    return _state


def _save_state():
    global _state
    if _state is None:
        return False
    for path in [STATE_FILE, FALLBACK_STATE_FILE]:
        try:
            with open(path, "w") as f:
                json.dump(_state, f, default=str, indent=2)
            return True
        except Exception:
            continue
    return False


# ── Tier evaluation ──────────────────────────────────────────
def get_eligible_tier(closed_trades: int, total_pnl: float) -> int:
    """
    Given a trade record and P&L, return the highest tier this AI
    qualifies for. Used to detect tier-up events.

    Note: an AI doesn't auto-promote — promotion is gated by the bot
    actually triggering an evolution proposal cycle. This just tells
    us the ceiling.
    """
    eligible = 0
    for tier_num, spec in TIERS.items():
        if (closed_trades >= spec["min_trades"]
                and total_pnl >= spec["min_pnl_usd"]):
            eligible = max(eligible, tier_num)
    return eligible


def get_ai_tier(ai_name: str) -> int:
    """Current active tier for the named AI."""
    s = _load_state()
    if ai_name not in s:
        return 0
    return int(s[ai_name].get("current_tier", 0))


def get_ai_state(ai_name: str) -> dict:
    """Full state snapshot for one AI — for /evolution endpoint + dashboard."""
    s = _load_state()
    if ai_name not in s:
        return dict(_default_state()[ai_name])
    return dict(s[ai_name])


def get_full_status(claude_stats: dict = None, grok_stats: dict = None) -> dict:
    """
    Snapshot for /evolution endpoint. Optionally pass per-AI stats
    (trade count + P&L) to compute "next tier eligibility."
    """
    s = _load_state()
    out = {
        "schema_version":        s.get("schema_version", 1),
        "created_iso":           s.get("created_iso"),
        "tiers":                 {str(k): v for k, v in TIERS.items()},
        "auto_revert_threshold": AUTO_REVERT_WINRATE_THRESHOLD,
        "soft_reverts_until_hard": SOFT_REVERT_BEFORE_HARD,
        "ai_states":             {},
    }
    for ai in ("claude", "grok"):
        ai_state = dict(s.get(ai, _default_state()[ai]))
        # Augment with eligibility info if stats supplied
        stats = (claude_stats if ai == "claude" else grok_stats) or {}
        trades = int(stats.get("trades", 0))
        pnl    = float(stats.get("total_pnl", 0))
        eligible = get_eligible_tier(trades, pnl)
        ai_state["closed_trades"]      = trades
        ai_state["total_pnl_usd"]      = round(pnl, 2)
        ai_state["eligible_tier"]      = eligible
        ai_state["current_tier_name"]  = TIERS[ai_state["current_tier"]]["name"]
        ai_state["current_tier_cap"]   = TIERS[ai_state["current_tier"]]["prompt_token_cap"]
        # Distance to next tier
        next_tier = ai_state["current_tier"] + 1
        if next_tier in TIERS:
            spec = TIERS[next_tier]
            ai_state["next_tier"]         = next_tier
            ai_state["next_tier_name"]    = spec["name"]
            ai_state["trades_to_next"]    = max(0, spec["min_trades"] - trades)
            ai_state["pnl_to_next_usd"]   = round(max(0.0, spec["min_pnl_usd"] - pnl), 2)
        else:
            ai_state["next_tier"]         = None
        out["ai_states"][ai] = ai_state
    return out


# ── Audit helpers ────────────────────────────────────────────
def log_evolution_event(ai_name: str, event_type: str, message: str, **extra):
    """Append an event to the AI's audit trail. Capped at last 100."""
    s = _load_state()
    if ai_name not in s:
        return
    evt = {
        "ts":      datetime.now(timezone.utc).isoformat(),
        "type":    event_type,
        "message": message,
        **extra,
    }
    s[ai_name].setdefault("evolution_history", []).append(evt)
    if len(s[ai_name]["evolution_history"]) > 100:
        s[ai_name]["evolution_history"] = s[ai_name]["evolution_history"][-100:]
    _save_state()


# ── PASS A: Get the AI's current effective prompt addition ──
# In Pass A, this returns "" for every AI because nobody has earned
# customization yet. In Pass B, it returns the AI's earned custom block.
def get_custom_prompt_addition(ai_name: str) -> str:
    """
    Returns the AI's earned prompt addition (empty in Tier 0).
    Wrapped in a labeled block so it's clearly demarcated from the
    base prompt and easy to strip if reverting.
    """
    s = _load_state()
    if ai_name not in s:
        return ""
    custom = s[ai_name].get("custom_prompt", "").strip()
    if not custom:
        return ""
    tier = s[ai_name].get("current_tier", 0)
    tier_name = TIERS.get(tier, {}).get("name", "Unknown")
    return (f"\n\n=== YOUR EARNED STYLE (Tier {tier} {tier_name}) ===\n"
            f"{custom}\n"
            f"=== END EARNED STYLE ===\n")


# ── Rivalry context (Pass A — used immediately) ──────────────
def build_rivalry_context(ai_name: str,
                           my_pnl: float, my_trades: int, my_wins: int,
                           rival_pnl: float, rival_trades: int, rival_wins: int,
                           leader: str = None) -> str:
    """
    Return a short rivalry-context block for the AI's prompt.
    Pairs concrete standings with explicit anti-tilt guidance — the
    AI knows where it stands but is reminded NOT to revenge-trade.

    leader: 'claude', 'grok', 'tie', or None (no closed trades yet)
    """
    rival_name = "Grok" if ai_name == "claude" else "Claude"
    if my_trades == 0 and rival_trades == 0:
        return (
            "═══ RIVALRY ═══\n"
            f"You and {rival_name} both start at zero. First closed trade sets the tone.\n"
            "═════════════════"
        )

    my_wr    = round(my_wins / my_trades * 100, 1) if my_trades else 0
    rival_wr = round(rival_wins / rival_trades * 100, 1) if rival_trades else 0

    # Standing line — concrete, no spin
    if leader == ai_name:
        margin = abs(my_pnl - rival_pnl)
        standing = f"You are LEADING {rival_name} by ${margin:.2f}."
    elif leader == ("grok" if ai_name == "claude" else "claude"):
        margin = abs(rival_pnl - my_pnl)
        standing = f"You are TRAILING {rival_name} by ${margin:.2f}."
    else:
        standing = f"You and {rival_name} are TIED."

    # Anti-tilt guidance — proven psychology, not editorialized
    return (
        "═══ RIVALRY ═══\n"
        f"{standing}\n"
        f"Your record: {my_wins}W/{my_trades - my_wins}L ({my_wr:.0f}% win rate, ${my_pnl:+.2f} realized)\n"
        f"{rival_name}'s record: {rival_wins}W/{rival_trades - rival_wins}L "
        f"({rival_wr:.0f}% WR, ${rival_pnl:+.2f} realized)\n"
        "Patient trading wins long-term. Chasing losses is the #1 account killer.\n"
        "Focus on YOUR best setup, not on what the other AI is doing.\n"
        "═════════════════"
    )


# ── Validation layer (Pass A — partial; Pass B fills in) ─────
def validate_proposed_prompt(ai_name: str, proposed_addition: str,
                              target_tier: int) -> tuple:
    """
    Validates a proposed custom prompt addition against:
      • Tier-specific token cap
      • Hard ALWAYS_BANNED_PHRASES regex
      • Banned topics (Core Reserve, etc.)

    Returns (ok: bool, reason: str). In Pass A this is a stub that
    always returns False — AIs cannot modify yet. Pass B implements
    the full validation.
    """
    # Pass A: always reject. Self-modification is not yet enabled.
    return (False, "Pass A: self-modification is not yet enabled. "
                   "Pass B will activate this with full validation.")


# ── Public summary string for log lines ──────────────────────
def format_tier_log_line(ai_name: str, trades: int, pnl: float) -> str:
    """One-line tier status for logs."""
    tier = get_ai_tier(ai_name)
    spec = TIERS[tier]
    eligible = get_eligible_tier(trades, pnl)
    upgrade_pending = eligible > tier
    arrow = " 🔺 ELIGIBLE FOR UPGRADE" if upgrade_pending else ""
    return (f"[{ai_name.upper()}] Tier {tier} ({spec['name']}) "
            f"· {trades} trades · ${pnl:+.2f}{arrow}")
