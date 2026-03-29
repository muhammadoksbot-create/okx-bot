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

# FIXED SETTINGS
SYMBOL = "DOGE-USDT-SWAP"   # fixed pair
INTERVAL = "15m"            # fixed timeframe
LEVERAGE = 10               # 10x
RISK_PER_TRADE = 10.0       # 10 USDT per trade (max loss)

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

    headers = {
        "OK-ACCESS-KEY": API_KEY,
        "OK-ACCESS-SIGN": signature,
        "OK-ACCESS-TIMESTAMP": timestamp,
        "OK-ACCESS-PASSPHRASE": PASSPHRASE,
        "Content-Type": "application/json",
    }

    # DEMO / PAPER TRADING FLAG
    headers["x-simulated-trading"] = "1"

    return headers

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
        "tdMode": "isolated",
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

# ---------- NET-MODE SAFE POSITION READER ----------
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

# ---------- HARD SYNC LOCK ----------
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
    details = r["data"][0].get("details", [])
    for d in details:
        if d["ccy"] == "USDT":
            # availBal = free margin, eq = total equity
            avail = float(d.get("availBal", d.get("eq", 0)))
            return avail
    return None

# ---------- LEVERAGE ----------
def set_leverage(symbol, lever=10):
    path = "/api/v5/account/set-leverage"
    body = json.dumps({
        "instId": symbol,
        "lever": str(lever),
        "mgnMode": "isolated"
    })
    return requests.post(BASE_URL + path, headers=get_headers("POST", path, body), data=body).json()

# ---------- INDICATORS ----------
def ema(values, period):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e

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

# ---------- CSV SAVE ----------
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

# ---------- CORE CYCLE (EMA 9/21 PULLBACK) ----------
def run_cycle(symbol, interval):
    state = load_state()

    # --- GET CANDLES ---
    data = get_candles(symbol, interval)
    if data.get("code") != "0":
        info = {"status": "Candle error", "error": data}
        return attach_state_snapshot(info, state)

    candles = list(reversed(data["data"]))
    closes = [float(c[4]) for c in candles]
    highs  = [float(c[2]) for c in candles]
    lows   = [float(c[3]) for c in candles]

    if len(closes) < 30:
        info = {"status": "Not enough candles"}
        return attach_state_snapshot(info, state)

    ema9_series_vals  = ema_series(closes, 9)
    ema21_series_vals = ema_series(closes, 21)

    ema9  = ema9_series_vals[-1] if ema9_series_vals else None
    ema21 = ema21_series_vals[-1] if ema21_series_vals else None

    ticker = get_ticker(symbol)
    chart_price = ticker["last"]
    mark_price = ticker["mark"]
    price = mark_price if mark_price is not None else closes[-1]

    info = {
        "chart_price": chart_price,
        "mark_price": mark_price,
        "ema9": ema9,
        "ema21": ema21,
    }

    # ---------- HARD SYNC LOCK ----------
    exch_pos = get_open_position_double_check(symbol)

    if exch_pos == "MISMATCH":
        info["status"] = "SYNC MISMATCH — blocking trades"
        return attach_state_snapshot(info, state)

    # ---------- EXCHANGE-FIRST OVERRIDE ----------
    if exch_pos:
        state["position"] = exch_pos["position"]
        state["entry"] = exch_pos["entry"]
        state["size"] = exch_pos["size"]
        state["symbol"] = symbol
        save_state(state)
    else:
        state = default_state()
        save_state(state)

    # ---------- MANAGE OPEN POSITION ----------
    pos = state.get("position")
    if pos:
        entry = state["entry"]
        tp = state["tp"]
        sl = state["sl"]
        size = state["size"]

        if tp is None or sl is None:
            info["status"] = f"{pos} open (TP/SL missing)"
            return attach_state_snapshot(info, state)

        if pos == "long":
            if price >= tp:
                close_position(symbol, pos, size)
                pnl = (tp - entry) * size
                save_trade({
                    "time": datetime.utcnow().isoformat(),
                    "symbol": symbol,
                    "side": "LONG",
                    "entry": entry,
                    "exit": tp,
                    "pnl": pnl,
                    "result": "TP HIT"
                })
                state = default_state()
                save_state(state)
                info["status"] = "LONG TP HIT"
                return attach_state_snapshot(info, state)

            if price <= sl:
                close_position(symbol, pos, size)
                pnl = (sl - entry) * size
                save_trade({
                    "time": datetime.utcnow().isoformat(),
                    "symbol": symbol,
                    "side": "LONG",
                    "entry": entry,
                    "exit": sl,
                    "pnl": pnl,
                    "result": "SL HIT"
                })
                state = default_state()
                save_state(state)
                info["status"] = "LONG SL HIT"
                return attach_state_snapshot(info, state)

        if pos == "short":
            if price <= tp:
                close_position(symbol, pos, size)
                pnl = (entry - tp) * size
                save_trade({
                    "time": datetime.utcnow().isoformat(),
                    "symbol": symbol,
                    "side": "SHORT",
                    "entry": entry,
                    "exit": tp,
                    "pnl": pnl,
                    "result": "TP HIT"
                })
                state = default_state()
                save_state(state)
                info["status"] = "SHORT TP HIT"
                return attach_state_snapshot(info, state)

            if price >= sl:
                close_position(symbol, pos, size)
                pnl = (entry - sl) * size
                save_trade({
                    "time": datetime.utcnow().isoformat(),
                    "symbol": symbol,
                    "side": "SHORT",
                    "entry": entry,
                    "exit": sl,
                    "pnl": pnl,
                    "result": "SL HIT"
                })
                state = default_state()
                save_state(state)
                info["status"] = "SHORT SL HIT"
                return attach_state_snapshot(info, state)

        info["status"] = f"{pos} open"
        return attach_state_snapshot(info, state)

    # ---------- NEW ENTRY (EMA 9/21 PULLBACK) ----------
    if ema9 is None or ema21 is None or chart_price is None:
        info["status"] = "Indicators not ready"
        return attach_state_snapshot(info, state)

    last_close = closes[-1]
    prev_close = closes[-2]
    last_ema9  = ema9_series_vals[-1]
    prev_ema9  = ema9_series_vals[-2]

    side = None
    decision = None

    # LONG:
    if ema9 > ema21 and prev_close < prev_ema9 and last_close > last_ema9:
        side = "buy"
        decision = "LONG_EMA_PULLBACK"

    # SHORT:
    elif ema9 < ema21 and prev_close > prev_ema9 and last_close < last_ema9:
        side = "sell"
        decision = "SHORT_EMA_PULLBACK"

    if not side:
        info["status"] = "No signal (EMA pullback)"
        return attach_state_snapshot(info, state)

    balance = get_usdt_balance()
    if balance is None:
        info["status"] = "Balance error"
        return attach_state_snapshot(info, state)

    # ---- SAFETY: agar balance bohot kam ho to skip ----
    if balance < RISK_PER_TRADE * 1.2:
        info["status"] = f"Balance too low for risk={RISK_PER_TRADE}, avail={balance}"
        return attach_state_snapshot(info, state)

    # ---- FIXED RISK: 10 USDT PER TRADE + 10x LEVERAGE ----
    risk = min(RISK_PER_TRADE, balance * 0.9)
    exposure = risk * LEVERAGE
    raw_size = exposure / price

    lot = get_lot_size(symbol)
    steps = math.floor(raw_size / lot)
    if steps <= 0:
        info["status"] = "Order size too small"
        return attach_state_snapshot(info, state)

    order_size = steps * lot
    pos_side = "long" if side == "buy" else "short"

    order = place_market_order(symbol, side, order_size)
    info["order"] = order
    info["decision"] = decision
    info["order_size"] = order_size
    info["balance"] = balance

    if order.get("code") != "0":
        msg = order.get("msg", "unknown error")
        # 51008 = margin/balance issue
        if order.get("code") == "1":
            # sCode inside data
            try:
                sCode = order["data"][0].get("sCode")
                sMsg = order["data"][0].get("sMsg")
                msg = f"{msg} | sCode={sCode}, sMsg={sMsg}"
            except:
                pass
        info["status"] = f"ORDER FAILED: {msg}"
        return attach_state_snapshot(info, state)

    exch_after = get_open_position_double_check(symbol)
    if exch_after == "MISMATCH" or not exch_after:
        info["status"] = "ENTRY LOCK FAILED"
        return attach_state_snapshot(info, state)

    real_entry = exch_after["entry"]
    real_size = exch_after["size"]

    # --- TP/SL (abhi 0.3% / 0.3%) ---
    SL_PCT = 0.003
    TP_PCT = 0.003

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
    info["status"] = f"{pos_side} opened (EMA pullback)"
    info["tp_pct"] = TP_PCT * 100
    info["sl_pct"] = SL_PCT * 100
    return attach_state_snapshot(info, state)

# ---------- SIMPLE MAIN LOOP (BACKEND) ----------
def main():
    set_leverage(SYMBOL, LEVERAGE)
    print(f"Bot started on {SYMBOL}, interval={INTERVAL}, lev={LEVERAGE}x, risk={RISK_PER_TRADE} USDT")

    while True:
        try:
            info = run_cycle(SYMBOL, INTERVAL)
            print(datetime.utcnow().isoformat(), info)
        except Exception as e:
            print("ERROR in cycle:", e)
        time.sleep(60)

if __name__ == "__main__":
    main()
