import os
import time
import json
import httpx
import requests
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from flask import Flask, jsonify, request
from flask_cors import CORS

# ── Environment variables ────────────────────────────────────
ALPACA_KEY     = os.environ.get("ALPACA_KEY", "")
ALPACA_SECRET  = os.environ.get("ALPACA_SECRET", "")
ANTHROPIC_KEY  = os.environ.get("ANTHROPIC_KEY", "") or os.environ.get("ANTHROPIC_API_KEY", "")
GROK_KEY       = os.environ.get("GROK_KEY", "")
GITHUB_TOKEN   = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO    = os.environ.get("GITHUB_REPO", "")
GITHUB_BRANCH  = os.environ.get("GITHUB_BRANCH", "main")
BASE_URL       = "https://api.alpaca.markets"
DATA_URL       = "https://data.alpaca.markets"
BOT_NAME       = "NovaTrade"
PORT           = int(os.environ.get("PORT", 8080))

CRYPTO_TRADING_ENABLED = False

app = Flask(__name__)
CORS(app)
