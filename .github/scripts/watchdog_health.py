#!/usr/bin/env python3
"""
watchdog_health.py — NovaTrade deploy watchdog (decision brain)
================================================================

Polls the live bot's /health endpoint and decides what the GitHub Action
should DO. It does NOT touch git — it only emits a decision; the workflow
performs the git revert / promote / alert. Keeping it this way makes the
logic pure and testable (see test_watchdog.py).

Decisions emitted (written to $GITHUB_OUTPUT as decision=... reason=...):
    healthy   — bot is up; nothing to do
    promote   — bot is up AND has completed a recent trading cycle; the
                workflow should advance the last-known-good branch to HEAD
    revert    — bot is unreachable (hard down); workflow should restore
                files from last-known-good and push
    alert     — something is wrong but NOT a clean hard-down (e.g. server
                up but trading loop dead/stalled); workflow should notify a
                human and NOT auto-revert

Modes:
    deploy_check — run right after a push; we just confirmed a deploy. A hard
                   down here = the new code broke the boot → revert.
    maintain     — run on a schedule. Healthy + a fresh cycle → promote.
                   Hard down → revert (the workflow's tree-compare prevents
                   re-reverting if HEAD already equals last-known-good).

Health response contract (ideal — see the /health snippet we ship):
    { "ok": true, "trading_loop_alive": true, "last_cycle_age_sec": 142 }
If the endpoint only returns HTTP 200 with no JSON, we degrade safely:
reachable=up is enough to AVOID a revert, but without a cycle signal we will
never auto-promote (conservative — we won't enshrine an unproven version).
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error

# ── Tunables (overridable via env) ───────────────────────────────────────────
BOT_URL        = os.environ.get("BOT_URL", "").rstrip("/")
MODE           = os.environ.get("MODE", "maintain")           # deploy_check | maintain
RETRIES        = int(os.environ.get("RETRIES", "8"))
RETRY_DELAY    = int(os.environ.get("RETRY_DELAY", "15"))     # seconds between tries
HTTP_TIMEOUT   = int(os.environ.get("HTTP_TIMEOUT", "10"))    # per-request timeout
MAX_CYCLE_AGE  = int(os.environ.get("MAX_CYCLE_AGE_SEC", "1800"))  # 30 min: a "fresh" cycle


def poll_health(url, retries, delay, timeout):
    """
    Try /health up to `retries` times. Return (reachable: bool, body: dict|None).
    reachable is True if we ever got HTTP 200. body is the parsed JSON of the
    last 200 response (or None if no JSON / never reached).
    """
    last_body = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url + "/health", headers={"Cache-Control": "no-cache"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status == 200:
                    raw = resp.read().decode("utf-8", errors="replace")
                    try:
                        last_body = json.loads(raw)
                    except Exception:
                        last_body = None  # 200 but not JSON — still "up"
                    print(f"  attempt {attempt}/{retries}: 200 OK")
                    return True, last_body
                print(f"  attempt {attempt}/{retries}: HTTP {resp.status}")
        except urllib.error.HTTPError as e:
            print(f"  attempt {attempt}/{retries}: HTTP {e.code}")
        except Exception as e:
            print(f"  attempt {attempt}/{retries}: unreachable ({type(e).__name__})")
        if attempt < retries:
            time.sleep(delay)
    return False, last_body


def decide(reachable, body, mode, max_cycle_age):
    """
    PURE decision function — no I/O. Returns (decision, reason).
    This is the unit-tested core.
    """
    # ── Hard down ────────────────────────────────────────────────────────────
    if not reachable:
        return "revert", (
            f"/health unreachable after all retries (mode={mode}). "
            f"Treating as hard-down — restore last-known-good."
        )

    # Reachable. Pull whatever signals the endpoint gave us.
    body = body or {}
    loop_alive = body.get("trading_loop_alive", None)   # None = endpoint didn't say
    cycle_age  = body.get("last_cycle_age_sec", None)    # None = endpoint didn't say

    # ── Server up but trading engine explicitly dead → soft alert, no revert ──
    if loop_alive is False:
        return "alert", (
            "Server responds but trading_loop_alive=false — engine is down "
            "while the web server is up. Needs a human; not auto-reverting."
        )

    # ── Server up but its last cycle is stale → soft alert ────────────────────
    if cycle_age is not None and cycle_age > max_cycle_age:
        return "alert", (
            f"Server up but last trading cycle was {cycle_age}s ago "
            f"(> {max_cycle_age}s threshold) — possibly stalled. Notifying a human."
        )

    # ── Promotion only on the maintain pass, and only with a proven cycle ────
    if mode == "maintain":
        if cycle_age is not None and cycle_age <= max_cycle_age:
            return "promote", (
                f"Healthy and a trading cycle completed {cycle_age}s ago — "
                f"safe to advance last-known-good to HEAD."
            )
        # Up, but no cycle signal (basic /health). Stay conservative.
        return "healthy", (
            "Healthy, but endpoint reports no cycle age — not promoting "
            "(conservative). Add the enriched /health to enable auto-promote."
        )

    # ── deploy_check: server came up after the push. Good enough; don't promote
    return "healthy", "Deploy is up and serving — boot succeeded."


def main():
    if not BOT_URL:
        print("ERROR: BOT_URL is not set (set it as a repo variable).", file=sys.stderr)
        # Fail safe: emit 'alert' rather than risk a wrong revert/promote.
        _emit("alert", "BOT_URL not configured")
        return

    print(f"Watchdog: polling {BOT_URL}/health  (mode={MODE})")
    reachable, body = poll_health(BOT_URL, RETRIES, RETRY_DELAY, HTTP_TIMEOUT)
    decision, reason = decide(reachable, body, MODE, MAX_CYCLE_AGE)
    print(f"\nDECISION: {decision}\nREASON:   {reason}")
    _emit(decision, reason)


def _emit(decision, reason):
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"decision={decision}\n")
            # reason may contain newlines; keep it single-line for the output
            f.write(f"reason={reason.replace(chr(10), ' ')}\n")


if __name__ == "__main__":
    main()
