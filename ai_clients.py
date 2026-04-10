"""
ai_clients.py — NovaTrade AI Client Module
═══════════════════════════════════════════
Claude and Grok API wrappers, JSON parsing, health checks.
Injected with shared_state and log by bot_with_proxy.py.
"""

import os
import re
import json
import time
import requests
import httpx
from datetime import datetime

# ── API keys (from env vars) ─────────────────────────────────
ANTHROPIC_KEY    = os.environ.get("ANTHROPIC_KEY", "") or os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_BASE   = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
GROK_KEY         = os.environ.get("GROK_KEY", "")

# ── Shared references (injected by bot) ──────────────────────
log          = print
shared_state = {}


def _set_context(log_fn, shared_state_ref=None):
    """Called by bot to inject log and shared_state."""
    global log, shared_state, ANTHROPIC_KEY, GROK_KEY
    log = log_fn
    if shared_state_ref is not None:
        shared_state = shared_state_ref
    # Re-read keys at runtime (Railway env vars)
    ANTHROPIC_KEY = os.environ.get("ANTHROPIC_KEY", "") or os.environ.get("ANTHROPIC_API_KEY", "")
    GROK_KEY      = os.environ.get("GROK_KEY", "")


def ask_claude(prompt, system="You are a trading AI. Respond with ONLY valid compact JSON. No markdown, no prose, no extra text.", max_tokens=1200):
    with httpx.Client(timeout=60) as http:
        res = http.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": max_tokens,
                  "system": system, "messages": [{"role": "user", "content": prompt}]},
        )
        if not res.is_success: raise Exception(f"{res.status_code}: {res.text}")
        return res.json()["content"][0]["text"]

def ask_grok(prompt, system="You are a trading AI. Respond with ONLY valid compact JSON. No markdown, no prose, no extra text.", max_tokens=1200):
    with httpx.Client(timeout=60) as http:
        res = http.post(
            "https://api.x.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROK_KEY}", "Content-Type": "application/json"},
            json={"model": "grok-4-1-fast-non-reasoning", "max_tokens": max_tokens,
                  "messages": [{"role": "system", "content": system},
                                {"role": "user", "content": prompt}]},
        )
        if not res.is_success: raise Exception(f"{res.status_code}: {res.text}")
        return res.json()["choices"][0]["message"]["content"]

def clean_json_str(raw):
    import re
    # Remove markdown code fences (```json ... ``` or ``` ... ```)
    raw = re.sub(r'```(?:json)?\s*', '', raw).replace('```', '').strip()
    # Remove non-printable chars except whitespace
    raw = "".join(ch for ch in raw if ord(ch) >= 32 or ch in "\n\t")
    # Remove trailing commas before } or ]
    raw = re.sub(r",\s*([}\]])", r"\1", raw)
    # Find the outermost JSON object or array
    first_brace  = raw.find("{")
    first_bracket = raw.find("[")
    if first_brace == -1 and first_bracket == -1:
        return raw
    if first_brace == -1:
        first = first_bracket
    elif first_bracket == -1:
        first = first_brace
    else:
        first = min(first_brace, first_bracket)
    if first > 0:
        raw = raw[first:]
    last = max(raw.rfind("}"), raw.rfind("]"))
    if last != -1:
        raw = raw[:last+1]
    return raw.strip()

# Abbreviated key → full key mapping for compact R1 responses
_R1_KEY_MAP = {
    "sn": "strategy_name",
    "mt": "market_thesis",
    "pt": "proposed_trades",
    "cc": "collaborative_candidates",
    "bw": "bearish_watchlist",
    # Inside proposed_trades objects
    "a":  "action",
    "s":  "symbol",
    "n":  "notional_usd",
    "c":  "confidence",
    "f":  "flags",
    "r":  "rationale",
}

def _expand_r1_keys(obj):
    """Recursively expand abbreviated R1 keys to full names."""
    if isinstance(obj, dict):
        return {_R1_KEY_MAP.get(k, k): _expand_r1_keys(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_r1_keys(i) for i in obj]
    return obj

def parse_json(raw):
    try:
        raw = clean_json_str(raw)
        s=raw.find("{"); e=raw.rfind("}")+1
        if s==-1 or e==0: return None
        json_str = raw[s:e]
        result = None
        try:
            result = json.loads(json_str)
        except json.JSONDecodeError:
            # Try trimming truncated response — remove last incomplete field
            last_comma = json_str.rfind(",")
            if last_comma > 0:
                try: result = json.loads(json_str[:last_comma]+"}")
                except: pass
            # Try closing unclosed string + object as last resort
            if result is None:
                try:
                    patched = json_str.rstrip().rstrip(',').rstrip('"') + '"}'  # close truncation
                    result = json.loads(patched)
                except: pass
        if result and isinstance(result, dict):
            result = _expand_r1_keys(result)
        return result
    except: return None

def ask_with_retry(ask_fn, prompt, system, retries=3):
    for attempt in range(retries+1):
        try:
            raw    = ask_fn(prompt, system)
            result = parse_json(raw)
            if result: return result
            if attempt < retries:
                log(f"⚠️ JSON parse failed attempt {attempt+1}, raw: {raw[:100]}")
                time.sleep(2)
            else:
                log(f"⚠️ All retries failed. Raw: {raw[:200]}")
        except Exception as e:
            if attempt < retries:
                log(f"⚠️ API error {attempt+1}: {e}")
                time.sleep(3)
            else:
                log(f"❌ Final error: {e}")
    return None

# ── Market Schedule ──────────────────────────────────────

def classify_ai_error(error_str):
    """
    Classify what kind of failure this is.
    Credit exhaustion needs different handling than a timeout.
    """
    e = str(error_str).lower()
    if any(x in e for x in ["credit", "billing", "quota", "insufficient_quota",
                              "rate_limit", "429", "payment", "balance", "overloaded"]):
        return "credits_exhausted"
    if any(x in e for x in ["timeout", "connection", "network", "resolve", "unreachable"]):
        return "network_error"
    if any(x in e for x in ["401", "403", "invalid_api_key", "authentication"]):
        return "auth_error"
    return "unknown_error"

def safe_ask_claude(prompt, system, retries=3):
    """
    Claude with full health tracking.
    Detects: credit exhaustion, network errors, auth errors.
    On credits exhausted → immediately hands off to Grok (no retries).
    On network error → retries then marks unhealthy.
    Auto-recovers after 30 min for network errors.
    Credit exhaustion needs manual top-up — logs clear warning.
    """
    try:
        result = ask_with_retry(ask_claude, prompt, system, retries=retries)
        if result:
            shared_state["claude_healthy"]    = True
            shared_state["claude_fail_count"] = 0
            shared_state["claude_fail_reason"] = None
        else:
            shared_state["claude_fail_count"] += 1
            if shared_state["claude_fail_count"] >= RULES["failover_max_retries"]:
                shared_state["claude_healthy"]   = False
                shared_state["last_claude_fail"] = datetime.now().isoformat()
                log(f"⚠️ Claude UNHEALTHY — Grok taking over")
        return result
    except Exception as e:
        error_type = classify_ai_error(str(e))
        shared_state["claude_fail_count"]  += 1
        shared_state["claude_fail_reason"]  = error_type

        if error_type == "credits_exhausted":
            # Immediate handoff — no point retrying
            shared_state["claude_healthy"]    = False
            shared_state["claude_credits_ok"] = False
            shared_state["last_claude_fail"]  = datetime.now().isoformat()
            log(f"💳 CLAUDE CREDITS EXHAUSTED — Immediate handoff to Grok")
            log(f"   ACTION REQUIRED: Top up Anthropic API credits at console.anthropic.com")
            log(f"   Grok will handle ALL trading until Claude credits are restored")
        elif error_type == "auth_error":
            shared_state["claude_healthy"]   = False
            shared_state["last_claude_fail"] = datetime.now().isoformat()
            log(f"🔑 CLAUDE AUTH ERROR — Check ANTHROPIC_KEY in Railway variables")
        else:
            if shared_state["claude_fail_count"] >= RULES["failover_max_retries"]:
                shared_state["claude_healthy"]   = False
                shared_state["last_claude_fail"] = datetime.now().isoformat()
                log(f"❌ Claude UNHEALTHY ({error_type}) — Grok taking over")
        return None

def safe_ask_grok(prompt, system, retries=3):
    """
    Grok with full health tracking.
    Same logic as Claude — detects credit exhaustion vs network errors.
    On credits exhausted → immediately hands off to Claude (no retries).
    """
    try:
        result = ask_with_retry(ask_grok, prompt, system, retries=retries)
        if result:
            shared_state["grok_healthy"]    = True
            shared_state["grok_fail_count"] = 0
            shared_state["grok_fail_reason"] = None
        else:
            shared_state["grok_fail_count"] += 1
            if shared_state["grok_fail_count"] >= RULES["failover_max_retries"]:
                shared_state["grok_healthy"]   = False
                shared_state["last_grok_fail"] = datetime.now().isoformat()
                log(f"⚠️ Grok UNHEALTHY — Claude taking over")
        return result
    except Exception as e:
        error_type = classify_ai_error(str(e))
        shared_state["grok_fail_count"]  += 1
        shared_state["grok_fail_reason"]  = error_type

        if error_type == "credits_exhausted":
            shared_state["grok_healthy"]    = False
            shared_state["grok_credits_ok"] = False
            shared_state["last_grok_fail"]  = datetime.now().isoformat()
            log(f"💳 GROK CREDITS EXHAUSTED — Immediate handoff to Claude")
            log(f"   ACTION REQUIRED: Top up xAI API credits at console.x.ai")
            log(f"   Claude will handle ALL trading until Grok credits are restored")
        elif error_type == "auth_error":
            shared_state["grok_healthy"]   = False
            shared_state["last_grok_fail"] = datetime.now().isoformat()
            log(f"🔑 GROK AUTH ERROR — Check GROK_KEY in Railway variables")
        else:
            if shared_state["grok_fail_count"] >= RULES["failover_max_retries"]:
                shared_state["grok_healthy"]   = False
                shared_state["last_grok_fail"] = datetime.now().isoformat()
                log(f"❌ Grok UNHEALTHY ({error_type}) — Claude taking over")
        return None

def check_ai_health():
    """Check AI health; auto-recover network errors after 30min. Returns (c_ok, g_ok, mode)."""
    try:
        now = datetime.now()
        for ai in ["claude", "grok"]:
            last_fail   = shared_state.get(f"last_{ai}_fail")
            fail_reason = shared_state.get(f"{ai}_fail_reason")
            if last_fail and not shared_state[f"{ai}_healthy"]:
                fail_time = datetime.fromisoformat(last_fail)
                mins_down = (now - fail_time).seconds // 60
                if fail_reason == "credits_exhausted":
                    log(f"💳 {ai.title()} credits exhausted ({mins_down}m) — top-up needed")
                    continue
                elif fail_reason == "auth_error":
                    log(f"🔑 {ai.title()} auth error ({mins_down}m) — check Railway env key")
                    continue
                elif (now - fail_time).seconds >= 1800:
                    shared_state[f"{ai}_healthy"]     = True
                    shared_state[f"{ai}_fail_count"]  = 0
                    shared_state[f"{ai}_fail_reason"] = None
                    log(f"🔄 {ai.title()} auto-recovered after {mins_down}m")
        c_ok     = shared_state["claude_healthy"]
        g_ok     = shared_state["grok_healthy"]
        c_reason = shared_state.get("claude_fail_reason", "")
        g_reason = shared_state.get("grok_fail_reason",  "")
        if c_ok and g_ok:
            mode = None
            log("✅ Both AIs healthy — full collaboration")
        elif c_ok and not g_ok:
            mode = "claude_only"
            log(f"⚠️ FAILOVER: Grok down ({g_reason}) — Claude solo")
        elif g_ok and not c_ok:
            mode = "grok_only"
            log(f"⚠️ FAILOVER: Claude down ({c_reason}) — Grok solo")
        else:
            mode = "autopilot"
            log(f"🆘 BOTH AIs down — AUTOPILOT | Claude:{c_reason} Grok:{g_reason}")
        shared_state["failover_mode"] = mode
        return c_ok, g_ok, mode
    except Exception as e:
        log(f"⚠️ check_ai_health: {e}")
        return True, True, None
