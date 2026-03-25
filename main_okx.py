import requests
import hmac
import base64
import json
import streamlit as st
import pandas as pd
import os
from datetime import datetime
from config_okx import API_KEY, SECRET_KEY, PASSPHRASE

BASE_URL = "https://www.okx.com"
CSV_FILE = "trade_history.csv"
STATE_FILE = "state_okx.json"

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
        "X-SIMULATED-TRADING": "1"
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

def get_mark_price(symbol):
    path = f"/api/v5/market/ticker?instId={symbol}"
    r = requests.get(BASE_URL + path, headers=get_headers("GET", path)).json()
    if r.get("code") == "0" and r.get("data"):
        return float(r["data"][0]["last"])
    return None

def place_market_order(symbol, side, size):
    path = "/api/v5/trade/order"
    body = json.dumps({
        "instId": symbol,
        "tdMode": "isolated",
        "side": side,
        "ordType": "market",
        "sz": str(size)
    })
    return requests.post(BASE_URL + path, headers=get_headers("POST", path, body), data=body).json()

def close_position(symbol, side, size):
    opposite = "sell" if side == "long" else "buy"
    return place_market_order(symbol, opposite, size)

def get_open_position_once(symbol):
    path = "/api/v5/account/positions"
    r = requests.get(BASE_URL + path, headers=get_headers("GET", path)).json()
    if r.get("code") != "0":
        return None

    for p in r.get("data", []):
        if p.get("instId") == symbol and float(p.get("pos", 0)) != 0:
            side = "long" if p.get("posSide") == "long" else "short"
            entry = float(p.get("avgPx", 0))
            size = abs(float(p.get("pos", 0)))
            return {
                "position": side,
                "entry": entry,
                "size": size
            }
    return None

def get_open_position_double_check(symbol):
    p1 = get_open_position_once(symbol)
    p2 = get_open_position_once(symbol)

    # dono calls same honi chahiye, warna unsafe
    if (p1 is None and p2 is None):
        return None
    if (p1 is not None and p2 is not None and
        p1["position"] == p2["position"] and
        abs(p1["entry"] - p2["entry"]) < 0.5 and
        abs(p1["size"] - p2["size"]) < 1e-6):
        return p1

    # mismatch → unsafe, bot trade nahi karega
    return "MISMATCH"

def get_usdt_balance():
    path = "/api/v5/account/balance"
    r = requests.get(BASE_URL + path, headers=get_headers("GET", path)).json()
    if r.get("code") != "0":
        return None
    for d in r["data"][0]["details"]:
        if d["ccy"] == "USDT":
            return float(d["eq"])
    return None

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

def atr(highs, lows, closes, period=14):
    if len(highs) < period + 1:
        return None
    trs = []
    for i in range(1, period + 1):
        high, low, prev_close = highs[i], lows[i], closes[i - 1]
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return sum(trs) / period

def macd(values, fast=12, slow=26, signal=9):
    if len(values) < slow + signal:
        return None, None, None

    def ema_series(vals, period):
        k = 2 / (period + 1)
        e = vals[0]
        out = [e]
        for v in vals[1:]:
            e = v * k + e * (1 - k)
            out.append(e)
        return out

    fast_ema = ema_series(values, fast)
    slow_ema = ema_series(values, slow)
    fast_ema = fast_ema[-len(slow_ema):]
    macd_line_series = [f - s for f, s in zip(fast_ema, slow_ema)]
    signal_line_series = ema_series(macd_line_series, signal)
    hist_series = [m - s for m, s in zip(macd_line_series[-len(signal_line_series):], signal_line_series)]

    return macd_line_series[-1], signal_line_series[-1], hist_series[-1]

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

# ---------- RUN CYCLE ----------
def run_cycle(symbol, interval):
    state = load_state()

    # --- GET DATA FIRST ---
    data = get_candles(symbol, interval)
    if data.get("code") != "0":
        return attach_state_snapshot({"error": data}, state)

    candles = list(reversed(data["data"]))
    closes = [float(c[4]) for c in candles]
    highs  = [float(c[2]) for c in candles]
    lows   = [float(c[3]) for c in candles]

    ema9  = ema(closes, 9)
    ema21 = ema(closes, 21)
    atr14 = atr(highs, lows, closes, 14)
    macd_line, macd_signal, macd_hist = macd(closes)

    price = get_mark_price(symbol)
    if price is None:
        price = closes[-1]

    info = {
        "price": price,
        "ema9": ema9,
        "ema21": ema21,
        "atr14": atr14,
        "macd": macd_line,
        "macd_signal": macd_signal,
        "macd_hist": macd_hist
    }

    # --- DOUBLE CHECK EXCHANGE POSITION ---
    exch_pos = get_open_position_double_check(symbol)
    if exch_pos == "MISMATCH":
        info["status"] = "SYNC MISMATCH: blocking new trades"
        return attach_state_snapshot(info, state)

    if exch_pos:
        # exchange pe jo hai, wahi sach hai
        state["position"] = exch_pos["position"]
        state["entry"] = exch_pos["entry"]
        state["size"] = exch_pos["size"]
        state["symbol"] = symbol

        # TP/SL auto rebuild if missing
        if atr14 is not None and (state.get("tp") is None or state.get("sl") is None):
            if state["position"] == "long":
                state["sl"] = state["entry"] - atr14
                state["tp"] = state["entry"] + atr14 * 2
            else:
                state["sl"] = state["entry"] + atr14
                state["tp"] = state["entry"] - atr14 * 2
        save_state(state)
    else:
        # exchange pe koi position nahi → state clear
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
            info["status"] = f"{pos} open (TP/SL rebuilding...)"
            return attach_state_snapshot(info, state)

        # LONG CLOSE
        if pos == "long":
            if price >= tp:
                close_position(symbol, pos, size)
                pnl = (tp - entry) * size
                save_trade({
                    "time": datetime.utcnow(),
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
                    "time": datetime.utcnow(),
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

        # SHORT CLOSE
        if pos == "short":
            if price <= tp:
                close_position(symbol, pos, size)
                pnl = (entry - tp) * size
                save_trade({
                    "time": datetime.utcnow(),
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
                    "time": datetime.utcnow(),
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

    # ---------- NEW ENTRY ----------
    # yahan tak aane ka matlab: exchange pe bhi koi position nahi, state bhi empty
    if macd_hist is None or atr14 is None or ema9 is None or ema21 is None:
        info["status"] = "Indicators not ready"
        return attach_state_snapshot(info, state)

    up = ema9 > ema21
    down = ema9 < ema21

    recent_high = max(highs[-8:])
    recent_low  = min(lows[-8:])

    side = None
    decision = None

    if up and macd_hist > -1 and price > recent_high * 0.998:
        side = "buy"
        decision = "BUY"
    elif down and macd_hist < 1 and price < recent_low * 1.002:
        side = "sell"
        decision = "SELL"

    if not side:
        info["status"] = "No signal"
        return attach_state_snapshot(info, state)

    balance = get_usdt_balance()
    if balance is None:
        info["status"] = "Balance error"
        return attach_state_snapshot(info, state)

    risk = balance * 0.10
    exposure = risk * 10
    raw_size = exposure / price

    lot = get_lot_size(symbol)
    steps = round(raw_size / lot)
    order_size = steps * lot

    if order_size <= 0:
        info["status"] = "Order size too small"
        return attach_state_snapshot(info, state)

    # ATR TP/SL
    if side == "buy":
        pos_side = "long"
    else:
        pos_side = "short"

    order = place_market_order(symbol, side, order_size)
    info["order"] = order
    info["decision"] = decision

    if order.get("code") != "0":
        info["status"] = f"ORDER FAILED: {order.get('msg', 'unknown error')}"
        return attach_state_snapshot(info, state)

    # ENTRY LOCK FROM EXCHANGE
    exch_after = get_open_position_double_check(symbol)
    if exch_after == "MISMATCH" or not exch_after:
        info["status"] = "ENTRY LOCK FAILED / SYNC ISSUE"
        return attach_state_snapshot(info, state)

    real_entry = exch_after["entry"]
    real_size = exch_after["size"]

    if atr14 is not None:
        if pos_side == "long":
            sl = real_entry - atr14
            tp = real_entry + atr14 * 2
        else:
            sl = real_entry + atr14
            tp = real_entry - atr14 * 2
    else:
        sl = None
        tp = None

    state.update({
        "position": pos_side,
        "entry": real_entry,
        "tp": tp,
        "sl": sl,
        "size": real_size,
        "symbol": symbol
    })
    save_state(state)
    info["status"] = f"{pos_side} opened (entry locked)"
    return attach_state_snapshot(info, state)

# ---------- STREAMLIT UI ----------
st.title("OKX Auto Bot — Double Sync + Entry Lock")

symbol = st.selectbox(
    "Symbol",
    [
        "BTC-USDT-SWAP",
        "ETH-USDT-SWAP",
        "SOL-USDT-SWAP",
        "AAVE-USDT-SWAP",
        "XRP-USDT-SWAP"
    ]
)
interval = st.selectbox("Interval", ["1m", "5m", "15m"])

if "run" not in st.session_state:
    st.session_state.run = False
if "cycle" not in st.session_state:
    st.session_state.cycle = 0

state = load_state()

col1, col2 = st.columns(2)
with col1:
    if st.button("Start Auto"):
        set_leverage(symbol, 10)
        st.session_state.run = True
with col2:
    if st.button("Stop Auto"):
        st.session_state.run = False

st.write("Status:", "RUNNING" if st.session_state.run else "STOPPED")
st.write("Position:", state.get("position"))
st.write("Entry:", state.get("entry"))
st.write("TP:", state.get("tp"))
st.write("SL:", state.get("sl"))
st.write("Size:", state.get("size"))
st.write("Symbol (state):", state.get("symbol"))

st.subheader("Manual Cycle")
if st.button("Run One Cycle"):
    st.json(run_cycle(symbol, interval))

st.subheader("Auto Cycles")
auto_cycles = st.number_input("Cycles per click", 1, 50, 5)

if st.session_state.run:
    if st.button("Run Auto Batch"):
        for _ in range(int(auto_cycles)):
            st.session_state.cycle += 1
            st.json(run_cycle(symbol, interval))
else:
    st.info("Auto stopped.")
