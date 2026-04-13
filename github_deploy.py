"""
github_deploy.py — NovaTrade GitHub Deploy Module
══════════════════════════════════════════════════
Pushes bot files to GitHub, triggering Railway auto-deploy.
Self-contained — only needs env vars and requests.
"""

import os
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


def github_get_file_sha(filename: str) -> str | None:
    """Get current SHA of a file in GitHub repo (needed to update it)."""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return None
    try:
        url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}"
        res = requests.get(url, headers={
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json",
        }, params={"ref": GITHUB_BRANCH}, timeout=10)
        if res.ok:
            return res.json().get("sha")
        return None
    except Exception:
        return None

def github_push_file(filename: str, content: str, commit_msg: str) -> dict:
    """
    Push a single file to GitHub via the Contents API.
    Returns {"success": bool, "message": str}
    """
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return {"success": False, "message": "GITHUB_TOKEN or GITHUB_REPO not set"}
    try:
        import base64
        sha     = github_get_file_sha(filename)
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
    log(f"{'✅' if failed == 0 else '⚠️'} {summary}")

    if success > 0:
        log(f"   Railway will auto-deploy from GitHub shortly (~60s)")

    return {
        "success":  failed == 0,
        "pushed":   success,
        "failed":   failed,
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
