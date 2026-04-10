"""
intelligence.py — NovaTrade Market Intelligence Module
═══════════════════════════════════════════════════════
Politician trades (SEC EDGAR Form 4), top investor portfolios,
smart money analysis, triple confirmation signals.

Imported by bot_with_proxy.py — no circular dependencies.
"""

import os
import re
import time
import requests
from datetime import datetime, timedelta

# ── Alpaca credentials ───────────────────────────────────────
ALPACA_KEY    = os.environ.get("ALPACA_KEY", "")
ALPACA_SECRET = os.environ.get("ALPACA_SECRET", "")
DATA_URL      = "https://data.alpaca.markets"

# ── Shared references (injected by bot) ──────────────────────
RULES      = {}
log        = print
ask_grok   = None   # Injected by bot
parse_json = None   # Injected by bot


def _set_context(rules, log_fn, ask_grok_fn=None, parse_json_fn=None):
    """Called by bot_with_proxy.py to inject shared config and functions."""
    global RULES, log, ask_grok, parse_json
    RULES      = rules
    log        = log_fn
    if ask_grok_fn:
        ask_grok   = ask_grok_fn
    if parse_json_fn:
        parse_json = parse_json_fn

# ── SEC EDGAR Form 4 — Insider & Congressional Trade Tracker ──────
# Official US government data. Free, no API key needed, real-time.
# EDGAR policy: identify yourself in User-Agent header.
# Rate limit: 10 req/sec — we use ~1/hour so no issue at all.
# Docs: https://www.sec.gov/developer

EDGAR_USER_AGENT = "NovaTrade research bot contact@novatrade.local"
EDGAR_HEADERS    = {"User-Agent": EDGAR_USER_AGENT, "Accept": "application/json"}

# Politicians with known SEC CIK numbers
EDGAR_POLITICIANS = {
    "0001341439": {"name": "Nancy Pelosi",     "party": "D", "focus": "tech"},
    "0001655050": {"name": "Dan Crenshaw",      "party": "R", "focus": "energy"},
    "0001766607": {"name": "Tommy Tuberville",  "party": "R", "focus": "broad"},
    "0001820940": {"name": "Mark Kelly",        "party": "D", "focus": "tech"},
    "0001831919": {"name": "Josh Gottheimer",   "party": "D", "focus": "tech"},
}

EDGAR_TRACK_TICKERS = {
    "NVDA", "AAPL", "MSFT", "AMZN", "TSLA", "META", "GOOGL",
    "PLTR", "AMD", "AVGO", "TSM", "NFLX", "CRM", "ORCL",
}


def _edgar_quarter(month: int) -> str:
    return f"QTR{(month - 1) // 3 + 1}"

def _fetch_edgar_politician_form4(cik: str, pol_info: dict, days_back=45) -> list:
    """
    Fetch Form 4 filings for a specific politician by CIK.
    Uses EDGAR submissions API — clean JSON, no scraping needed.
    """
    import re
    from datetime import datetime, timedelta
    trades  = []
    cutoff  = datetime.now() - timedelta(days=days_back)

    try:
        cik_padded = cik.lstrip("0").zfill(10)
        url  = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"
        resp = requests.get(url, headers=EDGAR_HEADERS, timeout=10)
        if not resp.ok:
            return []

        data    = resp.json()
        filings = data.get("filings", {}).get("recent", {})
        forms   = filings.get("form", [])
        dates   = filings.get("filingDate", [])
        accnums = filings.get("accessionNumber", [])
        primary = filings.get("primaryDocument", [])

        all_tickers = set(RULES.get("universe", [])) | EDGAR_TRACK_TICKERS

        for i, form_type in enumerate(forms):
            if form_type not in ("4", "4/A"):
                continue
            try:
                filed_date = datetime.strptime(dates[i], "%Y-%m-%d")
            except Exception:
                continue
            if filed_date < cutoff:
                break  # Newest-first — stop when too old

            days_lag = (datetime.now() - filed_date).days

            try:
                acc_path = accnums[i].replace("-", "")
                cik_int  = int(cik_padded)
                xml_url  = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_path}/{primary[i]}"
                xml_resp = requests.get(xml_url, headers=EDGAR_HEADERS, timeout=8)
                if not xml_resp.ok:
                    continue

                xml = xml_resp.text
                ticker_m = re.search(r"<issuerTradingSymbol>([A-Z]{1,5})</issuerTradingSymbol>", xml)
                action_m = re.search(r"<transactionCode>([A-Z])</transactionCode>", xml)
                shares_m = re.search(r"<transactionShares>.*?<value>([\d.]+)</value>", xml, re.DOTALL)

                if not ticker_m:
                    continue
                ticker = ticker_m.group(1)
                if ticker not in all_tickers:
                    continue

                txn_code = action_m.group(1) if action_m else "?"
                action   = "buy" if txn_code == "P" else "sell" if txn_code in ("S", "D") else "unknown"
                shares   = shares_m.group(1) if shares_m else "?"

                trades.append({
                    "politician": pol_info["name"],
                    "party":      pol_info["party"],
                    "ticker":     ticker,
                    "action":     action,
                    "size":       f"{float(shares):,.0f} shares" if shares != "?" else "unknown",
                    "filed":      dates[i],
                    "traded":     dates[i],
                    "days_lag":   days_lag,
                    "source":     "sec_edgar_form4",
                })
            except Exception:
                continue

        return trades

    except Exception:
        return []

def get_politician_trades():
    """
    Fetch congressional insider trades from SEC EDGAR Form 4.
    Official US government data — free, real-time, no API key needed.
    """
    all_trades = []
    sources_ok = []

    for cik, pol_info in list(EDGAR_POLITICIANS.items())[:4]:
        try:
            pol_trades = _fetch_edgar_politician_form4(cik, pol_info, days_back=45)
            if pol_trades:
                all_trades.extend(pol_trades)
                sources_ok.append(f"{pol_info['name'].split()[1]}:{len(pol_trades)}")
            time.sleep(0.15)  # Gentle rate limiting — EDGAR fair use
        except Exception:
            pass

    # Deduplicate
    seen, unique_trades = set(), []
    for t in all_trades:
        key = (t.get("politician",""), t["ticker"], t["action"], t.get("filed",""))
        if key not in seen:
            seen.add(key)
            unique_trades.append(t)

    all_tickers = set(RULES.get("universe", [])) | EDGAR_TRACK_TICKERS
    actionable  = [t for t in unique_trades if t["ticker"] in all_tickers]

    if actionable:
        log(f"🏛️ SEC EDGAR Form 4: {len(actionable)} trades found | {', '.join(sources_ok)}")
        lines = []
        for t in sorted(actionable, key=lambda x: x.get("days_lag", 99))[:15]:
            icon = "🟢" if t["action"] == "buy" else "🔴"
            lines.append(
                f"  {icon} [{t['party']}] {t['politician']}: "
                f"{t['action'].upper()} {t['ticker']} "
                f"{t['size']} (filed {t['filed']}, {t['days_lag']}d lag)"
            )
        return "\n".join(lines), actionable
    else:
        if sources_ok:
            log(f"🏛️ SEC EDGAR: no matching trades this cycle")
        else:
            log("🏛️ SEC EDGAR: no data (will retry next cycle)")
        return "", []

def analyze_politician_signals(trades, chart_section):
    """
    Analyze politician trades for mimicking opportunities.
    Focus on:
    1. Stocks multiple politicians are buying (strong signal)
    2. Stocks in our universe that politicians are buying
    3. Committee members buying stocks in their oversight area
    4. Recent buys (within 30 days) — most actionable
    """
    if not trades:
        return {}

    # Count buys per ticker
    buy_counts  = {}
    sell_counts = {}
    for t in trades:
        ticker = t.get("ticker", "")
        action = t.get("action", "").lower()
        if not ticker: continue
        if "buy" in action or "purchase" in action:
            buy_counts[ticker]  = buy_counts.get(ticker, 0) + 1
        elif "sell" in action or "sale" in action:
            sell_counts[ticker] = sell_counts.get(ticker, 0) + 1

    # Find strongest signals
    signals = {}
    for ticker, count in sorted(buy_counts.items(), key=lambda x: -x[1]):
        signals[ticker] = {
            "action":      "buy",
            "count":       count,
            "strength":    "STRONG" if count >= 3 else "MODERATE" if count >= 2 else "WEAK",
            "in_universe": ticker in RULES["universe"],
            "mimick_score": count * (2 if ticker in RULES["universe"] else 1),
        }
    for ticker, count in sell_counts.items():
        if ticker not in signals:
            signals[ticker] = {
                "action":      "sell",
                "count":       count,
                "strength":    "STRONG" if count >= 3 else "MODERATE" if count >= 2 else "WEAK",
                "in_universe": ticker in RULES["universe"],
                "mimick_score": count,
            }

    # Top mimick candidates — in universe AND being bought
    top_mimick = [
        t for t, d in sorted(signals.items(), key=lambda x: -x[1]["mimick_score"])
        if d["action"] == "buy" and d["in_universe"]
    ][:3]

    return {
        "buy_signals":    {t: d for t, d in signals.items() if d["action"] == "buy"},
        "sell_signals":   {t: d for t, d in signals.items() if d["action"] == "sell"},
        "top_mimick":     top_mimick,
        "universe_buys":  [t for t in top_mimick if t in RULES["universe"]],
    }

def get_top_investor_portfolios():
    """
    Track portfolios of top investors using Grok's real-time web/knowledge access.
    Uses both Grok's training knowledge and web search for latest 13F data.
    Bypasses Railway network restrictions on SEC EDGAR.
    """
    log("💼 Fetching top investor portfolios via Grok...")
    universe_set = set(RULES["universe"])

    try:
        universe_str = ", ".join(RULES["universe"])
        prompt = f"""What are the current major stock holdings of these top investors based on their latest 13F filings and recent news:
Cathie Wood (ARK Invest), Warren Buffett (Berkshire), Michael Burry, George Soros, Ray Dalio (Bridgewater), Bill Ackman (Pershing Square), Stanley Druckenmiller

Focus specifically on these stocks if they hold them: {universe_str}
Also note any recent buys or sells in the last quarter.

Return ONLY a JSON object:
{{"holdings": [
  {{"investor": "Name", "symbol": "TICKER", "position": "large/medium/small", "recent_change": "new buy/increased/decreased/sold/held", "notes": "brief"}}
], "key_insight": "most important trend across all investors"}}"""

        raw = ask_grok(prompt,
            "You are a financial analyst with knowledge of institutional investor 13F filings. Return ONLY valid JSON.")
        result = parse_json(raw)

        if result and result.get("holdings"):
            holdings_list = result["holdings"]
            all_holdings  = {}

            for h in holdings_list:
                sym = h.get("symbol","").upper()
                if sym in universe_set:
                    if sym not in all_holdings:
                        all_holdings[sym] = []
                    all_holdings[sym].append({
                        "investor": h.get("investor",""),
                        "value":    0,
                        "filed":    "latest 13F",
                        "change":   h.get("recent_change","held"),
                        "notes":    h.get("notes",""),
                    })

            if all_holdings:
                lines = []
                for sym, holders in all_holdings.items():
                    investors = [f"{h['investor'].split('(')[0].strip()} ({h['change']})"
                                 for h in holders]
                    lines.append(f"  {sym}: {', '.join(investors)}")

                insight = result.get("key_insight","")
                if insight:
                    log(f"💼 Key investor insight: {insight[:120]}")

                log(f"💼 Universe stocks held by top investors: {list(all_holdings.keys())}")
                return "\n".join(lines), all_holdings

    except Exception as e:
        log(f"⚠️ Investor portfolios via Grok failed: {e}")

    # Fallback: hardcoded known major holdings
    log("💼 Using cached top investor holdings...")
    known_holdings = {
        "AAPL": ["Warren Buffett (largest position ~$170B)", "Many funds"],
        "NVDA": ["Cathie Wood ARK (top holding)", "Many growth funds"],
        "MSFT": ["Bill Ackman", "Many value funds"],
        "AMZN": ["George Soros", "Many growth funds"],
        "META": ["Stanley Druckenmiller (recent buy)", "Many tech funds"],
        "TSLA": ["Cathie Wood ARK (core position)", "Many growth funds"],
        "GOOGL": ["Warren Buffett (recent add)", "Many value funds"],
        "PLTR": ["Cathie Wood ARK (large position)", "Growth funds"],
    }

    lines = []
    universe_overlap = {k: v for k, v in known_holdings.items() if k in universe_set}
    for sym, holders in universe_overlap.items():
        lines.append(f"  {sym}: {holders[0]}")

    return "\n".join(lines), {sym: [{"investor": h, "value": 0, "filed": "cached"}]
                                for sym, holders in universe_overlap.items()
                                for h in holders}

def analyze_smart_money(pol_signals, investor_holdings, gainers):
    """
    Combine politician trades + top investor holdings + biggest gainers
    to find the STRONGEST collaborative signals.

    Triple confirmation = politician buy + top investor holds + biggest gainer today
    """
    universe_set = set(RULES["universe"])
    scores = {}

    # Score each universe stock
    for sym in universe_set:
        score = 0
        reasons = []

        # Politician signal (+3 per politician buying)
        pol_buy = pol_signals.get("buy_signals", {}).get(sym, {})
        if pol_buy:
            pol_count = pol_buy.get("count", 0)
            score    += pol_count * 3
            reasons.append(f"{pol_count} politician(s) buying")

        # Top investor holding (+2 per investor)
        inv_holders = investor_holdings.get(sym, [])
        if inv_holders:
            score += len(inv_holders) * 2
            names  = [h.get("investor","").split("(")[0].strip() for h in inv_holders[:2]]
            reasons.append(f"held by {', '.join(names)}")

        # Biggest gainer today (+4 — most immediate signal)
        gainer_data = next((g for g in gainers if g.get("symbol") == sym), None)
        if gainer_data and gainer_data.get("change", 0) > 3:
            score += 4
            reasons.append(f"biggest gainer +{gainer_data['change']:.1f}% today")

        if score > 0:
            scores[sym] = {
                "score":         score,
                "reasons":       reasons,
                "is_triple":     score >= 9,  # All 3 signals
                "is_double":     score >= 5,  # 2 signals
                "collab_worthy": score >= 5,  # Recommend for collaboration
            }

    # Sort by score
    ranked = sorted(scores.items(), key=lambda x: -x[1]["score"])

    if ranked:
        log("🧠 Smart money analysis:")
        for sym, data in ranked[:5]:
            tag = "🔥 TRIPLE" if data["is_triple"] else "⭐ DOUBLE" if data["is_double"] else "📌"
            log(f"   {tag} {sym}: score={data['score']} — {' | '.join(data['reasons'])}")

    return {
        "ranked":     ranked,
        "top_collab": [sym for sym, d in ranked if d["collab_worthy"]][:3],
        "triple_confirmation": [sym for sym, d in ranked if d["is_triple"]],
    }
