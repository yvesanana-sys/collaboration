"""
github_deploy.py — NovaTrade GitHub Deploy Module
══════════════════════════════════════════════════
Pushes bot files to GitHub, triggering Railway auto-deploy.
Self-contained — only needs env vars and requests.

HARDENED 2026-06-04: every push now passes through an integrity guard
(_validate_push_content) that BLOCKS pushing a critical file if it would
truncate or break it. This is the fix for the 2026-06-03 incident, where the
self-repair pipeline regenerated bot_with_proxy.py as a 367-line stub (missing
app.run, the trading loop, and 28 routes) and pushed it straight to main,
taking the bot down. A push that fails validation is refused, not shipped.
"""

import os
import ast
import json
import base64
import requests
from datetime import datetime

# ── GitHub config (from env vars) ────────────────────────────
GITHUB_TOKEN  = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO   = os.environ.get("GITHUB_REPO", "")
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")

# ── Shared references (injected by bot) ──────────────────────
log = print

def _set_context(log_fn):
    """Called by bot to inject log function."""
    global log
    log = log_fn


# ══════════════════════════════════════════════════════════════
# PUSH INTEGRITY GUARD  (added 2026-06-04)
# ══════════════════════════════════════════════════════════════
# Two independent safety nets, both fail-closed (block the push on doubt):
#
#   1. SHRINK GUARD — if a file already exists on GitHub and the new content
#      is dramatically smaller, refuse. Truncation always shows up as a sudden
#      size collapse. This catches ANY truncated file, not just known ones.
#
#   2. STRUCTURE GUARD — critical files must contain the components that make
#      them function. bot_with_proxy.py without app.run() is not a smaller
#      bot, it's a dead one.
#
# A push is blocked if EITHER guard trips. False positives fail safe (a real
# update just needs the human to confirm / bump the threshold); a false
# negative ships a broken bot to production. We bias hard toward blocking.

# new content must be at least this fraction of the existing file's size,
# or the push is refused as a suspected truncation.
SHRINK_FLOOR_RATIO = 0.55

# Critical files: (absolute_min_bytes, [(required_substring, description), ...])
# Substrings are plain text matches — cheap and robust. Tune as files evolve.
CRITICAL_FILE_RULES = {
    "bot_with_proxy.py": (
        60_000,
        [
            ("app.run(",                       "Flask server bind"),
            ('if __name__ == "__main__"',      "program entry point"),
            ("def trading_loop",               "trading engine"),
            ("target=trading_loop",            "trading loop thread start"),
        ],
    ),
    # binance_crypto.py is the other large, truncation-prone file.
    "binance_crypto.py": (
        120_000,
        [
            ("class CryptoTrader", "crypto trading engine class"),
        ],
    ),
}


def _validate_push_content(filename: str, content: str, current_size):
    """
    Return (ok: bool, reason: str). ok=False means DO NOT PUSH.
    current_size is the byte size of the file currently on GitHub, or None
    if the file is new (no shrink comparison possible).
    """
    new_size = len(content.encode("utf-8"))

    # ── Guard 0: any .py file must parse (catches mid-statement truncation) ──
    if filename.endswith(".py"):
        try:
            ast.parse(content)
        except SyntaxError as e:
            return False, f"{filename} has a syntax error (truncated mid-statement?): {e}"

    # ── Guard 1: shrink guard vs the existing GitHub version ─────────────────
    if current_size and current_size > 0:
        ratio = new_size / current_size
        if ratio < SHRINK_FLOOR_RATIO:
            return False, (
                f"{filename} would shrink from {current_size:,} to {new_size:,} bytes "
                f"({ratio:.0%} of current) — suspected TRUNCATION. Push refused."
            )

    # ── Guard 2: structure guard for critical files ─────────────────────────
    rule = CRITICAL_FILE_RULES.get(filename)
    if rule:
        min_bytes, required = rule
        if new_size < min_bytes:
            return False, (
                f"{filename} is {new_size:,} bytes, below the {min_bytes:,}-byte "
                f"floor for a critical file — suspected TRUNCATION. Push refused."
            )
        missing = [desc for sub, desc in required if sub not in content]
        if missing:
            return False, (
                f"{filename} is MISSING required components: {', '.join(missing)}. "
                f"Push refused — this would deploy a broken bot."
            )

    return True, "ok"


def github_get_file_meta(filename: str):
    """Return (sha, size) of a file in the repo, or (None, None) if missing."""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return None, None
    try:
        url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}"
        res = requests.get(url, headers={
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json",
        }, params={"ref": GITHUB_BRANCH}, timeout=10)
        if res.ok:
            j = res.json()
            return j.get("sha"), j.get("size")
        return None, None
    except Exception:
        return None, None


def github_get_file_sha(filename: str):
    """Get current SHA of a file in GitHub repo (needed to update it)."""
    sha, _ = github_get_file_meta(filename)
    return sha


def github_push_file(filename: str, content: str, commit_msg: str) -> dict:
    """
    Push a single file to GitHub via the Contents API.
    Validates content first — a file that fails the integrity guard is NOT
    pushed. Returns {"success": bool, "message": str}
    """
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return {"success": False, "message": "GITHUB_TOKEN or GITHUB_REPO not set"}
    try:
        sha, current_size = github_get_file_meta(filename)

        # ── INTEGRITY GUARD — refuse to push a truncated/broken critical file ──
        ok, reason = _validate_push_content(filename, content, current_size)
        if not ok:
            log(f"   🛑 BLOCKED push of {filename}: {reason}")
            return {"success": False, "blocked": True, "message": reason}

        encoded = base64.b64encode(content.encode("utf-8")).decode("utf-8")
        url     = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}"
        body    = {
            "message": commit_msg,
            "content": encoded,
            "branch":  GITHUB_BRANCH,
        }
        if sha:
            body["sha"] = sha  # Required for updates (not new files)

        res = requests.put(url, headers={
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept":        "application/vnd.github.v3+json",
            "Content-Type":  "application/json",
        }, json=body, timeout=30)

        if res.ok:
            action = "updated" if sha else "created"
            return {"success": True, "message": f"{filename} {action} successfully"}
        else:
            return {"success": False, "message": f"GitHub API error {res.status_code}: {res.text[:200]}"}
    except Exception as e:
        return {"success": False, "message": str(e)}

# All files the bot deploys to GitHub — keep in sync with bot_with_proxy._DEPLOY_FILES
_DEPLOY_FILES = [
    "bot_with_proxy.py", "binance_crypto.py", "projection_engine.py",
    "prompt_builder.py", "self_repair.py", "dashboard.html",
    "thesis_manager.py", "wallet_intelligence.py", "NOVATRADE_MASTER.md",
    "market_data.py", "intelligence.py", "github_deploy.py",
    "ai_clients.py", "sleep_manager.py", "pdt_manager.py",
    "portfolio_manager.py",
]


def github_push_all(commit_msg: str = None, files: list = None) -> dict:
    """
    Push all bot files to GitHub.
    files: optional override — defaults to _DEPLOY_FILES

    If the integrity guard blocks ANY critical file, the whole batch is treated
    as failed (failed > 0) so the caller doesn't report a clean self-update.
    """
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return {
            "success": False,
            "message": "GitHub not configured. Add GITHUB_TOKEN and GITHUB_REPO to Railway env vars.",
            "setup": {
                "GITHUB_TOKEN": "Personal Access Token with repo scope → github.com/settings/tokens",
                "GITHUB_REPO":  "Your repo in owner/repo format e.g. hanz/novatrade",
                "GITHUB_BRANCH": f"Branch to push to (currently: {GITHUB_BRANCH})",
            }
        }

    if not commit_msg:
        commit_msg = (f"NovaTrade auto-deploy {datetime.now().strftime('%Y-%m-%d %H:%M')} "
                      f"— bot self-update")

    results  = {}
    success  = 0
    failed   = 0
    blocked  = 0

    log(f"🚀 GitHub auto-deploy starting → {GITHUB_REPO}:{GITHUB_BRANCH}")

    deploy_list = files if files is not None else _DEPLOY_FILES
    for filename in deploy_list:
        # Try /app first (Railway), then current directory
        for path in [f"/app/{filename}", f"./{filename}", filename]:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()

                result = github_push_file(filename, content, commit_msg)
                results[filename] = result

                if result["success"]:
                    log(f"   ✅ {filename} → {GITHUB_REPO}")
                    success += 1
                else:
                    if result.get("blocked"):
                        blocked += 1
                    log(f"   ❌ {filename}: {result['message']}")
                    failed += 1
                break  # Stop trying paths on first success

            except FileNotFoundError:
                continue
            except Exception as e:
                results[filename] = {"success": False, "message": str(e)}
                failed += 1
                log(f"   ❌ {filename}: {e}")
                break

    summary = (f"GitHub deploy: {success}/{len(deploy_list)} files pushed "
               f"to {GITHUB_REPO}:{GITHUB_BRANCH}")
    if blocked:
        summary += f" — {blocked} BLOCKED by integrity guard (truncation prevented)"
    log(f"{'✅' if failed == 0 else '⚠️'} {summary}")

    if success > 0 and blocked == 0:
        log(f"   Railway will auto-deploy from GitHub shortly (~60s)")
    elif blocked:
        log(f"   ⚠️ Deploy halted on a critical file — nothing dangerous was shipped.")

    return {
        "success":  failed == 0,
        "pushed":   success,
        "failed":   failed,
        "blocked":  blocked,
        "files":    results,
        "repo":     GITHUB_REPO,
        "branch":   GITHUB_BRANCH,
        "message":  summary,
        "commit":   commit_msg,
    }

# ── Crypto symbol guard ───────────────────────────────────────
# Prevents any crypto pair from accidentally routing through Alpaca.
# MSTR, COIN are stocks — allowed. BTC/ETH direct pairs are not.
_CRYPTO_BASES = frozenset([
    "BTC","ETH","SOL","DOGE","AVAX","LINK","ADA","DOT",
    "BNB","XRP","MATIC","ATOM","NEAR","KAVA","ONE","XTZ",
])
