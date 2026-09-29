"""
prompt_builder.py
═══════════════════════════════════════════════════════════════════════════════
Adaptive Prompt Builder + Evolving Memory System
Drop in same folder as bot_with_proxy.py and projection_engine.py

PURPOSE:
  Replaces static hardcoded prompts with situation-aware prompts that:
  1. Classify the current market/account situation (7 modes)
  2. Weight and reorder prompt sections based on what matters NOW
  3. Inject learned lessons from your bot's own trade history
  4. Use projection engine data in specific, actionable language
  5. Evolve over time — the more trades, the smarter the prompts

ZERO BREAKING CHANGES:
  All functions return plain strings — just swap them into existing
  r1_prompt, research_prompt, and brief prompts in bot_with_proxy.py.
  Falls back gracefully if any data is missing.

INTEGRATION (4 lines in bot_with_proxy.py):
  from prompt_builder import PromptBuilder
  prompt_builder = PromptBuilder()                    # module-level init
  # In collaborative_session:  replace r1_prompt build → prompt_builder.build_r1(...)
  # In run_premarket:           replace research_prompt → prompt_builder.build_premarket(...)
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


# ═════════════════════════════════════════════════════
# PROMPT MEMORY — persists learned lessons across sessions
# Kept in memory (resets on restart) but grows during a session.
# Format designed for easy serialization if you want to add
# persistence to a file or DB later.
# ═════════════════════════════════════════════════════

class PromptMemory:
    """
    Accumulates trade outcomes and extracts lessons.
    Injected into every AI prompt as 'LEARNED CONTEXT'.
    Starts empty, grows with every closed trade.
    """

    MAX_LESSONS = 50   # Rolling window — older lessons drop off
    MAX_INJECT  = 4    # Max lessons injected per prompt (keep prompts tight)
    MEMORY_FILE = "/data/ai_memory.json"   # Railway volume — persists across redeploys

    def __init__(self):
        self.lessons        = []   # All learned lessons
        self.symbol_memory  = {}   # Per-symbol win/loss patterns
        self.ai_patterns    = {    # Which AI wins on which setup type
            "claude": {"wins": 0, "losses": 0, "best_setup": ""},
            "grok":   {"wins": 0, "losses": 0, "best_setup": ""},
        }
        self.market_regime_stats = {
            "bull":    {"trades": 0, "wins": 0},
            "bear":    {"trades": 0, "wins": 0},
            "neutral": {"trades": 0, "wins": 0},
        }
        self.situation_stats = {}   # Win rates per situation mode
        self.total_closed    = 0
        self.total_wins      = 0
        self.last_save_iso   = None   # ISO timestamp of last successful save
        self.created_iso     = datetime.now().isoformat()
        self.backfilled      = False  # True once Binance history has been scanned

    def record_outcome(self, symbol, action, pnl_usd, pnl_pct,
                       owner, strategy, signals, spy_trend,
                       situation_mode, entry_reason=""):
        """
        Called after every trade closes (TP, stop, trail, time).
        Extracts a lesson and updates all stats.
        """
        won = pnl_usd > 0
        self.total_closed += 1
        if won:
            self.total_wins += 1

        # ── Per-symbol memory ────────────────
        if symbol not in self.symbol_memory:
            self.symbol_memory[symbol] = {
                "trades": 0, "wins": 0, "avg_pnl": 0.0,
                "best_setup": "", "worst_setup": "",
            }
        sm = self.symbol_memory[symbol]
        sm["trades"] += 1
        if won:
            sm["wins"] += 1
            if not sm["best_setup"]:
                sm["best_setup"] = entry_reason[:60]
        else:
            if not sm["worst_setup"]:
                sm["worst_setup"] = entry_reason[:60]
        sm["avg_pnl"] = round(
            (sm["avg_pnl"] * (sm["trades"] - 1) + (pnl_pct or 0)) / sm["trades"], 2
        )

        # ── AI pattern tracking ──────────────
        if owner in self.ai_patterns:
            if won:
                self.ai_patterns[owner]["wins"] += 1
                if not self.ai_patterns[owner]["best_setup"]:
                    self.ai_patterns[owner]["best_setup"] = entry_reason[:60]
            else:
                self.ai_patterns[owner]["losses"] += 1

        # ── Market regime stats ──────────────
        regime = spy_trend or "neutral"
        if regime in self.market_regime_stats:
            self.market_regime_stats[regime]["trades"] += 1
            if won:
                self.market_regime_stats[regime]["wins"] += 1

        # ── Situation mode stats ─────────────
        if situation_mode:
            if situation_mode not in self.situation_stats:
                self.situation_stats[situation_mode] = {"trades": 0, "wins": 0}
            self.situation_stats[situation_mode]["trades"] += 1
            if won:
                self.situation_stats[situation_mode]["wins"] += 1

        # ── Extract lesson ─────────────────
        outcome_str = f"+{pnl_pct:.1f}% WIN" if won else f"{pnl_pct:.1f}% LOSS"
        signals_str = ", ".join(signals[:3]) if signals else "no signals"
        lesson = {
            "symbol":    symbol,
            "outcome":   "win" if won else "loss",
            "pnl_pct":   round(pnl_pct or 0, 2),
            "pnl_usd":   round(pnl_usd or 0, 2),
            "owner":     owner,
            "strategy":  strategy,
            "spy_trend": regime,
            "situation": situation_mode,
            "signals":   signals[:3] if signals else [],
            "reason":    entry_reason[:80],
            "summary":   f"{symbol} {outcome_str} via {strategy} ({signals_str}) in {regime} market",
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        self.lessons.append(lesson)
        # Persist to volume after every new lesson
        try: self.save()
        except Exception: pass
        # Rolling window
        if len(self.lessons) > self.MAX_LESSONS:
            self.lessons.pop(0)

    def save(self):
        """Persist AI memory to Railway volume — survives redeploys."""
        try:
            import json
            self.last_save_iso = datetime.now().isoformat()
            data = {
                "lessons":            self.lessons[-50:],
                "total_closed":       self.total_closed,
                "total_wins":         self.total_wins,
                "symbol_memory":      {k: v for k, v in list(self.symbol_memory.items())[-100:]},
                "ai_patterns":        self.ai_patterns,
                "market_regime_stats":self.market_regime_stats,
                "situation_stats":    self.situation_stats,
                "last_save_iso":      self.last_save_iso,
                "created_iso":        self.created_iso,
                "backfilled":         self.backfilled,
            }
            for path in [self.MEMORY_FILE, "./ai_memory.json"]:
                try:
                    with open(path, "w") as f:
                        json.dump(data, f, default=str)
                    return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    def load(self):
        """Load AI memory from volume on boot — restores learned lessons."""
        import json
        for path in [self.MEMORY_FILE, "./ai_memory.json"]:
            try:
                with open(path) as f:
                    data = json.load(f)
                self.lessons             = data.get("lessons", [])
                self.total_closed        = data.get("total_closed", 0)
                self.total_wins          = data.get("total_wins", 0)
                self.symbol_memory       = data.get("symbol_memory", {})
                self.ai_patterns         = data.get("ai_patterns", self.ai_patterns)
                self.market_regime_stats = data.get("market_regime_stats", self.market_regime_stats)
                self.situation_stats     = data.get("situation_stats", self.situation_stats)
                self.last_save_iso       = data.get("last_save_iso")
                self.created_iso         = data.get("created_iso", self.created_iso)
                self.backfilled          = data.get("backfilled", False)
                return True
            except Exception:
                continue
        return False

    def backfill_from_binance_history(self, binance_trades, force=False):
        """
        One-shot: convert pre-existing Binance fills into synthetic lessons
        so the AIs aren't starting from zero on first boot. Pairs buy→sell
        per symbol in chronological order to compute realized P&L.

        Args:
            binance_trades: list of dicts from /data/binance_trade_history.json
                            (each has symbol, side, qty, price, time_ms, notional)
            force: re-run even if already backfilled (use when adding new history)

        Returns: number of synthetic lessons created.
        """
        if self.backfilled and not force:
            return 0
        if not binance_trades:
            return 0

        # Group fills by symbol, sort chronologically
        from collections import defaultdict
        by_symbol = defaultdict(list)
        for t in binance_trades:
            sym = (t.get("symbol") or "").upper()
            if sym.endswith(("USDT", "USDC", "BUSD")):
                by_symbol[sym].append(t)
        for sym in by_symbol:
            by_symbol[sym].sort(key=lambda x: x.get("time_ms", 0))

        # FIFO match: each buy is paired with the next sell of the same symbol
        synthetic_lessons = 0
        for sym, fills in by_symbol.items():
            buy_queue = []   # (qty_remaining, price)
            for f in fills:
                side  = f.get("side", "")
                qty   = float(f.get("qty", 0))
                price = float(f.get("price", 0))
                if qty <= 0 or price <= 0:
                    continue
                if side == "buy":
                    buy_queue.append([qty, price, f.get("time_ms", 0)])
                elif side == "sell" and buy_queue:
                    sell_qty_remaining = qty
                    weighted_entry = 0.0
                    matched_qty = 0.0
                    while sell_qty_remaining > 0 and buy_queue:
                        b_qty, b_price, _ = buy_queue[0]
                        take = min(sell_qty_remaining, b_qty)
                        weighted_entry      += b_price * take
                        matched_qty         += take
                        sell_qty_remaining  -= take
                        buy_queue[0][0]     -= take
                        if buy_queue[0][0] <= 0.000001:
                            buy_queue.pop(0)
                    if matched_qty <= 0:
                        continue
                    avg_entry = weighted_entry / matched_qty
                    pnl_pct   = ((price - avg_entry) / avg_entry) * 100 if avg_entry else 0
                    pnl_usd   = (price - avg_entry) * matched_qty
                    if abs(pnl_usd) < 0.01:    # Skip ~zero-P&L noise (dust)
                        continue
                    won = pnl_usd > 0
                    self.total_closed += 1
                    if won:
                        self.total_wins += 1
                    # Per-symbol stats (mirrors record_outcome logic)
                    if sym not in self.symbol_memory:
                        self.symbol_memory[sym] = {
                            "trades": 0, "wins": 0, "avg_pnl": 0.0,
                            "best_setup": "", "worst_setup": "",
                        }
                    sm = self.symbol_memory[sym]
                    sm["trades"] += 1
                    if won:
                        sm["wins"] += 1
                    sm["avg_pnl"] = round(
                        (sm["avg_pnl"] * (sm["trades"] - 1) + pnl_pct) / sm["trades"], 2
                    )
                    # Add as a lesson — owner is "historical" since these
                    # predate the AI competition system
                    self.lessons.append({
                        "symbol":    sym,
                        "outcome":   "win" if won else "loss",
                        "pnl_pct":   round(pnl_pct, 2),
                        "owner":     "historical",
                        "strategy":  "crypto",
                        "situation": "backfill",
                        "spy_trend": "neutral",
                        "signals":   "binance_history_backfill",
                        "reason":    f"backfill from Binance fill history",
                        "summary":   f"{sym} {'win' if won else 'loss'} historical "
                                     f"({pnl_pct:+.1f}%) — pre-AI baseline",
                        "timestamp": datetime.fromtimestamp(
                                        f.get("time_ms", 0) / 1000
                                    ).strftime("%Y-%m-%d %H:%M") if f.get("time_ms") else "historical",
                    })
                    synthetic_lessons += 1

        # Trim lesson window
        if len(self.lessons) > self.MAX_LESSONS:
            self.lessons = self.lessons[-self.MAX_LESSONS:]
        self.backfilled = True
        try:
            self.save()
        except Exception:
            pass
        return synthetic_lessons

    def get_stats(self):
        """
        Return a snapshot of the AI's learned knowledge for /memory endpoint.
        Used by the dashboard's brain panel.
        """
        # Top symbols by trade count
        top_symbols = sorted(
            self.symbol_memory.items(),
            key=lambda kv: -kv[1].get("trades", 0)
        )[:10]
        symbols_view = [
            {
                "symbol":   sym,
                "trades":   s.get("trades", 0),
                "wins":     s.get("wins", 0),
                "win_rate": round(s.get("wins", 0) / max(s.get("trades", 1), 1) * 100, 1),
                "avg_pnl_pct": s.get("avg_pnl", 0.0),
                "best":     s.get("best_setup", ""),
                "worst":    s.get("worst_setup", ""),
            }
            for sym, s in top_symbols
        ]
        # AI personas
        ai_view = {}
        for ai in ("claude", "grok"):
            p = self.ai_patterns.get(ai, {})
            tot = p.get("wins", 0) + p.get("losses", 0)
            ai_view[ai] = {
                "wins":      p.get("wins", 0),
                "losses":    p.get("losses", 0),
                "win_rate":  round(p.get("wins", 0) / max(tot, 1) * 100, 1) if tot else 0,
                "best_setup": p.get("best_setup", ""),
            }
        # Recent lessons
        recent = list(reversed(self.lessons))[:8]
        return {
            "total_closed":     self.total_closed,
            "total_wins":       self.total_wins,
            "win_rate_overall": round(self.total_wins / max(self.total_closed, 1) * 100, 1)
                                if self.total_closed else 0,
            "lessons_count":    len(self.lessons),
            "symbols_tracked":  len(self.symbol_memory),
            "ai_patterns":      ai_view,
            "market_regimes":   self.market_regime_stats,
            "top_symbols":      symbols_view,
            "recent_lessons":   recent,
            "last_save_iso":    self.last_save_iso,
            "created_iso":      self.created_iso,
            "backfilled":       self.backfilled,
            "memory_file":      self.MEMORY_FILE,
        }

    def get_relevant_lessons(self, symbol=None, situation=None, spy_trend=None, n=None):
        """
        Return the most relevant lessons for current context.
        Priority: same symbol > same situation > same spy_trend > recent.
        """
        n = n or self.MAX_INJECT
        scored = []
        for lesson in reversed(self.lessons):  # newest first
            score = 0
            if symbol    and lesson["symbol"]    == symbol:    score += 10
            if situation and lesson["situation"] == situation: score += 5
            if spy_trend and lesson["spy_trend"] == spy_trend: score += 3
            scored.append((score, lesson))
        scored.sort(key=lambda x: -x[0])
        return [l for _, l in scored[:n]]

    def format_for_prompt(self, symbol=None, situation=None, spy_trend=None):
        """
        Format relevant lessons as a compact block for AI prompts.
        Returns empty string if no lessons yet.
        """
        lessons = self.get_relevant_lessons(symbol, situation, spy_trend)
        if not lessons:
            return ""

        lines = ["LEARNED FROM PAST TRADES:"]
        for l in lessons:
            icon = "✅" if l["outcome"] == "win" else "❌"
            lines.append(f"  {icon} {l['summary']}")

        # Add win-rate context if enough trades
        if self.total_closed >= 5:
            wr = round(self.total_wins / self.total_closed * 100, 0)
            lines.append(f"  Overall win rate: {int(wr)}% ({self.total_wins}/{self.total_closed} trades)")

        # Symbol-specific insight
        if symbol and symbol in self.symbol_memory:
            sm = self.symbol_memory[symbol]
            if sm["trades"] >= 2:
                sym_wr = round(sm["wins"] / sm["trades"] * 100, 0)
                lines.append(f"  {symbol} specifically: {int(sym_wr)}% win rate ({sm['trades']} trades, avg {sm['avg_pnl']:+.1f}%)")

        # AI persona insight
        for ai in ["claude", "grok"]:
            p = self.ai_patterns[ai]
            total = p["wins"] + p["losses"]
            if total >= 3:
                ai_wr = round(p["wins"] / total * 100, 0)
                if p["best_setup"]:
                    lines.append(f"  {ai.title()} best setup: {p['best_setup'][:50]} ({int(ai_wr)}% win rate)")

        # Regime warning
        bear_stats = self.market_regime_stats.get("bear", {})
        if spy_trend == "bear" and bear_stats.get("trades", 0) >= 3:
            bear_wr = round(bear_stats["wins"] / bear_stats["trades"] * 100, 0)
            if bear_wr < 40:
                lines.append(f"  ⚠️ BEAR MARKET WARNING: Your win rate in bear markets is {int(bear_wr)}% — be cautious")

        return "\n".join(lines)

    def get_ai_persona(self, ai_name):
        """
        Return a persona note for Claude or Grok based on what's worked.
        Injected at the top of their system prompt.
        """
        p = self.ai_patterns.get(ai_name, {})
        total = p.get("wins", 0) + p.get("losses", 0)
        if total < 3:
            return ""
        wr = round(p["wins"] / total * 100, 0)
        best = p.get("best_setup", "")
        if wr >= 60 and best:
            return f"Your strongest setup historically: {best}. Win rate: {int(wr)}%."
        elif wr < 40:
            return f"Recent performance has been challenging ({int(wr)}% win rate). Be more selective today."
        return ""


# ═════════════════════════════════════════════════════
# SITUATION CLASSIFIER
# Pure logic — no API calls, instant
# ═════════════════════════════════════════════════════

def classify_situation(equity, cash, positions, spy_trend,
                       pnl_today_pct, has_triple_confirmation,
                       positions_near_stop, positions_near_tp,
                       pdt_trades_remaining=3, pdt_is_swing=True):
    """
    Classify the current trading situation into one of 8 modes.
    This determines how the prompt is weighted and framed.

    pdt_trades_remaining: how many day trades left today (0-3)
    pdt_is_swing: True if all positions were bought on a prior day (safe to sell)

    Returns: (mode_str, priority_focus, urgency)
    """
    # ── PDT HARD BLOCK: 0 trades left + positions bought today ──
    # If we have 0 day trades and positions bought today, selling = PDT violation.
    # Switch to hold_only so AIs don't waste cycles proposing illegal trades.
    if pdt_trades_remaining == 0 and not pdt_is_swing:
        return (
            "pdt_hold_only",
            "PDT limit reached (0/3 trades). All positions bought today — CANNOT sell without PDT violation. "
            "Hold everything. No new buys. Do NOT propose any exits today.",
            "HIGH"
        )

    # ── PDT CAUTION: 0 trades left but positions are swing (safe to sell) ──
    if pdt_trades_remaining == 0 and pdt_is_swing:
        return (
            "pdt_swing_hold",
            "PDT limit reached (0/3 day trades used). Positions are swing trades — exits are safe. "
            "No new buys today (would create a new day trade risk). Monitor exits only.",
            "MEDIUM"
        )

    # Emergency / damage control
    if pnl_today_pct <= -0.04:
        return "damage_control", "Stop losing money. No new buys. Review all positions.", "HIGH"

    # Position in danger zone
    if positions_near_stop:
        return "defensive", f"Positions near stop: {positions_near_stop}. Protect capital.", "HIGH"

    # Ready to harvest profits
    if positions_near_tp:
        return "harvest_profits", f"Positions near TP: {positions_near_tp}. Lock in gains.", "MEDIUM"

    # Bear market — conservative mode
    if spy_trend == "bear":
        return "capital_preservation", "Bear market. No new buys. Manage exits only.", "MEDIUM"

    # High conviction signal available
    if has_triple_confirmation:
        return "high_conviction_entry", "Triple confirmation signal detected. High priority entry.", "HIGH"

    # Cash ready, market good — opportunity mode
    if cash > 20 and spy_trend in ("bull", "neutral"):
        return "opportunity_seeking", "Cash available, market cooperative. Find best entry.", "MEDIUM"

    # Low cash — manage what we have
    if cash < 15:
        return "capital_conservation", "Low cash. Focus on managing open positions efficiently.", "LOW"

    return "standard_monitoring", "Normal conditions. Balanced approach.", "LOW"


# ═══════════════════════════════════════════════════════════════════════════════════════════════════════════════
