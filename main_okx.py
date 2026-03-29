import requests
import hmac
import base64
import json
import os
import math
import time
from datetime import datetime
from config_okx import API_KEY, SECRET_KEY, PASSPHRASE

BASE_URL = "https://www.okx.com"
CSV_FILE = "trade_history.csv"
STATE_FILE = "state_okx.json"

SYMBOL = "LINK-USDT-SWAP"
INTERVAL = "5m"
LEVERAGE = 10          # 10x CONFIRMED
RISK_PCT = 0.005       # wallet ka 0.5%

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
        "x-simulated-trading": "1"   # LIVE me is line ko hata dena
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
        max_mkt = float(d.get("maxMktSz", 5000))
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

# ---------- SMC HELPERS ----------
def find_swings(closes, lookback=3):
    swings = []
    n = len(closes)
    for i in range(lookback, n - lookback):
        high = closes[i]
        low = closes[i]
        is_high = all(high >= closes[i - j] and high >= closes[i + j] for j in range(1, lookback + 1))
        is_low = all(low <= closes[i - j] and low <= closes[i + j] for j in range(1, lookback + 1))
        if is_high:
            swings.append(("HH", i, closes[i]))
        if is_low:
            swings.append(("LL", i, closes[i]))
    return swings

def detect_structure(swings):
    if len(swings) < 3:
        return None
    last = swings[-1]
    prev = swings[-2]
    prev2 = swings[-3]

    if prev[0] == "LL" and last[0] == "HH" and last[2] > prev2[2]:
        return "BOS_UP"
    if prev[0] == "HH" and last[0] == "LL" and last[2] < prev2[2]:
        return "BOS_DOWN"
    return None

def detect_choch(swings):
    if len(swings) < 4:
        return None
    last = swings[-1]
    prev = swings[-2]
    prev2 = swings[-3]
    prev3 = swings[-4]

    if prev3[0] == "HH" and prev[0] == "LL" and last[2] < prev2[2]:
        return "CHoCH_DOWN"
    if prev3[0] == "LL" and prev[0] == "HH" and last[2] > prev2[2]:
        return "CHoCH_UP"
    return None

def detect_liquidity_sweep(candles, side="long"):
    if len(candles) < 3:
        return False
    last = candles[-1]
    prev = candles[-2]

    high_last = float(last[2])
    low_last = float(last[3])
    high_prev = float(prev[2])
    low_prev = float(prev[3])

    if side == "long":
        return low_last < low_prev and float(last[4]) > float(prev[4])
    else:
        return high_last > high_prev and float(last[4]) < float(prev[4])

def detect_order_block(candles, side="long"):
    if len(candles) < 5:
        return None
    last5 = candles[-5:]
    if side == "long":
        for i in range(len(last5) - 2):
            c1 = last5[i]
            c2 = last5[i + 1]
            open1, close1 = float(c1[1]), float(c1[4])
            open2, close2 = float(c2[1]), float(c2[4])
            if close1 < open1 and close2 > open2 and (close2 - open2) > (open1 - close1):
                ob_low = float(c1[3])
                ob_high = float(c1[2])
                return (ob_low, ob_high)
    else:
        for i in range(len(last5) - 2):
            c1 = last5[i]
            c2 = last5[i + 1]
            open1, close1 = float(c1[1]), float(c1[4])
            open2, close2 = float(c2[1]), float(c2[4])
            if close1 > open1 and close2 < open2 and (open2 - close2) > (close1 - open1):
                ob_low = float(c1[3])
                ob_high = float(c1[2])
                return (ob_low, ob_high)
    return None

def detect_fvg(candles):
    if len(candles) < 3:
        return None
    c1, c2, c3 = candles[-3], candles[-2], candles[-1]
    high1 = float(c1[2])
    low1 = float(c1[3])
    high3 = float(c3[2])
    low3 = float(c3[3])

    if low3 > high1:
        return ("bull", high1, low3)
    if high3 < low1:
        return ("bear", high3, low1)
    return None

# ---------- CORE ----------
def run_cycle(symbol, interval):
    state = load_state()

    data = get_candles(symbol, interval)
    if data.get("code") != "0":
        return {"status": "Candle error"}

    candles = list(reversed(data["data"]))
    closes = [float(c[4]) for c in candles]

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

        if tp is None or sl is None:
            return {"status": "POSITION WITHOUT SLTP", "entry": entry, "size": size}

        if pos == "long":
            if price >= tp:
                close_position(symbol, pos, size)
                save_state(default_state())
                return {"status": "LONG TP HIT", "entry": entry, "tp": tp, "sl": sl, "size": size}
            if price <= sl:
                close_position(symbol, pos, size)
                save_state(default_state())
                return {"status": "LONG SL HIT", "entry": entry, "tp": tp, "sl": sl, "size": size}

        if pos == "short":
            if price <= tp:
                close_position(symbol, pos, size)
                save_state(default_state())
                return {"status": "SHORT TP HIT", "entry": entry, "tp": tp, "sl": sl, "size": size}
            if price >= sl:
                close_position(symbol, pos, size)
                save_state(default_state())
                return {"status": "SHORT SL HIT", "entry": entry, "tp": tp, "sl": sl, "size": size}

        return {
            "status": f"{pos} open",
            "entry": entry,
            "tp": tp,
            "sl": sl,
            "size": size
        }

    # ---------- SMC LOGIC ----------
    swings = find_swings(closes)
    structure = detect_structure(swings)
    choch = detect_choch(swings)
    fvg = detect_fvg(candles)

    side = None
    reason = None

    # Reversal entries
    if choch == "CHoCH_UP":
        if detect_liquidity_sweep(candles, side="long"):
            ob = detect_order_block(candles, side="long")
            if ob:
                ob_low, ob_high = ob
                if ob_low <= price <= ob_high:
                    side = "buy"
                    reason = "SMC_REVERSAL_LONG"

    if choch == "CHoCH_DOWN":
        if detect_liquidity_sweep(candles, side="short"):
            ob = detect_order_block(candles, side="short")
            if ob:
                ob_low, ob_high = ob
                if ob_low <= price <= ob_high:
                    side = "sell"
                    reason = "SMC_REVERSAL_SHORT"

    # Continuation entries
    if side is None and structure == "BOS_UP" and fvg and fvg[0] == "bull":
        _, f_low, f_high = fvg
        if f_low <= price <= f_high:
            side = "buy"
            reason = "SMC_CONTINUATION_LONG"

    if side is None and structure == "BOS_DOWN" and fvg and fvg[0] == "bear":
        _, f_low, f_high = fvg
        if f_low <= price <= f_high:
            side = "sell"
            reason = "SMC_CONTINUATION_SHORT"

    if side is None:
        return {"status": "No SMC signal", "structure": structure, "choch": choch}

    # --- DYNAMIC RISK ---
    balance = get_usdt_balance()
    if balance is None:
        return {"status": "Balance error"}

    risk = balance * RISK_PCT
    exposure = risk * LEVERAGE
    raw_size = exposure / price

    steps = max(1, math.floor(raw_size / lot))
    order_size = steps * lot
    order_size = min(order_size, max_limit)

    if order_size <= 0:
        return {"status": "Order size too small"}

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

    return {
        "status": f"{pos_side} opened ({reason})",
        "entry": entry,
        "tp": tp,
        "sl": sl,
        "size": size
    }

# ---------- MAIN ----------
def main():
    print(f"Bot started on {SYMBOL}, interval={INTERVAL}, lev={LEVERAGE}x")

    while True:
        info = run_cycle(SYMBOL, INTERVAL)
        print(datetime.utcnow().isoformat(), info)
        time.sleep(60)

if __name__ == "__main__":
    main()
