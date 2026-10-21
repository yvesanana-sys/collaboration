#!/usr/bin/env python3
"""
railway_deploy_monitor.py — can we still ship to Railway?
=========================================================

Run by .github/workflows/railway-deploy-monitor.yml. It answers two questions
WITHOUT any Railway credentials, so it keeps working when the Railway connector,
dashboard or API is down:

  1. Is Railway accepting our pushes?  Railway's GitHub app posts a commit
     status (context "desirable-exploration - collaboration") on every commit it
     deploys: "pending" within seconds of the push, then success/failure. So for
     the current main HEAD:
        success              → the push → deploy path works
        failure / error      → Railway built or deployed it and it FAILED
        pending too long     → a deploy is stuck
        no status, too long  → Railway never picked the push up (outage, builds
                               paused, or the GitHub integration disconnected)
     This is the 2026-10-21 signature: three merges, zero statuses, while the
     dashboard/API were down and builds were paused.

  2. Is Railway itself having an incident?  Reads Railway's public status page.
     Best effort: an unreadable page is "unknown", never an alert by itself.

Output: decision/reason/platform lines on $GITHUB_OUTPUT, plus a JSON summary on
stdout. The workflow opens or closes the tracking issue.

Decisions: ok | not_picked_up | stuck | deploy_failed | waiting
"""

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

REPO = os.environ.get("GITHUB_REPOSITORY", "yvesanana-sys/collaboration")
TOKEN = os.environ.get("GITHUB_TOKEN", "")
RAILWAY_CONTEXT = os.environ.get("RAILWAY_STATUS_CONTEXT", "desirable-exploration - collaboration")
PICKUP_GRACE_MIN = int(os.environ.get("PICKUP_GRACE_MIN", "10"))   # no status after this → not picked up
STUCK_MIN = int(os.environ.get("STUCK_MIN", "25"))                 # pending longer than this → stuck

# Railway platform status. status.railway.com 403s non-browser clients, and the
# railway.instatus.com mirror is deprecated and always reports "UP" (a source
# that always says "fine" would wave a revert through an outage). This
# no-auth API is the reachable one (components / incidents / maintenances).
STATUS_URL = "https://api.railwaystatus.com/status"

def http_json(url, headers=None, timeout=15):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def gh(path):
    headers = {"Accept": "application/vnd.github+json"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    return http_json(f"https://api.github.com/repos/{REPO}/{path}", headers)


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _flatten(components, out):
    for c in components or []:
        if not isinstance(c, dict):
            continue
        kids = c.get("components") or c.get("children") or []
        if kids:
            _flatten(kids, out)
        else:
            out.append((str(c.get("name", "?")), str(c.get("status", "")).lower()))
    return out


def _severity(text):
    t = text.lower()
    if any(w in t for w in ("major", "outage", "down", "critical")):
        return "major"
    if any(w in t for w in ("partial", "degraded", "minor", "maintenance", "investigating", "identified")):
        return "minor"
    if t in ("operational", "up", "none", "resolved", "completed", ""):
        return "none"
    return "unknown"


def railway_platform_status():
    """Return (indicator, description, source). indicator: none|minor|major|unknown."""
    try:
        d = http_json(STATUS_URL, {"Accept": "application/json", "User-Agent": "novatrade-deploy-monitor"})
    except Exception as e:
        return "unknown", f"status API unreachable ({type(e).__name__})", STATUS_URL
    if not isinstance(d, dict):
        return "unknown", "status API returned an unexpected shape", STATUS_URL
    rank = {"none": 0, "unknown": 0, "minor": 1, "major": 2}
    worst, notes = "none", []
    for name, st in _flatten(d.get("components"), []):
        sev = _severity(st)
        if rank[sev] > 0:
            notes.append(f"{name}: {st}")
        if rank[sev] > rank[worst]:
            worst = sev
    for inc in d.get("incidents") or []:
        if not isinstance(inc, dict):
            continue
        st = str(inc.get("status", "")).lower()
        if st in ("resolved", "completed", "postmortem"):
            continue
        sev = _severity(str(inc.get("impact", ""))) or "minor"
        sev = sev if rank.get(sev, 0) > 0 else "minor"   # an open incident is at least minor
        notes.append(f"incident: {inc.get('name') or inc.get('title') or '?'} ({st or 'open'})")
        if rank[sev] > rank[worst]:
            worst = sev
    if not d.get("components") and "incidents" not in d:
        return "unknown", f"status API shape not recognised (keys: {sorted(d)[:8]})", STATUS_URL
    return worst, "; ".join(notes[:6]) or "all components operational", STATUS_URL


def decide(statuses, commit_time, now, pickup_grace_min, stuck_min):
    """PURE. statuses = Railway's statuses for HEAD, newest first. Returns (decision, reason)."""
    age_min = (now - commit_time).total_seconds() / 60
    if not statuses:
        if age_min < pickup_grace_min:
            return "waiting", f"HEAD is {age_min:.0f} min old; Railway has not posted yet (grace {pickup_grace_min} min)."
        return "not_picked_up", (
            f"Railway posted NO deploy status on main HEAD {age_min:.0f} min after the commit. "
            f"Pushes are not reaching Railway: platform outage, builds paused, or the GitHub "
            f"integration disconnected. Merged fixes are NOT live.")
    latest = statuses[0]
    state, desc = latest.get("state"), latest.get("description", "")
    if state == "success":
        return "ok", f"Railway deployed main HEAD: {desc}"
    if state in ("failure", "error"):
        return "deploy_failed", f"Railway build/deploy of main HEAD FAILED: {desc}"
    since = (now - parse_ts(latest["created_at"])).total_seconds() / 60
    if since > stuck_min:
        return "stuck", f"Railway deploy of main HEAD pending for {since:.0f} min (> {stuck_min}): {desc}"
    return "waiting", f"Railway is deploying main HEAD ({since:.0f} min): {desc}"


def main():
    sha = os.environ.get("HEAD_SHA") or subprocess.check_output(
        ["git", "rev-parse", "HEAD"], text=True).strip()
    commit = gh(f"commits/{sha}")
    commit_time = parse_ts(commit["commit"]["committer"]["date"])
    all_statuses = gh(f"commits/{sha}/statuses?per_page=100")
    statuses = sorted((s for s in all_statuses if s.get("context") == RAILWAY_CONTEXT),
                      key=lambda s: s["created_at"], reverse=True)
    now = datetime.now(timezone.utc)
    decision, reason = decide(statuses, commit_time, now, PICKUP_GRACE_MIN, STUCK_MIN)
    indicator, pdesc, psrc = railway_platform_status()
    platform = f"{indicator} ({pdesc})" + (f" via {psrc}" if psrc else "")

    out = {"sha": sha[:7], "decision": decision, "reason": reason, "platform": platform}
    # Revert guard for the health watchdog: restoring last-known-good only makes
    # sense if main HEAD actually went through Railway and Railway itself is fine.
    # Otherwise the outage is Railway's, and a revert would just roll back good
    # code (in June one such revert silently deleted the self_repair human gate).
    head_ran = bool(statuses) and statuses[0].get("state") in ("success", "failure", "error")
    platform_bad = indicator in ("major", "critical")
    if platform_bad:
        out["guard"], out["guard_reason"] = "block", f"Railway status page reports {platform}"
    elif not head_ran:
        out["guard"], out["guard_reason"] = "block", f"main HEAD was never deployed by Railway ({decision}): {reason}"
    else:
        out["guard"], out["guard_reason"] = "allow", "HEAD went through Railway and the platform shows no major incident"
    print(json.dumps(out, indent=2))
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            for k in ("decision", "reason", "platform", "sha", "guard", "guard_reason"):
                f.write(f"{k}={str(out[k]).replace(chr(10), ' ')}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
