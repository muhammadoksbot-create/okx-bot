import requests
import hmac
import base64
import json
import pandas as pd
import os
import math
import time
from datetime import datetime
from config_okx import API_KEY, SECRET_KEY, PASSPHRASE

BASE_URL = "https://www.okx.com"
CSV_FILE = "trade_history.csv"
STATE_FILE = "state_okx.json"

SYMBOL = "LINK-USDT-SWAP"   # DOGE removed — LINK added
INTERVAL = "5m"
LEVERAGE = 10
RISK_PCT = 0.005   # wallet ka 0.5% — Unified Account safe

# ---------- SIGN ----------
def sign(message, secret_key):
    return base64.b64encode(
        hmac.new(secret_key.encode(), message.encode(), digestmod="sha256").digest()
    ).decode()

# ---------- HEADERS ----------
def get_headers(method, path, body=""):
    timestamp = datetime.utcnow().isoformat("T", "milliseconds") + "Z"
    message = timestamp + method + path + body
    signature = sign(message, SECRET_KEY)

    return {
        "OK-ACCESS-KEY": API_KEY,
        "OK-ACCESS-SIGN": signature,
        "OK-ACCESS-TIMESTAMP": timestamp,
        "OK-ACCESS-PASSPHRASE": PASSPHRASE,
        "Content-Type": "application/json",
        "x-simulated-trading": "1"   # DEMO MODE — LIVE me is line ko hata dena
    }

# ---------- STATE ----------
def default_state():
    return {
        "position": None,
        "entry": None,
        "tp": None,
        "sl": None,
        "size": None,
        "symbol": None
    }

def load_state():
    if not os.path.exists(STATE_FILE):
        return default_state()
    with open(STATE_FILE, "r") as f:
        return json.load(f)

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

# ---------- INSTRUMENT LIMITS ----------
def get_instrument_limits(symbol):
    path = f"/api/v5/public/instruments?instType=SWAP&instId={symbol}"
    r = requests.get(BASE_URL + path, headers=get_headers("GET", path)).json()

    if r.get("code") == "0" and r.get("data"):
        d = r["data"][0]
        lot = float(d["lotSz"])
        max_mkt = float(d.get("maxMktSz", 5000))  # LINK ka limit DOGE se chhota hota hai
        return lot, max_mkt

    return 1.0, 5000

# ---------- API ----------
def get_candles(symbol, interval, limit=200):
    path = f"/api/v5/market/candles?instId={symbol}&bar={interval}&limit={limit}"
    return requests.get(BASE_URL + path, headers=get_headers("GET", path)).json()

def get_ticker(symbol):
    path = f"/api/v5/market/ticker?instId={symbol}"
    r = requests.get(BASE_URL + path, headers=get_headers("GET", path)).json()

    if r.get("code") == "0" and r.get("data"):
        d = r["data"][0]
        last = float(d["last"])
        mark = float(d.get("markPx", last))
        return {"last": last, "mark": mark}

    return {"last": None, "mark": None}

def place_market_order(symbol, side, size):
    path = "/api/v5/trade/order"
    body = json.dumps({
        "instId": symbol,
        "tdMode": "cross",
        "side": side,
        "ordType": "market",
        "sz": str(size)
    })

    try:
        return requests.post(BASE_URL + path, headers=get_headers("POST", path, body), data=body).json()
    except Exception as e:
        return {"code": "-1", "msg": f"REQUEST ERROR: {e}"}

def close_position(symbol, side, size):
    opposite = "sell" if side == "long" else "buy"
    return place_market_order(symbol, opposite, size)

# ---------- POSITION ----------
def get_open_position_once(symbol):
    path = "/api/v5/account/positions"
    r = requests.get(BASE_URL + path, headers=get_headers("GET", path)).json()

    if r.get("code") != "0":
        return None

    for p in r.get("data", []):
        if p.get("instId") != symbol:
            continue

        pos_val = float(p.get("pos", "0"))
        if pos_val == 0:
            continue

        return {
            "position": "long" if pos_val > 0 else "short",
            "entry": float(p.get("avgPx", 0)),
            "size": abs(pos_val)
        }

    return None

def get_open_position_double_check(symbol):
    p1 = get_open_position_once(symbol)
    p2 = get_open_position_once(symbol)

    if p1 is None and p2 is None:
        return None

    if p1 and p2 and p1["position"] == p2["position"]:
        return p1

    return "MISMATCH"

# ---------- BALANCE ----------
def get_usdt_balance():
    path = "/api/v5/account/balance"
    r = requests.get(BASE_URL + path, headers=get_headers("GET", path)).json()

    if r.get("code") != "0":
        return None

    for d in r["data"][0]["details"]:
        if d["ccy"] == "USDT":
            return float(d.get("availBal", d.get("eq", 0)))

    return None

# ---------- INDICATORS ----------
def ema(values, period):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e

# ---------- CORE ----------
def run_cycle(symbol, interval):
    state = load_state()

    # --- CANDLES ---
    data = get_candles(symbol, interval)
    if data.get("code") != "0":
        return {"status": "Candle error"}

    candles = list(reversed(data["data"]))
    closes = [float(c[4]) for c in candles]

    ema9 = ema(closes, 9)
    ema21 = ema(closes, 21)

    ticker = get_ticker(symbol)
    price = ticker["mark"]

    lot, max_limit = get_instrument_limits(symbol)

    # --- SYNC ---
    exch_pos = get_open_position_double_check(symbol)

    if exch_pos == "MISMATCH":
        return {"status": "SYNC MISMATCH"}

    if exch_pos:
        state.update(exch_pos)
        save_state(state)
    else:
        state = default_state()
        save_state(state)

    # --- MANAGE OPEN POSITION ---
    if state["position"]:
        pos = state["position"]
        entry = state["entry"]
        tp = state["tp"]
        sl = state["sl"]
        size = state["size"]

        if pos == "long":
            if price >= tp:
                close_position(symbol, pos, size)
                save_state(default_state())
                return {"status": "LONG TP HIT"}

            if price <= sl:
                close_position(symbol, pos, size)
                save_state(default_state())
                return {"status": "LONG SL HIT"}

        if pos == "short":
            if price <= tp:
                close_position(symbol, pos, size)
                save_state(default_state())
                return {"status": "SHORT TP HIT"}

            if price >= sl:
                close_position(symbol, pos, size)
                save_state(default_state())
                return {"status": "SHORT SL HIT"}

        return {"status": f"{pos} open"}

    # --- ENTRY SIGNAL ---
    side = None
    if ema9 > ema21:
        side = "buy"
    elif ema9 < ema21:
        side = "sell"
    else:
        return {"status": "No signal"}

    # --- DYNAMIC RISK ---
    balance = get_usdt_balance()
    risk = balance * RISK_PCT
    exposure = risk * LEVERAGE
    raw_size = exposure / price

    # --- LOT FIX ---
    steps = max(1, math.floor(raw_size / lot))
    order_size = steps * lot

    # --- REAL-TIME MAX LIMIT FIX ---
    order_size = min(order_size, max_limit)

    # --- PLACE ORDER ---
    order = place_market_order(symbol, side, order_size)

    if order.get("code") != "0":
        return {"status": "ORDER FAILED", "order": order}

    exch_after = get_open_position_double_check(symbol)
    if not exch_after:
        return {"status": "ENTRY LOCK FAILED"}

    entry = exch_after["entry"]
    size = exch_after["size"]

    SL_PCT = 0.003
    TP_PCT = 0.003

    if side == "buy":
        sl = entry * (1 - SL_PCT)
        tp = entry * (1 + TP_PCT)
        pos_side = "long"
    else:
        sl = entry * (1 + SL_PCT)
        tp = entry * (1 - TP_PCT)
        pos_side = "short"

    state.update({
        "position": pos_side,
        "entry": entry,
        "tp": tp,
        "sl": sl,
        "size": size,
        "symbol": symbol
    })
    save_state(state)

    return {"status": f"{pos_side} opened"}

# ---------- MAIN ----------
def main():
    print(f"Bot started on {SYMBOL}, interval={INTERVAL}, lev={LEVERAGE}x")

    while True:
        info = run_cycle(SYMBOL, INTERVAL)
        print(datetime.utcnow().isoformat(), info)
        time.sleep(60)   # 1-minute cycle

if __name__ == "__main__":
    main()
