#!/usr/bin/env python3
"""
predeploy_check.py — NovaTrade deploy integrity gate
====================================================

Run this BEFORE every push/deploy. It catches the failure that took the bot
down on 2026-06-04: a silently TRUNCATED bot_with_proxy.py that still compiled
clean but was missing app.run(), the trading loop, and ~28 Flask routes.

A plain "does it compile?" check CANNOT catch truncation, because a file cut at
a clean function boundary is still valid Python. This script checks for the
PRESENCE of the components that must exist for the bot to actually serve and
trade — plus that every endpoint the dashboard calls has a matching route.

Usage:
    python predeploy_check.py            # check current directory
    python predeploy_check.py /path/repo # check a specific repo dir

Exit code 0 = safe to deploy. Non-zero = DO NOT DEPLOY (reasons printed).

Wire it into your flow however you like:
    python predeploy_check.py && git push      # block push on failure
"""

import ast
import os
import re
import sys

# ── Tunable floors ───────────────────────────────────────────────────────────
# bot_with_proxy.py was ~3,800 lines / ~140 KB when complete. These floors are
# deliberately conservative: high enough to catch the 367-line truncation with
# huge margin, low enough not to trip on normal edits. Raise them as the file
# grows. The truncated file was 367 lines — these floors are ~4x that.
MIN_ENTRYPOINT_LINES = 1500
MIN_ENTRYPOINT_BYTES = 60_000

ENTRYPOINT = "bot_with_proxy.py"
DASHBOARD = "dashboard.html"

# Components that MUST be present in the entry point or the bot can't run.
# (pattern, human-readable description)
REQUIRED_IN_ENTRYPOINT = [
    (r"if\s+__name__\s*==\s*['\"]__main__['\"]", "__main__ guard (program entry point)"),
    (r"\bapp\.run\s*\(",                          "app.run() — Flask server bind (without this the URL is dead)"),
    (r"host\s*=\s*['\"]0\.0\.0\.0['\"]",          "app.run host=0.0.0.0 (Railway needs external bind)"),
    (r"def\s+trading_loop\s*\(",                  "trading_loop() definition (the trading engine)"),
    (r"target\s*=\s*trading_loop",                "trading_loop started in a thread (engine actually launches)"),
    (r"os\.environ\.get\(\s*['\"]PORT['\"]",      "PORT env read (Railway-assigned port)"),
]

# A hard floor on route count. The complete file has ~30 @app.route handlers.
MIN_ROUTES = 12


def _fail(reasons):
    print("\n" + "=" * 70)
    print("  ❌ PRE-DEPLOY CHECK FAILED — DO NOT DEPLOY")
    print("=" * 70)
    for r in reasons:
        print(f"  • {r}")
    print("=" * 70)
    print("  Most likely cause: a file was truncated during a web-editor paste.")
    print("  Restore the complete file (git history / local backup / drag-drop)")
    print("  and run this check again.\n")
    sys.exit(1)


def _ok():
    print("\n  ✅ PRE-DEPLOY CHECK PASSED — safe to deploy.\n")
    sys.exit(0)


def main(repo_dir):
    reasons = []
    warnings = []

    ep_path = os.path.join(repo_dir, ENTRYPOINT)

    # ── 1. Entry point exists ────────────────────────────────────────────────
    if not os.path.isfile(ep_path):
        _fail([f"{ENTRYPOINT} not found in {repo_dir}"])

    src = open(ep_path, encoding="utf-8", errors="replace").read()
    n_lines = src.count("\n") + 1
    n_bytes = len(src.encode("utf-8"))

    print(f"  Checking {ENTRYPOINT}: {n_lines} lines, {n_bytes:,} bytes")

    # ── 2. Size floor (the truncation tripwire) ──────────────────────────────
    if n_lines < MIN_ENTRYPOINT_LINES:
        reasons.append(
            f"{ENTRYPOINT} is only {n_lines} lines (floor {MIN_ENTRYPOINT_LINES}). "
            f"Looks TRUNCATED — the complete file is ~3,800 lines."
        )
    if n_bytes < MIN_ENTRYPOINT_BYTES:
        reasons.append(
            f"{ENTRYPOINT} is only {n_bytes:,} bytes (floor {MIN_ENTRYPOINT_BYTES:,}). "
            f"Looks TRUNCATED — the complete file is ~140 KB."
        )

    # ── 3. Required components present ────────────────────────────────────────
    for pattern, desc in REQUIRED_IN_ENTRYPOINT:
        if not re.search(pattern, src):
            reasons.append(f"{ENTRYPOINT} is MISSING: {desc}")

    # ── 4. Route count floor ──────────────────────────────────────────────────
    routes = re.findall(r"@app\.route\(\s*['\"]([^'\"]+)['\"]", src)
    print(f"  Found {len(routes)} Flask routes")
    if len(routes) < MIN_ROUTES:
        reasons.append(
            f"Only {len(routes)} @app.route handlers found (floor {MIN_ROUTES}). "
            f"The complete file has ~30. Likely truncated."
        )

    # ── 5. Every endpoint the dashboard calls must have a route ──────────────
    dash_path = os.path.join(repo_dir, DASHBOARD)
    if os.path.isfile(dash_path):
        dash = open(dash_path, encoding="utf-8", errors="replace").read()
        # The dashboard calls endpoints via a wrapper: apiFetch('/foo') /
        # fetch(getUrl() + '/foo'). So match any quoted string literal that
        # looks like an endpoint path ('/lowercase_name...'), not just fetch(.
        called = set(re.findall(r"""['"`](/[a-z_][a-z0-9_]*)""", dash))
        route_prefixes = {("/" + r.lstrip("/").split("/")[0].split("<")[0]) for r in routes}
        missing = sorted(e for e in called
                         if ("/" + e.lstrip("/").split("/")[0]) not in route_prefixes)
        if missing:
            reasons.append(
                f"dashboard.html calls endpoints with no matching route in "
                f"{ENTRYPOINT}: {', '.join(missing)}"
            )
        else:
            print(f"  All {len(called)} dashboard endpoints have matching routes")

    # ── 6. Every .py file in the repo must parse ─────────────────────────────
    bad_parse = []
    for fn in sorted(os.listdir(repo_dir)):
        if not fn.endswith(".py"):
            continue
        p = os.path.join(repo_dir, fn)
        try:
            ast.parse(open(p, encoding="utf-8", errors="replace").read())
        except SyntaxError as e:
            bad_parse.append(f"{fn}: {e}")
    if bad_parse:
        reasons.extend(f"Syntax error (truncated mid-statement?): {b}" for b in bad_parse)
    else:
        print("  All .py files parse cleanly")

    # ── 7. SOFT: no-timeout network calls in the entry point ─────────────────
    # Not fatal, but flag requests.* calls lacking a timeout (they can hang
    # the bot forever on an upstream blip).
    for m in re.finditer(r"requests\.(get|post|put|delete|request)\(([^)]*)\)", src):
        if "timeout" not in m.group(2):
            line_no = src[:m.start()].count("\n") + 1
            warnings.append(
                f"{ENTRYPOINT}:{line_no} requests.{m.group(1)}() has no timeout= "
                f"(can hang forever on a slow/unreachable API)"
            )

    # ── Verdict ──────────────────────────────────────────────────────────────
    if warnings:
        print("\n  ⚠️  Warnings (not blocking, but worth fixing):")
        for w in warnings:
            print(f"     - {w}")

    if reasons:
        _fail(reasons)
    _ok()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")
