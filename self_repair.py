"""
self_repair.py — NovaTrade Autonomous Bug Detection & Repair
════════════════════════════════════════════════════════════

How it works:
1. Monitors logs every cycle for recurring error patterns
2. When a pattern fires 3+ times → calls Claude Haiku to diagnose
3. Claude reads the broken file from GitHub + NOVATRADE_MASTER.md
4. Claude writes a fix + updated master doc entry
5. Opens a GitHub Pull Request with both changes
6. You review the PR on GitHub and click Merge — Railway auto-deploys

Required Railway env vars:
  GITHUB_TOKEN  — Personal Access Token (repo scope)
  GITHUB_REPO   — e.g. "yvesanana-sys/collaboration"
  GITHUB_BRANCH — default "main"
  ANTHROPIC_API_KEY — already set for the bot
"""

import os
import re
import json
import base64
import requests
import time
from datetime import datetime
from collections import defaultdict

# ── Config ────────────────────────────────────────────────────
GITHUB_TOKEN  = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO   = os.environ.get("GITHUB_REPO", "")
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# How many times an error must appear before triggering repair
ERROR_THRESHOLD = 3

# Files Claude is allowed to read and patch
PATCHABLE_FILES = [
    "bot_with_proxy.py",
    "binance_crypto.py",
    "prompt_builder.py",
    "thesis_manager.py",
    "wallet_intelligence.py",
    "projection_engine.py",
]

MASTER_DOC = "NOVATRADE_MASTER.md"

# ── Error Pattern Registry ─────────────────────────────────────
# Maps error signature → (description, which file to patch, priority)
ERROR_PATTERNS = [
    {
        "pattern": r"Skip \w+USDT — invalid ticker format",
        "description": "Claude proposing crypto USDT pairs in stock execution cycle",
        "file": "bot_with_proxy.py",
        "priority": "medium",
    },
    {
        "pattern": r"Invalid format specifier '\.2f if bull else 0:\.2f'",
        "description": "f-string format error in sleep brief logging",
        "file": "bot_with_proxy.py",
        "priority": "low",
    },
    {
        "pattern": r"Claude crypto parse failed",
        "description": "Claude using compressed JSON keys (sn, mt etc) in crypto response",
        "file": "prompt_builder.py",
        "priority": "medium",
    },
    {
        "pattern": r"Notional \$[\d\.]+ below minimum \$10",
        "description": "Crypto buy attempted with insufficient USDT",
        "file": "binance_crypto.py",
        "priority": "low",
    },
    {
        "pattern": r"❌ Sell \w+: .*(403|Forbidden)",
        "description": "Alpaca 403 on sell — smart_sell not being called",
        "file": "bot_with_proxy.py",
        "priority": "high",
    },
    {
        "pattern": r"Sleep brief error \(non-fatal\)",
        "description": "Sleep brief parse or format error",
        "file": "bot_with_proxy.py",
        "priority": "low",
    },
]

# ── State tracking ─────────────────────────────────────────────
_error_counts  = defaultdict(int)        # pattern → count this session
_repaired      = set()                   # patterns already repaired this session
_repair_log    = []                      # list of repair attempts for status endpoint


def _log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [REPAIR] {msg}", flush=True)


# ══════════════════════════════════════════════════════════════
# STEP 1: ERROR DETECTION
# ══════════════════════════════════════════════════════════════

def scan_log_line(line: str):
    """
    Call this on every log line. When an error pattern crosses the
    threshold it triggers autonomous repair in a background thread.
    """
    if not GITHUB_TOKEN or not GITHUB_REPO or not ANTHROPIC_KEY:
        return  # Not configured — skip silently

    for ep in ERROR_PATTERNS:
        if re.search(ep["pattern"], line):
            key = ep["pattern"]
            if key in _repaired:
                continue  # Already fixed this session

            _error_counts[key] += 1
            count = _error_counts[key]

            if count == ERROR_THRESHOLD:
                _log(f"⚠️ Error threshold reached ({count}x): {ep['description']}")
                _log(f"   → Triggering autonomous repair for {ep['file']}...")

                import threading
                t = threading.Thread(
                    target=_run_repair,
                    args=(ep,),
                    daemon=True
                )
                t.start()


# ══════════════════════════════════════════════════════════════
# STEP 2: FETCH CONTEXT FROM GITHUB
# ══════════════════════════════════════════════════════════════

def _github_headers():
    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }


def _fetch_file_from_github(filename: str) -> tuple[str, str]:
    """
    Returns (content, sha) for a file in the repo.
    sha is needed to update the file via the API.
    """
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}"
    res = requests.get(url, headers=_github_headers(), timeout=15)
    if not res.ok:
        raise Exception(f"Failed to fetch {filename}: {res.status_code} {res.text[:100]}")
    data    = res.json()
    content = base64.b64decode(data["content"]).decode("utf-8")
    sha     = data["sha"]
    return content, sha


def _fetch_master_doc() -> tuple[str, str]:
    """Fetch NOVATRADE_MASTER.md — returns (content, sha). Creates stub if missing."""
    try:
        return _fetch_file_from_github(MASTER_DOC)
    except Exception:
        stub = (
            "# NovaTrade Master Reference\n\n"
            "## Bug Fix Log\n\n"
            "_No fixes recorded yet._\n"
        )
        return stub, None


# ══════════════════════════════════════════════════════════════
# STEP 3: CLAUDE DIAGNOSIS & FIX
# ══════════════════════════════════════════════════════════════

def _call_claude_for_fix(ep: dict, broken_file_content: str, master_doc: str) -> dict:
    """
    Ask Claude Haiku to:
    1. Identify the exact bug in the file
    2. Write the minimal fix
    3. Write a master doc update entry

    Returns {
      "fixed_code": str,          # full corrected file content
      "master_doc_entry": str,    # markdown entry to append to master doc
      "pr_title": str,
      "pr_body": str,
      "confidence": int,          # 0-100
    }
    """
    prompt = f"""You are the autonomous self-repair system for NovaTrade, an AI trading bot.

A recurring error has been detected:
- Error pattern: {ep['pattern']}
- Description: {ep['description']}
- File to fix: {ep['file']}
- Priority: {ep['priority']}

Here is the current content of {ep['file']}:
```python
{broken_file_content[:12000]}
```

Here is the current NOVATRADE_MASTER.md for context:
```markdown
{master_doc[:3000]}
```

Your job:
1. Find the MINIMAL fix for this bug in the file
2. Return the COMPLETE corrected file content
3. Write a brief master doc entry documenting what you changed
4. Write a PR title and body for the GitHub Pull Request

Rules:
- Make the SMALLEST possible change that fixes the bug
- Do NOT refactor or change anything unrelated
- Do NOT change variable names, function signatures, or logic flow
- If you are not confident (< 60%), set confidence low and explain why
- The fixed_code must be the COMPLETE file, not just the diff

Respond with ONLY valid JSON, no markdown fences:
{{
  "fixed_code": "complete corrected file content here",
  "master_doc_entry": "### Bug Fix — {datetime.now().strftime('%Y-%m-%d')}\\n**Error:** {ep['description']}\\n**Fix:** brief description of what you changed\\n**File:** {ep['file']}\\n**Status:** ✅ PR opened",
  "pr_title": "🤖 Auto-fix: brief title",
  "pr_body": "## What was broken\\n...\\n## What was fixed\\n...\\n## Files changed\\n- {ep['file']}\\n- NOVATRADE_MASTER.md",
  "confidence": 85
}}"""

    res = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key":         ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
            "content-type":      "application/json",
        },
        json={
            "model":      "claude-haiku-4-5-20251001",
            "max_tokens": 8000,
            "messages":   [{"role": "user", "content": prompt}],
        },
        timeout=120,
    )

    if not res.ok:
        raise Exception(f"Claude API error: {res.status_code} {res.text[:200]}")

    raw = res.json()["content"][0]["text"].strip()

    # Strip markdown fences if Claude added them
    raw = re.sub(r"^```json\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    return json.loads(raw)


# ══════════════════════════════════════════════════════════════
# STEP 4: OPEN GITHUB PULL REQUEST
# ══════════════════════════════════════════════════════════════

def _create_pr_branch(branch_name: str) -> bool:
    """Create a new branch from main for the PR."""
    # Get SHA of main branch head
    url = f"https://api.github.com/repos/{GITHUB_REPO}/git/refs/heads/{GITHUB_BRANCH}"
    res = requests.get(url, headers=_github_headers(), timeout=15)
    if not res.ok:
        return False
    sha = res.json()["object"]["sha"]

    # Create new branch
    url = f"https://api.github.com/repos/{GITHUB_REPO}/git/refs"
    res = requests.post(url, headers=_github_headers(), json={
        "ref": f"refs/heads/{branch_name}",
        "sha": sha,
    }, timeout=15)
    return res.ok


def _push_file_to_branch(filename: str, content: str, sha: str, branch: str, commit_msg: str) -> bool:
    """Push a file to a specific branch."""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}"
    body = {
        "message": commit_msg,
        "content": base64.b64encode(content.encode("utf-8")).decode("utf-8"),
        "branch":  branch,
    }
    if sha:
        body["sha"] = sha
    res = requests.put(url, headers=_github_headers(), json=body, timeout=30)
    return res.ok


def _open_pull_request(title: str, body: str, branch: str) -> str:
    """Open a PR from branch → main. Returns PR URL."""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/pulls"
    res = requests.post(url, headers=_github_headers(), json={
        "title": title,
        "body":  body,
        "head":  branch,
        "base":  GITHUB_BRANCH,
    }, timeout=15)
    if res.ok:
        return res.json().get("html_url", "")
    raise Exception(f"PR creation failed: {res.status_code} {res.text[:200]}")


# ══════════════════════════════════════════════════════════════
# STEP 5: MASTER DOC UPDATE
# ══════════════════════════════════════════════════════════════

def _append_to_master_doc(current_content: str, new_entry: str) -> str:
    """Append a new bug fix entry to the master doc."""
    bug_fix_header = "## Bug Fix Log"
    if bug_fix_header in current_content:
        # Insert after the header
        parts = current_content.split(bug_fix_header, 1)
        return f"{parts[0]}{bug_fix_header}\n\n{new_entry}\n{parts[1]}"
    else:
        # Append at end
        return current_content.rstrip() + f"\n\n{bug_fix_header}\n\n{new_entry}\n"


# ══════════════════════════════════════════════════════════════
# MAIN REPAIR FLOW
# ══════════════════════════════════════════════════════════════

def _run_repair(ep: dict):
    """
    Full repair flow — runs in background thread.
    1. Fetch broken file + master doc from GitHub
    2. Ask Claude to diagnose and fix
    3. Create PR branch
    4. Push fixed file + updated master doc
    5. Open PR
    6. Log result
    """
    key       = ep["pattern"]
    filename  = ep["file"]
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    branch    = f"auto-fix/{timestamp}-{filename.replace('.py','').replace('_','-')}"

    try:
        # Step 1: Fetch context
        _log(f"📥 Fetching {filename} from GitHub...")
        broken_content, file_sha = _fetch_file_from_github(filename)

        _log(f"📥 Fetching {MASTER_DOC}...")
        master_content, master_sha = _fetch_master_doc()

        # Step 2: Claude diagnosis
        _log("🧠 Calling Claude to diagnose and write fix...")
        result = _call_claude_for_fix(ep, broken_content, master_content)

        confidence = result.get("confidence", 0)
        _log(f"   Claude confidence: {confidence}%")

        if confidence < 60:
            _log(f"⚠️ Claude confidence too low ({confidence}%) — skipping PR to avoid bad fix")
            _repaired.add(key)  # Don't retry this session
            _repair_log.append({
                "timestamp": timestamp,
                "error":     ep["description"],
                "status":    f"skipped — low confidence ({confidence}%)",
                "pr_url":    None,
            })
            return

        # Step 3: Create PR branch
        _log(f"🌿 Creating branch: {branch}")
        if not _create_pr_branch(branch):
            raise Exception("Failed to create PR branch")

        # Step 4: Push fixed file
        _log(f"📤 Pushing fixed {filename} to branch...")
        commit_msg = f"🤖 Auto-fix: {ep['description'][:80]}"
        if not _push_file_to_branch(filename, result["fixed_code"], file_sha, branch, commit_msg):
            raise Exception(f"Failed to push {filename}")

        # Step 5: Push updated master doc
        _log(f"📤 Pushing updated {MASTER_DOC}...")
        updated_master = _append_to_master_doc(master_content, result["master_doc_entry"])
        _push_file_to_branch(MASTER_DOC, updated_master, master_sha, branch, f"📝 Update master doc: {ep['description'][:60]}")

        # Step 6: Open PR
        _log("🔀 Opening Pull Request...")
        pr_url = _open_pull_request(result["pr_title"], result["pr_body"], branch)

        _log(f"✅ PR opened: {pr_url}")
        _log(f"   Review and merge at: {pr_url}")

        _repaired.add(key)
        _repair_log.append({
            "timestamp":   timestamp,
            "error":       ep["description"],
            "file":        filename,
            "confidence":  confidence,
            "status":      "PR opened — awaiting review",
            "pr_url":      pr_url,
        })

    except Exception as e:
        _log(f"❌ Repair failed for {filename}: {e}")
        _repair_log.append({
            "timestamp": timestamp,
            "error":     ep["description"],
            "status":    f"failed: {e}",
            "pr_url":    None,
        })


# ══════════════════════════════════════════════════════════════
# STATUS — for /repair_status endpoint
# ══════════════════════════════════════════════════════════════

def get_repair_status() -> dict:
    """Return current repair state for the status API endpoint."""
    return {
        "configured":    bool(GITHUB_TOKEN and GITHUB_REPO and ANTHROPIC_KEY),
        "error_counts":  dict(_error_counts),
        "repaired":      list(_repaired),
        "repair_log":    _repair_log[-10:],  # Last 10 repairs
        "threshold":     ERROR_THRESHOLD,
        "watched_files": PATCHABLE_FILES,
    }


def reset_session():
    """Reset error counts — call at start of each trading day."""
    _error_counts.clear()
    _repaired.clear()
    _log("🔄 Self-repair counters reset for new trading day")
