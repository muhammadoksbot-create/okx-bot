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

SYMBOL = "DOGE-USDT-SWAP"
INTERVAL = "5m"       # candle timeframe
LEVERAGE = 10         # 10x leverage
RISK_PCT = 0.10       # wallet ka 10%

MAX_ORDER_SIZE = 200000  # safety cap (e.g. 200k DOGE)

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
        "x-simulated-trading": "1"   # DEMO MODE; LIVE me is line ko hata dena
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

# ---------- LOT SIZE ----------
def get_lot_size(symbol):
    path = f"/api/v5/public/instruments?instType=SWAP&instId={symbol}"
    r = requests.get(BASE_URL + path, headers=get_headers("GET", path)).json()
    if r.get("code") == "0" and r.get("data"):
        return float(r["data"][0]["lotSz"])
    return 1.0

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
        "tdMode": "cross",   # cross margin
        "side": side,
        "ordType": "market",
        "sz": str(size)
    })

    try:
        r = requests.post(
            BASE_URL + path,
            headers=get_headers("POST", path, body),
            data=body
        ).json()
    except Exception as e:
        return {"code": "-1", "msg": f"REQUEST ERROR: {e}"}

    return r

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

        pos_raw = p.get("pos", "0")

        try:
            pos_val = float(pos_raw)
        except:
            pos_val = 0.0

        if pos_val == 0:
            continue

        side = "long" if pos_val > 0 else "short"
        entry = float(p.get("avgPx", 0))
        size = abs(pos_val)

        return {
            "position": side,
            "entry": entry,
            "size": size
        }

    return None

def get_open_position_double_check(symbol):
    p1 = get_open_position_once(symbol)
    p2 = get_open_position_once(symbol)

    if p1 is None and p2 is None:
        return None

    if (p1 and p2 and
        p1["position"] == p2["position"] and
        abs(p1["entry"] - p2["entry"]) < 0.5 and
        abs(p1["size"] - p2["size"]) < 1e-6):
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
def ema_series(values, period):
    if len(values) < period:
        return []
    k = 2 / (period + 1)
    out = []
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out

# ---------- CSV ----------
def save_trade(data):
    df = pd.DataFrame([data])
    df.to_csv(CSV_FILE, mode="a", header=not os.path.exists(CSV_FILE), index=False)

# ---------- SNAPSHOT ----------
def attach_state_snapshot(info, state):
    info["position"] = state.get("position")
    info["entry"] = state.get("entry")
    info["tp"] = state.get("tp")
    info["sl"] = state.get("sl")
    info["size"] = state.get("size")
    info["symbol"] = state.get("symbol")
    return info

# ---------- CORE ----------
def run_cycle(symbol, interval):
    state = load_state()

    # --- CANDLES ---
    data = get_candles(symbol, interval)
    if data.get("code") != "0":
        return attach_state_snapshot({"status": "Candle error", "error": data}, state)

    candles = list(reversed(data["data"]))
    closes = [float(c[4]) for c in candles]

    if len(closes) < 30:
        return attach_state_snapshot({"status": "Not enough candles"}, state)

    ema9_series = ema_series(closes, 9)
    ema21_series = ema_series(closes, 21)

    ema9 = ema9_series[-1]
    ema21 = ema21_series[-1]

    ticker = get_ticker(symbol)
    price = ticker["mark"]

    info = {
        "chart_price": ticker["last"],
        "mark_price": price,
        "ema9": ema9,
        "ema21": ema21,
    }

    # --- SYNC ---
    exch_pos = get_open_position_double_check(symbol)

    if exch_pos == "MISMATCH":
        return attach_state_snapshot({"status": "SYNC MISMATCH"}, state)

    if exch_pos:
        state.update({
            "position": exch_pos["position"],
            "entry": exch_pos["entry"],
            "size": exch_pos["size"],
            "symbol": symbol
        })
        save_state(state)
    else:
        state = default_state()
        save_state(state)

    # --- MANAGE OPEN POSITION ---
    pos = state.get("position")
    if pos:
        entry = state["entry"]
        tp = state["tp"]
        sl = state["sl"]
        size = state["size"]

        if pos == "long":
            if price >= tp:
                close_position(symbol, pos, size)
                state = default_state()
                save_state(state)
                return attach_state_snapshot({"status": "LONG TP HIT"}, state)

            if price <= sl:
                close_position(symbol, pos, size)
                state = default_state()
                save_state(state)
                return attach_state_snapshot({"status": "LONG SL HIT"}, state)

        if pos == "short":
            if price <= tp:
                close_position(symbol, pos, size)
                state = default_state()
                save_state(state)
                return attach_state_snapshot({"status": "SHORT TP HIT"}, state)

            if price >= sl:
                close_position(symbol, pos, size)
                state = default_state()
                save_state(state)
                return attach_state_snapshot({"status": "SHORT SL HIT"}, state)

        return attach_state_snapshot({"status": f"{pos} open"}, state)

    # --- SIMPLE TREND ENTRY (EMA) ---
    side = None
    decision = None

    if ema9 > ema21:
        side = "buy"
        decision = "TREND_LONG"
    elif ema9 < ema21:
        side = "sell"
        decision = "TREND_SHORT"

    if not side:
        return attach_state_snapshot({"status": "No signal (trend EMA)"}, state)

    # --- DYNAMIC 10% WALLET RISK ---
    balance = get_usdt_balance()
    if balance is None:
        return attach_state_snapshot({"status": "Balance error"}, state)

    risk = balance * RISK_PCT          # wallet ka 10%
    exposure = risk * LEVERAGE         # 10x leverage
    raw_size = exposure / price

    lot = get_lot_size(symbol)

    # LOT FIX + MIN 1 LOT
    steps = max(1, math.floor(raw_size / lot))
    order_size = steps * lot

    # MAX CAP
    order_size = min(order_size, MAX_ORDER_SIZE)

    if order_size <= 0:
        return attach_state_snapshot({"status": "Order size too small"}, state)

    pos_side = "long" if side == "buy" else "short"

    order = place_market_order(symbol, side, order_size)

    if order.get("code") != "0":
        return attach_state_snapshot({
            "status": "ORDER FAILED",
            "order": order
        }, state)

    exch_after = get_open_position_double_check(symbol)
    if not exch_after or exch_after == "MISMATCH":
        return attach_state_snapshot({"status": "ENTRY LOCK FAILED"}, state)

    real_entry = exch_after["entry"]
    real_size = exch_after["size"]

    SL_PCT = 0.003   # 0.3%
    TP_PCT = 0.003   # 0.3%

    if pos_side == "long":
        sl = real_entry * (1 - SL_PCT)
        tp = real_entry * (1 + TP_PCT)
    else:
        sl = real_entry * (1 + SL_PCT)
        tp = real_entry * (1 - TP_PCT)

    state.update({
        "position": pos_side,
        "entry": real_entry,
        "tp": tp,
        "sl": sl,
        "size": real_size,
        "symbol": symbol
    })
    save_state(state)

    return attach_state_snapshot({
        "status": f"{pos_side} opened (TREND EMA)",
        "decision": decision
    }, state)

# ---------- MAIN ----------
def main():
    print(f"Bot started on {SYMBOL}, interval={INTERVAL}, lev={LEVERAGE}x")

    while True:
        try:
            info = run_cycle(SYMBOL, INTERVAL)
            print(datetime.utcnow().isoformat(), info)
        except Exception as e:
            print("ERROR:", e)

        time.sleep(60)   # 1-minute cycle

if __name__ == "__main__":
    main()
