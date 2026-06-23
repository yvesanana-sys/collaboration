# Multi-Account Alpaca Dashboard

A standalone, read-only performance dashboard for one or more Alpaca accounts.
One page, a tab per account, each showing **only that account's** equity, day
P&L, positions (stocks · options · crypto), and 30-day equity curve. Optionally
shows each account's Binance.US crypto holdings too.

It is completely separate from the trading bot — it never imports or modifies
any bot file, and it only ever issues **GET** requests (it cannot place trades).

---

## Files

- `alpaca_dashboard.py` — the whole app (Flask + embedded page, one file)
- `requirements.txt` — `flask`, `requests`

---

## Configure

Everything is driven by one environment variable, `ALPACA_ACCOUNTS`, a JSON list.
**Adding an account = adding one entry. No code change.**

```json
[
  {
    "name": "Hanz",
    "key": "AK_your_alpaca_key",
    "secret": "your_alpaca_secret",
    "paper": false,
    "binance_key": "optional_binanceus_read_key",
    "binance_secret": "optional_binanceus_read_secret"
  },
  {
    "name": "Mom",
    "key": "AK_moms_alpaca_key",
    "secret": "moms_alpaca_secret",
    "paper": true
  }
]
```

- `paper`: `true` uses `paper-api.alpaca.markets`, `false` uses live `api.alpaca.markets`.
- `binance_key` / `binance_secret`: **optional.** Include them only if you want
  that account's Binance.US crypto shown. Accounts without them simply omit the
  crypto panel.

Optional extra env vars:

- `DASHBOARD_PASSWORD` — if set, the page requires HTTP Basic auth (any username,
  this password). **Set this** if the dashboard is reachable on the public internet.
- `PORT` — defaults to `8080`. Railway sets this automatically.

---

## Run locally

```bash
pip install -r requirements.txt
export ALPACA_ACCOUNTS='[{"name":"Test","key":"...","secret":"...","paper":true}]'
export DASHBOARD_PASSWORD='choose-something'
python alpaca_dashboard.py
# open http://localhost:8080  (username: anything, password: what you set)
```

---

## Deploy on Railway (its own service, separate from the bot)

1. Put `alpaca_dashboard.py` and `requirements.txt` in a repo or subfolder.
2. New Railway service → point it at that repo/folder.
3. Start command: `python alpaca_dashboard.py`
4. Add variables: `ALPACA_ACCOUNTS` (the JSON above) and `DASHBOARD_PASSWORD`.
5. Deploy. Railway provides `PORT`; the app binds to it automatically.

Because it's a separate service, it has zero effect on the trading bot — deploy,
restart, or break this without touching production.

---

## Security notes — read these

- **Read-only by design.** The app only calls Alpaca's `/account`, `/positions`,
  and `/portfolio/history`, and Binance.US `/account` + public prices. It never
  submits or cancels an order.
- **Use restricted keys anyway.** On Binance.US, create API keys with **Reading
  only** (no trading, no withdrawals). On Alpaca, the trading key is what's
  available, but this app never trades with it — still, treat the keys as secrets.
- **Keys stay server-side.** They live in the `ALPACA_ACCOUNTS` env var and are
  used only in server-to-broker calls. The browser only ever receives computed
  numbers — never a key. (The account-list endpoint returns names only.)
- **Always set `DASHBOARD_PASSWORD`** for any internet-reachable deployment. The
  page shows real balances and positions.

---

## What it shows / doesn't

- **Shows:** per-account Alpaca equity, today's P&L, buying power, open positions
  across equities / options / crypto (tagged), a 30-day equity sparkline, and —
  if Binance keys are provided — that account's Binance.US holdings valued in USD.
- **Doesn't show:** the bot's internal Claude-vs-Grok attribution (that lives in
  the bot's state, not in Alpaca). These figures are broker ground truth, which
  is the honest per-account number.
