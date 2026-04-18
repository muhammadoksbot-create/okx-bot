import requests
import hmac
import hashlib
import json
import os
import time
import math
from datetime import datetime, timezone
from config_okx import API_KEY, SECRET_KEY

# ============================================================
#                    CONFIGURATION (BYBIT ADAUSDT)
# ============================================================
BASE_URL     = "https://api.bybit.ae"   # ✅ UAE ke liye sahi
STATE_FILE   = "state_bybit.json"

SYMBOL       = "ADAUSDT"
CATEGORY     = "linear"
INTERVAL     = "5"
LEVERAGE     = 5
POSITION_PCT = 0.30
SWING_LB     = 3
RR_RATIO     = 1.5
ATR_PERIOD   = 14
ATR_MULTIPLIER = 2.0

# 🔔 TELEGRAM (Optional)
TELEGRAM_TOKEN = "8756536068:AAFu7zrR5W-gu0Mv9bX4Tf9O7kokeqk6G5U"
CHAT_ID        = "1118069943"


# ============================================================
#                    LOGGING & TELEGRAM
# ============================================================
def log(tag: str, msg: str):
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] [{tag}] {msg}")

def send_telegram(msg: str):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": CHAT_ID, "text": msg}, timeout=10)
    except Exception as e:
        log("TG_ERR", str(e))


# ============================================================
#                    BYBIT AUTHENTICATION
# ============================================================
def sign_request(timestamp: str, params: str) -> str:
    param_str = timestamp + API_KEY + "5000" + params
    signature = hmac.new(
        SECRET_KEY.encode(),
        param_str.encode(),
        hashlib.sha256
    ).hexdigest()
    return signature

def make_headers(payload: str = "") -> dict:
    timestamp = str(int(time.time() * 1000))
    return {
        "X-BAPI-API-KEY":    API_KEY,
        "X-BAPI-SIGN":       sign_request(timestamp, payload),
        "X-BAPI-TIMESTAMP":  timestamp,
        "X-BAPI-RECV-WINDOW": "5000",
        "Content-Type":      "application/json"
    }


# ============================================================
#                    REQUEST HANDLER
# ============================================================
def req(method: str, path: str, body: str = None):
    try:
        url = BASE_URL + path
        h = make_headers(body or "")
        if method == "GET":
            r = requests.get(url, headers=h, timeout=10)
        else:
            r = requests.post(url, headers=h, data=body, timeout=10)
        return r.json()
    except Exception as e:
        log("API_ERR", str(e))
        send_telegram(f"⚠️ API ERROR: {e}")
        return {}


# ============================================================
#                    STATE MANAGEMENT
# ============================================================
def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {"pos": None}
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"pos": None}

def save_state(s: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=2)


# ============================================================
#                    MARKET DATA
# ============================================================
def get_candles():
    r = req("GET", f"/v5/market/kline?category={CATEGORY}&symbol={SYMBOL}&interval={INTERVAL}&limit=300")
    if r.get("retCode") == 0 and r.get("result", {}).get("list"):
        return list(reversed(r["result"]["list"]))
    log("DATA", "❌ Candles fetch failed")
    return []

def get_price() -> float | None:
    r = req("GET", f"/v5/market/tickers?category={CATEGORY}&symbol={SYMBOL}")
    try:
        return float(r["result"]["list"][0]["lastPrice"])
    except Exception:
        return None

def get_balance() -> float | None:
    r = req("GET", "/v5/account/wallet-balance?accountType=UNIFIED&coin=USDT")
    try:
        for coin in r["result"]["list"][0]["coin"]:
            if coin["coin"] == "USDT":
                return float(coin["availableToWithdraw"])
    except Exception:
        return None

def get_open_position() -> dict | None:
    r = req("GET", f"/v5/position/list?category={CATEGORY}&symbol={SYMBOL}")
    try:
        for pos in r["result"]["list"]:
            if float(pos["size"]) > 0:
                return pos
    except Exception:
        pass
    return None

def set_leverage():
    body = json.dumps({
        "category": CATEGORY,
        "symbol": SYMBOL,
        "buyLeverage": str(LEVERAGE),
        "sellLeverage": str(LEVERAGE)
    })
    r = req("POST", "/v5/position/set-leverage", body)
    if r.get("retCode") == 0:
        log("LEVERAGE", f"✅ {LEVERAGE}x set on {SYMBOL}")
    else:
        log("LEVERAGE", f"❌ Failed: {r}")


# ============================================================
#                    SMC LOGIC
# ============================================================
def find_swings(closes: list, lb: int = SWING_LB) -> list:
    swings = []
    for i in range(lb, len(closes) - lb):
        if all(closes[i] > closes[i - j] and closes[i] > closes[i + j] for j in range(1, lb + 1)):
            swings.append(("H", i, closes[i]))
        elif all(closes[i] < closes[i - j] and closes[i] < closes[i + j] for j in range(1, lb + 1)):
            swings.append(("L", i, closes[i]))
    return swings

def detect_bos(swings: list) -> str | None:
    if len(swings) < 3:
        return None
    a, b, c = swings[-3], swings[-2], swings[-1]
    if b[0] == "L" and c[0] == "H" and c[2] > a[2]:
        return "BOS_UP"
    if b[0] == "H" and c[0] == "L" and c[2] < a[2]:
        return "BOS_DOWN"
    return None

def detect_choch(swings: list) -> str | None:
    if len(swings) < 4:
        return None
    a, b, c, d = swings[-4], swings[-3], swings[-2], swings[-1]
    if a[0] == "H" and b[0] == "L" and c[0] == "H" and d[0] == "L":
        if d[2] < b[2]:
            return "SHORT"
    if a[0] == "L" and b[0] == "H" and c[0] == "L" and d[0] == "H":
        if d[2] > b[2]:
            return "LONG"
    return None

def detect_fvg(candles: list) -> tuple | None:
    if len(candles) < 3:
        return None
    c0, c2 = candles[-3], candles[-1]
    c0_high = float(c0[2])
    c0_low  = float(c0[3])
    c2_high = float(c2[2])
    c2_low  = float(c2[3])
    if c2_low > c0_high:
        return ("bull", c0_high, c2_low)
    if c2_high < c0_low:
        return ("bear", c2_high, c0_low)
    return None

def detect_liquidity(candles: list) -> bool:
    if len(candles) < 2:
        return False
    last = candles[-1]
    prev = candles[-2]
    last_high = float(last[2])
    last_low  = float(last[3])
    prev_high = float(prev[2])
    prev_low  = float(prev[3])
    last_close = float(last[4])
    swept_high = (last_high > prev_high) and (last_close < prev_high)
    swept_low  = (last_low < prev_low)   and (last_close > prev_low)
    return swept_high or swept_low


# ============================================================
#                    ATR & SMART SL
# ============================================================
def calc_atr(candles: list, period: int = ATR_PERIOD) -> float:
    if len(candles) < period + 1:
        return 0.0
    tr_values = []
    for i in range(1, len(candles)):
        high = float(candles[i][2])
        low = float(candles[i][3])
        prev_close = float(candles[i-1][4])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        tr_values.append(tr)
    if not tr_values:
        return 0.0
    return sum(tr_values[-period:]) / period

def smart_stop_loss(side: str, swings: list, current_price: float) -> float:
    if not swings:
        return None
    if side == "buy":
        lows = [s[2] for s in swings if s[0] == "L"]
        if len(lows) >= 2:
            return lows[-2] * 0.999
        elif len(lows) == 1:
            return lows[-1] * 0.999
        else:
            return current_price * 0.98
    else:
        highs = [s[2] for s in swings if s[0] == "H"]
        if len(highs) >= 2:
            return highs[-2] * 1.001
        elif len(highs) == 1:
            return highs[-1] * 1.001
        else:
            return current_price * 1.02


# ============================================================
#                    ORDER PLACEMENT
# ============================================================
def place_order(side: str, size: float, tp: float, sl: float) -> dict:
    sz_int = int(size)
    body = json.dumps({
        "category": CATEGORY,
        "symbol": SYMBOL,
        "side": side,
        "orderType": "Market",
        "qty": str(sz_int),
        "positionIdx": 0,
        "takeProfit": str(round(tp, 4)),
        "stopLoss": str(round(sl, 4)),
        "tpOrderType": "Market",
        "slOrderType": "Market"
    })
    result = req("POST", "/v5/order/create", body)
    if result.get("retCode") != 0:
        log("ORDER", f"❌ Order failed: {result}")
        send_telegram(f"❌ ORDER FAILED\n{SYMBOL}\n{result}")
        return result
    log("ORDER", f"✅ Market order with TP/SL placed.")
    return result

def close_position(side: str, size: float) -> dict:
    close_side = "Sell" if side == "Buy" else "Buy"
    body = json.dumps({
        "category": CATEGORY,
        "symbol": SYMBOL,
        "side": close_side,
        "orderType": "Market",
        "qty": str(int(size)),
        "positionIdx": 0,
        "reduceOnly": True
    })
    return req("POST", "/v5/order/create", body)


# ============================================================
#                    TRADE DISPLAY
# ============================================================
def print_trade_details(action: str, side: str, entry: float,
                        sl: float, tp: float, size: float,
                        bal: float, risk_usdt: float):
    risk_pct  = (risk_usdt / bal) * 100 if bal else 0
    reward    = abs(tp - entry) * size
    direction = "⬆️  LONG" if side == "Buy" else "⬇️  SHORT"
    print("\n" + "="*55)
    print(f"  {action}")
    print("="*55)
    print(f"  Pair      : {SYMBOL}")
    print(f"  Direction : {direction}")
    print(f"  Entry     : {entry:.4f} USDT")
    print(f"  Stop Loss : {sl:.4f} USDT")
    print(f"  Take Prof : {tp:.4f} USDT  (RR 1:{RR_RATIO})")
    print(f"  Size      : {size} contracts")
    print(f"  Leverage  : {LEVERAGE}x")
    print(f"  Risk      : {risk_usdt:.2f} USDT ({risk_pct:.1f}%)")
    print(f"  Balance   : {bal:.2f} USDT")
    print("="*55 + "\n")


# ============================================================
#                    MAIN LOOP
# ============================================================
def run():
    state = load_state()
    candles = get_candles()
    if not candles:
        return "No candle data"
    p = get_price()
    if not p:
        return "No price"
    closes = [float(x[4]) for x in candles]

    actual_pos = get_open_position()
    if actual_pos:
        if not state.get("pos"):
            log("SYNC", "⚡ Bybit pe position mili — state sync")
            pos_side = "Buy" if actual_pos["side"] == "Buy" else "Sell"
            state["pos"]   = pos_side
            state["size"]  = float(actual_pos["size"])
            state["entry"] = float(actual_pos["avgPrice"])
            save_state(state)
        entry = state.get("entry", p)
        sl    = state.get("sl", 0)
        tp    = state.get("tp", 0)
        pnl   = (p - entry) if state["pos"] == "Buy" else (entry - p)
        log("STATUS", f"📊 Position: {state['pos'].upper()} | Entry: {entry:.4f} | Now: {p:.4f} | PnL: {pnl:+.4f}")
        return "Position running"
    else:
        if state.get("pos"):
            log("SYNC", "✅ Position closed — state reset")
            save_state({"pos": None})
            state = {"pos": None}

    swings = find_swings(closes)
    bos    = detect_bos(swings)
    choch  = detect_choch(swings)
    fvg    = detect_fvg(candles)
    liq    = detect_liquidity(candles)

    volumes = [float(c[5]) for c in candles[-20:]]
    avg_vol = sum(volumes[:-1]) / (len(volumes) - 1) if len(volumes) > 1 else 0
    current_vol = volumes[-1]
    volume_surge = current_vol > avg_vol * 1.5

    log("SMC", f"BOS={bos} | CHoCH={choch} | FVG={fvg[0] if fvg else None} | Liq={liq} | VolSurge={volume_surge}")

    side = None
    reason = ""
    if choch == "LONG" and liq and volume_surge:
        side = "Buy"
        reason = "CHoCH LONG + Liq + Volume"
    elif choch == "SHORT" and liq and volume_surge:
        side = "Sell"
        reason = "CHoCH SHORT + Liq + Volume"
    elif bos == "BOS_UP" and fvg and fvg[0] == "bull" and volume_surge:
        side = "Buy"
        reason = "BOS UP + Bullish FVG + Volume"
    elif bos == "BOS_DOWN" and fvg and fvg[0] == "bear" and volume_surge:
        side = "Sell"
        reason = "BOS DOWN + Bearish FVG + Volume"

    if not side:
        log("SIGNAL", "⏳ No SMC setup — waiting...")
        return "No setup"

    log("SIGNAL", f"✅ Setup: {reason} → {side.upper()}")

    bal = get_balance()
    if not bal:
        return "No balance"

    position_value = bal * POSITION_PCT
    exposure = position_value * LEVERAGE
    size = math.floor(exposure / p)
    if size < 1:
        size = 1

    sl = smart_stop_loss("buy" if side == "Buy" else "sell", swings, p)
    if sl is None:
        atr = calc_atr(candles)
        sl_distance = atr * ATR_MULTIPLIER
        if side == "Buy":
            sl = p - sl_distance
        else:
            sl = p + sl_distance
        log("SL", f"Using ATR fallback SL: {sl:.4f}")
    else:
        log("SL", f"Using Smart SL: {sl:.4f}")

    if side == "Buy":
        tp = p + (abs(p - sl) * RR_RATIO)
    else:
        tp = p - (abs(p - sl) * RR_RATIO)

    risk_usdt = abs(p - sl) * size
    if risk_usdt <= 0:
        return "Invalid risk"

    result = place_order(side, size, tp, sl)
    if result.get("retCode") != 0:
        return "Order failed"

    state.update({
        "pos": side, "entry": p, "sl": sl, "tp": tp,
        "size": size, "reason": reason, "opened": datetime.utcnow().isoformat()
    })
    save_state(state)

    print_trade_details("🚀 TRADE OPENED", side, p, sl, tp, size, bal, risk_usdt)

    direction_emoji = "🟢 LONG" if side == "Buy" else "🔴 SHORT"
    tg_msg = f"""
🚀 TRADE OPENED — {SYMBOL}
Direction  : {direction_emoji}
Reason     : {reason}
Entry      : {p:.4f} USDT
Stop Loss  : {sl:.4f} USDT
Take Profit: {tp:.4f} USDT (RR 1:{RR_RATIO})
Size       : {size} contracts
Leverage   : {LEVERAGE}x
Risk       : {risk_usdt:.2f} USDT
Balance    : {bal:.2f} USDT
⚙️ TP/SL Bybit pe set hai ✅
"""
    send_telegram(tg_msg)
    return "✅ Trade Opened"


# ============================================================
#                    ENTRY POINT
# ============================================================
def main():
    log("BOT", "="*50)
    log("BOT", "  BYBIT SMC BOT — ADAUSDT (SINGAPORE)")
    log("BOT", f"  Leverage : {LEVERAGE}x | Position: {int(POSITION_PCT*100)}% | RR: 1:{RR_RATIO}")
    log("BOT", "="*50)
    set_leverage()
    send_telegram(f"🤖 Bybit Bot Started\n{SYMBOL} | {LEVERAGE}x | RR 1:{RR_RATIO}")
    while True:
        try:
            result = run()
            log("RUN", result)
            time.sleep(60)
        except Exception as e:
            log("ERROR", str(e))
            send_telegram(f"⚠️ BOT ERROR: {e}")
            time.sleep(15)

if __name__ == "__main__":
    main()
