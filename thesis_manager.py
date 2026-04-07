"""
thesis_manager.py
══════════════════════════════════════════════════════════════════
NovaTrade v3.0 — AI-Led Position Thesis & Smart Wake System

PHILOSOPHY:
  The bot is a dumb executor. The AI is the decision maker.
  Every position has an AI-written thesis. The bot reads it,
  monitors it, and wakes the AI when conditions change —
  never acting on its own except for absolute emergencies.

WHAT THIS FILE DOES:
  1. ThesisManager  — stores, loads, and monitors per-position thesis
  2. WalletSnapshot — builds complete portfolio picture for AI
  3. SleepBrief     — parses AI sleep output into structured instructions
  4. WakeContext    — builds rich context for AI on wake

INTEGRATION:
  from thesis_manager import ThesisManager, WalletSnapshot
  thesis_mgr = ThesisManager()          # module-level, one instance

  # When AI goes to sleep:
  thesis_mgr.update_from_sleep_brief(ai_json_output, positions, crypto_positions)

  # Every 5-min bot loop:
  triggered = thesis_mgr.check_all_conditions(positions, crypto_positions, spy_pct)
  if triggered:
      wake_reason, wake_context = triggered
      ai_wake(wake_reason)
      # pass wake_context to AI on next cycle

  # When AI wakes:
  context = thesis_mgr.build_wake_context(wake_reason, positions, crypto_positions)
"""

import json
import os
import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# ── Persistence path ─────────────────────────────────────────────
# Survives Railway restarts. Written after every AI sleep.
THESIS_PATH = "/tmp/novatrade_thesis.json"


# ══════════════════════════════════════════════════════════════════
# POSITION THESIS DATA CLASS
# ══════════════════════════════════════════════════════════════════

class PositionThesis:
    """
    One thesis per position (stock or crypto).
    Written by the AI when going to sleep.
    Read by the bot every 5-min loop.
    """

    def __init__(self, symbol: str, asset_type: str = "stock"):
        self.symbol       = symbol
        self.asset_type   = asset_type  # "stock" or "crypto"

        # Core thesis
        self.thesis       = ""          # Human-readable thesis statement
        self.entry_price  = 0.0
        self.sleep_price  = 0.0         # Price when AI went to sleep
        self.written_at   = ""          # ISO timestamp

        # Price levels (AI estimates)
        self.support      = []          # List of support prices
        self.resistance   = []          # List of resistance prices

        # AI-written wake conditions
        self.emergency_below  = None    # Price: wake IMMEDIATELY if crossed
        self.bullish_above    = None    # Price: wake if bullish breakout
        self.recovery_above   = None    # Price: wake if recovering
        self.time_review_hrs  = None    # Hours: scheduled review
        self.max_hold_days    = None    # Days: force review

        # Recovery vs invalidation signals (text descriptions)
        self.recovery_signals      = []  # "RSI crosses 45", "MACD turns positive"
        self.invalidation_signals  = []  # "closes below $340 on volume"

        # Bot permissions — what bot can do WITHOUT AI
        self.bot_may_sell    = False    # True only if AI explicitly approves
        self.bot_may_buy     = False
        self.approved_action = None     # "sell_full", "sell_half", "buy", None

        # Price history since AI slept (bot tracks this)
        self.high_since_sleep = None
        self.low_since_sleep  = None
        self.last_price       = None

        # Crypto-specific
        self.flash_crash_pct  = None    # % drop in 30 min = emergency
        self.btc_correlation  = True    # If True: BTC drop triggers wake

        # Circuit breaker (absolute emergency — no AI needed)
        self.circuit_breaker  = None    # Price below which bot logs emergency

    def update_price_history(self, current_price: float):
        """Called every 5-min loop. Tracks high/low since AI slept."""
        self.last_price = current_price
        if self.high_since_sleep is None or current_price > self.high_since_sleep:
            self.high_since_sleep = current_price
        if self.low_since_sleep is None or current_price < self.low_since_sleep:
            self.low_since_sleep  = current_price

    def pnl_pct_from_sleep(self) -> float:
        """P&L % from the price when AI went to sleep."""
        if not self.sleep_price or not self.last_price:
            return 0.0
        return (self.last_price - self.sleep_price) / self.sleep_price * 100

    def hours_since_written(self) -> float:
        """Hours since AI wrote this thesis."""
        if not self.written_at:
            return 0.0
        try:
            written = datetime.fromisoformat(self.written_at)
            return (datetime.now() - written).total_seconds() / 3600
        except Exception:
            return 0.0

    def to_dict(self) -> dict:
        return {
            "symbol":             self.symbol,
            "asset_type":         self.asset_type,
            "thesis":             self.thesis,
            "entry_price":        self.entry_price,
            "sleep_price":        self.sleep_price,
            "written_at":         self.written_at,
            "support":            self.support,
            "resistance":         self.resistance,
            "emergency_below":    self.emergency_below,
            "bullish_above":      self.bullish_above,
            "recovery_above":     self.recovery_above,
            "time_review_hrs":    self.time_review_hrs,
            "max_hold_days":      self.max_hold_days,
            "recovery_signals":   self.recovery_signals,
            "invalidation_signals": self.invalidation_signals,
            "bot_may_sell":       self.bot_may_sell,
            "bot_may_buy":        self.bot_may_buy,
            "approved_action":    self.approved_action,
            "high_since_sleep":   self.high_since_sleep,
            "low_since_sleep":    self.low_since_sleep,
            "last_price":         self.last_price,
            "flash_crash_pct":    self.flash_crash_pct,
            "btc_correlation":    self.btc_correlation,
            "circuit_breaker":    self.circuit_breaker,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PositionThesis":
        t = cls(d["symbol"], d.get("asset_type", "stock"))
        for key, val in d.items():
            if hasattr(t, key):
                setattr(t, key, val)
        return t


# ══════════════════════════════════════════════════════════════════
# PORTFOLIO BRIEF — what AI approves while sleeping
# ══════════════════════════════════════════════════════════════════

class PortfolioBrief:
    """
    Stores the AI's complete portfolio instructions from its last session.
    The bot reads this and executes ONLY what's explicitly approved.
    """

    def __init__(self):
        self.written_at           = ""
        self.portfolio_assessment = ""   # AI's overall view
        self.market_context       = ""   # AI's macro read

        # Stock decisions (keyed by symbol)
        self.stock_decisions      = {}   # {symbol: {"action": ..., "instruction": ...}}
        self.new_stock_entries    = []   # Approved entries to watch for
        self.avoid_sectors        = []

        # Crypto decisions (keyed by symbol)
        self.crypto_decisions     = {}   # {symbol: {"action": ..., "instruction": ...}}
        self.new_crypto_entries   = []   # Approved crypto entries

        # Dust coin assessments
        self.dust_assessments     = {}   # {symbol: "hold" | "sell" | "ignore"}

        # Global wake conditions (across all positions)
        self.global_wake_on      = []    # List of condition strings
        self.scheduled_reviews   = []    # ["8:30am always", "4pm always", ...]

        # Bot permission whitelist
        self.bot_may_execute     = []    # Explicit permission list
        self.bot_may_NOT_execute = []    # Explicit prohibition list

    def to_dict(self) -> dict:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, d: dict) -> "PortfolioBrief":
        b = cls()
        for key, val in d.items():
            if hasattr(b, key):
                setattr(b, key, val)
        return b


# ══════════════════════════════════════════════════════════════════
# THESIS MANAGER — central coordinator
# ══════════════════════════════════════════════════════════════════

class ThesisManager:
    """
    Central coordinator for the AI-led trading architecture.

    One instance at module level in bot_with_proxy.py.
    Persists thesis to disk so Railway restarts don't lose state.
    """

    # ── Flash crash detection window ─────────────────────────────
    FLASH_CRASH_WINDOW_MIN = 30     # 30-minute window
    FLASH_CRASH_DEFAULT_PCT = 8.0   # 8% drop in 30 min = emergency

    def __init__(self):
        self._theses: dict[str, PositionThesis]  = {}   # symbol → PositionThesis
        self._brief:  PortfolioBrief             = PortfolioBrief()
        self._price_snapshots: dict              = {}   # For flash crash detection
        self._triggered_this_session: set        = set()  # Avoid re-waking same condition
        self._load_from_disk()

    # ──────────────────────────────────────────────────────────────
    # SLEEP BRIEF PARSER
    # Called after AI session ends, given AI JSON output
    # ──────────────────────────────────────────────────────────────

    def update_from_sleep_brief(
        self,
        ai_output: dict,
        stock_positions: list,
        crypto_positions: dict,
    ) -> None:
        """
        Parse the AI's sleep brief JSON and update all theses.

        Expected ai_output structure (see build_sleep_brief_prompt for schema):
        {
          "portfolio_assessment": "...",
          "stocks": {
            "TSLA": {
              "action": "HOLD|EXIT|PARTIAL_EXIT",
              "thesis": "...",
              "support": [343, 330],
              "resistance": [365, 375],
              "emergency_below": 340,
              "bullish_above": 370,
              "recovery_signals": ["RSI > 45", "MACD turns positive"],
              "invalidation_signals": ["closes below 340 on volume"],
              "max_hold_days": 7,
              "bot_approved_action": null
            }
          },
          "crypto": {
            "ALGOUSDT": {
              "action": "HOLD",
              "thesis": "...",
              "support": [0.118, 0.112],
              "resistance": [0.130, 0.145],
              "emergency_below": 0.112,
              "bullish_above": 0.135,
              "flash_crash_pct": 8.0,
              "time_review_hrs": 72,
              "bot_approved_action": null
            }
          },
          "new_stock_entries": [{"symbol":"NFLX","condition":"pull back to $95"}],
          "new_crypto_entries": [{"symbol":"SOLUSDT","condition":"BTC stable + >$90"}],
          "dust_assessments": {"AUDIOUSDT": "sell"},
          "bot_may_execute": ["sell TSLA if approved_action set", "..."],
          "bot_may_NOT_execute": ["buy any unapproved asset", "..."]
        }
        """
        now = datetime.now().isoformat()

        # ── Build position price maps ──────────────────────────
        stock_price_map  = {p["symbol"]: float(p.get("current_price", 0))
                            for p in (stock_positions or [])}
        crypto_price_map = {}
        for sym, pos in (crypto_positions or {}).items():
            try:
                crypto_price_map[sym] = float(pos.entry_price) if hasattr(pos, "entry_price") else 0.0
            except Exception:
                pass

        # ── Update portfolio brief ─────────────────────────────
        self._brief.written_at           = now
        self._brief.portfolio_assessment = ai_output.get("portfolio_assessment", "")
        self._brief.market_context       = ai_output.get("market_context", "")
        self._brief.stock_decisions      = ai_output.get("stocks", {})
        self._brief.crypto_decisions     = ai_output.get("crypto", {})
        self._brief.new_stock_entries    = ai_output.get("new_stock_entries", [])
        self._brief.new_crypto_entries   = ai_output.get("new_crypto_entries", [])
        self._brief.dust_assessments     = ai_output.get("dust_assessments", {})
        self._brief.bot_may_execute      = ai_output.get("bot_may_execute", [])
        self._brief.bot_may_NOT_execute  = ai_output.get("bot_may_NOT_execute", [])
        self._brief.global_wake_on       = ai_output.get("global_wake_on", [])
        self._brief.scheduled_reviews    = ai_output.get("scheduled_reviews", [])

        # ── Parse stock theses ─────────────────────────────────
        for symbol, data in ai_output.get("stocks", {}).items():
            t = self._theses.get(symbol) or PositionThesis(symbol, "stock")

            t.written_at            = now
            t.sleep_price           = stock_price_map.get(symbol, t.sleep_price)
            t.entry_price           = data.get("entry_price", t.entry_price)
            t.thesis                = data.get("thesis", "")
            t.support               = data.get("support", [])
            t.resistance            = data.get("resistance", [])
            t.emergency_below       = _safe_float(data.get("emergency_below"))
            t.bullish_above         = _safe_float(data.get("bullish_above"))
            t.recovery_above        = _safe_float(data.get("recovery_above"))
            t.time_review_hrs       = _safe_float(data.get("time_review_hrs"))
            t.max_hold_days         = _safe_float(data.get("max_hold_days"))
            t.recovery_signals      = data.get("recovery_signals", [])
            t.invalidation_signals  = data.get("invalidation_signals", [])
            t.circuit_breaker       = _safe_float(data.get("circuit_breaker"))

            # Bot permissions
            approved = data.get("bot_approved_action")
            t.approved_action = approved
            t.bot_may_sell    = approved in ("sell_full", "sell_half", "sell")
            t.bot_may_buy     = approved in ("buy",)

            # Reset price history for this sleep cycle
            t.high_since_sleep = t.sleep_price
            t.low_since_sleep  = t.sleep_price
            t.last_price       = t.sleep_price

            self._theses[symbol] = t

        # ── Parse crypto theses ────────────────────────────────
        for symbol, data in ai_output.get("crypto", {}).items():
            t = self._theses.get(symbol) or PositionThesis(symbol, "crypto")

            t.written_at           = now
            t.sleep_price          = crypto_price_map.get(symbol, t.sleep_price)
            t.entry_price          = data.get("entry_price", t.entry_price)
            t.thesis               = data.get("thesis", "")
            t.support              = data.get("support", [])
            t.resistance           = data.get("resistance", [])
            t.emergency_below      = _safe_float(data.get("emergency_below"))
            t.bullish_above        = _safe_float(data.get("bullish_above"))
            t.recovery_above       = _safe_float(data.get("recovery_above"))
            t.time_review_hrs      = _safe_float(data.get("time_review_hrs"))
            t.max_hold_days        = _safe_float(data.get("max_hold_days"))
            t.recovery_signals     = data.get("recovery_signals", [])
            t.invalidation_signals = data.get("invalidation_signals", [])
            t.flash_crash_pct      = _safe_float(data.get("flash_crash_pct",
                                                           self.FLASH_CRASH_DEFAULT_PCT))
            t.btc_correlation      = data.get("btc_correlation", True)
            t.circuit_breaker      = _safe_float(data.get("circuit_breaker"))

            approved = data.get("bot_approved_action")
            t.approved_action = approved
            t.bot_may_sell    = approved in ("sell_full", "sell_half", "sell")
            t.bot_may_buy     = approved in ("buy",)

            t.high_since_sleep = t.sleep_price
            t.low_since_sleep  = t.sleep_price
            t.last_price       = t.sleep_price

            self._theses[symbol] = t

        # Reset triggered set for new sleep cycle
        self._triggered_this_session = set()
        self._save_to_disk()

    # ──────────────────────────────────────────────────────────────
    # CONDITION CHECKER — runs every 5-min bot loop
    # ──────────────────────────────────────────────────────────────

    def check_all_conditions(
        self,
        stock_positions: list,
        crypto_positions: dict,
        spy_change_pct: float = 0.0,
        btc_price_now: float  = 0.0,
        btc_price_1h_ago: float = 0.0,
    ) -> tuple[str, str] | None:
        """
        Check all thesis wake conditions.
        Returns (wake_reason, wake_context_summary) if AI should wake.
        Returns None if no conditions triggered.

        Priority:
          1. Emergency (price below emergency_below) → wake IMMEDIATELY
          2. Flash crash (crypto 8% drop in 30 min) → wake IMMEDIATELY
          3. Circuit breaker (>20% drop, system failure) → log + wake
          4. BTC correlation drop → wake crypto AI
          5. Bullish trigger (price above bullish_above) → wake
          6. Recovery signal (price above recovery_above) → wake
          7. Time review (hours since written) → wake
          8. Max hold days → wake
        """
        # ── Update price history for all positions ─────────────
        stock_map  = {p["symbol"]: float(p.get("current_price", 0))
                      for p in (stock_positions or [])}
        crypto_map = {}
        for sym, pos in (crypto_positions or {}).items():
            try:
                from binance_crypto import get_crypto_price
                crypto_map[sym] = get_crypto_price(sym)
            except Exception:
                try:
                    crypto_map[sym] = float(pos.entry_price)
                except Exception:
                    crypto_map[sym] = 0.0

        for symbol, thesis in self._theses.items():
            price = (stock_map.get(symbol)
                     if thesis.asset_type == "stock"
                     else crypto_map.get(symbol))
            if price and price > 0:
                thesis.update_price_history(price)

        self._save_price_snapshots(crypto_map)

        # ── BTC correlation check (crypto-wide) ───────────────
        if btc_price_1h_ago > 0 and btc_price_now > 0:
            btc_drop = (btc_price_now - btc_price_1h_ago) / btc_price_1h_ago * 100
            if btc_drop <= -5.0:
                key = "btc_drop_5pct"
                if key not in self._triggered_this_session:
                    self._triggered_this_session.add(key)
                    ctx = (f"BTC dropped {btc_drop:.1f}% in 1 hour "
                           f"(${btc_price_1h_ago:.0f} → ${btc_price_now:.0f}). "
                           f"Crypto positions may be affected.")
                    return f"⚡ BTC crashed {btc_drop:.1f}% in 1h — crypto emergency", ctx

        # ── Per-position checks ────────────────────────────────
        for symbol, thesis in self._theses.items():
            current = thesis.last_price
            if not current or current <= 0:
                continue

            # 1. CIRCUIT BREAKER (20%+ drop — system failure level)
            if thesis.circuit_breaker and current <= thesis.circuit_breaker:
                key = f"{symbol}_circuit_breaker"
                if key not in self._triggered_this_session:
                    self._triggered_this_session.add(key)
                    drop_pct = (current - thesis.sleep_price) / thesis.sleep_price * 100
                    ctx = self._build_position_context(thesis, current, "CIRCUIT BREAKER")
                    return (f"🚨 CIRCUIT BREAKER — {symbol} hit ${current:.4f} "
                            f"({drop_pct:.1f}% from sleep price)", ctx)

            # 2. EMERGENCY BELOW (AI-set danger level)
            if thesis.emergency_below and current <= thesis.emergency_below:
                key = f"{symbol}_emergency"
                if key not in self._triggered_this_session:
                    self._triggered_this_session.add(key)
                    ctx = self._build_position_context(thesis, current, "EMERGENCY")
                    return (f"🚨 EMERGENCY — {symbol} dropped below AI threshold "
                            f"${thesis.emergency_below:.4f} (now ${current:.4f})", ctx)

            # 3. CRYPTO FLASH CRASH (8%+ drop in 30 min)
            if thesis.asset_type == "crypto" and thesis.flash_crash_pct:
                crash = self._detect_flash_crash(symbol, thesis.flash_crash_pct)
                if crash:
                    key = f"{symbol}_flash_crash"
                    if key not in self._triggered_this_session:
                        self._triggered_this_session.add(key)
                        ctx = self._build_position_context(thesis, current, "FLASH CRASH")
                        return (f"⚡ FLASH CRASH — {symbol} dropped {crash:.1f}% "
                                f"in 30 minutes (now ${current:.6f})", ctx)

            # 4. BULLISH TRIGGER (price broke above resistance)
            if thesis.bullish_above and current >= thesis.bullish_above:
                key = f"{symbol}_bullish"
                if key not in self._triggered_this_session:
                    self._triggered_this_session.add(key)
                    ctx = self._build_position_context(thesis, current, "BULLISH BREAKOUT")
                    return (f"📈 BULLISH TRIGGER — {symbol} broke above "
                            f"${thesis.bullish_above:.4f} (now ${current:.4f})", ctx)

            # 5. RECOVERY WATCH (price recovering from lows)
            if thesis.recovery_above and current >= thesis.recovery_above:
                key = f"{symbol}_recovery"
                if key not in self._triggered_this_session:
                    self._triggered_this_session.add(key)
                    ctx = self._build_position_context(thesis, current, "RECOVERY")
                    return (f"🟢 RECOVERY — {symbol} recovered above "
                            f"${thesis.recovery_above:.4f} (now ${current:.4f})", ctx)

            # 6. TIME REVIEW (scheduled check)
            if thesis.time_review_hrs:
                hrs = thesis.hours_since_written()
                if hrs >= thesis.time_review_hrs:
                    key = f"{symbol}_time_{int(hrs)}"
                    if key not in self._triggered_this_session:
                        self._triggered_this_session.add(key)
                        ctx = self._build_position_context(thesis, current, "SCHEDULED REVIEW")
                        return (f"⏰ TIME REVIEW — {symbol} held {hrs:.0f}h "
                                f"(AI requested review after {thesis.time_review_hrs:.0f}h)", ctx)

            # 7. MAX HOLD DAYS
            if thesis.max_hold_days:
                days = thesis.hours_since_written() / 24
                if days >= thesis.max_hold_days:
                    key = f"{symbol}_maxhold"
                    if key not in self._triggered_this_session:
                        self._triggered_this_session.add(key)
                        ctx = self._build_position_context(thesis, current, "MAX HOLD REACHED")
                        return (f"⏰ MAX HOLD — {symbol} held {days:.1f} days "
                                f"(AI max: {thesis.max_hold_days:.0f} days)", ctx)

        return None

    # ──────────────────────────────────────────────────────────────
    # WAKE CONTEXT BUILDER — rich briefing for AI on wake
    # ──────────────────────────────────────────────────────────────

    def build_wake_context(
        self,
        wake_reason: str,
        stock_positions: list,
        crypto_positions: dict,
        spy_trend: str = "neutral",
        spy_change_pct: float = 0.0,
    ) -> str:
        """
        Build a rich context block for the AI when it wakes.
        AI gets: why it woke, what happened since sleep, current state.
        """
        lines = []
        lines.append("═══════════════════════════════════════════")
        lines.append("  🌅 AI WAKE CONTEXT (NovaTrade v3.0)")
        lines.append("═══════════════════════════════════════════")
        lines.append(f"WAKE REASON: {wake_reason}")
        lines.append(f"TIME: {datetime.now(ZoneInfo('America/New_York')).strftime('%Y-%m-%d %H:%M ET')}")
        lines.append(f"SPY: {spy_trend.upper()} ({spy_change_pct:+.1f}% today)")
        lines.append("")

        # ── What happened to each position since you slept ────
        lines.append("POSITION REPORT SINCE YOU SLEPT:")
        lines.append("─" * 43)

        stock_map  = {p["symbol"]: p for p in (stock_positions or [])}

        for symbol, thesis in self._theses.items():
            current = thesis.last_price
            if not current or current <= 0:
                continue

            pct_from_sleep = thesis.pnl_pct_from_sleep()
            high           = thesis.high_since_sleep
            low            = thesis.low_since_sleep
            hrs_slept      = thesis.hours_since_written()
            asset          = thesis.asset_type.upper()

            lines.append(f"\n[{asset}] {symbol}:")
            lines.append(f"  You slept at:  ${thesis.sleep_price:.4f}")
            lines.append(f"  Now:           ${current:.4f} ({pct_from_sleep:+.1f}% since sleep)")
            if high and low:
                lines.append(f"  Range while sleeping: ${low:.4f} – ${high:.4f}")
            lines.append(f"  Slept for:     {hrs_slept:.1f} hours")
            lines.append(f"  Your thesis:   {thesis.thesis[:120]}")

            if thesis.emergency_below and current <= thesis.emergency_below:
                lines.append(f"  ⚠️  BELOW your emergency level ${thesis.emergency_below:.4f}")
            if thesis.bullish_above and current >= thesis.bullish_above:
                lines.append(f"  📈 ABOVE your bullish trigger ${thesis.bullish_above:.4f}")

            # Show live Alpaca data for stocks
            if thesis.asset_type == "stock" and symbol in stock_map:
                pos = stock_map[symbol]
                pnl_from_entry = float(pos.get("unrealized_plpc", 0)) * 100
                pnl_usd = float(pos.get("unrealized_pl", 0))
                lines.append(f"  P&L from entry: {pnl_from_entry:+.2f}% (${pnl_usd:+.2f})")

        # ── Portfolio brief reminder ───────────────────────────
        if self._brief.portfolio_assessment:
            lines.append("")
            lines.append("WHAT YOU SAID BEFORE SLEEPING:")
            lines.append("─" * 43)
            lines.append(self._brief.portfolio_assessment[:300])

        lines.append("")
        lines.append("YOUR TASK NOW:")
        lines.append("─" * 43)
        lines.append("1. Review each position listed above")
        lines.append("2. Decide: EXIT / HOLD / PARTIAL / ADJUST STOP")
        lines.append("3. If holding: write NEW sleep brief with updated wake conditions")
        lines.append("4. If exiting: write exact instructions for the bot")
        lines.append("5. Scan for new opportunities if cash is available")
        lines.append("")
        lines.append("Output full portfolio brief JSON (see schema in system prompt).")

        return "\n".join(lines)

    # ──────────────────────────────────────────────────────────────
    # WALLET SNAPSHOT — full picture for AI every session
    # ──────────────────────────────────────────────────────────────

    def build_full_wallet_snapshot(
        self,
        stock_positions:  list,
        stock_equity:     float,
        stock_cash:       float,
        crypto_positions: dict,
        crypto_wallet:    float,
        usdt_free:        float,
        wallet_holdings:  list,
        day_pnl_stock:    float = 0.0,
        day_pnl_crypto:   float = 0.0,
    ) -> str:
        """
        Build the complete wallet picture for every AI wake.
        AI sees EVERYTHING: positions, dust coins, cash, thesis state.
        """
        lines = []
        lines.append("═══════════════════════════════════════════")
        lines.append("  💼 COMPLETE PORTFOLIO SNAPSHOT")
        lines.append("═══════════════════════════════════════════")

        # ── STOCKS ────────────────────────────────────────────
        lines.append(f"\n📈 STOCKS:")
        lines.append(f"  Equity: ${stock_equity:.2f} | Cash: ${stock_cash:.2f}")
        lines.append(f"  Day P&L: ${day_pnl_stock:+.2f}")

        if stock_positions:
            for pos in stock_positions:
                sym   = pos["symbol"]
                pnl   = float(pos.get("unrealized_plpc", 0)) * 100
                curr  = float(pos.get("current_price", 0))
                entry = float(pos.get("avg_entry_price", 0))
                val   = float(pos.get("market_value", 0))
                icon  = "🟢" if pnl >= 0 else "🔴"
                t     = self._theses.get(sym)
                held  = f"{t.hours_since_written():.0f}h" if t else "?"
                thesis_note = f"\n    Thesis: {t.thesis[:80]}" if t and t.thesis else ""
                lines.append(
                    f"  {icon} {sym}: entry=${entry:.2f} → now=${curr:.2f} "
                    f"P&L={pnl:+.2f}% val=${val:.2f} held={held}"
                    f"{thesis_note}"
                )
                if t:
                    if t.emergency_below:
                        lines.append(f"    Emergency level: ${t.emergency_below:.2f} "
                                     f"({'⚠️ BREACHED' if curr <= t.emergency_below else '✅ safe'})")
                    if t.support:
                        lines.append(f"    Support: {[f'${x}' for x in t.support[:3]]}")
        else:
            lines.append("  No stock positions")

        # ── CRYPTO ───────────────────────────────────────────
        lines.append(f"\n🪙 CRYPTO (Binance.US):")
        lines.append(f"  Wallet: ${crypto_wallet:.2f} | USDT free: ${usdt_free:.2f}")
        lines.append(f"  Reserve: $10.00 (minimum) | Deployable: ${max(0, usdt_free-10):.2f}")
        lines.append(f"  Day P&L: ${day_pnl_crypto:+.2f}")

        if crypto_positions:
            for sym, pos in crypto_positions.items():
                try:
                    entry = float(pos.entry_price)
                    curr  = float(pos.last_price) if hasattr(pos, "last_price") else entry
                    pnl   = (curr - entry) / entry * 100 if entry else 0
                    qty   = float(pos.qty) if hasattr(pos, "qty") else 0
                    val   = curr * qty
                    hrs   = float(pos.hours_held()) if hasattr(pos, "hours_held") else 0
                    stop  = float(pos.stop_price) if hasattr(pos, "stop_price") else 0
                    tp    = float(pos.tp_price) if hasattr(pos, "tp_price") else 0
                    icon  = "🟢" if pnl >= 0 else "🔴"
                    t     = self._theses.get(sym)
                    thesis_note = f"\n    Thesis: {t.thesis[:80]}" if t and t.thesis else ""
                    lines.append(
                        f"  {icon} {sym}: entry=${entry:.6f} → now=${curr:.6f} "
                        f"P&L={pnl:+.2f}% val=${val:.2f} held={hrs:.0f}h"
                        f"\n    Stop=${stop:.6f} | TP=${tp:.6f}"
                        f"{thesis_note}"
                    )
                    if t and t.emergency_below:
                        lines.append(f"    Emergency level: ${t.emergency_below:.6f} "
                                     f"({'⚠️ BREACHED' if curr <= t.emergency_below else '✅ safe'})")
                except Exception:
                    lines.append(f"  ⚠️ {sym}: error reading position data")
        else:
            lines.append("  No active crypto positions")

        # ── DUST COINS (wallet holdings) ─────────────────────
        dust = [(h["symbol"], h["value"], h["price"])
                for h in (wallet_holdings or [])
                if h.get("value", 0) >= 0.50 and h["symbol"] != "USDT"]

        if dust:
            lines.append(f"\n👤 WALLET HOLDINGS (assess for opportunity or cleanup):")
            for sym, val, price in sorted(dust, key=lambda x: -x[1]):
                assessment = self._brief.dust_assessments.get(sym, "unreviewed")
                flag = "🔍 ASSESS" if val >= 1.50 else "💤 dust"
                lines.append(f"  {flag} {sym}: ${val:.2f} @ ${price:.6f} "
                             f"[previous assessment: {assessment}]")

        # ── AI PREVIOUS BRIEF SUMMARY ─────────────────────────
        if self._brief.portfolio_assessment:
            lines.append(f"\n📋 LAST BRIEF (written {self._brief.written_at[:16]}):")
            lines.append(f"  {self._brief.portfolio_assessment[:200]}")

        lines.append("\n═══════════════════════════════════════════")
        lines.append("ANALYZE EACH POSITION — provide full portfolio brief JSON")
        lines.append("═══════════════════════════════════════════")

        return "\n".join(lines)

    # ──────────────────────────────────────────────────────────────
    # BOT PERMISSION CHECK
    # ──────────────────────────────────────────────────────────────

    def bot_may_sell(self, symbol: str) -> tuple[bool, str]:
        """
        Returns (allowed, reason).
        Bot checks this before executing any sell while AIs sleep.
        """
        t = self._theses.get(symbol)
        if not t:
            return False, f"no thesis for {symbol} — AI approval required"

        if t.bot_may_sell:
            return True, f"AI approved: {t.approved_action}"

        # Absolute circuit breaker — bot MUST act even without approval
        if t.circuit_breaker and t.last_price and t.last_price <= t.circuit_breaker:
            return True, f"CIRCUIT BREAKER — price ${t.last_price:.4f} below ${t.circuit_breaker:.4f}"

        return False, f"AI approval required — thesis: {t.thesis[:60]}"

    def bot_may_buy(self, symbol: str) -> tuple[bool, str]:
        """Returns (allowed, reason)."""
        t = self._theses.get(symbol)
        if not t:
            return False, f"no thesis for {symbol} — AI approval required"
        if t.bot_may_buy:
            return True, f"AI approved: {t.approved_action}"
        return False, "AI approval required"

    def get_approved_watchlist_entries(self) -> list:
        """Return list of new stock entries AI approved while sleeping."""
        return self._brief.new_stock_entries or []

    def get_approved_crypto_entries(self) -> list:
        """Return list of new crypto entries AI approved while sleeping."""
        return self._brief.new_crypto_entries or []

    def get_dust_instruction(self, symbol: str) -> str:
        """Return AI's instruction for a dust coin: 'sell', 'hold', 'ignore'."""
        return self._brief.dust_assessments.get(symbol, "ignore")

    # ──────────────────────────────────────────────────────────────
    # GETTERS
    # ──────────────────────────────────────────────────────────────

    def get_thesis(self, symbol: str) -> PositionThesis | None:
        return self._theses.get(symbol)

    def get_brief(self) -> PortfolioBrief:
        return self._brief

    def get_all_theses(self) -> dict:
        return dict(self._theses)

    def clear_thesis(self, symbol: str) -> None:
        """Call when position closes."""
        self._theses.pop(symbol, None)
        self._save_to_disk()

    def reset_triggered_session(self) -> None:
        """Reset triggered conditions — call when AIs wake (new session)."""
        self._triggered_this_session = set()

    # ──────────────────────────────────────────────────────────────
    # PRIVATE HELPERS
    # ──────────────────────────────────────────────────────────────

    def _build_position_context(
        self, thesis: PositionThesis, current: float, trigger_type: str
    ) -> str:
        """Build context string for a triggered condition."""
        pct = thesis.pnl_pct_from_sleep()
        lines = [
            f"TRIGGER: {trigger_type} — {thesis.symbol}",
            f"  Current price: ${current:.6f}",
            f"  Sleep price:   ${thesis.sleep_price:.6f} ({pct:+.1f}% since sleep)",
        ]
        if thesis.high_since_sleep:
            lines.append(f"  High since sleep: ${thesis.high_since_sleep:.6f}")
        if thesis.low_since_sleep:
            lines.append(f"  Low since sleep:  ${thesis.low_since_sleep:.6f}")
        lines.append(f"  Thesis: {thesis.thesis[:100]}")
        if thesis.emergency_below:
            lines.append(f"  Emergency level: ${thesis.emergency_below:.6f}")
        if thesis.invalidation_signals:
            lines.append(f"  Invalidation signals: {', '.join(thesis.invalidation_signals[:2])}")
        return "\n".join(lines)

    def _save_price_snapshots(self, crypto_map: dict) -> None:
        """Save timestamped prices for flash crash detection (30-min window)."""
        now     = datetime.now(timezone.utc)
        cutoff  = now - timedelta(minutes=self.FLASH_CRASH_WINDOW_MIN)

        for symbol, price in crypto_map.items():
            if symbol not in self._price_snapshots:
                self._price_snapshots[symbol] = []
            self._price_snapshots[symbol].append((now, price))
            # Prune old snapshots outside window
            self._price_snapshots[symbol] = [
                (t, p) for t, p in self._price_snapshots[symbol]
                if t >= cutoff
            ]

    def _detect_flash_crash(self, symbol: str, threshold_pct: float) -> float | None:
        """
        Detect if price dropped threshold_pct% within FLASH_CRASH_WINDOW_MIN.
        Returns drop % if flash crash detected, None otherwise.
        """
        snaps = self._price_snapshots.get(symbol, [])
        if len(snaps) < 2:
            return None

        current_price  = snaps[-1][1]
        highest_recent = max(p for _, p in snaps)
        if highest_recent <= 0:
            return None

        drop_pct = (current_price - highest_recent) / highest_recent * 100
        if drop_pct <= -abs(threshold_pct):
            return abs(drop_pct)
        return None

    def _save_to_disk(self) -> None:
        """Persist thesis to disk so Railway restarts don't lose state."""
        try:
            data = {
                "theses": {sym: t.to_dict() for sym, t in self._theses.items()},
                "brief":  self._brief.to_dict(),
                "saved_at": datetime.now().isoformat(),
            }
            with open(THESIS_PATH, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            pass  # Never crash on disk write failure

    def _load_from_disk(self) -> None:
        """Load persisted thesis on startup."""
        try:
            if not os.path.exists(THESIS_PATH):
                return
            with open(THESIS_PATH) as f:
                data = json.load(f)

            for sym, td in data.get("theses", {}).items():
                self._theses[sym] = PositionThesis.from_dict(td)

            brief_data = data.get("brief", {})
            if brief_data:
                self._brief = PortfolioBrief.from_dict(brief_data)

        except Exception:
            pass  # Fresh start if file is corrupt


# ══════════════════════════════════════════════════════════════════
# SLEEP BRIEF PROMPT BUILDER
# ══════════════════════════════════════════════════════════════════

def build_sleep_brief_prompt(
    stock_positions:  list,
    stock_equity:     float,
    stock_cash:       float,
    crypto_positions: dict,
    crypto_wallet:    float,
    usdt_free:        float,
    wallet_holdings:  list,
    spy_trend:        str  = "neutral",
    spy_change_pct:   float = 0.0,
    grok_intel:       str  = "",
    day_pnl_stock:    float = 0.0,
    day_pnl_crypto:   float = 0.0,
) -> str:
    """
    Build the prompt asking the AI to write its sleep brief.

    This is called BEFORE ai_sleep() — AI writes thesis for each position,
    sets custom wake conditions, and specifies what bot may/may not do.

    Returns: prompt string → send to Claude Haiku
    Output:  JSON sleep brief → pass to thesis_mgr.update_from_sleep_brief()
    """

    # ── Format stock positions ─────────────────────────────────
    stock_lines = []
    for pos in (stock_positions or []):
        sym   = pos["symbol"]
        pnl   = float(pos.get("unrealized_plpc", 0)) * 100
        curr  = float(pos.get("current_price", 0))
        entry = float(pos.get("avg_entry_price", 0))
        val   = float(pos.get("market_value", 0))
        icon  = "🟢" if pnl >= 0 else "🔴"
        stock_lines.append(
            f'    "{sym}": {{'
            f'"current": {curr:.2f}, "entry": {entry:.2f}, '
            f'"pnl_pct": {pnl:.2f}, "val": {val:.2f}'
            f'}}'
        )

    # ── Format crypto positions ────────────────────────────────
    crypto_lines = []
    for sym, pos in (crypto_positions or {}).items():
        try:
            entry = float(pos.entry_price)
            curr  = float(pos.last_price) if hasattr(pos, "last_price") else entry
            pnl   = (curr - entry) / entry * 100 if entry else 0
            stop  = float(pos.stop_price) if hasattr(pos, "stop_price") else 0
            tp    = float(pos.tp_price) if hasattr(pos, "tp_price") else 0
            qty   = float(pos.qty) if hasattr(pos, "qty") else 0
            val   = curr * qty
            crypto_lines.append(
                f'    "{sym}": {{'
                f'"current": {curr:.6f}, "entry": {entry:.6f}, '
                f'"pnl_pct": {pnl:.2f}, "val": {val:.2f}, '
                f'"stop": {stop:.6f}, "tp": {tp:.6f}'
                f'}}'
            )
        except Exception:
            pass

    # ── Format dust holdings ───────────────────────────────────
    dust_lines = []
    for h in (wallet_holdings or []):
        if h.get("value", 0) >= 0.50 and h["symbol"] != "USDT":
            dust_lines.append(
                f'    "{h["symbol"]}": {{"value": {h["value"]:.2f}, "price": {h["price"]:.6f}}}'
            )

    stock_json  = "{\n" + ",\n".join(stock_lines)  + "\n  }"  if stock_lines  else "{}"
    crypto_json = "{\n" + ",\n".join(crypto_lines) + "\n  }"  if crypto_lines else "{}"
    dust_json   = "{\n" + ",\n".join(dust_lines)   + "\n  }"  if dust_lines   else "{}"

    prompt = f"""You are going to sleep. The bot will monitor all positions autonomously.
Write a complete sleep brief so the bot knows EXACTLY when to wake you and what to do.

CURRENT PORTFOLIO STATE:
SPY: {spy_trend.upper()} ({spy_change_pct:+.1f}% today)
Stock equity: ${stock_equity:.2f} | Cash: ${stock_cash:.2f} | Day P&L: ${day_pnl_stock:+.2f}
Crypto wallet: ${crypto_wallet:.2f} | USDT free: ${usdt_free:.2f} | Day P&L: ${day_pnl_crypto:+.2f}

STOCK POSITIONS:
{stock_json}

CRYPTO POSITIONS:
{crypto_json}

WALLET DUST HOLDINGS (assess each — sell to USDT or hold?):
{dust_json}

GROK INTEL (latest news/sentiment):
{grok_intel[:500] if grok_intel else "Not available this cycle"}

WRITE A COMPLETE SLEEP BRIEF. Output ONLY valid JSON, no prose, no markdown:

{{
  "portfolio_assessment": "<2-3 sentence overall view of portfolio right now>",
  "market_context": "<1 sentence on macro/SPY/tariff situation>",
  "stocks": {{
    "<SYMBOL>": {{
      "action": "<HOLD|EXIT|PARTIAL_EXIT|ADJUST>",
      "thesis": "<why you hold or exit — max 15 words>",
      "entry_price": <float>,
      "support": [<price1>, <price2>],
      "resistance": [<price1>, <price2>],
      "emergency_below": <price or null — wake AI IMMEDIATELY if crossed>,
      "bullish_above": <price or null — wake AI if bullish break>,
      "recovery_above": <price or null — wake if recovering>,
      "time_review_hrs": <hours or null — scheduled check>,
      "max_hold_days": <days or null>,
      "recovery_signals": ["<signal1>", "<signal2>"],
      "invalidation_signals": ["<signal1>"],
      "circuit_breaker": <price 20% below entry or null>,
      "bot_approved_action": <"sell_full"|"sell_half"|"buy"|null>
    }}
  }},
  "crypto": {{
    "<SYMBOL>": {{
      "action": "<HOLD|EXIT|PARTIAL_EXIT>",
      "thesis": "<max 15 words>",
      "entry_price": <float>,
      "support": [<price1>, <price2>],
      "resistance": [<price1>, <price2>],
      "emergency_below": <price or null>,
      "bullish_above": <price or null>,
      "recovery_above": <price or null>,
      "time_review_hrs": <hours or null>,
      "flash_crash_pct": 8.0,
      "btc_correlation": true,
      "circuit_breaker": <price or null>,
      "bot_approved_action": <null — crypto exits require AI approval>
    }}
  }},
  "dust_assessments": {{
    "<SYMBOL>": "<sell|hold|ignore>"
  }},
  "new_stock_entries": [
    {{"symbol": "<TICK>", "condition": "<when to buy>", "target_price": <float>}}
  ],
  "new_crypto_entries": [
    {{"symbol": "<SYMBOL>", "condition": "<when to buy>", "notional_usdt": <float>}}
  ],
  "bot_may_execute": [
    "<explicit permission 1>",
    "<explicit permission 2>"
  ],
  "bot_may_NOT_execute": [
    "sell any position not listed above",
    "buy any asset not in new_entries"
  ],
  "global_wake_on": [
    "<condition string 1>",
    "<condition string 2>"
  ]
}}

RULES FOR WRITING GOOD WAKE CONDITIONS:
- emergency_below: price where thesis is CLEARLY broken — not just uncomfortable
- For stocks: set emergency 5-8% below current price (not at stop-loss level)
- For crypto: set emergency at strong support level, not at stop
- bullish_above: price that would confirm recovery thesis
- time_review_hrs: for crypto 24-48h, for stocks 24-72h during market hours
- bot_approved_action: ONLY set this if you want the bot to execute without you
- For crypto: NEVER set bot_approved_action — always require AI on crypto exits
- circuit_breaker: 20%+ drop from entry = system failure, bot must log emergency
"""
    return prompt


# ══════════════════════════════════════════════════════════════════
# PORTFOLIO ANALYSIS PROMPT BUILDER
# ══════════════════════════════════════════════════════════════════

def build_portfolio_analysis_prompt(
    wallet_snapshot:  str,
    wake_context:     str,
    spy_trend:        str,
    spy_change_pct:   float,
    grok_intel:       str,
    chart_data:       str,
    available_cash:   float,
    usdt_deployable:  float,
) -> str:
    """
    Build the full portfolio analysis prompt for AI on wake.
    Combines wallet snapshot + wake context + market data.
    AI outputs complete portfolio brief JSON.
    """

    prompt = f"""{wake_context}

{wallet_snapshot}

MARKET DATA:
SPY: {spy_trend.upper()} ({spy_change_pct:+.1f}% today)
Technical indicators: {chart_data[:400] if chart_data else "Not available"}
Grok live intel: {grok_intel[:400] if grok_intel else "Not available"}

CAPITAL AVAILABLE:
  Stock cash: ${available_cash:.2f}
  Crypto USDT deployable: ${usdt_deployable:.2f} (above $10 reserve)

ANALYZE THE FULL PORTFOLIO AND OUTPUT SLEEP BRIEF JSON.

Answer these questions for EVERY position:
1. EXIT: Is the thesis still valid? Should I cut losses/take profits?
2. HOLD: If holding, what are my exact wake conditions?
3. ROTATE: Is there a better trade to rotate into?
4. DEPLOY: Should available cash/USDT be deployed now or wait?
5. DUST: Which dust coins should be sold to free up USDT?

IMPORTANT RULES:
- For crypto exits: ALWAYS require AI confirmation (bot_approved_action: null)
- For stocks: only approve bot action if exit is CLEAR (e.g. definitive stop hit)
- Set emergency levels that are MEANINGFUL, not just uncomfortable
- Write thesis in plain English — the bot will log it for you to review
- If unsure: set time_review_hrs: 24 and let the bot wake you for review

Output ONLY valid JSON sleep brief (same schema as before).
"""
    return prompt


# ══════════════════════════════════════════════════════════════════
# UTILITY FUNCTIONS
# ══════════════════════════════════════════════════════════════════

def _safe_float(val) -> float | None:
    """Convert value to float, return None if not possible."""
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def parse_sleep_brief(raw_response: str) -> dict | None:
    """
    Parse AI sleep brief response into dict.
    Handles markdown fences and trailing commas.
    Returns None if parsing fails.
    """
    import re
    try:
        # Strip markdown fences
        clean = re.sub(r'```(?:json)?\s*', '', str(raw_response))
        clean = clean.replace('```', '').strip()
        # Remove trailing commas before } or ]
        clean = re.sub(r',\s*([}\]])', r'\1', clean)
        # Find outermost JSON object
        s = clean.find('{')
        e = clean.rfind('}') + 1
        if s < 0 or e <= s:
            return None
        return json.loads(clean[s:e])
    except Exception:
        return None
