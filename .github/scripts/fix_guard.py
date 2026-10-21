#!/usr/bin/env python3
"""
fix_guard.py — structural gate for every PR into main
=====================================================

Run by .github/workflows/fix-guard.yml. The automated fix pipeline only merges
a PR whose `fix-guard` check is green, so this is the floor under every
autonomous change. It cannot prove a fix is CORRECT — it proves the fix is not
one of the failures this repo has already shipped:

  • a large file silently truncated (Jun 5 and Oct 20 2026: bot_with_proxy.py
    gutted to a few hundred lines, valid Python, auto-merged, deployed)
  • a file that no longer parses
  • the entry point missing its server / trading loop (predeploy_check.py)
  • an "auto-fix" that is really a rewrite

Usage:  python .github/scripts/fix_guard.py <base-sha> <head-branch-name>
Exit 0 = pass. Non-zero = do not merge (reasons printed).
"""

import ast
import subprocess
import sys

HUMAN_GATE_FILES = {"bot_with_proxy.py", "binance_crypto.py"}

# Components that must survive any edit (kept in step with self_repair.py).
REQUIRED_MARKERS = {
    "bot_with_proxy.py": ["def trading_loop", "app.run("],
    "binance_crypto.py": ["class CryptoTrader", "def binance_get"],
}

# A fix almost never removes much. Gated files get a tight absolute floor;
# other Python files may shrink at most a small fraction.
GATED_MAX_SHRINK_BYTES = 1_000
OTHER_MAX_SHRINK_FRACTION = 0.05

# Diff-size caps for PRs opened by the automated pipeline (branch autofix/*).
# Anything bigger is not a minimal fix and needs a human.
AUTOFIX_MAX_CHANGED_LINES = 120
AUTOFIX_MAX_CHANGED_LINES_GATED = 60
AUTOFIX_MAX_FILES = 4


def git(*args):
    return subprocess.check_output(["git", *args], text=True)


def blob_size(rev, path):
    try:
        return int(subprocess.check_output(["git", "cat-file", "-s", f"{rev}:{path}"], text=True, stderr=subprocess.DEVNULL).strip())
    except subprocess.CalledProcessError:
        return None  # file did not exist at rev


def main():
    base, head_ref = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "")
    autofix = head_ref.startswith("autofix/")
    fails, notes = [], []

    numstat = git("diff", "--numstat", f"{base}...HEAD").strip().splitlines()
    changed = []
    for row in numstat:
        added, deleted, path = row.split("\t", 2)
        if added == "-":  # binary
            changed.append((path, 0, 0))
            continue
        changed.append((path, int(added), int(deleted)))

    if not changed:
        print("No changes vs base — nothing to check.")
        return 0

    total_lines = 0
    for path, added, deleted in changed:
        total_lines += added + deleted
        gated = path in HUMAN_GATE_FILES
        before = blob_size(base, path)
        try:
            after_text = open(path, encoding="utf-8").read()
        except FileNotFoundError:
            if path.endswith(".py"):
                fails.append(f"{path}: deleted — automated fixes never delete Python files")
            continue
        after = len(after_text.encode("utf-8"))

        if path.endswith(".py"):
            try:
                ast.parse(after_text, filename=path)
            except SyntaxError as e:
                fails.append(f"{path}: does not parse — {e}")

        if before is not None and path.endswith(".py"):
            shrink = before - after
            if gated and shrink > GATED_MAX_SHRINK_BYTES:
                fails.append(f"{path}: shrank {before} → {after} bytes (−{shrink}); "
                             f"gated files may shrink at most {GATED_MAX_SHRINK_BYTES} — probable truncation")
            elif not gated and before > 0 and shrink / before > OTHER_MAX_SHRINK_FRACTION:
                fails.append(f"{path}: shrank {before} → {after} bytes ({shrink / before:.0%}) — probable truncation")
            notes.append(f"{path}: {before} → {after} bytes, +{added}/−{deleted} lines{' [GATED]' if gated else ''}")

        for marker in REQUIRED_MARKERS.get(path, []):
            if marker not in after_text:
                fails.append(f"{path}: required component missing: {marker!r}")

        if autofix and gated and added + deleted > AUTOFIX_MAX_CHANGED_LINES_GATED:
            fails.append(f"{path}: {added + deleted} changed lines exceeds the gated-file "
                         f"autofix cap of {AUTOFIX_MAX_CHANGED_LINES_GATED}")

    if autofix:
        if total_lines > AUTOFIX_MAX_CHANGED_LINES:
            fails.append(f"autofix PR changes {total_lines} lines (cap {AUTOFIX_MAX_CHANGED_LINES}) — not a minimal fix")
        if len(changed) > AUTOFIX_MAX_FILES:
            fails.append(f"autofix PR touches {len(changed)} files (cap {AUTOFIX_MAX_FILES})")

    # Whole-repo floor: the entry point must still be able to serve and trade.
    r = subprocess.run([sys.executable, "predeploy_check.py", "."], capture_output=True, text=True)
    if r.returncode != 0:
        fails.append("predeploy_check.py failed:\n" + r.stdout[-2000:])

    print("Changed files:")
    for n in notes:
        print("  " + n)
    if fails:
        print("\n❌ FIX GUARD FAILED — do not merge:")
        for f in fails:
            print("  • " + f)
        return 1
    print(f"\n✅ FIX GUARD PASSED ({len(changed)} files, {total_lines} changed lines, autofix={autofix})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
