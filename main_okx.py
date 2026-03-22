import requests
import hmac
import base64
import json
import time
import streamlit as st
import pandas as pd
import os
from datetime import datetime, timedelta
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
def load_state():
    if not os.path.exists(STATE_FILE):
        return {
            "position": None,
            "entry": None,
            "tp": None,
            "sl": None,
            "size": None,
            "symbol": None
        }
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

def place_order(symbol, side, size):
    path = "/api/v5/trade/order"
    body = json.dumps({
        "instId": symbol,
        "tdMode": "isolated",
        "side": side,
        "ordType": "market",
        "sz": str(size)
    })
    return requests.post(BASE_URL + path, headers=get_headers("POST", path, body), data=body).json()

def set_leverage(symbol, lever=10):
    path = "/api/v5/account/set-leverage"
    body = json.dumps({
        "instId": symbol,
        "lever": str(lever),
        "mgnMode": "isolated"
    })
    return requests.post(BASE_URL + path, headers=get_headers("POST", path, body), data=body).json()

def get_usdt_balance():
    path = "/api/v5/account/balance"
    r = requests.get(BASE_URL + path, headers=get_headers("GET", path)).json()
    if r.get("code") != "0":
        return None
    for d in r["data"][0]["details"]:
        if d["ccy"] == "USDT":
            return float(d["eq"])
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

def rsi(values, period=14):
    if len(values) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, period + 1):
        diff = values[i] - values[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

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

# ---------- CSV SAFE WRITE ----------
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

# ---------- RUN CYCLE (UPDATED STRATEGY + SOFTENED) ----------
def run_cycle(symbol, interval):
    state = load_state()

    data = get_candles(symbol, interval)
    if data.get("code") != "0":
        return attach_state_snapshot({"error": data}, state)

    candles = list(reversed(data["data"]))
    closes = [float(c[4]) for c in candles]
    highs  = [float(c[2]) for c in candles]
    lows   = [float(c[3]) for c in candles]

    # --- Indicators (chart style) ---
    ema9  = ema(closes, 9)
    ema13 = ema(closes, 13)
    ema21 = ema(closes, 21)
    ema55 = ema(closes, 55)
    atr14 = atr(highs, lows, closes, 14)
    macd_line, macd_signal, macd_hist = macd(closes)
    price = closes[-1]

    info = {
        "price": price,
        "ema9": ema9,
        "ema13": ema13,
        "ema21": ema21,
        "ema55": ema55,
        "atr14": atr14,
        "macd": macd_line,
        "macd_signal": macd_signal,
        "macd_hist": macd_hist
    }

    # ---------- MANAGE OPEN POSITION ----------
    pos = state.get("position")
    if pos and state.get("symbol") == symbol:
        entry = state["entry"]
        tp = state["tp"]
        sl = state["sl"]
        size = state["size"]

        if pos == "long" and price >= tp:
            pnl = (tp - entry) * size
            save_trade({"time": datetime.utcnow(), "symbol": symbol, "side": "LONG",
                        "entry": entry, "exit": tp, "pnl": pnl, "result": "TP HIT"})
            state = {k: None for k in state}
            save_state(state)
            info["status"] = "Long TP"
            return attach_state_snapshot(info, state)

        if pos == "long" and price <= sl:
            pnl = (sl - entry) * size
            save_trade({"time": datetime.utcnow(), "symbol": symbol, "side": "LONG",
                        "entry": entry, "exit": sl, "pnl": pnl, "result": "SL HIT"})
            state = {k: None for k in state}
            save_state(state)
            info["status"] = "Long SL"
            return attach_state_snapshot(info, state)

        if pos == "short" and price <= tp:
            pnl = (entry - tp) * size
            save_trade({"time": datetime.utcnow(), "symbol": symbol, "side": "SHORT",
                        "entry": entry, "exit": tp, "pnl": pnl, "result": "TP HIT"})
            state = {k: None for k in state}
            save_state(state)
            info["status"] = "Short TP"
            return attach_state_snapshot(info, state)

        if pos == "short" and price >= sl:
            pnl = (entry - sl) * size
            save_trade({"time": datetime.utcnow(), "symbol": symbol, "side": "SHORT",
                        "entry": entry, "exit": sl, "pnl": pnl, "result": "SL HIT"})
            state = {k: None for k in state}
            save_state(state)
            info["status"] = "Short SL"
            return attach_state_snapshot(info, state)

        info["status"] = f"{pos} open"
        return attach_state_snapshot(info, state)

    # ---------- NEW ENTRY (SOFTENED EMA + MACD + BREAKOUT) ----------
    if state.get("position"):
        info["status"] = "Position already open"
        return attach_state_snapshot(info, state)

    # Softer trend rules
    up_trend = ema9 > ema21 and ema13 > ema55
    down_trend = ema9 < ema21 and ema13 < ema55

    # Softer breakout levels
    recent_high = max(highs[-5:])
    recent_low  = min(lows[-5:])

    side = None
    decision = None

    # Softer LONG ENTRY
    if up_trend and macd_hist > -0.5 and price > recent_high:
        side = "buy"
        decision = "BUY"

    # Softer SHORT ENTRY
    elif down_trend and macd_hist < 0.5 and price < recent_low:
        side = "sell"
        decision = "SELL"

    if not side:
        info["status"] = "No signal"
        return attach_state_snapshot(info, state)

    # ---------- Position Sizing ----------
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

    # ---------- TP/SL (ATR based) ----------
    if side == "buy":
        sl = price - atr14
        tp = price + atr14 * 2
        pos_side = "long"
    else:
        sl = price + atr14
        tp = price - atr14 * 2
        pos_side = "short"

    order = place_order(symbol, side, order_size)

    if order.get("code") == "0":
        state.update({
            "position": pos_side,
            "entry": price,
            "tp": tp,
            "sl": sl,
            "size": order_size,
            "symbol": symbol
        })
        save_state(state)

    info["order"] = order
    info["decision"] = decision
    info["tp"] = tp
    info["sl"] = sl
    info["size"] = order_size

    return attach_state_snapshot(info, state)

# ---------- STREAMLIT UI ----------
st.title("OKX Auto Bot + Dashboard + Weekly Report")

symbol = st.selectbox("Symbol", ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"])
interval = st.selectbox("Interval", ["1m", "5m", "15m"])
loop_time = 20

if "run" not in st.session_state:
    st.session_state.run = False

state = load_state()

col1, col2 = st.columns(2)
with col1:
    if st.button("Start Auto"):
        st.session_state.run = True
        st.write(set_leverage(symbol, 10))
with col2:
    if st.button("Stop Auto"):
        st.session_state.run = False

st.write("Status:", "RUNNING" if st.session_state.run else "STOPPED")
st.write("Position:", state.get("position"))
st.write("Entry:", state.get("entry"))
st.write("TP:", state.get("tp"))
st.write("SL:", state.get("sl"))
st.write("Size:", state.get("size"))
st.write("State symbol:", state.get("symbol"))

# ---------- LIVE DASHBOARD ----------
st.subheader("📊 Live Dashboard")

if os.path.exists(CSV_FILE):
    df = pd.read_csv(CSV_FILE, on_bad_lines="skip")
    st.write(df.tail(10))
    st.line_chart(df["pnl"].cumsum())
else:
    st.write("No trades yet.")

# ---------- MANUAL CYCLE ----------
st.subheader("🔁 Manual Cycle")
if st.button("Run One Cycle"):
    st.json(run_cycle(symbol, interval))

# ---------- AUTO LOOP ----------
st.subheader("🚀 Auto Cycles (24/7)")

left, right = st.columns(1)
latest_box = left.empty()
history_box = st.empty()
cycle_history = []

if st.session_state.run:
    i = 0
    while st.session_state.run:
        i += 1
        res = run_cycle(symbol, interval)
        latest_box.markdown(f"### 🔵 Latest Cycle: {i}")
        latest_box.json(res)
        cycle_history.append({"cycle": i, **res})
        history_box.markdown("### 📜 Cycle History")
        history_box.json(cycle_history)
        time.sleep(loop_time)

# ---------- WEEKLY REPORT ----------
st.subheader("📅 Weekly Report")

if os.path.exists(CSV_FILE):
    df = pd.read_csv(CSV_FILE, on_bad_lines="skip")
    df["time"] = pd.to_datetime(df["time"])
    last_week = datetime.utcnow() - timedelta(days=7)
    weekly = df[df["time"] >= last_week]
    st.write("Weekly Trades:", len(weekly))
    st.write("Weekly Profit:", weekly["pnl"].sum())
    st.line_chart(weekly["pnl"].cumsum())
else:
    st.write("No weekly data yet.")
