"""
claude_code_trigger.py — Autonomous Repair Trigger System
==========================================================
Wakes Claude Code SSH on Railway when the bot detects errors it can't self-fix.
Writes repair jobs to /data/repair_queue.json (shared volume).
Logs all repair activity to /data/repair_log.json (permanent history).
Claude Code SSH reads the queue on boot, fixes, verifies, suspends itself.
"""

import os
import json
import time
import uuid
import requests
import threading
from datetime import datetime, timezone
from collections import deque

# ── Config ────────────────────────────────────────────────────────────────────
RAILWAY_TOKEN        = os.environ.get("RAILWAY_TOKEN", "")
RAILWAY_PROJECT_ID   = os.environ.get("RAILWAY_PROJECT_ID", "")
CLAUDE_CODE_SERVICE  = os.environ.get("CLAUDE_CODE_SERVICE_ID", "")  # Service ID of Claude Code SSH
BOT_SERVICE_ID       = os.environ.get("RAILWAY_SERVICE_ID", "")      # This bot's service ID
BOT_URL              = os.environ.get("BOT_URL", "")                  # e.g. collaboration-production-cba3.up.railway.app

REPAIR_QUEUE_FILE    = "/data/repair_queue.json"
REPAIR_LOG_FILE      = "/data/repair_log.json"
REPAIR_STATE_FILE    = "/data/repair_state.json"

# Rolling buffer of recent log lines — filled by scan_log_line()
_LOG_BUFFER          = deque(maxlen=150)
_trigger_lock        = threading.Lock()
_active_repair_id    = None   # Prevent duplicate triggers for same error


# ── Railway API ───────────────────────────────────────────────────────────────

def _railway_headers():
    return {
        "Authorization": f"Bearer {RAILWAY_TOKEN}",
        "Content-Type":  "application/json",
    }


def _railway_graphql(query: str, variables: dict = None) -> dict:
    """Execute a Railway GraphQL API call."""
    resp = requests.post(
        "https://backboard.railway.app/graphql/v2",
        headers=_railway_headers(),
        json={"query": query, "variables": variables or {}},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def wake_claude_code_service() -> bool:
    """
    Wake (resume) the Claude Code SSH Railway service.
    Returns True if successful.
    """
    if not all([RAILWAY_TOKEN, CLAUDE_CODE_SERVICE]):
        _log_trigger("⚠️  Claude Code trigger: RAILWAY_TOKEN or CLAUDE_CODE_SERVICE_ID not set — skipping wake")
        return False

    try:
        mutation = """
        mutation serviceInstanceRedeploy($serviceId: String!, $environmentId: String) {
            serviceInstanceRedeploy(serviceId: $serviceId, environmentId: $environmentId)
        }
        """
        # First try to resume if suspended
        resume_mutation = """
        mutation serviceInstanceResume($serviceId: String!) {
            serviceInstanceResume(serviceId: $serviceId)
        }
        """
        result = _railway_graphql(resume_mutation, {"serviceId": CLAUDE_CODE_SERVICE})
        _log_trigger(f"🔧 Claude Code SSH service woken via Railway API")
        return True
    except Exception as e:
        _log_trigger(f"⚠️  Failed to wake Claude Code service: {e}")
        return False


def get_railway_logs(lines: int = 100) -> list:
    """
    Fetch recent Railway logs for this bot service.
    Returns list of log line strings.
    """
    if not all([RAILWAY_TOKEN, BOT_SERVICE_ID]):
        return list(_LOG_BUFFER)  # Fall back to in-memory buffer

    try:
        query = """
        query deploymentLogs($deploymentId: String!, $limit: Int) {
            deploymentLogs(deploymentId: $deploymentId, limit: $limit) {
                timestamp
                message
                severity
            }
        }
        """
        # Get latest deployment ID first
        dep_query = """
        query deployments($serviceId: String!) {
            deployments(serviceId: $serviceId) {
                edges { node { id status createdAt } }
            }
        }
        """
        dep_result = _railway_graphql(dep_query, {"serviceId": BOT_SERVICE_ID})
        deployments = dep_result.get("data", {}).get("deployments", {}).get("edges", [])
        if not deployments:
            return list(_LOG_BUFFER)

        latest_dep_id = deployments[0]["node"]["id"]
        log_result = _railway_graphql(query, {"deploymentId": latest_dep_id, "limit": lines})
        logs = log_result.get("data", {}).get("deploymentLogs", [])
        return [f"[{l['timestamp']}] {l['message']}" for l in logs]

    except Exception:
        return list(_LOG_BUFFER)  # Fall back to buffer


# ── Log Buffer ────────────────────────────────────────────────────────────────

def buffer_log_line(line: str):
    """Called by bot's log() function to keep rolling buffer of recent logs."""
    _LOG_BUFFER.append(f"[{datetime.now(timezone.utc).isoformat()}] {line}")


# ── Repair Queue ──────────────────────────────────────────────────────────────

def _write_repair_queue(job: dict):
    """Write repair job to shared volume — Claude Code reads this on boot."""
    for path in [REPAIR_QUEUE_FILE, "./repair_queue.json"]:
        try:
            with open(path, "w") as f:
                json.dump(job, f, indent=2, default=str)
            return True
        except Exception:
            continue
    return False


def _clear_repair_queue():
    """Clear the queue after job is consumed."""
    for path in [REPAIR_QUEUE_FILE, "./repair_queue.json"]:
        try:
            with open(path, "w") as f:
                json.dump({"status": "idle", "cleared_at": datetime.now(timezone.utc).isoformat()}, f)
            return
        except Exception:
            continue


# ── Repair Log ────────────────────────────────────────────────────────────────

def _load_repair_log() -> list:
    """Load full repair history from volume."""
    for path in [REPAIR_LOG_FILE, "./repair_log.json"]:
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            continue
    return []


def _save_repair_log(log: list):
    """Save repair history to volume. Keep last 200 entries."""
    if len(log) > 200:
        log = log[-200:]
    for path in [REPAIR_LOG_FILE, "./repair_log.json"]:
        try:
            with open(path, "w") as f:
                json.dump(log, f, indent=2, default=str)
            return True
        except Exception:
            continue
    return False


def start_repair_log_entry(repair_id: str, trigger: str, severity: str,
                            affected_file: str, log_snapshot: list) -> dict:
    """Create a new repair log entry and save it."""
    entry = {
        "id":            repair_id,
        "timestamp":     datetime.now(timezone.utc).isoformat(),
        "trigger":       trigger,
        "severity":      severity,
        "affected_file": affected_file,
        "log_snapshot":  log_snapshot[-50:],  # Last 50 lines
        "attempts":      [],
        "final_result":  "IN_PROGRESS",
        "resolved_at":   None,
        "total_duration": None,
    }
    log = _load_repair_log()
    log.append(entry)
    _save_repair_log(log)
    _log_trigger(f"📋 Repair log entry created: {repair_id}")
    return entry


def update_repair_log_attempt(repair_id: str, attempt: dict):
    """Add an attempt record to an existing repair log entry."""
    log = _load_repair_log()
    for entry in reversed(log):
        if entry.get("id") == repair_id:
            entry["attempts"].append(attempt)
            _save_repair_log(log)
            return
    _log_trigger(f"⚠️  Repair log entry {repair_id} not found for update")


def close_repair_log_entry(repair_id: str, final_result: str, started_at: str):
    """Mark a repair log entry as complete."""
    log = _load_repair_log()
    started = datetime.fromisoformat(started_at.replace("Z", "+00:00")) if started_at else None
    for entry in reversed(log):
        if entry.get("id") == repair_id:
            entry["final_result"]   = final_result
            entry["resolved_at"]    = datetime.now(timezone.utc).isoformat()
            if started:
                entry["total_duration"] = int(
                    (datetime.now(timezone.utc) - started).total_seconds()
                )
            _save_repair_log(log)
            icon = "✅" if final_result == "FIXED" else "🔄" if final_result == "REVERTED" else "🆘"
            _log_trigger(f"{icon} Repair {repair_id[:8]} closed: {final_result}")
            return


def get_repair_log_summary() -> dict:
    """Return summary stats of repair history for /repair_status endpoint."""
    log = _load_repair_log()
    if not log:
        return {"total": 0, "message": "No repairs yet"}

    total    = len(log)
    fixed    = sum(1 for e in log if e.get("final_result") == "FIXED")
    reverted = sum(1 for e in log if e.get("final_result") == "REVERTED")
    manual   = sum(1 for e in log if e.get("final_result") == "MANUAL_NEEDED")
    in_prog  = sum(1 for e in log if e.get("final_result") == "IN_PROGRESS")

    recent = []
    for e in reversed(log[-10:]):
        recent.append({
            "id":       e.get("id", "")[:8],
            "when":     e.get("timestamp", "")[:16],
            "trigger":  e.get("trigger", "")[:60],
            "severity": e.get("severity", ""),
            "result":   e.get("final_result", ""),
            "duration": e.get("total_duration"),
        })

    return {
        "total":        total,
        "fixed":        fixed,
        "reverted":     reverted,
        "manual_needed": manual,
        "in_progress":  in_prog,
        "fix_rate_pct": round(fixed / total * 100, 1) if total else 0,
        "recent":       recent,
        "log_file":     REPAIR_LOG_FILE,
    }


# ── Repair State ──────────────────────────────────────────────────────────────

def _write_repair_state(state: str, repair_id: str = None, message: str = ""):
    """Track current repair state: idle / active / failed."""
    for path in [REPAIR_STATE_FILE, "./repair_state.json"]:
        try:
            with open(path, "w") as f:
                json.dump({
                    "state":      state,
                    "repair_id":  repair_id,
                    "message":    message,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }, f, indent=2)
            return
        except Exception:
            continue


def _read_repair_state() -> dict:
    """Read current repair state."""
    for path in [REPAIR_STATE_FILE, "./repair_state.json"]:
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            continue
    return {"state": "idle"}


# ── Main Trigger ──────────────────────────────────────────────────────────────

def trigger_claude_code_repair(error_msg: str, severity: str,
                                 affected_file: str = "unknown",
                                 log_fn=None):
    """
    Main entry point called by self_repair.py when escalation is needed.
    Writes repair job + wakes Claude Code SSH service.
    Thread-safe — won't trigger twice for same active repair.
    """
    global _active_repair_id

    with _trigger_lock:
        # Don't trigger if already repairing
        state = _read_repair_state()
        if state.get("state") == "active":
            _log_trigger(f"⚠️  Repair already in progress ({state.get('repair_id', '')[:8]}) — skipping duplicate trigger")
            return None

        repair_id   = str(uuid.uuid4())
        log_snapshot = get_railway_logs(100)
        _active_repair_id = repair_id

        _log_trigger(f"🔧 ESCALATING TO CLAUDE CODE — {severity}: {error_msg[:80]}")
        _log_trigger(f"   Repair ID: {repair_id[:8]}")
        _log_trigger(f"   Affected:  {affected_file}")

        # 1. Create repair log entry
        start_repair_log_entry(
            repair_id    = repair_id,
            trigger      = error_msg,
            severity     = severity,
            affected_file = affected_file,
            log_snapshot = log_snapshot,
        )

        # 2. Write repair queue job for Claude Code to read on boot
        job = {
            "repair_id":       repair_id,
            "created_at":      datetime.now(timezone.utc).isoformat(),
            "severity":        severity,
            "error_message":   error_msg,
            "affected_file":   affected_file,
            "log_snapshot":    log_snapshot[-100:],
            "repo":            "yvesanana-sys/collaboration",
            "bot_url":         BOT_URL or "collaboration-production-cba3.up.railway.app",
            "bot_service_id":  BOT_SERVICE_ID,
            "max_attempts":    3,
            "status":          "pending",
            "instructions": (
                "Read CLAUDE.md first for full project context. "
                "Then read the error_message and log_snapshot carefully. "
                "Fix the root cause across ALL affected files. "
                "Run python3 -m py_compile on every .py file before pushing. "
                "After pushing, watch Railway logs for 120 seconds to verify fix. "
                "If bot crashes after your fix: GIT REVERT immediately, then try again. "
                "Max 3 attempts. After 3 failures: revert to last good commit and set status=manual_needed. "
                "Write results to /data/repair_queue.json status field after each attempt. "
                "When done (fixed or manual_needed): suspend the Claude Code SSH service via Railway API."
            ),
        }

        success = _write_repair_queue(job)
        if not success:
            _log_trigger("❌ Failed to write repair queue — volume not mounted?")
            return None

        # 3. Update state
        _write_repair_state("active", repair_id, f"Escalated: {error_msg[:60]}")

        # 4. Wake Claude Code SSH service
        woken = wake_claude_code_service()
        if woken:
            _log_trigger(f"✅ Claude Code SSH service waking — repair job queued")
        else:
            _log_trigger(f"⚠️  Could not wake Claude Code SSH — repair job in queue when service next boots")

        return repair_id


def mark_repair_complete(repair_id: str, result: str, started_at: str):
    """Called when Claude Code reports back via repair_queue.json."""
    global _active_repair_id
    close_repair_log_entry(repair_id, result, started_at)
    _write_repair_state("idle", message=f"Last repair: {result}")

    # Update NOVATRADE_MASTER.md with repair summary
    try:
        log      = _load_repair_log()
        entry    = next((e for e in reversed(log) if e.get("id") == repair_id), {})
        trigger  = entry.get("trigger", "unknown")
        severity = entry.get("severity", "ERROR")
        affected = entry.get("affected_file", "unknown")
        attempts = entry.get("attempts", [])
        duration = entry.get("total_duration", 0) or 0
        threading.Thread(
            target=_update_master_doc_with_repair,
            args=(repair_id, trigger, severity, affected, attempts, result, duration),
            daemon=True,
        ).start()
    except Exception as e:
        _log_trigger(f"⚠️ Master doc update failed: {e}")

    _clear_repair_queue()
    _active_repair_id = None


# ── Scheduled Maintenance ─────────────────────────────────────────────────────

def trigger_scheduled_maintenance(maintenance_type: str = "weekly"):
    """
    Called by bot on Sunday 3am ET for scheduled maintenance.
    Wakes Claude Code SSH with a maintenance job instead of repair job.
    """
    job = {
        "repair_id":   f"maintenance-{datetime.now(timezone.utc).strftime('%Y%m%d')}",
        "created_at":  datetime.now(timezone.utc).isoformat(),
        "type":        "maintenance",
        "maintenance_type": maintenance_type,
        "status":      "pending",
        "instructions": (
            "This is a scheduled maintenance run, not an error fix. "
            "Read CLAUDE.md for full project context. Then audit ALL 18 files for: "
            "1. Missing shared_state keys (any key read but not in defaults dict) "
            "2. datetime arithmetic on shared_state values without isinstance guards "
            "3. Module-level executable code in extracted modules "
            "4. Missing env var declarations in bot_with_proxy.py "
            "5. Exception handlers that silently swallow errors (bare except: pass) "
            "6. Hardcoded values that should use RULES dict "
            "7. Any function calling another module's private function directly "
            "Apply all fixes found, test with py_compile, push to GitHub. "
            "Write maintenance report to /data/repair_log.json. "
            "When complete: suspend Claude Code SSH service."
        ),
    }

    _write_repair_queue(job)
    _write_repair_state("maintenance", job["repair_id"], f"{maintenance_type} maintenance")

    start_repair_log_entry(
        repair_id    = job["repair_id"],
        trigger      = f"Scheduled {maintenance_type} maintenance",
        severity     = "MAINTENANCE",
        affected_file = "all",
        log_snapshot  = [],
    )

    woken = wake_claude_code_service()
    _log_trigger(f"🔧 Scheduled {maintenance_type} maintenance triggered — Claude Code SSH waking: {woken}")
    return job["repair_id"]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _log_trigger(msg: str):
    """Internal logging — uses print so it shows in Railway logs."""
    print(f"[TRIGGER] {msg}", flush=True)




# ── Master Doc Logger ─────────────────────────────────────────────────────────

MASTER_DOC = "NOVATRADE_MASTER.md"
GITHUB_REPO = os.environ.get("GITHUB_REPO", "yvesanana-sys/collaboration")
GITHUB_TOKEN_VAR = os.environ.get("GITHUB_TOKEN", "")


def _update_master_doc_with_repair(repair_id: str, trigger_msg: str,
                                    severity: str, affected_file: str,
                                    attempts: list, final_result: str,
                                    duration_secs: int = 0):
    """
    Append a repair entry to NOVATRADE_MASTER.md on GitHub.
    Called after every completed repair (fixed / reverted / manual_needed).
    """
    if not GITHUB_TOKEN_VAR or not GITHUB_REPO:
        _log_trigger("⚠️ Cannot update master doc — GITHUB_TOKEN or GITHUB_REPO not set")
        return False

    try:
        headers = {
            "Authorization": f"Bearer {GITHUB_TOKEN_VAR}",
            "Content-Type": "application/json",
        }

        # Fetch current master doc
        resp = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/contents/{MASTER_DOC}",
            headers=headers, timeout=15,
        )
        resp.raise_for_status()
        data        = resp.json()
        current_sha = data["sha"]
        import base64 as _b64
        current_content = _b64.b64decode(data["content"]).decode("utf-8")

        # Build new entry
        now       = datetime.now(timezone.utc)
        icon      = "✅" if final_result == "FIXED" else "🔄" if final_result == "REVERTED" else "🆘"
        mins      = f"{duration_secs // 60}m {duration_secs % 60}s" if duration_secs else "—"
        attempt_summary = ""
        for i, att in enumerate(attempts, 1):
            result  = att.get("deploy_result", "?")
            action  = att.get("action_taken", "?")
            summary = att.get("fix_summary", "")[:80]
            attempt_summary += f"  - Attempt {i}: {result} -> {action} | {summary}\n"

        entry = f"""### {icon} Claude Code Repair — {now.strftime('%Y-%m-%d %H:%M')} UTC
**Repair ID:** `{repair_id[:8]}`  
**Severity:** {severity}  
**Trigger:** {trigger_msg[:120]}  
**File:** `{affected_file}`  
**Result:** {final_result}  
**Duration:** {mins}  
**Attempts:**
{attempt_summary if attempt_summary else '  - (no attempt details recorded)'}
---
"""

        # Append to Bug Fix Log section
        bug_fix_header = "## Bug Fix Log"
        if bug_fix_header in current_content:
            parts = current_content.split(bug_fix_header, 1)
            updated = f"{parts[0]}{bug_fix_header}\n\n{entry}{parts[1]}"
        else:
            updated = current_content.rstrip() + f"\n\n{bug_fix_header}\n\n{entry}"

        # Push updated master doc
        import base64 as _b64
        encoded = _b64.b64encode(updated.encode("utf-8")).decode("utf-8")
        push_resp = requests.put(
            f"https://api.github.com/repos/{GITHUB_REPO}/contents/{MASTER_DOC}",
            headers=headers,
            json={
                "message": f"📝 Repair log: {final_result} — {trigger_msg[:60]}",
                "content": encoded,
                "sha":     current_sha,
            },
            timeout=15,
        )
        push_resp.raise_for_status()
        _log_trigger(f"📝 NOVATRADE_MASTER.md updated with repair entry: {repair_id[:8]}")
        return True

    except Exception as e:
        _log_trigger(f"⚠️ Failed to update master doc: {e}")
        return False


def check_pending_repair_result():
    """
    Called periodically by bot to check if Claude Code finished a repair.
    If repair_queue.json shows status=complete/failed, update repair log.
    """
    try:
        for path in [REPAIR_QUEUE_FILE, "./repair_queue.json"]:
            try:
                with open(path) as f:
                    queue = json.load(f)
            except Exception:
                continue

            status    = queue.get("status", "")
            repair_id = queue.get("repair_id", "")
            started   = queue.get("created_at", "")

            if status == "complete" and repair_id:
                _log_trigger(f"✅ Claude Code repair complete: {repair_id[:8]}")
                mark_repair_complete(repair_id, "FIXED", started)

            elif status == "manual_needed" and repair_id:
                _log_trigger(f"🆘 Claude Code repair exhausted — needs manual fix: {repair_id[:8]}")
                mark_repair_complete(repair_id, "MANUAL_NEEDED", started)

            elif status == "reverted" and repair_id:
                _log_trigger(f"🔄 Claude Code reverted to last good commit: {repair_id[:8]}")
                mark_repair_complete(repair_id, "REVERTED", started)

            break
    except Exception:
        pass
