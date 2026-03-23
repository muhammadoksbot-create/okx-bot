import time
import hmac
import base64
import hashlib
import json
from datetime import datetime

import requests
import numpy as np
import streamlit as st


# ================== CONFIG ==================

OKX_API_KEY = "YOUR_API_KEY"
OKX_API_SECRET = "YOUR_API_SECRET"
OKX_PASSPHRASE = "YOUR_PASSPHRASE"
OKX_BASE_URL = "https://www.okx.com"

SYMBOL = "BTC-USDT-SWAP"
BAR_INTERVAL = "15m"   # candle timeframe
TP_MULTIPLIER = 1.0    # ATR multiplier for TP
SL_MULTIPLIER = 1.0    # ATR multiplier for SL
ORDER_SIZE = 1         # contract size


# ================== OKX HELPERS ==================

def okx_sign(timestamp, method, request_path, body=""):
    prehash = timestamp + method + request_path + body
    return base64.b64encode(
        hmac.new(OKX_API_SECRET.encode(), prehash.encode(), hashlib.sha256).digest()
    ).decode()


def okx_headers(timestamp, method, request_path, body=""):
    return {
        "OK-ACCESS-KEY": OKX_API_KEY,
        "OK-ACCESS-SIGN": okx_sign(timestamp, method, request_path, body),
        "OK-ACCESS-TIMESTAMP": timestamp,
        "OK-ACCESS-PASSPHRASE": OKX_PASSPHRASE,
        "Content-Type": "application/json"
    }


def okx_get(path, params=""):
    ts = str(time.time())
    method = "GET"
    request_path = path + (f"?{params}" if params else "")
    url = OKX_BASE_URL + request_path
    headers = okx_headers(ts, method, request_path)
    r = requests.get(url, headers=headers)
    return r.json()


def okx_post(path, body_dict):
    ts = str(time.time())
    method = "POST"
    request_path = path
    url = OKX_BASE_URL + request_path
    body = json.dumps(body_dict)
    headers = okx_headers(ts, method, request_path, body)
    r = requests.post(url, headers=headers, data=body)
    return r.json()


# ================== MARKET DATA ==================

def get_candles(symbol=SYMBOL, bar=BAR_INTERVAL, limit=100):
    params = f"instId={symbol}&bar={bar}&limit={limit}"
    data = okx_get("/api/v5/market/candles", params)
    candles = data.get("data", [])
    # OKX returns newest first, reverse to oldest->newest
    candles = candles[::-1]
    closes = [float(c[4]) for c in candles]
    highs = [float(c[2]) for c in candles]
    lows = [float(c[3]) for c in candles]
    return np.array(closes), np.array(highs), np.array(lows)


# ================== INDICATORS ==================

def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()


def compute_indicators():
    import pandas as pd

    closes, highs, lows = get_candles()
    if len(closes) < 60:
        return None

    df = pd.DataFrame({"close": closes, "high": highs, "low": lows})

    df["ema9"] = df["close"].ewm(span=9, adjust=False).mean()
    df["ema13"] = df["close"].ewm(span=13, adjust=False).mean()
    df["ema21"] = df["close"].ewm(span=21, adjust=False).mean()
    df["ema55"] = df["close"].ewm(span=55, adjust=False).mean()

    # ATR14
    df["prev_close"] = df["close"].shift(1)
    df["tr1"] = df["high"] - df["low"]
    df["tr2"] = (df["high"] - df["prev_close"]).abs()
    df["tr3"] = (df["low"] - df["prev_close"]).abs()
    df["tr"] = df[["tr1", "tr2", "tr3"]].max(axis=1)
    df["atr14"] = df["tr"].rolling(window=14).mean()

    # MACD
    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    df["macd"] = ema12 - ema26
    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
    df["macd_hist"] = df["macd"] - df["macd_signal"]

    last = df.iloc[-1]

    return {
        "price": float(last["close"]),
        "ema9": float(last["ema9"]),
        "ema13": float(last["ema13"]),
        "ema21": float(last["ema21"]),
        "ema55": float(last["ema55"]),
        "atr14": float(last["atr14"]),
        "macd": float(last["macd"]),
        "macd_signal": float(last["macd_signal"]),
        "macd_hist": float(last["macd_hist"]),
    }


# ================== POSITION & ORDERS ==================

def check_okx_position(symbol=SYMBOL):
    params = f"instId={symbol}"
    data = okx_get("/api/v5/account/positions", params)
    arr = data.get("data", [])
    if not arr:
        return {"status": "no_position"}

    for pos in arr:
        size = float(pos.get("pos", 0))
        if size > 0:
            return {
                "status": "position",
                "posSide": pos.get("posSide", ""),
                "size": size,
                "avgPx": float(pos.get("avgPx", 0)),
                "instId": pos.get("instId", symbol)
            }

    return {"status": "no_position"}


def place_market_order(side, size, symbol=SYMBOL):
    body = {
        "instId": symbol,
        "tdMode": "cross",
        "side": side,          # "buy" or "sell"
        "ordType": "market",
        "sz": str(size)
    }
    return okx_post("/api/v5/trade/order", body)


def close_position(pos_info):
    pos_side = pos_info["posSide"]
    size = pos_info["size"]
    symbol = pos_info["instId"]

    if pos_side == "long":
        side = "sell"
    elif pos_side == "short":
        side = "buy"
    else:
        return {"error": "unknown posSide"}

    return place_market_order(side, size, symbol)


# ================== STRATEGY ==================

def get_signal(ind):
    price = ind["price"]
    ema9 = ind["ema9"]
    ema21 = ind["ema21"]
    macd = ind["macd"]
    macd_signal = ind["macd_signal"]

    # Simple example logic – tum apni marzi se change kar sakte ho
    if ema9 > ema21 and macd > macd_signal:
        return "long"
    elif ema9 < ema21 and macd < macd_signal:
        return "short"
    else:
        return "none"


def calc_tp_sl(ind, direction):
    price = ind["price"]
    atr = ind["atr14"]
    tp_dist = atr * TP_MULTIPLIER
    sl_dist = atr * SL_MULTIPLIER

    if direction == "long":
        tp = price + tp_dist
        sl = price - sl_dist
    else:
        tp = price - tp_dist
        sl = price + sl_dist

    return tp, sl


# ================== MAIN CYCLE ==================

def run_one_cycle():
    ind = compute_indicators()
    if ind is None:
        return {"error": "Not enough data"}

    pos = check_okx_position()

    # If position exists, just manage it (no opposite trade)
    if pos["status"] == "position":
        direction = pos["posSide"]
        entry = pos["avgPx"]
        size = pos["size"]
        price = ind["price"]

        # Simple TP/SL check based on ATR from now price
        tp, sl = calc_tp_sl(ind, direction)

        status = f"{direction.capitalize()} open"
        info = {
            "price": price,
            "status": status,
            "position": direction.capitalize(),
            "entry": entry,
            "tp": tp,
            "sl": sl,
            "size": size,
            "symbol": SYMBOL
        }
        return info

    # No position → use strategy
    signal = get_signal(ind)

    if signal == "none":
        ind["status"] = "No trade"
        ind["position"] = "Flat"
        ind["entry"] = None
        ind["tp"] = None
        ind["sl"] = None
        ind["size"] = 0
        ind["symbol"] = SYMBOL
        return ind

    tp, sl = calc_tp_sl(ind, signal)

    if signal == "long":
        order = place_market_order("buy", ORDER_SIZE)
        status = "Long open"
    else:
        order = place_market_order("sell", ORDER_SIZE)
        status = "Short open"

    # For demo, we trust order filled near current price
    info = {
        "price": ind["price"],
        "ema9": ind["ema9"],
        "ema13": ind["ema13"],
        "ema21": ind["ema21"],
        "ema55": ind["ema55"],
        "atr14": ind["atr14"],
        "macd": ind["macd"],
        "macd_signal": ind["macd_signal"],
        "macd_hist": ind["macd_hist"],
        "status": status,
        "position": signal.capitalize(),
        "entry": ind["price"],
        "tp": tp,
        "sl": sl,
        "size": ORDER_SIZE,
        "symbol": SYMBOL,
        "order_raw": order
    }
    return info


# ================== STREAMLIT UI ==================

st.set_page_config(page_title="OKX Bot", layout="wide")

st.title("🚀 OKX BTC-USDT Bot (Demo + Sync Safe)")

col1, col2 = st.columns(2)

with col1:
    st.subheader("Manual Cycle")
    if st.button("Run One Cycle"):
        with st.spinner("Running one cycle..."):
            result = run_one_cycle()
        st.json(result)

with col2:
    st.subheader("Auto Cycles (24/7 style)")
    auto = st.checkbox("Enable auto cycles")
    delay = st.number_input("Delay between cycles (seconds)", min_value=5, max_value=300, value=30)

    if "auto_log" not in st.session_state:
        st.session_state.auto_log = []

    if auto:
        st.info("Auto mode ON — page must stay open.")
        if "last_run" not in st.session_state:
            st.session_state.last_run = 0

        now = time.time()
        if now - st.session_state.last_run >= delay:
            st.session_state.last_run = now
            result = run_one_cycle()
            stamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
            st.session_state.auto_log.append({"time": stamp, "data": result})

    st.write("Latest auto cycles:")
    for item in reversed(st.session_state.auto_log[-10:]):
        st.caption(item["time"])
        st.json(item["data"])
