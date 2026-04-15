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
STATE_FILE = "state_okx.json"

SYMBOL = "LINK-USDT-SWAP"
INTERVAL = "5m"
LEVERAGE = 5            # 5x leverage
POSITION_PCT = 0.50     # Wallet ka 50%

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
        "Content-Type": "application/json"
    }

# ---------- STATE ----------
def default_state():
    return {"position": None, "entry": None, "tp": None, "sl": None, "size": None}

def load_state():
    if not os.path.exists(STATE_FILE):
        return default_state()
    with open(STATE_FILE, "r") as f:
        return json.load(f)

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

# ---------- API ----------
def get_candles(symbol, interval, limit=200):
    path = f"/api/v5/market/candles?instId={symbol}&bar={interval}&limit={limit}"
    return requests.get(BASE_URL + path, headers=get_headers("GET", path)).json()

def get_ticker(symbol):
    path = f"/api/v5/market/ticker?instId={symbol}"
    r = requests.get(BASE_URL + path, headers=get_headers("GET", path)).json()
    if r.get("code") == "0" and r.get("data"):
        d = r["data"][0]
        return float(d.get("markPx", d.get("last", 0)))
    return None

def place_market_order(symbol, side, size):
    path = "/api/v5/trade/order"
    body = json.dumps({
        "instId": symbol,
        "tdMode": "cross",
        "side": side,
        "ordType": "market",
        "sz": str(size)
    })
    return requests.post(BASE_URL + path, headers=get_headers("POST", path, body), data=body).json()

def close_position(symbol, side, size):
    opposite = "sell" if side == "long" else "buy"
    return place_market_order(symbol, opposite, size)

def get_open_position(symbol):
    path = "/api/v5/account/positions"
    r = requests.get(BASE_URL + path, headers=get_headers("GET", path)).json()
    if r.get("code") != "0":
        return None
    for p in r["data"]:
        if p["instId"] == symbol and float(p["pos"]) != 0:
            return {
                "position": "long" if float(p["pos"]) > 0 else "short",
                "entry": float(p["avgPx"]),
                "size": abs(float(p["pos"]))
            }
    return None

def get_usdt_balance():
    path = "/api/v5/account/balance"
    r = requests.get(BASE_URL + path, headers=get_headers("GET", path)).json()
    if r.get("code") != "0":
        return None
    for d in r["data"][0]["details"]:
        if d["ccy"] == "USDT":
            return float(d.get("availBal", d.get("eq", 0)))
    return None
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
    last, prev, prev2 = swings[-1], swings[-2], swings[-3]
    if prev[0] == "LL" and last[0] == "HH" and last[2] > prev2[2]:
        return "BOS_UP"
    if prev[0] == "HH" and last[0] == "LL" and last[2] < prev2[2]:
        return "BOS_DOWN"
    return None

def detect_choch(swings):
    if len(swings) < 4:
        return None
    last, prev, prev2, prev3 = swings[-1], swings[-2], swings[-3], swings[-4]
    if prev3[0] == "HH" and prev[0] == "LL" and last[2] < prev2[2]:
        return "CHoCH_DOWN"
    if prev3[0] == "LL" and prev[0] == "HH" and last[2] > prev2[2]:
        return "CHoCH_UP"
    return None

def detect_liquidity_sweep(candles, side="long"):
    if len(candles) < 3:
        return False
    last, prev = candles[-1], candles[-2]
    if side == "long":
        return float(last[3]) < float(prev[3])
    return float(last[2]) > float(prev[2])

def detect_order_block(candles, side="long"):
    if len(candles) < 5:
        return None
    for c in candles[-5:]:
        if side == "long" and float(c[4]) < float(c[1]):
            return (float(c[3]), float(c[2]))
        if side == "short" and float(c[4]) > float(c[1]):
            return (float(c[3]), float(c[2]))
    return None

def detect_fvg(candles):
    if len(candles) < 3:
        return None
    c1, _, c3 = candles[-3], candles[-2], candles[-1]
    if float(c3[3]) > float(c1[2]):
        return ("bull", float(c1[2]), float(c3[3]))
    if float(c3[2]) < float(c1[3]):
        return ("bear", float(c3[2]), float(c1[3]))
    return None

# ---------- CORE ----------
def run_cycle(symbol, interval):
    state = load_state()

    data = get_candles(symbol, interval)
    if data.get("code") != "0":
        return {"status": "Candle error"}

    candles = list(reversed(data["data"]))
    closes = [float(c[4]) for c in candles]

    price = get_ticker(symbol)
    if price is None:
        return {"status": "Price error"}

    # Sync with exchange
    exch_pos = get_open_position(symbol)
    if exch_pos:
        state.update(exch_pos)
        save_state(state)
    else:
        state = default_state()
        save_state(state)

    # ---------- PRINT CURRENT POSITION ----------
    if state["position"]:
        print("\n--- CURRENT OPEN POSITION ---")
        print("Side:", state["position"])
        print("Entry:", state["entry"])
        print("TP:", state["tp"])
        print("SL:", state["sl"])
        print("Size:", state["size"])
        print("-----------------------------\n")

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

    # ---------- SMC LOGIC ----------
    swings = find_swings(closes)
    structure = detect_structure(swings)
    choch = detect_choch(swings)
    fvg = detect_fvg(candles)

    side = None
    reason = None

    if choch == "CHoCH_UP" and detect_liquidity_sweep(candles, "long"):
        side, reason = "buy", "SMC_REVERSAL_LONG"

    if choch == "CHoCH_DOWN" and detect_liquidity_sweep(candles, "short"):
        side, reason = "sell", "SMC_REVERSAL_SHORT"

    if side is None and structure == "BOS_UP" and fvg and fvg[0] == "bull":
        side, reason = "buy", "SMC_CONTINUATION_LONG"

    if side is None and structure == "BOS_DOWN" and fvg and fvg[0] == "bear":
        side, reason = "sell", "SMC_CONTINUATION_SHORT"

    if side is None:
        return {"status": "No SMC signal"}

    # ---------- POSITION SIZE ----------
    balance = get_usdt_balance()
    if balance is None:
        return {"status": "Balance error"}

    position_value = balance * POSITION_PCT
    exposure = position_value * LEVERAGE
    size = exposure / price

    size = math.floor(size * 10) / 10.0   # LOT SIZE FIX

    order = place_market_order(symbol, side, size)
    if order.get("code") != "0":
        return {"status": "ORDER FAILED", "order": order}

    exch_after = get_open_position(symbol)
    if not exch_after:
        return {"status": "ENTRY FAILED"}

    entry = exch_after["entry"]
    size = exch_after["size"]

    SL_PCT = 0.005
    TP_PCT = 0.01

    if side == "buy":
        sl = entry * (1 - SL_PCT)
        tp = entry * (1 + TP_PCT)
        pos_side = "long"
    else:
        sl = entry * (1 + SL_PCT)
        tp = entry * (1 - TP_PCT)
        pos_side = "short"

    state.update({"position": pos_side, "entry": entry, "tp": tp, "sl": sl, "size": size})
    save_state(state)

    print("\n--- NEW TRADE OPENED ---")
    print("Side:", pos_side)
    print("Entry:", entry)
    print("TP:", tp)
    print("SL:", sl)
    print("Size:", size)
    print("Reason:", reason)
    print("-------------------------\n")

    return {"status": f"{pos_side} opened ({reason})"}

# ---------- MAIN ----------
def main():
    print(f"Bot started on {SYMBOL}, interval={INTERVAL}, lev={LEVERAGE}x")
    while True:
        info = run_cycle(SYMBOL, INTERVAL)
        print(datetime.utcnow().isoformat(), info)
        time.sleep(60)

if __name__ == "__main__":
    main()
