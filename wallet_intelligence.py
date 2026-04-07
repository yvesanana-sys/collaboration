"""
wallet_intelligence.py
══════════════════════════════════════════════════════════════════
NovaTrade v3.0 — Unified Wallet Intelligence & Opportunity Scanner

PURPOSE:
  Reads ALL assets (stocks + crypto) in real-time and builds a
  unified opportunity picture for the AI every session.

  The AI sees:
    1. Complete stock portfolio: positions, cash, P&L, indicators
    2. Complete crypto wallet: positions, dust coins, USDT, staking
    3. Cross-portfolio risk: total exposure, correlation, concentration
    4. Live opportunities: what to buy NOW given current signals
    5. Rotation candidates: what to sell to fund a better trade
    6. Dust coin decisions: what small holdings to clean up

INTEGRATION:
  from wallet_intelligence import WalletIntelligence
  wallet_intel = WalletIntelligence()   # module-level

  # In every AI session:
  snapshot = wallet_intel.read_full_portfolio(
      alpaca_fn, crypto_trader, indicators_fn, projections
  )
  prompt_section = wallet_intel.build_ai_prompt_section(snapshot)

ZERO BREAKING CHANGES — additive only.
"""

import math
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


# ══════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════

# Minimum values to show/analyze (ignore sub-cent dust)
MIN_SHOW_VALUE_USD    = 0.10   # Show any holding worth more than this
MIN_ANALYZE_VALUE_USD = 1.00   # Analyze holdings worth more than this
MIN_OPPORTUNITY_USD   = 0.50   # Min deployable to flag an opportunity

# Correlation thresholds for risk warnings
STOCK_CRYPTO_CORR_THRESHOLD = 0.70   # If both sides are this correlated, warn

# Rotation trigger: if a position is this much worse than best opportunity
ROTATION_SCORE_GAP = 15   # points


# ══════════════════════════════════════════════════════════════════
# PORTFOLIO SNAPSHOT DATA STRUCTURES
# ══════════════════════════════════════════════════════════════════

class StockPosition:
    """A single stock position with full technical context."""

    def __init__(self, symbol, current_price, entry_price,
                 qty, market_value, pnl_pct, pnl_usd,
                 strategy="A", owner="unknown",
                 indicators=None, projection=None,
                 held_days=0):
        self.symbol        = symbol
        self.current_price = current_price
        self.entry_price   = entry_price
        self.qty           = qty
        self.market_value  = market_value
        self.pnl_pct       = pnl_pct        # e.g. -0.0805 = -8.05%
        self.pnl_usd       = pnl_usd
        self.strategy      = strategy
        self.owner         = owner
        self.indicators    = indicators or {}
        self.projection    = projection or {}
        self.held_days     = held_days

    @property
    def pnl_pct_display(self) -> float:
        return round(self.pnl_pct * 100, 2)

    @property
    def rsi(self) -> float:
        return self.indicators.get("rsi", 50.0)

    @property
    def macd_signal(self) -> str:
        """Returns 'bullish', 'bearish', or 'neutral'."""
        macd = self.indicators.get("macd", 0)
        sig  = self.indicators.get("macd_signal", 0)
        if macd > sig:
            return "bullish"
        elif macd < sig:
            return "bearish"
        return "neutral"

    @property
    def obv_trend(self) -> str:
        obv = self.indicators.get("obv_trend", "neutral")
        return str(obv)

    @property
    def at_support(self) -> bool:
        """Price near projection low (support zone)."""
        pl = self.projection.get("proj_low", 0)
        if not pl or not self.current_price:
            return False
        return self.current_price <= pl * 1.02

    @property
    def at_resistance(self) -> bool:
        """Price near projection high (resistance zone)."""
        ph = self.projection.get("proj_high", 0)
        if not ph or not self.current_price:
            return False
        return self.current_price >= ph * 0.98

    @property
    def upside_to_target(self) -> float:
        """% upside from current price to projection high."""
        ph = self.projection.get("proj_high", 0)
        if not ph or not self.current_price:
            return 0.0
        return (ph - self.current_price) / self.current_price * 100

    def opportunity_score(self) -> int:
        """
        Score this position as a BUY opportunity (0-100).
        Used for rotation decisions: sell low-score, buy high-score.
        """
        score = 50  # Base score

        # P&L adjustment
        if self.pnl_pct_display >= 0:
            score += min(10, self.pnl_pct_display)
        else:
            score += max(-20, self.pnl_pct_display)

        # Technical signals
        if self.rsi < 30:     score += 15   # Oversold = buy opportunity
        elif self.rsi < 40:   score += 8
        elif self.rsi > 70:   score -= 15   # Overbought = avoid
        elif self.rsi > 60:   score -= 5

        if self.macd_signal == "bullish":  score += 10
        elif self.macd_signal == "bearish": score -= 10

        if self.obv_trend == "rising":  score += 8
        elif self.obv_trend == "falling": score -= 8

        if self.at_support:     score += 12
        if self.at_resistance:  score -= 12

        # Upside remaining
        upside = self.upside_to_target
        if upside >= 15:   score += 10
        elif upside >= 8:  score += 5
        elif upside <= 2:  score -= 10

        return max(0, min(100, score))

    def to_dict(self) -> dict:
        return {
            "symbol":        self.symbol,
            "current_price": self.current_price,
            "entry_price":   self.entry_price,
            "qty":           self.qty,
            "market_value":  self.market_value,
            "pnl_pct":       self.pnl_pct_display,
            "pnl_usd":       self.pnl_usd,
            "strategy":      self.strategy,
            "owner":         self.owner,
            "rsi":           self.rsi,
            "macd":          self.macd_signal,
            "obv":           self.obv_trend,
            "at_support":    self.at_support,
            "at_resistance": self.at_resistance,
            "upside_pct":    self.upside_to_target,
            "opportunity_score": self.opportunity_score(),
            "held_days":     self.held_days,
        }


class CryptoHolding:
    """A crypto holding — either a bot-tracked position or a wallet coin."""

    def __init__(self, symbol, asset, qty, free_qty, price,
                 value_usd, is_position=False,
                 entry_price=0.0, stop_price=0.0, tp_price=0.0,
                 pnl_pct=0.0, hours_held=0.0,
                 projection=None, in_universe=True):
        self.symbol      = symbol        # e.g. "ALGOUSDT"
        self.asset       = asset         # e.g. "ALGO"
        self.qty         = qty           # total qty
        self.free_qty    = free_qty      # tradeable qty
        self.price       = price
        self.value_usd   = value_usd
        self.is_position = is_position   # True = bot-tracked, has stop/tp
        self.entry_price = entry_price
        self.stop_price  = stop_price
        self.tp_price    = tp_price
        self.pnl_pct     = pnl_pct
        self.hours_held  = hours_held
        self.projection  = projection or {}
        self.in_universe = in_universe

    @property
    def upside_to_target(self) -> float:
        ph = self.projection.get("proj_high", 0)
        if not ph or not self.price:
            return 0.0
        return (ph - self.price) / self.price * 100

    @property
    def is_dust(self) -> bool:
        return self.value_usd < MIN_ANALYZE_VALUE_USD

    @property
    def is_sellable(self) -> bool:
        """Can this be sold? Must have free qty and be in universe."""
        return self.free_qty > 0 and self.in_universe and self.value_usd >= 1.0

    def rotation_score(self) -> int:
        """
        Score this holding for rotation: low score = sell candidate.
        High score = keep / buy more.
        Range: 0-100.
        """
        score = 50

        # P&L (if position)
        if self.is_position:
            if self.pnl_pct >= 10:   score += 15   # Already profitable — hold
            elif self.pnl_pct >= 5:  score += 8
            elif self.pnl_pct <= -5: score -= 10   # Losing — consider rotate
            elif self.pnl_pct <= -10: score -= 20

        # Upside remaining
        upside = self.upside_to_target
        if upside >= 20:   score += 15
        elif upside >= 10: score += 8
        elif upside <= 3:  score -= 10

        # Projection confidence
        conf = self.projection.get("confidence", 0)
        if conf >= 70:   score += 10
        elif conf >= 50: score += 5
        elif conf < 30:  score -= 10

        # Projection bias
        bias = self.projection.get("bias", "neutral")
        if bias == "bullish":  score += 8
        elif bias == "bearish": score -= 15

        return max(0, min(100, score))

    def to_dict(self) -> dict:
        return {
            "symbol":         self.symbol,
            "asset":          self.asset,
            "qty":            self.qty,
            "free_qty":       self.free_qty,
            "price":          self.price,
            "value_usd":      self.value_usd,
            "is_position":    self.is_position,
            "is_dust":        self.is_dust,
            "is_sellable":    self.is_sellable,
            "pnl_pct":        self.pnl_pct,
            "hours_held":     self.hours_held,
            "entry_price":    self.entry_price,
            "stop_price":     self.stop_price,
            "tp_price":       self.tp_price,
            "upside_pct":     self.upside_to_target,
            "rotation_score": self.rotation_score(),
            "in_universe":    self.in_universe,
        }


class PortfolioSnapshot:
    """Complete real-time snapshot of all assets."""

    def __init__(self):
        # Metadata
        self.timestamp     = datetime.now(ZoneInfo("America/New_York")).isoformat()
        self.market_open   = False

        # Stocks
        self.stock_positions:   list[StockPosition] = []
        self.stock_equity       = 0.0
        self.stock_cash         = 0.0
        self.stock_day_pnl      = 0.0
        self.stock_day_pnl_pct  = 0.0

        # Crypto
        self.crypto_positions:  list[CryptoHolding] = []   # Bot-tracked
        self.crypto_holdings:   list[CryptoHolding] = []   # All wallet coins
        self.crypto_wallet      = 0.0   # Total wallet value
        self.usdt_free          = 0.0
        self.usdt_reserve       = 10.0  # Always keep $10
        self.usdt_deployable    = 0.0   # usdt_free - reserve
        self.crypto_day_pnl     = 0.0

        # Combined
        self.total_value        = 0.0
        self.total_day_pnl      = 0.0

        # Opportunities
        self.stock_opportunities:  list[dict] = []   # Ranked buy candidates
        self.crypto_opportunities: list[dict] = []   # Ranked buy candidates
        self.rotation_candidates:  list[dict] = []   # Positions to rotate out of
        self.dust_to_clean:        list[dict] = []   # Dust coins to sell

        # Risk
        self.risk_warnings: list[str] = []
        self.concentration_pct = 0.0   # % in single largest position

        # Errors
        self.errors: list[str] = []

    @property
    def has_stock_positions(self) -> bool:
        return len(self.stock_positions) > 0

    @property
    def has_crypto_positions(self) -> bool:
        return len(self.crypto_positions) > 0

    @property
    def stock_deployable(self) -> float:
        """Cash available for stock buys."""
        return self.stock_cash

    @property
    def total_deployable(self) -> float:
        return self.stock_deployable + self.usdt_deployable


# ══════════════════════════════════════════════════════════════════
# WALLET INTELLIGENCE ENGINE
# ══════════════════════════════════════════════════════════════════

class WalletIntelligence:
    """
    Reads all portfolio assets in real-time.
    Builds unified opportunity picture for AI.
    """

    def __init__(self):
        self._last_snapshot: PortfolioSnapshot | None = None

    # ──────────────────────────────────────────────────────────────
    # MAIN ENTRY POINT — read everything
    # ──────────────────────────────────────────────────────────────

    def read_full_portfolio(
        self,
        alpaca_fn,           # alpaca() function from bot_with_proxy
        crypto_trader,       # CryptoTrader instance
        get_bars_fn,         # get_bars() function
        compute_ind_fn,      # compute_indicators() function
        projections:  dict,  # shared_state["last_projections"]
        shared_state: dict,  # full shared_state dict
    ) -> PortfolioSnapshot:
        """
        Read complete portfolio in real-time from all sources.
        Returns PortfolioSnapshot with everything the AI needs.
        """
        snap = PortfolioSnapshot()

        # ── 1. Read Alpaca account ─────────────────────────────
        try:
            acct = alpaca_fn("GET", "/v2/account")
            snap.stock_equity   = float(acct["equity"])
            snap.stock_cash     = float(acct["cash"])
            day_start           = shared_state.get("day_start_equity", snap.stock_equity)
            snap.stock_day_pnl  = snap.stock_equity - day_start
            snap.stock_day_pnl_pct = (
                snap.stock_day_pnl / day_start * 100 if day_start > 0 else 0.0
            )
        except Exception as e:
            snap.errors.append(f"Alpaca account read failed: {e}")

        # ── 2. Read stock positions with indicators ────────────
        try:
            raw_positions = alpaca_fn("GET", "/v2/positions")
            for pos in (raw_positions or []):
                sym     = pos["symbol"]
                curr    = float(pos.get("current_price", 0))
                entry   = float(pos.get("avg_entry_price", 0))
                qty     = float(pos.get("qty", 0))
                val     = float(pos.get("market_value", 0))
                pnl     = float(pos.get("unrealized_plpc", 0))
                pnl_usd = float(pos.get("unrealized_pl", 0))
                owner   = ("Claude" if sym in shared_state.get("claude_positions", [])
                           else "Grok")
                strategy = shared_state.get("position_exits", {}).get(
                    sym, {}).get("strategy", "A")
                entry_date = shared_state.get("position_exits", {}).get(
                    sym, {}).get("entry_date", "")
                held_days = 0
                if entry_date:
                    try:
                        held_days = (datetime.now() -
                                     datetime.strptime(entry_date, "%Y-%m-%d")).days
                    except Exception:
                        pass

                # Get indicators (live, not cached)
                indicators = {}
                try:
                    bars = get_bars_fn(sym, days=14)
                    if bars:
                        ind = compute_ind_fn(bars)
                        if ind:
                            indicators = {
                                "rsi":         round(float(ind.get("rsi", 50)), 1),
                                "macd":        float(ind.get("macd", 0)),
                                "macd_signal": float(ind.get("macd_signal", 0)),
                                "ema9":        float(ind.get("ema9", 0)),
                                "ema21":       float(ind.get("ema21", 0)),
                                "sma50":       float(ind.get("sma50", 0)),
                                "bb_pct":      float(ind.get("bb_pct", 50)),
                                "atr":         float(ind.get("atr", 0)),
                                "obv_trend":   ind.get("obv_trend", "neutral"),
                                "volume_ratio": float(ind.get("volume_ratio", 1.0)),
                            }
                except Exception as ie:
                    snap.errors.append(f"{sym} indicators failed: {ie}")

                proj = projections.get(sym, {})

                sp = StockPosition(
                    symbol        = sym,
                    current_price = curr,
                    entry_price   = entry,
                    qty           = qty,
                    market_value  = val,
                    pnl_pct       = pnl,
                    pnl_usd       = pnl_usd,
                    strategy      = strategy,
                    owner         = owner,
                    indicators    = indicators,
                    projection    = proj,
                    held_days     = held_days,
                )
                snap.stock_positions.append(sp)

        except Exception as e:
            snap.errors.append(f"Stock positions read failed: {e}")

        # ── 3. Read crypto wallet ──────────────────────────────
        try:
            # Import here to avoid circular deps
            from binance_crypto import get_full_wallet, get_all_crypto_projections

            wallet = get_full_wallet()
            if wallet.get("error"):
                snap.errors.append(f"Crypto wallet: {wallet['error']}")
            else:
                snap.crypto_wallet   = wallet.get("total_value", 0.0)
                snap.usdt_free       = wallet.get("usdt_free", 0.0)
                snap.usdt_deployable = max(0, snap.usdt_free - snap.usdt_reserve)
                snap.crypto_day_pnl  = shared_state.get("crypto_day_pnl", 0.0)

                # Get crypto projections
                try:
                    crypto_proj = get_all_crypto_projections()
                except Exception:
                    crypto_proj = {}

                # Bot-tracked positions
                for sym, pos in (crypto_trader.positions or {}).items():
                    try:
                        curr  = float(getattr(pos, "last_price", pos.entry_price))
                        entry = float(pos.entry_price)
                        qty   = float(pos.qty)
                        stop  = float(getattr(pos, "stop_price", 0))
                        tp    = float(getattr(pos, "tp_price", 0))
                        pnl   = (curr - entry) / entry * 100 if entry else 0
                        hrs   = float(pos.hours_held()) if callable(
                            getattr(pos, "hours_held", None)) else 0.0
                        asset = sym.replace("USDT", "")

                        ch = CryptoHolding(
                            symbol      = sym,
                            asset       = asset,
                            qty         = qty,
                            free_qty    = qty,
                            price       = curr,
                            value_usd   = curr * qty,
                            is_position = True,
                            entry_price = entry,
                            stop_price  = stop,
                            tp_price    = tp,
                            pnl_pct     = pnl,
                            hours_held  = hrs,
                            projection  = crypto_proj.get(sym, {}),
                            in_universe = True,
                        )
                        snap.crypto_positions.append(ch)
                    except Exception as pe:
                        snap.errors.append(f"Crypto pos {sym}: {pe}")

                # All wallet holdings (including dust)
                all_holdings = (wallet.get("tradeable", []) +
                                wallet.get("non_tradeable", []))
                tracked_syms = {p.symbol for p in snap.crypto_positions}

                for h in all_holdings:
                    sym   = h.get("symbol", f"{h['asset']}USDT")
                    asset = h.get("asset", "")
                    qty   = h.get("qty", 0)
                    free  = h.get("free", 0)
                    price = h.get("price", 0)
                    val   = h.get("value_usdt", 0)

                    if val < MIN_SHOW_VALUE_USD and qty < 0.000001:
                        continue

                    ch = CryptoHolding(
                        symbol      = sym,
                        asset       = asset,
                        qty         = qty,
                        free_qty    = free,
                        price       = price,
                        value_usd   = val,
                        is_position = sym in tracked_syms,
                        in_universe = h.get("in_universe", False),
                        projection  = crypto_proj.get(sym, {}),
                    )
                    snap.crypto_holdings.append(ch)

        except Exception as e:
            snap.errors.append(f"Crypto wallet read failed: {e}")

        # ── 4. Compute totals ─────────────────────────────────
        stock_pos_val = sum(p.market_value for p in snap.stock_positions)
        snap.total_value   = (snap.stock_equity + snap.crypto_wallet)
        snap.total_day_pnl = snap.stock_day_pnl + snap.crypto_day_pnl

        # ── 5. Identify opportunities ─────────────────────────
        self._find_stock_opportunities(snap, projections)
        self._find_crypto_opportunities(snap)
        self._find_rotation_candidates(snap)
        self._find_dust_to_clean(snap)
        self._assess_risk(snap)

        self._last_snapshot = snap
        return snap

    # ──────────────────────────────────────────────────────────────
    # OPPORTUNITY FINDERS
    # ──────────────────────────────────────────────────────────────

    def _find_stock_opportunities(
        self, snap: PortfolioSnapshot, projections: dict
    ) -> None:
        """
        Identify best stock buy opportunities given current cash.
        Scans projections for bullish setups near support.
        """
        if snap.stock_cash < MIN_OPPORTUNITY_USD:
            return

        # Rank existing positions by opportunity score
        # (used for rotation — sell low-score positions to buy high-score)
        scored = []
        for pos in snap.stock_positions:
            scored.append({
                "symbol":  pos.symbol,
                "score":   pos.opportunity_score(),
                "pnl_pct": pos.pnl_pct_display,
                "rsi":     pos.rsi,
                "macd":    pos.macd_signal,
                "obv":     pos.obv_trend,
                "upside":  pos.upside_to_target,
                "value":   pos.market_value,
                "type":    "existing_position",
                "note":    _build_stock_note(pos),
            })

        # Scan projections for new opportunities
        for sym, proj in projections.items():
            if proj.get("error"):
                continue
            # Skip already owned
            owned = {p.symbol for p in snap.stock_positions}
            if sym in owned:
                continue

            ph   = proj.get("proj_high", 0)
            pl   = proj.get("proj_low", 0)
            conf = proj.get("confidence", 0)
            bias = proj.get("bias", "neutral")

            if not ph or not pl or conf < 50:
                continue

            # Only flag bullish high-confidence setups
            if bias == "bullish" and conf >= 65:
                upside = round((ph - pl) / pl * 100, 1) if pl else 0
                scored.append({
                    "symbol":  sym,
                    "score":   _proj_opportunity_score(conf, bias, upside),
                    "pnl_pct": 0.0,
                    "rsi":     0,
                    "macd":    "unknown",
                    "obv":     "unknown",
                    "upside":  upside,
                    "value":   0,
                    "type":    "new_entry",
                    "note":    (f"proj_low=${pl:.2f} → proj_high=${ph:.2f} "
                                f"+{upside:.1f}% | conf={conf} {bias.upper()}"),
                })

        snap.stock_opportunities = sorted(scored, key=lambda x: -x["score"])[:8]

    def _find_crypto_opportunities(self, snap: PortfolioSnapshot) -> None:
        """
        Identify best crypto buy opportunities given USDT deployable.
        Includes rotation candidates (sell weak → buy strong).
        """
        opps = []

        # All holdings as rotation/sell candidates
        for h in snap.crypto_holdings:
            if not h.is_position:  # Wallet coins (not bot-tracked)
                score = h.rotation_score()
                opps.append({
                    "symbol":      h.symbol,
                    "asset":       h.asset,
                    "score":       score,
                    "value_usd":   h.value_usd,
                    "upside":      h.upside_to_target,
                    "is_sellable": h.is_sellable,
                    "is_dust":     h.is_dust,
                    "type":        "wallet_holding",
                    "note":        _build_crypto_holding_note(h),
                })

        # Bot-tracked positions
        for pos in snap.crypto_positions:
            score = pos.rotation_score()
            opps.append({
                "symbol":    pos.symbol,
                "asset":     pos.asset,
                "score":     score,
                "value_usd": pos.value_usd,
                "pnl_pct":   pos.pnl_pct,
                "upside":    pos.upside_to_target,
                "hours_held": pos.hours_held,
                "type":      "active_position",
                "note":      (f"entry=${pos.entry_price:.6f} | "
                              f"P&L={pos.pnl_pct:+.1f}% | "
                              f"stop=${pos.stop_price:.6f} | "
                              f"TP=${pos.tp_price:.6f}"),
            })

        snap.crypto_opportunities = sorted(opps, key=lambda x: -x["score"])[:10]

    def _find_rotation_candidates(self, snap: PortfolioSnapshot) -> None:
        """
        Identify positions to rotate OUT of (sell) and INTO (buy).
        Rotation only makes sense if best_opp_score >> worst_position_score.
        """
        candidates = []

        # Stock rotation: lowest-scoring positions vs highest-scoring opportunities
        if snap.stock_positions and snap.stock_opportunities:
            worst_stocks = sorted(snap.stock_positions,
                                  key=lambda p: p.opportunity_score())[:2]
            best_opps    = [o for o in snap.stock_opportunities
                            if o["type"] == "new_entry"][:2]

            for ws in worst_stocks:
                for bo in best_opps:
                    score_gap = bo["score"] - ws.opportunity_score()
                    if score_gap >= ROTATION_SCORE_GAP:
                        candidates.append({
                            "type":        "stock_rotation",
                            "sell_symbol": ws.symbol,
                            "sell_score":  ws.opportunity_score(),
                            "sell_pnl":    ws.pnl_pct_display,
                            "buy_symbol":  bo["symbol"],
                            "buy_score":   bo["score"],
                            "score_gap":   score_gap,
                            "rationale":   (
                                f"Sell {ws.symbol} ({ws.pnl_pct_display:+.1f}%, "
                                f"score={ws.opportunity_score()}) → "
                                f"Buy {bo['symbol']} (score={bo['score']}, "
                                f"+{bo['upside']:.1f}% upside)"
                            ),
                        })

        # Crypto rotation: lowest-score wallet coin → highest-score new setup
        sellable_holdings = [h for h in snap.crypto_holdings
                             if h.is_sellable and not h.is_position]
        if sellable_holdings and snap.usdt_free < CRYPTO_RULES_MIN_TRADE:
            worst_holdings = sorted(sellable_holdings,
                                    key=lambda h: h.rotation_score())[:2]
            # Best crypto opps are already in crypto_opportunities
            best_crypto = [o for o in snap.crypto_opportunities
                           if o["type"] == "wallet_holding" and
                           o.get("score", 0) >= 65][:2]

            for wh in worst_holdings:
                for bc in best_crypto:
                    if bc["symbol"] == wh.symbol:
                        continue
                    gap = bc["score"] - wh.rotation_score()
                    if gap >= ROTATION_SCORE_GAP and wh.value_usd >= 2.0:
                        candidates.append({
                            "type":        "crypto_rotation",
                            "sell_symbol": wh.symbol,
                            "sell_value":  wh.value_usd,
                            "sell_score":  wh.rotation_score(),
                            "buy_symbol":  bc["symbol"],
                            "buy_score":   bc["score"],
                            "score_gap":   gap,
                            "rationale":   (
                                f"Sell {wh.asset} (${wh.value_usd:.2f}, "
                                f"score={wh.rotation_score()}) → "
                                f"Buy {bc['symbol']} (score={bc['score']}, "
                                f"+{bc['upside']:.1f}% upside)"
                            ),
                        })

        snap.rotation_candidates = sorted(candidates,
                                          key=lambda x: -x["score_gap"])[:5]

    def _find_dust_to_clean(self, snap: PortfolioSnapshot) -> None:
        """
        Find dust coins worth selling to free up USDT.
        Dust = in universe, sellable, value < $5, not actively managed.
        """
        tracked = {p.symbol for p in snap.crypto_positions}
        dust    = []

        for h in snap.crypto_holdings:
            if h.symbol in tracked:
                continue   # Don't touch bot-managed positions
            if not h.is_sellable:
                continue
            if h.value_usd < 0.50:
                continue   # Too small to bother

            # Flag as cleanup candidate if:
            # 1. Small value AND bearish projection
            # 2. OR value < $3 and no projection
            proj_bias = h.projection.get("bias", "neutral")
            proj_conf = h.projection.get("confidence", 0)

            should_clean = (
                (h.value_usd < 5.0 and proj_bias == "bearish") or
                (h.value_usd < 3.0 and proj_conf < 50) or  # neutral low-conf
                (h.value_usd < 2.0) or
                (h.value_usd < 5.0 and proj_bias == "neutral" and proj_conf < 60)
            )

            if should_clean:
                dust.append({
                    "symbol":    h.symbol,
                    "asset":     h.asset,
                    "value_usd": h.value_usd,
                    "qty":       h.free_qty,
                    "price":     h.price,
                    "proj_bias": proj_bias,
                    "proj_conf": proj_conf,
                    "action":    ("sell" if proj_bias == "bearish" else "assess"),
                    "note":      (f"${h.value_usd:.2f} | "
                                  f"proj={proj_bias} conf={proj_conf}"),
                })

        snap.dust_to_clean = sorted(dust, key=lambda x: x["value_usd"])

    def _assess_risk(self, snap: PortfolioSnapshot) -> None:
        """Identify portfolio risk concentrations and warnings."""
        warnings = []

        # Concentration risk
        all_values = (
            [p.market_value for p in snap.stock_positions] +
            [p.value_usd for p in snap.crypto_positions]
        )
        if all_values and snap.total_value > 0:
            max_pos = max(all_values)
            snap.concentration_pct = max_pos / snap.total_value * 100
            if snap.concentration_pct > 60:
                warnings.append(
                    f"⚠️ CONCENTRATION: {snap.concentration_pct:.0f}% in single position"
                )

        # Both sides losing money
        if snap.stock_day_pnl < -1.0 and snap.crypto_day_pnl < -1.0:
            warnings.append(
                "⚠️ CORRELATED LOSS: Both stocks and crypto down today — "
                "possible macro event, reduce risk"
            )

        # TSLA-style problem: stock at large loss
        for pos in snap.stock_positions:
            if pos.pnl_pct_display <= -7.0:
                warnings.append(
                    f"🚨 {pos.symbol} at {pos.pnl_pct_display:+.1f}% — "
                    f"near stop loss territory, AI review recommended"
                )
            if pos.pnl_pct_display <= -9.0:
                warnings.append(
                    f"🔴 CRITICAL: {pos.symbol} at {pos.pnl_pct_display:+.1f}% — "
                    f"approaching -10% hard stop"
                )

        # Crypto near stop
        for pos in snap.crypto_positions:
            if pos.stop_price and pos.price <= pos.stop_price * 1.05:
                warnings.append(
                    f"⚡ {pos.symbol} within 5% of stop ${pos.stop_price:.6f} — "
                    f"flash crash risk"
                )

        # No deployable capital
        if snap.total_deployable < MIN_OPPORTUNITY_USD:
            warnings.append(
                "💤 No deployable capital — "
                "consider rotation or wait for positions to close"
            )

        snap.risk_warnings = warnings

    # ──────────────────────────────────────────────────────────────
    # AI PROMPT SECTION BUILDER
    # ──────────────────────────────────────────────────────────────

    def build_ai_prompt_section(self, snap: PortfolioSnapshot) -> str:
        """
        Build the complete wallet intelligence section for AI prompt.
        Replaces static wallet summaries — this is rich, actionable.
        """
        lines = []
        lines.append("╔══════════════════════════════════════════════╗")
        lines.append("║   COMPLETE PORTFOLIO INTELLIGENCE (v3.0)     ║")
        lines.append("╚══════════════════════════════════════════════╝")
        lines.append(f"Time: {snap.timestamp}")
        lines.append(f"Total Portfolio: ${snap.total_value:.2f} | "
                     f"Day P&L: ${snap.total_day_pnl:+.2f}")
        lines.append("")

        # ── RISK WARNINGS (always first) ──────────────────────
        if snap.risk_warnings:
            lines.append("🚨 RISK WARNINGS:")
            for w in snap.risk_warnings:
                lines.append(f"  {w}")
            lines.append("")

        # ── STOCK PORTFOLIO ───────────────────────────────────
        lines.append("━━━ STOCKS (Alpaca) ━━━")
        lines.append(f"  Equity: ${snap.stock_equity:.2f} | "
                     f"Cash: ${snap.stock_cash:.2f} | "
                     f"Day: ${snap.stock_day_pnl:+.2f} "
                     f"({snap.stock_day_pnl_pct:+.2f}%)")
        lines.append(f"  Deployable: ${snap.stock_deployable:.2f}")

        if snap.stock_positions:
            lines.append("  POSITIONS:")
            for pos in sorted(snap.stock_positions,
                               key=lambda p: p.pnl_pct_display):
                icon = "🟢" if pos.pnl_pct_display >= 0 else "🔴"
                lines.append(
                    f"  {icon} [{pos.owner}] {pos.symbol} "
                    f"entry=${pos.entry_price:.2f} → ${pos.current_price:.2f} "
                    f"P&L={pos.pnl_pct_display:+.2f}% "
                    f"val=${pos.market_value:.2f} held={pos.held_days}d"
                )
                # Technical context
                if pos.indicators:
                    rsi_flag = ("🔥 oversold" if pos.rsi < 35 else
                                "❄️ overbought" if pos.rsi > 70 else "")
                    lines.append(
                        f"      RSI={pos.rsi:.0f} {rsi_flag} | "
                        f"MACD={pos.macd_signal} | OBV={pos.obv_trend} | "
                        f"BB={pos.indicators.get('bb_pct', 0):.0f}%"
                    )
                # Projection context
                if pos.projection and not pos.projection.get("error"):
                    ph = pos.projection.get("proj_high", 0)
                    pl = pos.projection.get("proj_low", 0)
                    if ph and pl:
                        lines.append(
                            f"      Proj: ${pl:.2f}–${ph:.2f} | "
                            f"+{pos.upside_to_target:.1f}% to target | "
                            f"{'🛡️ AT SUPPORT' if pos.at_support else ''}"
                            f"{'⚠️ AT RESISTANCE' if pos.at_resistance else ''}"
                        )
                lines.append(
                    f"      Opportunity score: {pos.opportunity_score()}/100 | "
                    f"Strategy: {pos.strategy}"
                )
        else:
            lines.append("  No stock positions")

        lines.append("")

        # ── CRYPTO PORTFOLIO ──────────────────────────────────
        lines.append("━━━ CRYPTO (Binance.US) ━━━")
        lines.append(f"  Wallet: ${snap.crypto_wallet:.2f} | "
                     f"USDT free: ${snap.usdt_free:.2f} | "
                     f"Reserve: ${snap.usdt_reserve:.2f} | "
                     f"Deployable: ${snap.usdt_deployable:.2f}")
        lines.append(f"  Day P&L: ${snap.crypto_day_pnl:+.2f}")

        if snap.crypto_positions:
            lines.append("  BOT POSITIONS:")
            for pos in snap.crypto_positions:
                icon = "🟢" if pos.pnl_pct >= 0 else "🔴"
                lines.append(
                    f"  {icon} {pos.symbol} "
                    f"entry=${pos.entry_price:.6f} → ${pos.price:.6f} "
                    f"P&L={pos.pnl_pct:+.1f}% "
                    f"val=${pos.value_usd:.2f} held={pos.hours_held:.0f}h"
                )
                lines.append(
                    f"      stop=${pos.stop_price:.6f} | "
                    f"TP=${pos.tp_price:.6f} | "
                    f"+{pos.upside_to_target:.1f}% to target"
                )
        else:
            lines.append("  No active crypto positions")

        if snap.crypto_holdings:
            non_tracked = [h for h in snap.crypto_holdings
                           if not h.is_position and h.value_usd >= MIN_SHOW_VALUE_USD]
            if non_tracked:
                lines.append("  WALLET HOLDINGS (assess):")
                for h in sorted(non_tracked, key=lambda x: -x.value_usd)[:8]:
                    sellable = "✅ tradeable" if h.is_sellable else "🔒 hold-only"
                    proj_note = ""
                    if h.projection and not h.projection.get("error"):
                        bias = h.projection.get("bias", "neutral")
                        conf = h.projection.get("confidence", 0)
                        proj_note = f" | proj={bias} conf={conf}"
                    lines.append(
                        f"    {h.asset}: {h.qty:.4f} = ${h.value_usd:.2f} "
                        f"@ ${h.price:.6f} | {sellable}{proj_note}"
                    )

        lines.append("")

        # ── OPPORTUNITIES ────────────────────────────────────
        lines.append("━━━ OPPORTUNITIES (ranked by score) ━━━")

        if snap.stock_opportunities:
            lines.append("  📈 STOCK OPPORTUNITIES:")
            for opp in snap.stock_opportunities[:5]:
                score_bar = "█" * (int(opp["score"]) // 10)
                lines.append(
                    f"  [{int(opp['score']):3d}] {score_bar} {opp['symbol']} "
                    f"({opp['type']}) | {opp.get('note', '')[:80]}"
                )

        if snap.crypto_opportunities:
            lines.append("  🪙 CRYPTO OPPORTUNITIES:")
            for opp in snap.crypto_opportunities[:5]:
                score_bar = "█" * (int(opp["score"]) // 10)
                lines.append(
                    f"  [{int(opp['score']):3d}] {score_bar} {opp['symbol']} "
                    f"({opp['type']}) | {opp.get('note', '')[:80]}"
                )

        lines.append("")

        # ── ROTATION CANDIDATES ───────────────────────────────
        if snap.rotation_candidates:
            lines.append("━━━ ROTATION CANDIDATES (sell → buy) ━━━")
            for rot in snap.rotation_candidates[:3]:
                lines.append(f"  🔄 [{rot['type']}] {rot['rationale'][:120]}")
            lines.append("")

        # ── DUST CLEANUP ──────────────────────────────────────
        if snap.dust_to_clean:
            total_dust = sum(d["value_usd"] for d in snap.dust_to_clean)
            lines.append(f"━━━ DUST CLEANUP (${total_dust:.2f} total recoverable) ━━━")
            for dust in snap.dust_to_clean[:5]:
                lines.append(
                    f"  💤 {dust['asset']}: ${dust['value_usd']:.2f} | "
                    f"{dust['action'].upper()} | {dust['note']}"
                )
            lines.append("")

        # ── DEPLOYMENT SUMMARY ────────────────────────────────
        lines.append("━━━ DEPLOYMENT SUMMARY ━━━")
        lines.append(f"  Stock cash:       ${snap.stock_deployable:.2f}")
        lines.append(f"  Crypto USDT:      ${snap.usdt_deployable:.2f}")
        lines.append(f"  Total deployable: ${snap.total_deployable:.2f}")

        if snap.rotation_candidates:
            rot_value = sum(
                r.get("sell_value", 0) for r in snap.rotation_candidates
                if r["type"] == "crypto_rotation"
            )
            if rot_value > 0:
                lines.append(f"  + Rotation value: ${rot_value:.2f} "
                             f"(if AI approves rotation)")

        if snap.errors:
            lines.append("")
            lines.append("⚠️ DATA ERRORS (some data may be missing):")
            for err in snap.errors[:3]:
                lines.append(f"  {err[:80]}")

        lines.append("")
        lines.append("ANALYZE ALL POSITIONS AND OPPORTUNITIES ABOVE.")
        lines.append("Output complete portfolio brief JSON.")

        return "\n".join(lines)

    def get_last_snapshot(self) -> PortfolioSnapshot | None:
        return self._last_snapshot


# ══════════════════════════════════════════════════════════════════
# UTILITY HELPERS
# ══════════════════════════════════════════════════════════════════

CRYPTO_RULES_MIN_TRADE = 10.0  # Minimum USDT for a crypto trade


def _build_stock_note(pos: StockPosition) -> str:
    parts = []
    if pos.rsi < 35:   parts.append(f"RSI={pos.rsi:.0f} OVERSOLD")
    elif pos.rsi > 70: parts.append(f"RSI={pos.rsi:.0f} OVERBOUGHT")
    if pos.macd_signal == "bullish":  parts.append("MACD+")
    elif pos.macd_signal == "bearish": parts.append("MACD-")
    if pos.obv_trend == "rising":  parts.append("OBV↑")
    if pos.at_support:     parts.append("AT SUPPORT")
    if pos.at_resistance:  parts.append("AT RESIST")
    if pos.upside_to_target >= 10:
        parts.append(f"+{pos.upside_to_target:.0f}% to target")
    return " | ".join(parts) if parts else f"P&L={pos.pnl_pct_display:+.1f}%"


def _build_crypto_holding_note(h: CryptoHolding) -> str:
    parts = [f"${h.value_usd:.2f}"]
    if h.projection and not h.projection.get("error"):
        bias = h.projection.get("bias", "neutral")
        conf = h.projection.get("confidence", 0)
        parts.append(f"{bias} conf={conf}")
        if h.upside_to_target > 5:
            parts.append(f"+{h.upside_to_target:.0f}% upside")
    if not h.is_sellable:
        parts.append("NOT TRADEABLE on Binance.US")
    return " | ".join(parts)


def _proj_opportunity_score(conf: int, bias: str, upside: float) -> int:
    """Score a projection-based buy opportunity."""
    score = 40
    score += min(25, conf // 3)
    if bias == "bullish":    score += 15
    elif bias == "bearish":  score -= 20
    if upside >= 20:  score += 15
    elif upside >= 10: score += 8
    elif upside <= 3:  score -= 15
    return max(0, min(100, score))
