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

Pass A: Foundation. Tier 0 framework, neutral prompts, rivalry
context injection, dashboard surfacing.

Pass B (ACTIVE): Self-modification loop. Once an AI has ≥10 closed
trades, run_evolution_cycle() (called hourly by the bot, AI call at
most once per 72h) handles tier-ups, asks the AI to propose its own
prompt addition, validates it (tier token budget + banned phrases),
applies it, and auto-reverts when the win rate over the trades made
under a prompt version drops below 40% — three consecutive reverts
demote the AI back to Tier 0 with a 7-day re-promotion lock.
═══════════════════════════════════════════════════════════════════════
"""
import json
import os
from datetime import datetime, timezone, timedelta

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

# ── Pass B: self-modification loop ───────────────────────────
ENABLE_SELF_MODIFICATION = True
PROPOSAL_MIN_TRADES      = 10     # Loop activates once the AI has this many closes
PROPOSAL_COOLDOWN_HOURS  = 72     # At most one proposal AI-call per AI per 3 days
DEMOTION_LOCK_HOURS      = 168    # After a hard revert, no re-promotion for 7 days
# Tier prompt_token_cap covers base prompt + addition; the base neutral
# prompt budget is Tier 0's cap, so each tier's addition budget is cap − base.
BASE_PROMPT_TOKENS       = TIERS[0]["prompt_token_cap"]

# Persistent state file (Railway /data volume — survives redeploys)
STATE_FILE = "/data/ai_evolution.json"
FALLBACK_STATE_FILE = "./ai_evolution.json"

# ── Injected logger (bot replaces on boot) ───────────────────
log = print

def _set_context(log_fn=None):
    """Inject the bot's log function. Called once on boot."""
    global log
    if log_fn:
        log = log_fn


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
        # Pass B
        "trades_at_version_start": 0,          # Closed-trade count when version applied
        "last_proposal_time":    None,
        "demoted_until":         None,         # Re-promotion lock after hard revert
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
        "pass_b": {
            "self_modification":     ENABLE_SELF_MODIFICATION,
            "proposal_min_trades":   PROPOSAL_MIN_TRADES,
            "proposal_cooldown_h":   PROPOSAL_COOLDOWN_HOURS,
            "demotion_lock_h":       DEMOTION_LOCK_HOURS,
        },
        "ai_states":             {},
    }
    for ai in ("claude", "grok"):
        ai_state = dict(s.get(ai, _default_state()[ai]))
        # Payload hygiene: cap audit trail at last 10 (full trail stays in state)
        ai_state["history_total"]     = len(ai_state.get("evolution_history", []))
        ai_state["evolution_history"] = ai_state.get("evolution_history", [])[-10:]
        ai_state["trades_on_version"] = None   # Filled below once stats known
        # Augment with eligibility info if stats supplied
        stats = (claude_stats if ai == "claude" else grok_stats) or {}
        trades = int(stats.get("trades", 0))
        pnl    = float(stats.get("total_pnl", 0))
        eligible = get_eligible_tier(trades, pnl)
        ai_state["closed_trades"]      = trades
        ai_state["total_pnl_usd"]      = round(pnl, 2)
        ai_state["eligible_tier"]      = eligible
        ai_state["trades_on_version"]  = max(0, trades - int(ai_state.get("trades_at_version_start", 0)))
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


# ── Pass B helpers ───────────────────────────────────────────
def _hours_since(iso_ts) -> float:
    """Hours elapsed since an ISO timestamp. Infinity if None/invalid."""
    if not iso_ts:
        return float("inf")
    try:
        ts = datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds() / 3600
    except Exception:
        return float("inf")


def _est_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token) for budget enforcement."""
    return max(1, len(text or "") // 4)


# ── Validation layer (Pass B — live) ─────────────────────────
def validate_proposed_prompt(ai_name: str, proposed_addition: str,
                              target_tier: int) -> tuple:
    """
    Validates a proposed custom prompt addition against:
      • Tier-specific token budget (tier cap − base prompt budget)
      • Hard ALWAYS_BANNED_PHRASES list
      • Tier modification rights (can_modify)

    Returns (ok: bool, reason: str).
    """
    if not ENABLE_SELF_MODIFICATION:
        return (False, "self-modification disabled")
    spec = TIERS.get(target_tier)
    if not spec:
        return (False, f"unknown tier {target_tier}")
    if not spec.get("can_modify"):
        return (False, f"Tier {target_tier} ({spec['name']}) has no modification rights")
    text = (proposed_addition or "").strip()
    if not text:
        return (False, "empty proposal")
    low = text.lower()
    for phrase in ALWAYS_BANNED_PHRASES:
        if phrase in low:
            return (False, f"banned phrase: '{phrase}'")
    budget = spec["prompt_token_cap"] - BASE_PROMPT_TOKENS
    est = _est_tokens(text)
    if est > budget:
        return (False, f"too long: ~{est} tokens > {budget} budget for Tier {target_tier}")
    return (True, "ok")


# ── Pass B: apply / revert ───────────────────────────────────
def apply_prompt_change(ai_name: str, new_addition: str,
                        current_trades: int, source: str = "self_proposal"):
    """Archive the old prompt, apply the new one, start a fresh
    evaluation window. Caller must have validated already."""
    s = _load_state()
    ai = s[ai_name]
    if ai.get("custom_prompt", "").strip():
        ai.setdefault("prompt_versions", []).append({
            "version":    ai.get("prompt_version", 0),
            "text":       ai["custom_prompt"],
            "retired_ts": datetime.now(timezone.utc).isoformat(),
        })
        ai["prompt_versions"] = ai["prompt_versions"][-10:]
    ai["custom_prompt"]           = new_addition.strip()
    ai["prompt_version"]          = int(ai.get("prompt_version", 0)) + 1
    ai["trades_at_version_start"] = int(current_trades)
    _save_state()
    log_evolution_event(ai_name, "PROMPT_APPLIED",
                        f"Prompt v{ai['prompt_version']} applied ({source}, "
                        f"~{_est_tokens(new_addition)} tokens)",
                        version=ai["prompt_version"], source=source)


def check_auto_revert(ai_name: str, trades: int, recent_results: list):
    """
    Judge the current prompt version once enough trades ran under it.
    recent_results: realized pnl_usd of the AI's last closed trades
    (newest last). Returns "soft", "hard", or None.

    Win rate below threshold → soft revert to the previous version.
    SOFT_REVERT_BEFORE_HARD consecutive reverts → hard revert to
    Tier 0 with a DEMOTION_LOCK_HOURS re-promotion lock.
    """
    s = _load_state()
    ai = s[ai_name]
    if not ai.get("custom_prompt", "").strip():
        return None
    trades_on_version = trades - int(ai.get("trades_at_version_start", 0))
    if trades_on_version < AUTO_REVERT_MIN_TRADES:
        return None
    window = [p for p in recent_results if p is not None][-min(10, trades_on_version):]
    if len(window) < AUTO_REVERT_MIN_TRADES:
        return None
    win_rate = sum(1 for p in window if p > 0) / len(window)
    if win_rate >= AUTO_REVERT_WINRATE_THRESHOLD:
        # Version survives its evaluation — clear the bad-change streak
        if trades_on_version >= 10 and ai.get("consecutive_soft_reverts", 0):
            ai["consecutive_soft_reverts"] = 0
            _save_state()
        return None

    # Soft revert — restore the previous archived version (or none)
    bad_version = ai.get("prompt_version", 0)
    previous = ai.get("prompt_versions", [])
    restored = previous.pop()["text"] if previous else ""
    ai["custom_prompt"]            = restored
    ai["prompt_version"]           = bad_version + 1
    ai["trades_at_version_start"]  = trades
    ai["consecutive_soft_reverts"] = int(ai.get("consecutive_soft_reverts", 0)) + 1
    streak = ai["consecutive_soft_reverts"]
    _save_state()
    log_evolution_event(ai_name, "PROMPT_REVERTED",
                        f"Prompt v{bad_version} reverted: {win_rate*100:.0f}% WR over "
                        f"last {len(window)} trades < {AUTO_REVERT_WINRATE_THRESHOLD*100:.0f}% "
                        f"(bad-change streak {streak}/{SOFT_REVERT_BEFORE_HARD})",
                        reverted_version=bad_version, win_rate=round(win_rate*100, 1))

    if streak >= SOFT_REVERT_BEFORE_HARD:
        ai["custom_prompt"]            = ""
        ai["current_tier"]             = 0
        ai["prompt_version"]           = ai["prompt_version"] + 1
        ai["consecutive_soft_reverts"] = 0
        ai["trades_at_version_start"]  = trades
        ai["demoted_until"] = (datetime.now(timezone.utc)
                               + timedelta(hours=DEMOTION_LOCK_HOURS)).isoformat()
        _save_state()
        log_evolution_event(ai_name, "HARD_REVERT",
                            f"{SOFT_REVERT_BEFORE_HARD} consecutive bad prompt changes "
                            f"→ demoted to Tier 0, re-promotion locked "
                            f"{DEMOTION_LOCK_HOURS//24}d",
                            demoted_until=ai["demoted_until"])
        return "hard"
    return "soft"


# ── Pass B: proposal cycle ───────────────────────────────────
_SCOPE_GUIDANCE = {
    "style_notes": "ONE short paragraph of style notes about how you trade "
                   "(pacing, conviction thresholds, position sizing instincts).",
    "preferences": "Up to TWO paragraphs of strategy preferences — you may name "
                   "specific indicators and setups you favor or avoid.",
    "philosophy":  "A personal trading philosophy block — your edge, your rules, "
                   "what you've learned from your own results.",
    "full":        "A full custom block including indicator combinations and "
                   "decision frameworks of your own design.",
}


def _build_proposal_prompt(ai_name: str, trades: int, total_pnl: float,
                           recent_results: list) -> str:
    """The self-reflection briefing the AI reads before proposing."""
    s = _load_state()
    ai = s[ai_name]
    tier = int(ai.get("current_tier", 0))
    spec = TIERS[tier]
    budget = spec["prompt_token_cap"] - BASE_PROMPT_TOKENS
    window = [p for p in recent_results if p is not None][-10:]
    wins = sum(1 for p in window if p > 0)
    recent_line = (f"{wins}W/{len(window) - wins}L over your last {len(window)} closes "
                   f"(${sum(window):+.2f})") if window else "no closed trades yet"
    current = ai.get("custom_prompt", "").strip() or "(none — neutral base prompt only)"
    banned = ", ".join(f'"{p}"' for p in ALWAYS_BANNED_PHRASES[:8]) + ", …"
    return f"""You are {ai_name.upper()}. You have earned Tier {tier} ({spec['name']}) in the evolution system, which grants you the right to maintain a custom addition to your own trading prompt.

YOUR RECORD: {trades} closed trades, ${total_pnl:+.2f} realized P&L. Recent: {recent_line}.

YOUR CURRENT PROMPT ADDITION (v{ai.get('prompt_version', 0)}):
{current}

YOUR MODIFICATION RIGHTS AT THIS TIER:
{_SCOPE_GUIDANCE.get(spec['can_modify'], _SCOPE_GUIDANCE['style_notes'])}

HARD RULES:
- Budget: ~{budget} tokens max for your addition.
- Banned content (auto-rejected): {banned}
- Risk rules, stops, and fees are system-enforced — your addition cannot change them.
- If your win rate drops below {AUTO_REVERT_WINRATE_THRESHOLD*100:.0f}% over the trades made under a new addition, it is auto-reverted. {SOFT_REVERT_BEFORE_HARD} consecutive reverts demote you to Tier 0.

Decide: keep your current addition, or replace it with something that better reflects what your own results say works. Only change it if you have evidence-based reason to.

Reply ONLY with valid JSON:
{{"action": "keep"}}
or
{{"action": "update", "prompt_addition": "your new addition text", "reasoning": "1-2 sentences citing your results"}}"""


def _extract_proposal(resp) -> dict:
    """Normalize an ask_fn response (dict from the safe_ask JSON parser,
    or raw string) into {"action": ..., "prompt_addition": ..., "reasoning": ...}."""
    if isinstance(resp, dict):
        return resp
    if isinstance(resp, str) and resp.strip():
        try:
            cleaned = resp.replace("```json", "").replace("```", "").strip()
            start, end = cleaned.find("{"), cleaned.rfind("}")
            if start >= 0 and end > start:
                return json.loads(cleaned[start:end + 1])
        except Exception:
            pass
    return None


def run_evolution_cycle(ai_name: str, trades: int, total_pnl: float,
                        recent_results: list, ask_fn=None) -> dict:
    """
    Pass B orchestrator — called hourly per AI by the bot. Rule-based
    steps (revert check, tier-up) run every call at zero AI cost; the
    proposal step makes an AI call at most once per PROPOSAL_COOLDOWN_HOURS.

    recent_results: pnl_usd list of the AI's recent closes (newest last).
    Returns {"actions": [...]} describing anything that happened.
    """
    actions = []
    if not ENABLE_SELF_MODIFICATION:
        return {"actions": actions}
    s = _load_state()
    if ai_name not in s:
        return {"actions": actions}
    ai = s[ai_name]

    # 1. Auto-revert — judge the current version first
    reverted = check_auto_revert(ai_name, trades, recent_results)
    if reverted:
        actions.append(f"{reverted}_revert")
        if reverted == "hard":
            return {"actions": actions}    # Demoted — nothing else this cycle

    # 2. Tier-up (blocked while demotion lock is active)
    eligible = get_eligible_tier(trades, total_pnl)
    if eligible > int(ai.get("current_tier", 0)):
        if _hours_since(ai.get("demoted_until")) < 0:
            pass    # Still locked out after a hard revert
        else:
            old = int(ai.get("current_tier", 0))
            ai["current_tier"]     = eligible
            ai["tier_unlocked_at"] = datetime.now(timezone.utc).isoformat()
            _save_state()
            log_evolution_event(ai_name, "TIER_UP",
                                f"Tier {old} → {eligible} ({TIERS[eligible]['name']}): "
                                f"{trades} trades, ${total_pnl:+.2f} P&L",
                                from_tier=old, to_tier=eligible)
            actions.append(f"tier_up_{eligible}")

    # 3. Proposal — needs modification rights, enough trades, cooldown, channel
    tier = int(ai.get("current_tier", 0))
    if (ask_fn and TIERS[tier].get("can_modify")
            and trades >= PROPOSAL_MIN_TRADES
            and _hours_since(ai.get("last_proposal_time")) >= PROPOSAL_COOLDOWN_HOURS):
        # Stamp up front so failures also respect the cooldown
        ai["last_proposal_time"] = datetime.now(timezone.utc).isoformat()
        _save_state()
        try:
            resp = ask_fn(
                _build_proposal_prompt(ai_name, trades, total_pnl, recent_results),
                "You are reviewing your own trading prompt. Reply ONLY with a "
                "compact valid JSON object matching the requested schema.")
        except Exception as e:
            resp = None
            log(f"⚠️ Evolution proposal call failed for {ai_name}: {e}")
        proposal = _extract_proposal(resp)
        if not proposal:
            log_evolution_event(ai_name, "PROPOSAL_FAILED",
                                "Proposal response missing or unparseable")
            actions.append("proposal_failed")
        elif str(proposal.get("action", "")).lower() == "update":
            addition = str(proposal.get("prompt_addition", ""))
            ok, reason = validate_proposed_prompt(ai_name, addition, tier)
            if ok:
                apply_prompt_change(ai_name, addition, trades)
                actions.append("prompt_updated")
            else:
                log_evolution_event(ai_name, "PROPOSAL_REJECTED",
                                    f"Validator rejected proposal: {reason}",
                                    reason=reason)
                actions.append("proposal_rejected")
        else:
            log_evolution_event(ai_name, "PROPOSAL_KEEP",
                                f"AI chose to keep prompt v{ai.get('prompt_version', 0)}")
            actions.append("proposal_keep")

    return {"actions": actions}


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
