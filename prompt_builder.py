"""
prompt_builder.py
══════════════════════════════════════════════════════════════════
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


# ══════════════════════════════════════════════════════════════
# PROMPT MEMORY — persists learned lessons across sessions
# Kept in memory (resets on restart) but grows during a session.
# Format designed for easy serialization if you want to add
# persistence to a file or DB later.
# ══════════════════════════════════════════════════════════════

class PromptMemory:
    """
    Accumulates trade outcomes and extracts lessons.
    Injected into every AI prompt as 'LEARNED CONTEXT'.
    Starts empty, grows with every closed trade.
    """

    MAX_LESSONS = 50   # Rolling window — older lessons drop off
    MAX_INJECT  = 4    # Max lessons injected per prompt (keep prompts tight)

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

        # ── Per-symbol memory ──────────────────────────────
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

        # ── AI pattern tracking ────────────────────────────
        if owner in self.ai_patterns:
            if won:
                self.ai_patterns[owner]["wins"] += 1
                if not self.ai_patterns[owner]["best_setup"]:
                    self.ai_patterns[owner]["best_setup"] = entry_reason[:60]
            else:
                self.ai_patterns[owner]["losses"] += 1

        # ── Market regime stats ────────────────────────────
        regime = spy_trend or "neutral"
        if regime in self.market_regime_stats:
            self.market_regime_stats[regime]["trades"] += 1
            if won:
                self.market_regime_stats[regime]["wins"] += 1

        # ── Situation mode stats ───────────────────────────
        if situation_mode:
            if situation_mode not in self.situation_stats:
                self.situation_stats[situation_mode] = {"trades": 0, "wins": 0}
            self.situation_stats[situation_mode]["trades"] += 1
            if won:
                self.situation_stats[situation_mode]["wins"] += 1

        # ── Extract lesson ─────────────────────────────────
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
        # Rolling window
        if len(self.lessons) > self.MAX_LESSONS:
            self.lessons.pop(0)

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


# ══════════════════════════════════════════════════════════════
# SITUATION CLASSIFIER
# Pure logic — no API calls, instant
# ══════════════════════════════════════════════════════════════

def classify_situation(equity, cash, positions, spy_trend,
                       pnl_today_pct, has_triple_confirmation,
                       positions_near_stop, positions_near_tp):
    """
    Classify the current trading situation into one of 7 modes.
    This determines how the prompt is weighted and framed.

    Returns: (mode_str, priority_focus, urgency)
    """
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


# ══════════════════════════════════════════════════════════════
# PROJECTION LANGUAGE GENERATOR
# Turns raw projection numbers into specific, actionable sentences
# ══════════════════════════════════════════════════════════════

def generate_projection_language(projections, positions_data, current_prices=None):
    """
    Convert projection engine output into specific trade language.
    Much more useful than raw numbers — tells AIs exactly what the
    projection means for each position and potential entry.

    projections     : shared_state["last_projections"]
    positions_data  : list of position dicts from Alpaca
    current_prices  : optional {symbol: price} for more precision
    """
    if not projections:
        return "No projection data available this cycle."

    lines = ["ACTIONABLE PROJECTION ANALYSIS:"]
    owned_syms = [p["symbol"] for p in (positions_data or [])]

    for sym, proj in sorted(projections.items(),
                            key=lambda x: x[1].get("confidence", 0), reverse=True):
        if proj.get("error"):
            continue

        ph   = proj.get("proj_high")
        pl   = proj.get("proj_low")
        piv  = proj.get("pivot")
        atr  = proj.get("atr")
        conf = proj.get("confidence", 0)
        bias = proj.get("bias", "neutral").upper()

        if not ph or not pl:
            continue

        # Get current price from positions if available
        curr = None
        if current_prices and sym in current_prices:
            curr = current_prices[sym]
        elif positions_data:
            pos = next((p for p in positions_data if p["symbol"] == sym), None)
            if pos:
                curr = float(pos.get("current_price", 0))

        conf_tag = "HIGH CONF" if conf >= 70 else "MED CONF" if conf >= 50 else "LOW CONF"

        if sym in owned_syms:
            # Position we own — frame as exit guidance
            if curr:
                dist_to_high = round((ph - curr) / curr * 100, 1)
                dist_to_low  = round((curr - pl) / curr * 100, 1)
                if curr >= ph * 0.99:
                    lines.append(
                        f"  🎯 {sym} [OWNED] AT PROJ HIGH ${ph} — consider taking profit now "
                        f"({conf_tag} conf={conf})"
                    )
                elif dist_to_high <= 1.5:
                    lines.append(
                        f"  ⚠️  {sym} [OWNED] near proj high ${ph} (${curr:.2f}, {dist_to_high:.1f}% away) "
                        f"— prepare to exit {conf_tag}"
                    )
                elif curr <= pl * 1.01:
                    lines.append(
                        f"  🛡️  {sym} [OWNED] AT PROJ LOW ${pl} — support zone, watch for bounce "
                        f"({conf_tag})"
                    )
                else:
                    lines.append(
                        f"  📊 {sym} [OWNED] ${curr:.2f} | range ${pl}–${ph} | "
                        f"{dist_to_high:.1f}% to TP | {conf_tag} {bias}"
                    )
            else:
                lines.append(
                    f"  📊 {sym} [OWNED] range ${pl}–${ph} pivot=${piv} {conf_tag} {bias}"
                )
        else:
            # Not owned — frame as entry opportunity
            if curr and curr <= pl * 1.005:
                lines.append(
                    f"  🟢 {sym} [ENTRY ZONE] price near proj_low ${pl} — "
                    f"target ${ph} (+{round((ph-pl)/pl*100,1)}%) {conf_tag} {bias}"
                )
            elif conf >= 70 and bias == "BULLISH":
                lines.append(
                    f"  ⭐ {sym} [WATCH] bullish setup — entry at ${pl}, "
                    f"target ${ph}, ATR=${atr} {conf_tag}"
                )
            elif conf < 50:
                pass  # Skip low confidence unwatched stocks to save prompt space

    if len(lines) == 1:
        lines.append("  All projections within normal ranges — no extreme setups.")

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════
# MAIN PROMPT BUILDER CLASS
# ══════════════════════════════════════════════════════════════

class PromptBuilder:
    """
    Central prompt factory for the trading bot.
    Initialized once at module level, used across all cycles.
    Memory grows with every closed trade.
    """

    def __init__(self):
        self.memory = PromptMemory()
        self._last_situation = "standard_monitoring"
        self._cycle_count    = 0

    # ── PUBLIC: Record trade outcome (call from record_trade) ──
    def on_trade_closed(self, symbol, pnl_usd, pnl_pct,
                        owner, strategy, signals=None,
                        spy_trend="neutral", entry_reason=""):
        """
        Call this every time a position closes.
        Feeds the memory system so prompts get smarter.
        """
        self.memory.record_outcome(
            symbol      = symbol,
            action      = "close",
            pnl_usd     = pnl_usd,
            pnl_pct     = pnl_pct,
            owner       = owner,
            strategy    = strategy,
            signals     = signals or [],
            spy_trend   = spy_trend,
            situation_mode = self._last_situation,
            entry_reason   = entry_reason,
        )

    # ── SITUATION ASSESSMENT ────────────────────────────────
    def assess_situation(self, equity, cash, positions,
                         spy_trend, pnl_today_pct,
                         triple_syms, projections):
        """
        Classify current situation and identify key pressure points.
        Returns (mode, focus, urgency, context_dict)
        """
        # Find positions near stop or TP
        near_stop = []
        near_tp   = []
        for p in (positions or []):
            pnl = float(p.get("unrealized_plpc", 0))
            sym = p["symbol"]
            if pnl <= -0.032:   near_stop.append(sym)  # Within 0.8% of -4% stop
            if pnl >= 0.055:    near_tp.append(sym)    # Within 1.5% of 7% TP
            # Also check projection-based exits
            proj = projections.get(sym, {})
            if proj and not proj.get("error"):
                curr = float(p.get("current_price", 0))
                ph   = proj.get("proj_high", 0)
                if ph and curr >= ph * 0.99:
                    if sym not in near_tp:
                        near_tp.append(sym)

        mode, focus, urgency = classify_situation(
            equity, cash, positions, spy_trend,
            pnl_today_pct,
            bool(triple_syms),
            near_stop, near_tp,
        )
        self._last_situation = mode

        return mode, focus, urgency, {
            "near_stop": near_stop,
            "near_tp":   near_tp,
        }

    # ── BUILD R1 PROMPT (main collaborative session) ────────
    def build_r1(self, equity, cash, positions, pos_details,
                 pool, chart_section, news, market_ctx,
                 pol_text, pol_mimick, gainers, ipos,
                 hot_ipos, triple_syms, top_collab, inv_text,
                 short_note, spy_trend, features,
                 projections=None,
                 crypto_context: str = ""):   # ← NEW: unified crypto section
        """
        Build the Round 1 collaborative session prompt.
        Situation-aware, projection-informed, memory-injected.
        Now includes optional crypto section — one big call instead of two.
        """
        self._cycle_count += 1

        # 1. Assess situation
        # Use today's start equity for accurate daily P&L
        # Fallback to equity itself (= 0% loss) if not set
        pnl_today_pct = 0.0
        try:
            day_start = getattr(self, '_day_start_equity', equity)
            if day_start and day_start > 0 and day_start != equity:
                pnl_today_pct = (equity - day_start) / day_start
        except Exception:
            pass

        mode, focus, urgency, pressure = self.assess_situation(
            equity, cash, positions, spy_trend,
            pnl_today_pct, triple_syms, projections or {}
        )

        near_stop = pressure["near_stop"]
        near_tp   = pressure["near_tp"]

        # 2. Generate projection language
        proj_text = generate_projection_language(
            projections or {}, positions
        )

        # 3. Get learned lessons relevant to this situation
        lessons = self.memory.format_for_prompt(
            situation=mode, spy_trend=spy_trend
        )

        # 4. Build situation header — this goes FIRST
        # ── Gather regime, timing, streak data for prompt ──────
        try:
            import importlib, sys
            # These functions live in bot_with_proxy — import safely
            _bwp = sys.modules.get("__main__") or sys.modules.get("bot_with_proxy")
            _get_regime  = getattr(_bwp, "get_market_regime",  None)
            _get_timing  = getattr(_bwp, "get_entry_quality",  None)
            _shared      = getattr(_bwp, "shared_state",       {})
            regime_data  = _get_regime() if _get_regime else None
            timing_data  = _get_timing() if _get_timing else None
            streak_data  = {
                "consecutive_wins":   _shared.get("consecutive_wins", 0),
                "consecutive_losses": _shared.get("consecutive_losses", 0),
                "win_streak_bonus":   _shared.get("win_streak_bonus", 1.0),
                "cooldown_until":     _shared.get("cooldown_until"),
            } if _shared else None
        except Exception:
            regime_data = timing_data = streak_data = None

        situation_header = self._build_situation_header(
            mode, focus, urgency, near_stop, near_tp,
            equity, cash, spy_trend,
            regime_data=regime_data,
            timing_data=timing_data,
            streak_data=streak_data,
        )

        # 5. Build open positions section with projection context
        pos_section = self._build_positions_section(
            positions, pos_details, projections or {}
        )

        # 6. Build market intelligence section (weighted by mode)
        intel_section = self._build_intel_section(
            mode, news, market_ctx, pol_text, pol_mimick,
            gainers, ipos, hot_ipos, triple_syms,
            top_collab, inv_text, chart_section
        )

        # ── Crypto section (unified — avoids separate AI call) ──
        crypto_block = ""
        if crypto_context:
            crypto_block = f"""
=== 🪙 CRYPTO (Binance.US — same call, no extra cost) ===
{crypto_context}
CRYPTO TASK: Alongside stock proposals, include optional crypto_trades.
Rules: min 2.5% profit | -4% stop | 72h max hold | LIMIT orders only | VIABLE range only
JSON crypto_trades field: [{{"symbol":"BTCUSDT","action":"buy","notional_usdt":12.0,"confidence":80,"entry_target":95000.0,"tp_target":97500.0,"rationale":"brief"}}]
Leave crypto_trades empty [] if no good setup — never force a crypto trade."""

        # 7. Assemble the full prompt
        # Performance vs targets
        trading_pool  = pool.get("trading", equity * 0.85)
        daily_target  = round(trading_pool * 0.05, 2)
        weekly_target = round(trading_pool * 0.15, 2)
        monthly_target= round(trading_pool * 0.40, 2)
        daily_pnl_est = round(equity - 55, 2)  # rough
        daily_pct     = round(daily_pnl_est / max(trading_pool, 1) * 100, 1)
        perf_status   = ("🟢 ON TRACK" if daily_pnl_est >= daily_target else
                         "🟡 BEHIND"   if daily_pnl_est > 0 else "🔴 LOSING")
        perf_block = f"📊 {perf_status} | Today: ${daily_pnl_est:+.2f} ({daily_pct:+.1f}%) | Targets: day=${daily_target:.2f} wk=${weekly_target:.2f} mo=${monthly_target:.2f}"

        prompt = f"""{situation_header}

=== PORTFOLIO ===
Equity: ${equity:.2f} | Cash: ${cash:.2f} | Pool: ${pool['trading']:.2f} (Claude=${pool['claude']:.2f} Grok=${pool['grok']:.2f}) | Reserve: ${pool['reserve']:.2f} LOCKED
{short_note}{perf_block}

{pos_section}

{intel_section}

=== PROJECTION ENGINE ANALYSIS ===
{proj_text}

{f"=== {lessons} ===" if lessons else ""}
{crypto_block}
=== YOUR TASK [{mode.upper().replace('_',' ')} MODE] ===
FOCUS: {focus}
SPY: {spy_trend.upper()} {'— NO NEW BUYS' if spy_trend == 'bear' else '— Full trading active'}
{self._build_task_instructions(mode, near_stop, near_tp, triple_syms, cash)}

TRADE RULES: Only propose if 6/8 pass — (1)specific thesis (2)entry near support not resistance (3)volume above avg (4)VIX<25 + SPY above 50SMA (5)good entry window 9:45-10:15 or 14:00-14:30 ET (6)know what invalidates trade (7)diversifies not concentrates portfolio (8)symbol not on recent loss streak. conf<75=skip | conf90+=125% size | conf80-89=100% | conf75-79=75%. Grok: tag each data point [NOW/<30min] [TODAY] or [STALE/>6h]. Portfolio: {[p["symbol"] for p in (positions or [])]} — 2 same-sector positions = -15 conf.
VOL_PROFILE GUIDE: ACCUM(OBV↑+price↓)=buy dip | DISTRIB(OBV↓+price↑)=exit soon | COILING(tight range+vol↓)=wait for breakout direction | CLIMAX_TOP(vol spike+price high)=take profit NOW | EXHAUSTION(vol spike+price low)=watch for bounce | price BELOW VWAP20=cheap entry zone | price ABOVE VWAP20=expensive avoid chasing | B/S ratio>1.3=buyers dominate bullish | B/S<0.7=sellers dominate bearish."""

        return prompt, mode

    # ── BUILD PREMARKET PROMPT ──────────────────────────────
    def build_premarket(self, equity, pool, chart, news, market,
                        pol_text, pol_mimick, triple_syms, top_collab,
                        gainers, ipos, hot_ipos, inv_text,
                        projections=None):
        """
        Build the pre-market research prompt.
        Includes overnight lessons and projection setup for the day.
        """
        # Morning lessons — what worked recently
        lessons = self.memory.format_for_prompt(situation="standard_monitoring")

        proj_text = generate_projection_language(projections or {}, [])

        # Win rate summary
        wr_note = ""
        if self.memory.total_closed >= 5:
            wr = round(self.memory.total_wins / self.memory.total_closed * 100, 0)
            wr_note = f"Current win rate: {int(wr)}% ({self.memory.total_wins}/{self.memory.total_closed} trades)"

        prompt = f"""=== PRE-MARKET RESEARCH — Day {self._cycle_count} ===
Budget: Claude=${pool['claude']:.2f} | Grok=${pool['grok']:.2f} | Reserve=${pool['reserve']:.2f}
{wr_note}

MARKET OPEN CONTEXT:
{market}

NEWS (24h):
{news[:250]}

TODAY'S PROJECTION SETUP:
{proj_text[:500]}

SMART MONEY SIGNALS:
🔥 Triple confirmation: {triple_syms}
⭐ Top collaborative: {top_collab}
📈 Biggest gainers (>3%): {[(g['symbol'], f'+{g["change"]:.1f}%') for g in gainers[:5]]}

POLITICIAN TRADES: {pol_text[:200]}
Top mimick: {pol_mimick}
TOP INVESTORS (13F): {inv_text[:150]}

RECENT IPOs (30-180d, high momentum):
{[(i['symbol'], f"{i['days_old']}d old", f"mom={i['mom_5d']}%") for i in ipos[:5]]}
Hot IPOs (>5% mom): {[i['symbol'] for i in hot_ipos]}

FULL INDICATORS: {chart[:400]}

{f"LEARNED CONTEXT:{chr(10)}{lessons}" if lessons else ""}

RESEARCH TASKS:
1. Triple confirmation = highest priority — both AIs must agree
2. Which projection setups look cleanest today? (high conf + bullish bias)
3. Hot IPOs near proj_low = ideal autonomous entry
4. Politician + investor alignment = strong signal
5. Any overnight news that changes the projection bias?
Respond in plain text, 150 words max."""

        return prompt

    # ── BUILD AFTERHOURS PROMPT ─────────────────────────────
    def build_afterhours_claude(self, pnl, positions, pol_text,
                                inv_text, smart_money, spy_trend):
        """
        Build Claude's after-hours smart money review prompt.
        Includes today's lessons and performance vs projections.
        """
        mode_stats = self.memory.situation_stats
        lessons    = self.memory.format_for_prompt(situation=self._last_situation)

        perf_by_mode = []
        for mode, stats in mode_stats.items():
            if stats["trades"] >= 2:
                wr = round(stats["wins"] / stats["trades"] * 100, 0)
                perf_by_mode.append(f"  {mode}: {int(wr)}% win rate ({stats['trades']} trades)")

        perf_text = "\n".join(perf_by_mode) if perf_by_mode else "  Not enough data yet"

        proj_accuracy = ""
        # This will be filled by the bot passing shared_state accuracy
        # Left as a hook for the bot to inject

        prompt = f"""AFTER-HOURS — CLAUDE reviewing smart money signals for TOMORROW.

TODAY'S RESULTS:
P&L: ${pnl:+.2f} | SPY: {spy_trend.upper()}
Positions held: {[p['symbol'] for p in positions]}

WIN RATE BY SITUATION MODE:
{perf_text}

{f"TODAY'S LESSONS:{chr(10)}{lessons}" if lessons else ""}

NEW POLITICIAN FILINGS:
{pol_text[:400]}

TOP INVESTOR MOVES:
{inv_text[:300]}

SMART MONEY SETUP:
Triple confirmation: {smart_money.get("triple_confirmation", [])}
Top collaborative: {smart_money.get("top_collab", [])}

ANALYSIS TASKS:
1. Any NEW politician filings that change tomorrow's outlook?
2. Are politicians buying into weakness? (contrarian signal)
3. Which smart money positions align with tomorrow's projection setups?
4. Any politician SELLS as warning signals?
5. Hold overnight or go to cash?
Plain text 180 words."""

        return prompt

    def build_afterhours_grok(self, pnl, positions, ipos,
                               gainers, news, spy_trend,
                               pol_text="", pol_mimick=None):
        """
        Build Grok's after-hours momentum review prompt.
        Now includes Capitol Trades politician data — Grok cross-references
        politician buys against momentum setups for tomorrow's watchlist.
        """
        ai_persona  = self.memory.get_ai_persona("grok")
        lessons     = self.memory.format_for_prompt(
            situation=self._last_situation, spy_trend=spy_trend
        )
        pol_mimick  = pol_mimick or []

        # Build politician section — highlight any overlap with after-hours movers
        gainer_syms = {g["symbol"] for g in gainers}
        pol_overlap = [s for s in pol_mimick if s in gainer_syms]

        pol_section = ""
        if pol_text and pol_text.strip() and pol_text != "  No politician trade data available":
            pol_section = f"""
POLITICIAN TRADES (Capitol Trades — filed recently):
{pol_text[:300]}
Top mimick candidates: {pol_mimick}
{f"⚡ OVERLAP — politicians buying today's movers: {pol_overlap}" if pol_overlap else ""}"""

        prompt = f"""AFTER-HOURS — GROK reviewing momentum + smart money for TOMORROW.

TODAY'S RESULTS:
P&L: ${pnl:+.2f} | SPY: {spy_trend.upper()}
{f"Your edge: {ai_persona}" if ai_persona else ""}
{pol_section}
AFTER-HOURS IPO ACTIVITY:
{[(i["symbol"], f"mom={i['mom_5d']}%", f"price=${i['price']}") for i in ipos[:6]]}

AFTER-HOURS MOVERS:
{[(g["symbol"], f'+{g["change"]:.1f}%') for g in gainers[:5]]}

NEWS: {news[:300]}

{f"RECENT LESSONS:{chr(10)}{lessons}" if lessons else ""}

ANALYSIS TASKS:
1. Which IPOs show after-hours strength? (pre-market gap up likely)
2. Any politician buys overlapping with today's movers? (double signal)
3. Twitter/X sentiment for tomorrow — fear or greed?
4. Any earnings surprises affecting our universe?
5. Which losers might bounce tomorrow?
6. Top 3 momentum plays for tomorrow's open — prioritize politician + momentum combos
7. Any new short candidates for bearish watchlist?
Plain text 180 words."""

        return prompt

    # ── BUILD CRYPTO CONTEXT (for unified R1 call) ──────────
    def build_crypto_context(self, wallet_summary: str = "",
                              crypto_pool: float = 0,
                              crypto_proj_text: str = "",
                              crypto_holdings: str = "",
                              crypto_stats: str = "",
                              stock_cross_ref: str = "") -> str:
        """
        Build the crypto section appended to build_r1().
        Keeps crypto decisions in the SAME AI call as stocks —
        zero extra API cost, full shared context.
        Returns empty string if nothing to show.
        """
        if not crypto_proj_text and not wallet_summary:
            return ""

        parts = []
        if wallet_summary:
            parts.append(wallet_summary)
        if crypto_pool > 0:
            parts.append(f"Spendable USDT: ${crypto_pool:.2f}")
        if crypto_holdings:
            parts.append(crypto_holdings)
        if crypto_stats:
            parts.append(f"24h movers: {crypto_stats}")
        if crypto_proj_text:
            parts.append(crypto_proj_text)
        if stock_cross_ref:
            parts.append(stock_cross_ref)

        return "\n".join(p for p in parts if p.strip())

    def build_claude_system(self):
        """Claude's system prompt — evolves with performance."""
        persona = self.memory.get_ai_persona("claude")
        base = "You are Claude, a disciplined quantitative trader. Compact JSON only. Use abbreviated keys (sn/mt/pt/cc/bw). market_thesis max 12 words, rationale max 8 words."
        if persona:
            return f"{base} {persona}"
        return base

    def build_grok_system(self):
        """Grok's system prompt — evolves with performance."""
        persona = self.memory.get_ai_persona("grok")
        base = "You are Grok, a momentum trader with Twitter/X access. Compact JSON only. Use abbreviated keys (sn/mt/pt/cc/bw). market_thesis max 12 words, rationale max 8 words."
        if persona:
            return f"{base} {persona}"
        return base

    # ── API ENDPOINT DATA ────────────────────────────────────
    def get_memory_stats(self):
        """Return memory stats for /prompt_memory API endpoint."""
        m = self.memory
        return {
            "total_closed":    m.total_closed,
            "total_wins":      m.total_wins,
            "win_rate_pct":    round(m.total_wins / m.total_closed * 100, 1) if m.total_closed else 0,
            "lessons_stored":  len(m.lessons),
            "symbols_tracked": len(m.symbol_memory),
            "last_situation":  self._last_situation,
            "cycle_count":     self._cycle_count,
            "ai_patterns": {
                ai: {
                    "wins":       p["wins"],
                    "losses":     p["losses"],
                    "win_rate":   round(p["wins"] / max(p["wins"]+p["losses"],1) * 100, 1),
                    "best_setup": p["best_setup"],
                }
                for ai, p in m.ai_patterns.items()
            },
            "regime_stats": {
                regime: {
                    "trades":   s["trades"],
                    "win_rate": round(s["wins"]/max(s["trades"],1)*100,1),
                }
                for regime, s in m.market_regime_stats.items()
            },
            "situation_stats": {
                mode: {
                    "trades":   s["trades"],
                    "win_rate": round(s["wins"]/max(s["trades"],1)*100,1),
                }
                for mode, s in m.situation_stats.items()
            },
            "recent_lessons": [
                {"symbol": l["symbol"], "outcome": l["outcome"],
                 "pnl_pct": l["pnl_pct"], "summary": l["summary"]}
                for l in list(reversed(m.lessons))[:10]
            ],
        }

    # ── PRIVATE HELPERS ──────────────────────────────────────

    def _build_situation_header(self, mode, focus, urgency,
                                 near_stop, near_tp,
                                 equity, cash, spy_trend,
                                 regime_data=None, timing_data=None,
                                 streak_data=None):
        urgency_icons = {"HIGH": "🚨", "MEDIUM": "⚠️", "LOW": "📊"}
        icon = urgency_icons.get(urgency, "📊")

        mode_display = mode.upper().replace("_", " ")
        header = f"{icon} SITUATION: {mode_display} (urgency={urgency})\nFOCUS: {focus}"

        if near_stop:
            header += f"\n🛑 POSITIONS NEAR STOP: {near_stop} — monitor closely"
        if near_tp:
            header += f"\n🎯 POSITIONS NEAR TAKE-PROFIT: {near_tp} — consider exiting"
        if spy_trend == "bear":
            header += f"\n🐻 BEAR MARKET ACTIVE — capital preservation mode"

        # ── Market Regime (VIX-based) ─────────────────────────
        if regime_data:
            header += f"\n\n{regime_data.get('description', '')}"
            if not regime_data.get("entry_ok", True):
                header += "\n🚫 NEW ENTRIES BLOCKED this cycle — wait for regime to improve"
            sz = regime_data.get("size_mult", 1.0)
            if sz != 1.0:
                header += f"\n📏 Position sizing: {sz:.0%} of normal (regime adjustment)"

        # ── Entry Timing Quality ─────────────────────────────
        if timing_data:
            score = timing_data.get("score", 50)
            label = timing_data.get("label", "")
            if score < 50:
                header += f"\n⏰ ENTRY TIMING: {label} (score={score}/100) — poor time to enter"
            elif score >= 80:
                header += f"\n⏰ ENTRY TIMING: {label} (score={score}/100) ✅"

        # ── Streak Intelligence ───────────────────────────────
        if streak_data:
            wins   = streak_data.get("consecutive_wins", 0)
            losses = streak_data.get("consecutive_losses", 0)
            bonus  = streak_data.get("win_streak_bonus", 1.0)
            cooldown = streak_data.get("cooldown_until")
            if cooldown:
                header += f"\n⛔ TRADING COOLDOWN ACTIVE until {cooldown} — 2 consecutive losses. NO new entries."
            elif wins >= 3:
                header += f"\n🔥 WIN STREAK: {wins} wins in a row! Size bonus {bonus:.1f}x active."
            elif losses == 1:
                header += f"\n⚠️ 1 recent loss — be selective. 1 more triggers 12hr cooldown."

        return header

    def _build_positions_section(self, positions, pos_details, projections):
        if not positions:
            return "=== OPEN POSITIONS ===\n  None"

        lines = ["=== OPEN POSITIONS ==="]
        for p in positions:
            sym   = p["symbol"]
            pnl   = round(float(p.get("unrealized_plpc", 0)) * 100, 2)
            curr  = float(p.get("current_price", 0))
            entry = float(p.get("avg_entry_price", 0))
            owner = "Claude" if sym in [] else "Grok"  # will be resolved by bot

            proj  = projections.get(sym, {})
            proj_note = ""
            if proj and not proj.get("error"):
                ph = proj.get("proj_high", 0)
                pl = proj.get("proj_low", 0)
                if ph and pl and curr:
                    if curr >= ph * 0.99:
                        proj_note = f" → AT PROJ HIGH ${ph} — consider exit"
                    elif curr <= pl * 1.01:
                        proj_note = f" → AT PROJ LOW ${pl} — support zone"
                    else:
                        dist_tp = round((ph - curr) / curr * 100, 1)
                        proj_note = f" → {dist_tp:.1f}% to proj_high ${ph}"

            lines.append(
                f"  {sym}: entry=${entry:.2f} now=${curr:.2f} "
                f"P&L={pnl:+.2f}%{proj_note}"
            )

        return "\n".join(lines)

    def _build_intel_section(self, mode, news, market_ctx,
                              pol_text, pol_mimick, gainers,
                              ipos, hot_ipos, triple_syms,
                              top_collab, inv_text, chart_section):
        """
        Weight intel sections differently depending on situation mode.
        High-conviction mode leads with smart money.
        Defensive mode leads with market context.
        Standard mode is balanced.
        """
        if mode in ("high_conviction_entry",):
            # Smart money front and center
            return f"""=== MARKET INTELLIGENCE ===
MARKET: {market_ctx}
🔥 TRIPLE CONFIRMATION: {triple_syms} — HIGHEST PRIORITY
⭐ Top collaborative: {top_collab}
POLITICIAN TRADES: {pol_text[:300]}
Top mimick: {pol_mimick}
NEWS: {news[:200]}
GAINERS: {[(g['symbol'], f'+{g["change"]:.1f}%') for g in gainers[:5]]}
TOP INVESTORS: {inv_text[:150]}
IPOs: {[(i['symbol'], f"mom={i['mom_5d']}%") for i in ipos[:3]]}
INDICATORS: {chart_section[:400]}"""

        elif mode in ("defensive", "damage_control", "capital_preservation"):
            # Risk context front and center
            return f"""=== MARKET INTELLIGENCE ===
MARKET: {market_ctx}
NEWS (watch for catalysts): {news[:300]}
INDICATORS (for exit signals): {chart_section[:500]}
GAINERS: {[(g['symbol'], f'+{g["change"]:.1f}%') for g in gainers[:3]]}
Smart money: triple={triple_syms} collab={top_collab}"""

        elif mode == "harvest_profits":
            # Focused on exit data
            return f"""=== MARKET INTELLIGENCE ===
MARKET: {market_ctx}
NEWS (anything that could hurt open positions?): {news[:250]}
INDICATORS (check for reversal signals): {chart_section[:500]}
Smart money: {triple_syms}"""

        else:
            # Standard balanced view
            return f"""=== MARKET INTELLIGENCE ===
MARKET: {market_ctx}
NEWS: {news[:250]}
POLITICIAN TRADES: {pol_text[:300]}
Top mimick: {pol_mimick}
TOP INVESTORS: {inv_text[:150]}
GAINERS: {[(g['symbol'], f'+{g["change"]:.1f}%') for g in gainers[:5]]}
IPOs: {[(i['symbol'], f"{i['days_old']}d", f"mom={i['mom_5d']}%") for i in ipos[:4]]}
Hot IPOs: {hot_ipos}
Smart money: triple={triple_syms} collab={top_collab}
INDICATORS: {chart_section[:450]}"""

    def _build_task_instructions(self, mode, near_stop, near_tp,
                                  triple_syms, cash):
        """
        Mode-specific task instructions — what the AIs should DO this cycle.
        """
        # ── Minimal schema (~120 tokens vs old ~280) ─────
        # flags: comma-sep from: ipo,momentum,breakout,news,politician,earnings
        # (used by decide_exit_strategy — replaces signals[]+bool fields)
        base_json = (
            'REPLY JSON ONLY — no prose, no markdown, market_thesis MAX 12 words:\n'
            '{"sn":"<name>","mt":"<12w>",'
            '"pt":[{"a":"buy|sell","s":"TICK","n":15.0,"c":85,'
            '"f":"ipo|momentum|breakout|news|politician|earnings","r":"<8w>"}],'
            '"cc":[{"s":"TICK","c":90}],'
            '"bw":["TICK"],'
            '"crypto_trades":[]}'
        )

        if mode == "damage_control":
            return f"""TASK: DAMAGE CONTROL — stop the bleeding
1. Review each open position — should anything be sold NOW before stop hits?
2. No new buys until P&L recovers
3. Which position is most at risk? Recommend defensive action.
{base_json}"""

        elif mode == "defensive":
            syms = ", ".join(near_stop) if near_stop else "none"
            return f"""TASK: DEFENSIVE — protect positions near stop
1. PRIORITY: Assess {syms} — sell early or hold to stop?
2. No new buys that add risk
3. Only buy if it's a clear high-confidence setup with room to run
{base_json}"""

        elif mode == "harvest_profits":
            syms = ", ".join(near_tp) if near_tp else "none"
            return f"""TASK: HARVEST PROFITS
1. PRIORITY: {syms} near take-profit — recommend exit now or let run?
2. If selling, what's the best replacement trade with freed cash?
3. Use proj_high as exit signal — sell at or near it
{base_json}"""

        elif mode == "high_conviction_entry":
            syms = ", ".join(triple_syms) if triple_syms else "top pick"
            return f"""TASK: HIGH CONVICTION ENTRY — triple confirmation detected
1. PRIORITY: Analyze {syms} — enter now or wait for proj_low?
2. Size appropriately for confidence level (conf>=70 = full size)
3. Set entry near proj_low, TP at proj_high
4. Flag as collaborative candidate if confidence >=95 from both AIs
{base_json}"""

        elif mode == "capital_preservation":
            return f"""TASK: CAPITAL PRESERVATION — bear market
1. NO new buys — market trend is down
2. Review open positions — any showing weakness? Consider early exit.
3. Identify best re-entry points for when market turns
4. Only propose sells, no buys (unless extraordinary setup >90% confidence)
{base_json}"""

        elif mode == "opportunity_seeking":
            return f"""TASK: OPPORTUNITY SEEKING — cash available, find best entry
1. Propose up to 2 AUTONOMOUS trades using proj_low as entry zone
2. Prefer bullish-bias projections with confidence >= 65
3. Flag any COLLABORATIVE candidates (triple confirmation or biggest gainers)
4. Use LIMIT ORDERS at bid/ask midpoint
{base_json}"""

        else:
            return f"""TASK: STANDARD MONITORING
1. Propose up to 2 AUTONOMOUS trades from your budget (min $8, conf 80%+)
2. Flag any COLLABORATIVE candidates
3. No overlap with partner AI if possible
4. LIMIT ORDERS at bid/ask midpoint for better fill
{base_json}"""
