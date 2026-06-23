#!/usr/bin/env python3
"""
alpaca_dashboard.py — standalone multi-account Alpaca performance dashboard.

WHY THIS IS A SEPARATE APP (not part of bot_with_proxy.py):
  - It never touches the 234KB bot file, so there's no web-upload truncation risk.
  - It reads each account's performance straight from Alpaca (ground truth), so
    the numbers can't be skewed by any bot-internal P&L bookkeeping.
  - Deploy it as its OWN Railway service (or run locally). The bot keeps running
    untouched.

WHAT IT DOES:
  - One dashboard, many Alpaca accounts. A tab bar switches between them.
  - Each account shows ONLY its own equity, day P&L, positions, and 30-day curve.
    Nothing is mixed across accounts — every number is fetched with that one
    account's keys.
  - Shows all asset classes that account holds on Alpaca: equity, options, crypto
    (each tagged). NOTE: crypto held on Binance.US is NOT shown here — this reads
    Alpaca only. A Binance panel can be added later if you want it.

  Read-only: it only issues GET requests to Alpaca. It never places or cancels
  orders, so even though the keys can trade, this app won't.

CONFIG (environment variables):
  ALPACA_ACCOUNTS   JSON list of accounts. Add an account = add an entry here.
                    [
                      {"name": "Hanz",  "key": "AK...", "secret": "...", "paper": false},
                      {"name": "Mom",   "key": "AK...", "secret": "...", "paper": true}
                    ]
  DASHBOARD_PASSWORD  Optional. If set, the page requires HTTP Basic auth with
                      this password (any username). STRONGLY recommended if the
                      service is reachable on the public internet.
  PORT                Optional, defaults to 8080 (Railway sets this for you).

RUN LOCALLY:
  pip install flask requests
  export ALPACA_ACCOUNTS='[{"name":"Test","key":"...","secret":"...","paper":true}]'
  python alpaca_dashboard.py
  # open http://localhost:8080
"""
import os
import json
import time
from functools import wraps

import requests
from flask import Flask, jsonify, request, Response, render_template_string

app = Flask(__name__)

LIVE_BASE  = "https://api.alpaca.markets"
PAPER_BASE = "https://paper-api.alpaca.markets"
HTTP_TIMEOUT = 12


# ── Account config ──────────────────────────────────────────
def load_accounts():
    """Load account list from ALPACA_ACCOUNTS env (JSON), or accounts.json locally."""
    raw = os.environ.get("ALPACA_ACCOUNTS", "").strip()
    if not raw and os.path.exists("accounts.json"):
        with open("accounts.json") as f:
            raw = f.read()
    if not raw:
        return []
    try:
        accts = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"[dashboard] ALPACA_ACCOUNTS is not valid JSON: {e}", flush=True)
        return []
    # Keep only well-formed entries; never trust partial config.
    clean = []
    for a in accts:
        if a.get("name") and a.get("key") and a.get("secret"):
            clean.append({
                "name":   str(a["name"]),
                "key":    str(a["key"]),
                "secret": str(a["secret"]),
                "paper":  bool(a.get("paper", False)),
            })
    return clean


ACCOUNTS = load_accounts()


# ── Auth (optional shared password) ─────────────────────────
def _check_auth():
    pw = os.environ.get("DASHBOARD_PASSWORD", "")
    if not pw:
        return True  # no gate configured
    auth = request.authorization
    return bool(auth and auth.password == pw)


def require_auth(fn):
    @wraps(fn)
    def wrapper(*a, **k):
        if not _check_auth():
            return Response(
                "Authentication required.", 401,
                {"WWW-Authenticate": 'Basic realm="Alpaca Dashboard"'},
            )
        return fn(*a, **k)
    return wrapper


# ── Alpaca helpers (server-side; keys never reach the browser) ──
def _headers(acct):
    return {
        "APCA-API-KEY-ID":     acct["key"],
        "APCA-API-SECRET-KEY": acct["secret"],
        "accept":              "application/json",
    }


def _base(acct):
    return PAPER_BASE if acct["paper"] else LIVE_BASE


def _get(acct, path, params=None):
    url = f"{_base(acct)}{path}"
    r = requests.get(url, headers=_headers(acct), params=params, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


def fetch_account_view(acct):
    """Assemble one account's full performance view from Alpaca ground truth."""
    account = _get(acct, "/v2/account")
    positions = _get(acct, "/v2/positions")
    try:
        hist = _get(acct, "/v2/account/portfolio/history",
                    {"period": "1M", "timeframe": "1D"})
    except Exception:
        hist = {"timestamp": [], "equity": []}

    equity      = float(account.get("equity", 0) or 0)
    last_equity = float(account.get("last_equity", 0) or 0)
    day_pl      = equity - last_equity
    day_pl_pct  = (day_pl / last_equity * 100) if last_equity else 0.0

    pos_out, unrealized = [], 0.0
    cls_map = {"us_equity": "EQUITY", "us_option": "OPTION", "crypto": "CRYPTO"}
    for p in positions:
        upl = float(p.get("unrealized_pl", 0) or 0)
        unrealized += upl
        pos_out.append({
            "symbol":      p.get("symbol", ""),
            "asset_class": cls_map.get(p.get("asset_class", ""), (p.get("asset_class", "") or "—").upper()),
            "qty":         float(p.get("qty", 0) or 0),
            "avg_entry":   float(p.get("avg_entry_price", 0) or 0),
            "current":     float(p.get("current_price", 0) or 0),
            "market_value": float(p.get("market_value", 0) or 0),
            "unrealized_pl": upl,
            "unrealized_plpc": float(p.get("unrealized_plpc", 0) or 0) * 100,
        })
    # Biggest movers first by absolute unrealized P&L
    pos_out.sort(key=lambda x: abs(x["unrealized_pl"]), reverse=True)

    return {
        "name":          acct["name"],
        "mode":          "PAPER" if acct["paper"] else "LIVE",
        "status":        account.get("status", ""),
        "equity":        equity,
        "last_equity":   last_equity,
        "cash":          float(account.get("cash", 0) or 0),
        "buying_power":  float(account.get("buying_power", 0) or 0),
        "day_pl":        day_pl,
        "day_pl_pct":    day_pl_pct,
        "unrealized_pl": unrealized,
        "positions":     pos_out,
        "history":       {
            "equity": [float(x) for x in (hist.get("equity") or [])],
        },
    }


# ── API routes ──────────────────────────────────────────────
@app.route("/api/accounts")
@require_auth
def api_accounts():
    # Names only — never expose keys to the client.
    return jsonify([{"idx": i, "name": a["name"], "mode": "PAPER" if a["paper"] else "LIVE"}
                    for i, a in enumerate(ACCOUNTS)])


@app.route("/api/account/<int:idx>")
@require_auth
def api_account(idx):
    if idx < 0 or idx >= len(ACCOUNTS):
        return jsonify({"error": "No such account."}), 404
    try:
        return jsonify(fetch_account_view(ACCOUNTS[idx]))
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        msg = "Alpaca rejected the request — check this account's keys and paper/live setting."
        if code == 403:
            msg = "Alpaca returned 403 — keys are invalid or lack permission for this environment."
        return jsonify({"error": f"{msg} (HTTP {code})", "name": ACCOUNTS[idx]["name"]}), 502
    except requests.RequestException as e:
        return jsonify({"error": f"Couldn't reach Alpaca: {e}", "name": ACCOUNTS[idx]["name"]}), 502


@app.route("/")
@require_auth
def index():
    return render_template_string(PAGE, has_accounts=bool(ACCOUNTS))


# ── Front-end (single page, no external JS libs) ────────────
PAGE = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Accounts — Live Performance</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,600;9..144,800&family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root{
    --bg:#0a0a0b; --panel:#111113; --line:#26262b; --ink:#ece7da;
    --muted:#8b8780; --verm:#e0492a; --green:#4caf72; --red:#e0492a;
    --mono:'IBM Plex Mono',monospace; --sans:'IBM Plex Sans',system-ui,sans-serif;
    --disp:'Fraunces',Georgia,serif;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--sans);
    -webkit-font-smoothing:antialiased;}
  .wrap{max-width:1080px;margin:0 auto;padding:28px 22px 80px;}
  .masthead{display:flex;justify-content:space-between;align-items:baseline;
    border-bottom:2px solid var(--ink);padding-bottom:10px;}
  .masthead h1{font-family:var(--disp);font-weight:800;font-size:26px;letter-spacing:-.01em;margin:0;}
  .masthead .tag{font-family:var(--mono);font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.14em;}
  /* account tabs */
  .tabs{display:flex;gap:0;flex-wrap:wrap;border-bottom:1px solid var(--line);margin-top:2px;}
  .tab{font-family:var(--mono);font-size:12px;letter-spacing:.04em;color:var(--muted);
    background:none;border:none;border-bottom:2px solid transparent;
    padding:13px 16px;cursor:pointer;text-transform:uppercase;}
  .tab:hover{color:var(--ink);}
  .tab.active{color:var(--ink);border-bottom-color:var(--verm);}
  .tab .mode{font-size:9px;opacity:.6;margin-left:6px;}
  /* hero */
  .hero{padding:26px 0 18px;border-bottom:1px solid var(--line);}
  .hero .acct{font-family:var(--mono);font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.16em;}
  .hero .equity{font-family:var(--disp);font-weight:600;font-size:54px;line-height:1;margin:8px 0 6px;letter-spacing:-.02em;}
  .hero .day{font-family:var(--mono);font-size:15px;}
  .pos{color:var(--green)} .neg{color:var(--red)}
  /* stat strip */
  .stats{display:grid;grid-template-columns:repeat(3,1fr);gap:1px;background:var(--line);
    border:1px solid var(--line);margin:18px 0;}
  .stat{background:var(--panel);padding:14px 16px;}
  .stat .k{font-family:var(--mono);font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.12em;}
  .stat .v{font-family:var(--mono);font-size:18px;margin-top:5px;}
  /* sparkline */
  .spark{margin:6px 0 22px;}
  .spark svg{width:100%;height:64px;display:block;}
  /* positions */
  .sechead{font-family:var(--mono);font-size:11px;color:var(--muted);text-transform:uppercase;
    letter-spacing:.16em;border-bottom:1px solid var(--line);padding-bottom:8px;margin:8px 0 0;}
  table{width:100%;border-collapse:collapse;font-family:var(--mono);font-size:13px;}
  th{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em;
    text-align:right;padding:11px 10px;border-bottom:1px solid var(--line);font-weight:500;}
  th:first-child{text-align:left;}
  td{padding:11px 10px;border-bottom:1px solid var(--line);text-align:right;}
  td:first-child{text-align:left;}
  .badge{font-size:9px;letter-spacing:.08em;padding:2px 6px;border:1px solid var(--line);
    color:var(--muted);margin-left:8px;vertical-align:middle;}
  .badge.OPTION{color:#d9a441;border-color:#5a4a25;}
  .badge.CRYPTO{color:#5aa9d9;border-color:#244a5a;}
  .badge.EQUITY{color:#9a96d9;border-color:#37355a;}
  .empty,.err{font-family:var(--mono);font-size:13px;color:var(--muted);padding:30px 4px;}
  .err{color:var(--verm);}
  .foot{font-family:var(--mono);font-size:10px;color:var(--muted);margin-top:26px;
    border-top:1px solid var(--line);padding-top:12px;letter-spacing:.04em;}
  @media(max-width:560px){.hero .equity{font-size:40px}.stats{grid-template-columns:1fr 1fr}}
</style>
</head>
<body>
<div class="wrap">
  <div class="masthead">
    <h1>The Ledger</h1>
    <span class="tag" id="clock">live · alpaca</span>
  </div>
  <div class="tabs" id="tabs"></div>
  <div id="view"></div>
  <div class="foot">Read-only · figures pulled live from each account's Alpaca records · crypto on external venues not shown</div>
</div>

<script>
const fmtUSD = n => (n<0?'-$':'$') + Math.abs(n).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
const fmtPct = n => (n>=0?'+':'') + n.toFixed(2) + '%';
const cls    = n => n>=0 ? 'pos' : 'neg';
let active = 0, accounts = [];

async function loadAccounts(){
  const r = await fetch('/api/accounts'); accounts = await r.json();
  const tabs = document.getElementById('tabs');
  if(!accounts.length){
    document.getElementById('view').innerHTML =
      '<div class="empty">No accounts configured yet. Set the ALPACA_ACCOUNTS environment variable (a JSON list of {name, key, secret, paper}) and reload. Adding an account later is just one more entry in that list.</div>';
    return;
  }
  tabs.innerHTML = accounts.map((a,i)=>
    `<button class="tab${i===0?' active':''}" data-i="${i}">${a.name}<span class="mode">${a.mode}</span></button>`).join('');
  tabs.querySelectorAll('.tab').forEach(t=>t.onclick=()=>{
    active = +t.dataset.i;
    tabs.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
    t.classList.add('active');
    loadAccount(active);
  });
  loadAccount(0);
}

function sparkline(eq){
  if(!eq || eq.length<2) return '';
  const w=1000,h=64,min=Math.min(...eq),max=Math.max(...eq),rng=(max-min)||1;
  const pts=eq.map((v,i)=>`${(i/(eq.length-1))*w},${h-((v-min)/rng)*(h-8)-4}`).join(' ');
  const up = eq[eq.length-1] >= eq[0];
  const color = up ? 'var(--green)' : 'var(--red)';
  return `<div class="spark"><svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
    <polyline points="${pts}" fill="none" stroke="${color}" stroke-width="2"/></svg></div>`;
}

async function loadAccount(i){
  const view = document.getElementById('view');
  view.innerHTML = '<div class="empty">Loading…</div>';
  let d;
  try{ d = await (await fetch('/api/account/'+i)).json(); }
  catch(e){ view.innerHTML = '<div class="err">Network error reaching the dashboard server.</div>'; return; }
  if(d.error){ view.innerHTML = `<div class="err">${d.name||''}: ${d.error}</div>`; return; }

  const rows = d.positions.length ? d.positions.map(p=>`
    <tr>
      <td>${p.symbol}<span class="badge ${p.asset_class}">${p.asset_class}</span></td>
      <td>${p.qty}</td>
      <td>${fmtUSD(p.avg_entry)}</td>
      <td>${fmtUSD(p.current)}</td>
      <td>${fmtUSD(p.market_value)}</td>
      <td class="${cls(p.unrealized_pl)}">${fmtUSD(p.unrealized_pl)} (${fmtPct(p.unrealized_plpc)})</td>
    </tr>`).join('') : '';

  view.innerHTML = `
    <div class="hero">
      <div class="acct">${d.name} · ${d.mode}${d.status&&d.status!=='ACTIVE'?' · '+d.status:''}</div>
      <div class="equity">${fmtUSD(d.equity)}</div>
      <div class="day ${cls(d.day_pl)}">${fmtUSD(d.day_pl)} (${fmtPct(d.day_pl_pct)}) today</div>
    </div>
    ${sparkline(d.history.equity)}
    <div class="stats">
      <div class="stat"><div class="k">Cash</div><div class="v">${fmtUSD(d.cash)}</div></div>
      <div class="stat"><div class="k">Buying power</div><div class="v">${fmtUSD(d.buying_power)}</div></div>
      <div class="stat"><div class="k">Open P&L</div><div class="v ${cls(d.unrealized_pl)}">${fmtUSD(d.unrealized_pl)}</div></div>
    </div>
    <div class="sechead">Positions · ${d.positions.length}</div>
    ${d.positions.length ? `<table>
      <thead><tr><th>Symbol</th><th>Qty</th><th>Entry</th><th>Last</th><th>Value</th><th>Open P&L</th></tr></thead>
      <tbody>${rows}</tbody></table>`
      : '<div class="empty">No open positions in this account.</div>'}`;
}

setInterval(()=>{ if(accounts.length) loadAccount(active); }, 30000); // refresh active account
loadAccounts();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    if not ACCOUNTS:
        print("[dashboard] WARNING: no accounts configured (set ALPACA_ACCOUNTS).", flush=True)
    if not os.environ.get("DASHBOARD_PASSWORD"):
        print("[dashboard] WARNING: DASHBOARD_PASSWORD not set — page is unprotected.", flush=True)
    app.run(host="0.0.0.0", port=port)
