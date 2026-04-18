import requests
import hmac
import hashlib
import base64
import json
import os
import time
import math
from datetime import datetime, timezone
from config_okx import API_KEY, SECRET_KEY, PASSPHRASE

# ============================================================
#                    CONFIGURATION (XRP OPTIMIZED)
# ============================================================
BASE_URL     = "https://www.okx.com"
STATE_FILE   = "state_okx.json"

SYMBOL       = "XRP-USDT-SWAP"
INTERVAL     = "5m"
LEVERAGE     = 5
POSITION_PCT = 0.30          # Wallet ka 30% (margin safe rahega)
SWING_LB     = 3             # Swing lookback bars
RR_RATIO     = 1.5           # Risk:Reward 1:1.5
ATR_PERIOD   = 14
ATR_MULTIPLIER = 2.0         # XRP ki volatility moderate hai

# 🔔 TELEGRAM (apna token aur chat id dalo)
TELEGRAM_TOKEN = "8756536068:AAFu7zrR5W-gu0Mv9bX4Tf9O7kokeqk6G5U"
CHAT_ID        = "1118069943"


# ============================================================
#                    LOGGING HELPER
# ============================================================
def log(tag: str, msg: str):
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] [{tag}] {msg}")


# ============================================================
#                    TELEGRAM
# ============================================================
def send_telegram(msg: str):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": CHAT_ID, "text": msg}, timeout=10)
    except Exception as e:
        log("TG_ERR", str(e))


# ============================================================
#                    AUTH
# ============================================================
def sign(message: str) -> str:
    return base64.b64encode(
        hmac.new(
            SECRET_KEY.encode(),
            message.encode(),
            digestmod=hashlib.sha256
        ).digest()
    ).decode()


def make_headers(method: str, path: str, body: str = "") -> dict:
    ts  = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    msg = ts + method + path + body
    return {
        "OK-ACCESS-KEY":        API_KEY,
        "OK-ACCESS-SIGN":       sign(msg),
        "OK-ACCESS-TIMESTAMP":  ts,
        "OK-ACCESS-PASSPHRASE": PASSPHRASE,
        "Content-Type":         "application/json"
    }


# ============================================================
#                    REQUEST
# ============================================================
def req(method: str, path: str, body: str = None):
    try:
        url = BASE_URL + path
        h   = make_headers(method, path, body or "")
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
#                    LEVERAGE
# ============================================================
def set_leverage():
    body = json.dumps({
        "instId":  SYMBOL,
        "lever":   str(LEVERAGE),
        "mgnMode": "cross"
    })
    r = req("POST", "/api/v5/account/set-leverage", body)
    if r.get("code") == "0":
        log("LEVERAGE", f"✅ {LEVERAGE}x set on {SYMBOL}")
    else:
        log("LEVERAGE", f"❌ Failed: {r}")


# ============================================================
#                    CONTRACT VALUE
# ============================================================
def get_ct_val() -> float:
    # XRP-USDT-SWAP ke liye ctVal = 1
    if "XRP" in SYMBOL:
        return 1.0
    r = req("GET", f"/api/v5/public/instruments?instType=SWAP&instId={SYMBOL}")
    try:
        return float(r["data"][0]["ctVal"])
    except Exception:
        return 1.0


# ============================================================
#                    STATE
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
    r = req("GET", f"/api/v5/market/candles?instId={SYMBOL}&bar={INTERVAL}&limit=300")
    if r.get("code") == "0":
        return list(reversed(r["data"]))
    log("DATA", "❌ Candles fetch failed")
    return []


def get_price() -> float | None:
    r = req("GET", f"/api/v5/market/ticker?instId={SYMBOL}")
    try:
        return float(r["data"][0]["last"])
    except Exception:
        return None


def get_balance() -> float | None:
    r = req("GET", "/api/v5/account/balance")
    try:
        for d in r["data"][0]["details"]:
            if d["ccy"] == "USDT":
                return float(d["availBal"])
    except Exception:
        return None


# ============================================================
#           REAL POSITION SYNC
# ============================================================
def get_open_position() -> dict | None:
    r = req("GET", f"/api/v5/account/positions?instId={SYMBOL}")
    try:
        for pos in r["data"]:
            sz = float(pos.get("pos", 0))
            if sz != 0:
                return pos
    except Exception:
        pass
    return None


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
#                    ATR CALCULATION
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


# ============================================================
#                    SMART STOP LOSS (Swing Based)
# ============================================================
def smart_stop_loss(side: str, swings: list, current_price: float) -> float:
    """
    Swing highs/lows ke peeche SL chhupao.
    """
    if not swings:
        return None
    
    if side == "buy":
        lows = [s[2] for s in swings if s[0] == "L"]
        if len(lows) >= 2:
            return lows[-2] * 0.999
        elif len(lows) == 1:
            return lows[-1] * 0.999
        else:
            return current_price * 0.98   # fallback 2%
    else:  # sell
        highs = [s[2] for s in swings if s[0] == "H"]
        if len(highs) >= 2:
            return highs[-2] * 1.001
        elif len(highs) == 1:
            return highs[-1] * 1.001
        else:
            return current_price * 1.02


# ============================================================
#                    ORDER PLACEMENT (FIXED)
# ============================================================
def place_order(side: str, size: float, tp: float, sl: float) -> dict:
    """
    Step 1: Market order.
    Step 2: Algo order for TP/SL.
    """
    sz_int = int(size)
    
    # ---- Step 1: Market Order ----
    body_market = json.dumps({
        "instId":  SYMBOL,
        "tdMode":  "cross",
        "side":    side,
        "ordType": "market",
        "sz":      str(sz_int)
    })
    
    result = req("POST", "/api/v5/trade/order", body_market)
    if result.get("code") != "0":
        log("ORDER", f"❌ Market order failed: {result}")
        send_telegram(f"❌ MARKET ORDER FAILED\n{SYMBOL}\n{result}")
        return result
    
    log("ORDER", f"✅ Market order placed.")
    time.sleep(1)
    
    # ---- Step 2: TP/SL Algo Order ----
    algo_side = "sell" if side == "buy" else "buy"
    tp_str = str(round(tp, 4))
    sl_str = str(round(sl, 4))
    
    body_algo = json.dumps({
        "instId":      SYMBOL,
        "tdMode":      "cross",
        "side":        algo_side,
        "ordType":     "conditional",
        "sz":          str(sz_int),
        "tpTriggerPx": tp_str,
        "tpOrdPx":     "-1",
        "slTriggerPx": sl_str,
        "slOrdPx":     "-1",
        "posSide":     "net",
        "reduceOnly":  "true"
    })
    
    algo_res = req("POST", "/api/v5/trade/order-algo", body_algo)
    if algo_res.get("code") != "0":
        log("ALGO", f"❌ TP/SL failed: {algo_res}")
        send_telegram(f"⚠️ TP/SL FAILED\n{SYMBOL}\nMarket OK, Algo Fail:\n{algo_res}")
    else:
        log("ALGO", "✅ TP/SL set successfully")
        send_telegram(f"✅ TP/SL SET\n{SYMBOL}\nTP: {tp_str}\nSL: {sl_str}")
    
    return result


def close_position(side: str, size: float) -> dict:
    close_side = "sell" if side == "buy" else "buy"
    body = json.dumps({
        "instId":   SYMBOL,
        "tdMode":   "cross",
        "side":     close_side,
        "ordType":  "market",
        "sz":       str(int(size)),
        "reduceOnly": "true"
    })
    return req("POST", "/api/v5/trade/order", body)


# ============================================================
#                    TRADE DETAILS DISPLAY
# ============================================================
def print_trade_details(action: str, side: str, entry: float,
                        sl: float, tp: float, size: float,
                        bal: float, risk_usdt: float):
    risk_pct  = (risk_usdt / bal) * 100 if bal else 0
    reward    = abs(tp - entry) * size
    direction = "⬆️  LONG" if side == "buy" else "⬇️  SHORT"

    print("\n" + "="*55)
    print(f"  {action}")
    print("="*55)
    print(f"  Pair      : {SYMBOL}")
    print(f"  Direction : {direction}")
    print(f"  Entry     : {entry:.4f} USDT")
    print(f"  Stop Loss : {sl:.4f} USDT  ({'below' if side=='buy' else 'above'} entry)")
    print(f"  Take Prof : {tp:.4f} USDT  (RR 1:{RR_RATIO})")
    print(f"  Size      : {size} contracts")
    print(f"  Leverage  : {LEVERAGE}x")
    print(f"  Risk      : {risk_usdt:.2f} USDT  ({risk_pct:.1f}% of balance)")
    print(f"  Reward    : {reward:.2f} USDT (if TP hit)")
    print(f"  Balance   : {bal:.2f} USDT")
    print("="*55 + "\n")


# ============================================================
#                    MAIN RUN LOOP
# ============================================================
def run():
    state = load_state()

    # ---- Candles + Price ----
    candles = get_candles()
    if not candles:
        return "No candle data"

    p = get_price()
    if not p:
        return "No price"

    closes = [float(x[4]) for x in candles]

    # ---- POSITION CHECK ----
    actual_pos = get_open_position()

    if actual_pos:
        if not state.get("pos"):
            log("SYNC", "⚡ OKX pe position mili — state sync")
            pos_side = "buy" if float(actual_pos["pos"]) > 0 else "sell"
            state["pos"]   = pos_side
            state["size"]  = abs(float(actual_pos["pos"]))
            state["entry"] = float(actual_pos.get("avgPx", p))
            save_state(state)

        # Reverse signal exit
        swings = find_swings(closes)
        choch  = detect_choch(swings)
        if state.get("pos"):
            if (state["pos"] == "buy" and choch == "SHORT") or (state["pos"] == "sell" and choch == "LONG"):
                log("SIGNAL", f"🔁 Reverse CHoCH ({choch}), closing position")
                close_res = close_position(state["pos"], state["size"])
                if close_res.get("code") == "0":
                    log("EXIT", "✅ Position closed on reverse signal")
                    save_state({"pos": None})
                    send_telegram(f"🔁 POSITION CLOSED (Reverse)\n{SYMBOL}")
                    return "Closed on reverse signal"

        entry = state.get("entry", p)
        sl    = state.get("sl", 0)
        tp    = state.get("tp", 0)
        pnl   = (p - entry) if state["pos"] == "buy" else (entry - p)

        log("STATUS", f"📊 Position: {state['pos'].upper()} | Entry: {entry:.4f} | "
                      f"Now: {p:.4f} | PnL: {pnl:+.4f} | SL: {sl:.4f} | TP: {tp:.4f}")
        return "Position running"

    else:
        if state.get("pos"):
            log("SYNC", "✅ Position closed — state reset")
            save_state({"pos": None})
            state = {"pos": None}

    # ---- ENTRY LOGIC ----
    swings = find_swings(closes)
    bos    = detect_bos(swings)
    choch  = detect_choch(swings)
    fvg    = detect_fvg(candles)
    liq    = detect_liquidity(candles)

    # Volume Filter
    volumes = [float(c[5]) for c in candles[-20:]]
    avg_vol = sum(volumes[:-1]) / (len(volumes) - 1) if len(volumes) > 1 else 0
    current_vol = volumes[-1]
    volume_surge = current_vol > avg_vol * 1.5

    log("SMC", f"BOS={bos} | CHoCH={choch} | FVG={fvg[0] if fvg else None} | Liq={liq} | VolSurge={volume_surge}")

    side = None
    reason = ""

    # Entry conditions with volume filter
    if choch == "LONG" and liq and volume_surge:
        side = "buy"
        reason = "CHoCH LONG + Liq + Volume"
    elif choch == "SHORT" and liq and volume_surge:
        side = "sell"
        reason = "CHoCH SHORT + Liq + Volume"
    elif bos == "BOS_UP" and fvg and fvg[0] == "bull" and volume_surge:
        side = "buy"
        reason = "BOS UP + Bullish FVG + Volume"
    elif bos == "BOS_DOWN" and fvg and fvg[0] == "bear" and volume_surge:
        side = "sell"
        reason = "BOS DOWN + Bearish FVG + Volume"

    if not side:
        log("SIGNAL", "⏳ No SMC setup — waiting...")
        return "No setup"

    log("SIGNAL", f"✅ Setup: {reason} → {side.upper()}")

    # ---- BALANCE + SIZE (WITH SAFETY BUFFER) ----
    bal = get_balance()
    if not bal:
        return "No balance"

    ct_val = get_ct_val()
    position_value = bal * POSITION_PCT
    exposure       = position_value * LEVERAGE
    size = math.floor(exposure / (p * ct_val))

    if size < 1:
        size = 1   # Force minimum 1 contract

    # Recalculate actual margin required with 5% safety buffer
    required_margin = (size * p * ct_val) / LEVERAGE
    safe_required = required_margin * 1.05   # 5% buffer for fees/slippage
    
    if safe_required > bal:
        log("SIZE", f"❌ Insufficient margin. Required: {safe_required:.2f}, Available: {bal:.2f}")
        send_telegram(f"⚠️ INSUFFICIENT MARGIN\n{SYMBOL}\nRequired: {safe_required:.2f} USDT\nAvailable: {bal:.2f} USDT")
        return "Insufficient margin"

    notional = size * p * ct_val
    if notional < 10:
        log("SIZE", f"❌ Notional value ${notional:.2f} < $10 minimum.")
        return "Size too small"

    # ---- SMART STOP LOSS ----
    sl = smart_stop_loss(side, swings, p)
    if sl is None:
        # Fallback to ATR
        atr = calc_atr(candles)
        sl_distance = atr * ATR_MULTIPLIER
        if side == "buy":
            sl = p - sl_distance
        else:
            sl = p + sl_distance
        log("SL", f"Using ATR fallback SL: {sl:.4f} (ATR: {atr:.4f})")
    else:
        log("SL", f"Using Smart SL: {sl:.4f} (swing based)")

    # ---- TP Calculation ----
    if side == "buy":
        tp = p + (abs(p - sl) * RR_RATIO)
    else:
        tp = p - (abs(p - sl) * RR_RATIO)

    risk_usdt = abs(p - sl) * size * ct_val
    if risk_usdt <= 0:
        return "Invalid risk"

    # ---- PLACE ORDER ----
    result = place_order(side, size, tp, sl)

    if result.get("code") != "0":
        err_msg = result.get("msg", "Unknown error")
        log("ORDER", f"❌ FAILED: {err_msg}")
        send_telegram(f"❌ ORDER FAILED\n{SYMBOL}\n{err_msg}")
        return "Order failed"

    # ---- SAVE STATE ----
    state.update({
        "pos":    side,
        "entry":  p,
        "sl":     sl,
        "tp":     tp,
        "size":   size,
        "reason": reason,
        "opened": datetime.utcnow().isoformat()
    })
    save_state(state)

    # ---- DISPLAY ----
    print_trade_details("🚀 TRADE OPENED", side, p, sl, tp, size, bal, risk_usdt)

    # ---- TELEGRAM ----
    direction_emoji = "🟢 LONG" if side == "buy" else "🔴 SHORT"
    tg_msg = f"""
🚀 TRADE OPENED — {SYMBOL}

Direction  : {direction_emoji}
Reason     : {reason}
Entry      : {p:.4f} USDT
Stop Loss  : {sl:.4f} USDT
Take Profit: {tp:.4f} USDT  (RR 1:{RR_RATIO})
Size       : {size} contracts
Leverage   : {LEVERAGE}x
Risk       : {risk_usdt:.2f} USDT
Balance    : {bal:.2f} USDT

⚙️ TP/SL Exchange pe set hai ✅
"""
    send_telegram(tg_msg)

    return "✅ Trade Opened"


# ============================================================
#                    ENTRY POINT
# ============================================================
def main():
    log("BOT", "="*50)
    log("BOT", "  OKX SMC BOT — XRP Optimized")
    log("BOT", f"  Pair     : {SYMBOL}")
    log("BOT", f"  Interval : {INTERVAL}")
    log("BOT", f"  Leverage : {LEVERAGE}x")
    log("BOT", f"  Position : {int(POSITION_PCT*100)}% of balance")
    log("BOT", f"  SL Type  : Smart Swing + ATR({ATR_PERIOD})*{ATR_MULTIPLIER}")
    log("BOT", f"  RR Ratio : 1:{RR_RATIO}")
    log("BOT", f"  Volume Filter: ON (>1.5x avg)")
    log("BOT", "="*50)

    set_leverage()
    send_telegram(f"🤖 OKX SMC Bot Started\n{SYMBOL} | {LEVERAGE}x | Smart SL | RR 1:{RR_RATIO}")

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
