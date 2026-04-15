import requests
import hmac
import hashlib
import base64
import json
import os
import time
import math                           # ✅ FIXED: Imported at top
from datetime import datetime, timezone
from config_okx import API_KEY, SECRET_KEY, PASSPHRASE

# ============================================================
#                    CONFIGURATION
# ============================================================
BASE_URL     = "https://www.okx.com"
STATE_FILE   = "state_okx.json"

SYMBOL       = "LINK-USDT-SWAP"
INTERVAL     = "5m"
LEVERAGE     = 5
POSITION_PCT = 0.50          # Wallet ka 50%
SWING_LB     = 3             # Swing lookback bars
RR_RATIO     = 2.0           # Risk:Reward 1:2

# 🔔 TELEGRAM  (apna token aur chat id dalo)
TELEGRAM_TOKEN = ""
CHAT_ID        = ""


# ============================================================
#                    LOGGING HELPER
# ============================================================
def log(tag: str, msg: str):
    """Console pe colored/structured print."""
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] [{tag}] {msg}")


# ============================================================
#                    TELEGRAM
# ============================================================
TELEGRAM_TOKEN = "8756536068:AAFu7zrR5W-gu0Mv9bX4Tf9O7kokeqk6G5U"   # <--- APNA TOKEN YAHAN DALO
CHAT_ID        = "1118069943"     # <--- APNI CHAT ID YAHAN DALO

def send_telegram(msg: str):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": CHAT_ID, "text": msg}, timeout=10)
    except Exception as e:
        log("TG_ERR", str(e))
        
# ============================================================
#                    AUTH  (BUG FIX: hashlib.sha256)
# ============================================================
def sign(message: str) -> str:
    return base64.b64encode(
        hmac.new(
            SECRET_KEY.encode(),
            message.encode(),
            digestmod=hashlib.sha256          # ✅ string "sha256" wala bug fix
        ).digest()
    ).decode()


def make_headers(method: str, path: str, body: str = "") -> dict:
    ts  = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    msg = ts + method + path + body
    return {
        "OK-ACCESS-KEY":       API_KEY,
        "OK-ACCESS-SIGN":      sign(msg),
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": PASSPHRASE,
        "Content-Type":        "application/json"
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
#                    CONTRACT VALUE  (BUG FIX: size in contracts)
# ============================================================
def get_ct_val() -> float:
    """
    OKX pe size CONTRACTS mein hoti hai, USDT mein nahi.
    ctVal = ek contract kitne LINK ke barabar hai (usually 1).
    """
    r = req("GET", f"/api/v5/public/instruments?instType=SWAP&instId={SYMBOL}")
    try:
        return float(r["data"][0]["ctVal"])
    except Exception:
        return 1.0          # fallback


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
    r = req("GET", f"/api/v5/market/candles?instId={SYMBOL}&bar={INTERVAL}&limit=200")
    if r.get("code") == "0":
        return list(reversed(r["data"]))   # oldest → newest
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
#           REAL POSITION SYNC  (OKX se actual check)
# ============================================================
def get_open_position() -> dict | None:
    """
    OKX se ACTUAL open position check karta hai.
    State file pe blindly trust nahi karta.
    """
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
#                    SMC LOGIC  (Improved)
# ============================================================

# ------ Swing Points ------
def find_swings(closes: list, lb: int = SWING_LB) -> list:
    """
    Returns list of ("H"/"L", index, price)
    """
    swings = []
    for i in range(lb, len(closes) - lb):
        if all(closes[i] > closes[i - j] and closes[i] > closes[i + j] for j in range(1, lb + 1)):
            swings.append(("H", i, closes[i]))
        elif all(closes[i] < closes[i - j] and closes[i] < closes[i + j] for j in range(1, lb + 1)):
            swings.append(("L", i, closes[i]))
    return swings


# ------ BOS (Break of Structure) ------
def detect_bos(swings: list) -> str | None:
    """
    BOS_UP  → market ne previous High todha (bullish)
    BOS_DOWN → market ne previous Low todha (bearish)
    """
    if len(swings) < 3:
        return None
    a, b, c = swings[-3], swings[-2], swings[-1]

    if b[0] == "L" and c[0] == "H" and c[2] > a[2]:
        return "BOS_UP"
    if b[0] == "H" and c[0] == "L" and c[2] < a[2]:
        return "BOS_DOWN"
    return None


# ------ CHoCH (Change of Character) ------
def detect_choch(swings: list) -> str | None:
    """
    CHoCH SHORT → bearish reversal (downtrend shuru)
    CHoCH LONG  → bullish reversal (uptrend shuru)
    """
    if len(swings) < 4:
        return None
    a, b, c, d = swings[-4], swings[-3], swings[-2], swings[-1]

    # Bearish CHoCH: H-L-H-L pattern mein last L ne prev L todha
    if a[0] == "H" and b[0] == "L" and c[0] == "H" and d[0] == "L":
        if d[2] < b[2]:
            return "SHORT"

    # Bullish CHoCH: L-H-L-H pattern mein last H ne prev H todha
    if a[0] == "L" and b[0] == "H" and c[0] == "L" and d[0] == "H":
        if d[2] > b[2]:
            return "LONG"

    return None


# ------ FVG (Fair Value Gap) ------
def detect_fvg(candles: list) -> tuple | None:
    """
    3 candle pattern:
    Bullish FVG → candle[0].high < candle[2].low  (gap up)
    Bearish FVG → candle[0].low  > candle[2].high (gap down)
    Returns: ("bull"/"bear", low, high) or None
    """
    if len(candles) < 3:
        return None

    c0, c2 = candles[-3], candles[-1]
    c0_high = float(c0[2])   # OKX candle: [ts, o, h, l, c, vol]
    c0_low  = float(c0[3])
    c2_high = float(c2[2])
    c2_low  = float(c2[3])

    if c2_low > c0_high:
        return ("bull", c0_high, c2_low)    # Bullish FVG
    if c2_high < c0_low:
        return ("bear", c2_high, c0_low)    # Bearish FVG
    return None


# ------ Liquidity Sweep (Improved) ------
def detect_liquidity(candles: list) -> bool:
    """
    Wick ne previous candle ki extreme cross ki — sweep signal.
    Proper sweep: price breaks structure and immediately reverses.
    """
    if len(candles) < 2:
        return False
    last = candles[-1]
    prev = candles[-2]
    last_high = float(last[2])
    last_low  = float(last[3])
    prev_high = float(prev[2])
    prev_low  = float(prev[3])
    last_close = float(last[4])

    # Sell-side liquidity sweep: wick above prev high but closed below it
    swept_high = (last_high > prev_high) and (last_close < prev_high)
    # Buy-side liquidity sweep: wick below prev low but closed above it
    swept_low  = (last_low < prev_low)   and (last_close > prev_low)
    return swept_high or swept_low


# ============================================================
#                    ORDER PLACEMENT
# ============================================================

def place_order(side: str, size: float, tp: float, sl: float) -> dict:
    """
    Market order + Exchange pe attached TP/SL algo order.
    """
    tp_str = str(round(tp, 4))
    sl_str = str(round(sl, 4))

    body = json.dumps({
        "instId":  SYMBOL,
        "tdMode":  "cross",
        "side":    side,
        "ordType": "market",
        "sz":      str(size),
        "attachAlgoOrds": [
            {
                "attachAlgoClOrdId": f"tp_{int(time.time())}",
                "tpTriggerPx":       tp_str,
                "tpOrdPx":           "-1",      # market order on TP hit
                "slTriggerPx":       sl_str,
                "slOrdPx":           "-1"       # market order on SL hit
            }
        ]
    })
    return req("POST", "/api/v5/trade/order", body)


def close_position(side: str, size: float) -> dict:
    """Existing position close karta hai opposite side se."""
    close_side = "sell" if side == "buy" else "buy"
    body = json.dumps({
        "instId":  SYMBOL,
        "tdMode":  "cross",
        "side":    close_side,
        "ordType": "market",
        "sz":      str(size),
        "reduceOnly": "true"
    })
    return req("POST", "/api/v5/trade/order", body)


# ============================================================
#                    TRADE DETAILS DISPLAY
# ============================================================
def print_trade_details(action: str, side: str, entry: float,
                        sl: float, tp: float, size: float,
                        bal: float, risk_usdt: float):
    """Console pe full trade details print karta hai."""
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

    # ===========================================================
    #  POSITION CHECK — OKX se actual sync
    # ===========================================================
    actual_pos = get_open_position()

    if actual_pos:
        # Position open hai — state sync karo agar file stale hai
        if not state.get("pos"):
            log("SYNC", "⚡ OKX pe position mili — state sync ho rahi hai")
            pos_side = "buy" if float(actual_pos["pos"]) > 0 else "sell"
            state["pos"]   = pos_side
            state["size"]  = abs(float(actual_pos["pos"]))
            state["entry"] = float(actual_pos.get("avgPx", p))
            save_state(state)

        # ====== ✨ NEW: Reverse Signal Exit Check ======
        swings = find_swings(closes)
        choch  = detect_choch(swings)
        if state.get("pos"):
            if (state["pos"] == "buy" and choch == "SHORT") or (state["pos"] == "sell" and choch == "LONG"):
                log("SIGNAL", f"🔁 Reverse CHoCH ({choch}) detected, closing position")
                close_res = close_position(state["pos"], state["size"])
                if close_res.get("code") == "0":
                    log("EXIT", "✅ Position closed due to reversal")
                    save_state({"pos": None})
                    send_telegram(f"🔁 POSITION CLOSED (Reverse Signal)\n{SYMBOL}\nCHoCH: {choch}")
                    return "Closed on reverse signal"
        # ================================================

        # TP/SL exchange pe lag chuka hai — sirf status dikhao
        entry = state.get("entry", p)
        sl    = state.get("sl", 0)
        tp    = state.get("tp", 0)
        pnl   = (p - entry) if state["pos"] == "buy" else (entry - p)

        log("STATUS", f"📊 Position: {state['pos'].upper()} | Entry: {entry:.4f} | "
                      f"Now: {p:.4f} | PnL: {pnl:+.4f} | SL: {sl:.4f} | TP: {tp:.4f}")
        return "Position running"

    else:
        # OKX pe koi position nahi — state reset karo
        if state.get("pos"):
            log("SYNC", "✅ Position close ho gayi (OKX confirm) — state reset")
            save_state({"pos": None})
            state = {"pos": None}

    # ===========================================================
    #  ENTRY LOGIC — SMC
    # ===========================================================
    swings = find_swings(closes)
    bos    = detect_bos(swings)
    choch  = detect_choch(swings)
    fvg    = detect_fvg(candles)
    liq    = detect_liquidity(candles)

    log("SMC", f"BOS={bos} | CHoCH={choch} | FVG={fvg[0] if fvg else None} | Liq={liq}")

    side = None

    # Signal priority: CHoCH > BOS + FVG
    if choch == "LONG" and liq:
        side = "buy"
        reason = "CHoCH LONG + Liquidity Sweep"

    elif choch == "SHORT" and liq:
        side = "sell"
        reason = "CHoCH SHORT + Liquidity Sweep"

    elif bos == "BOS_UP" and fvg and fvg[0] == "bull":
        side = "buy"
        reason = "BOS UP + Bullish FVG"

    elif bos == "BOS_DOWN" and fvg and fvg[0] == "bear":
        side = "sell"
        reason = "BOS DOWN + Bearish FVG"

    if not side:
        log("SIGNAL", "⏳ No SMC setup — waiting...")
        return "No setup"

    log("SIGNAL", f"✅ Setup: {reason} → {side.upper()}")

    # ===========================================================
    #  BALANCE + SIZE CALCULATION
    # ===========================================================
    bal = get_balance()
    if not bal:
        return "No balance"

    ct_val = get_ct_val()                     # e.g. 1.0 for LINK

    position_value = bal * POSITION_PCT       # 50% of wallet
    exposure       = position_value * LEVERAGE
    # size = exposure / (price * ctVal) → number of CONTRACTS
    size = math.floor(exposure / (p * ct_val))

    if size < 1:
        log("SIZE", "❌ Size too small (balance kam hai?)")
        return "Size too small"

    # ===========================================================
    #  SL / TP  (recent swing based)
    # ===========================================================
    lookback = closes[-10:]     # last 10 candles

    if side == "buy":
        sl = min(lookback) * 0.999      # thoda buffer
        tp = p + (abs(p - sl) * RR_RATIO)
    else:
        sl = max(lookback) * 1.001
        tp = p - (abs(p - sl) * RR_RATIO)

    risk_usdt = abs(p - sl) * size * ct_val

    if risk_usdt <= 0:
        return "Invalid risk"

    # ===========================================================
    #  PLACE ORDER + EXCHANGE TP/SL
    # ===========================================================
    result = place_order(side, size, tp, sl)

    if result.get("code") != "0":
        err_msg = result.get("msg", "Unknown error")
        log("ORDER", f"❌ FAILED: {err_msg}")
        send_telegram(f"❌ ORDER FAILED\n{SYMBOL}\n{err_msg}")
        return "Order failed"

    # ===========================================================
    #  SAVE STATE
    # ===========================================================
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

    # ===========================================================
    #  CONSOLE DETAILS
    # ===========================================================
    print_trade_details(
        action    = "🚀 TRADE OPENED",
        side      = side,
        entry     = p,
        sl        = sl,
        tp        = tp,
        size      = size,
        bal       = bal,
        risk_usdt = risk_usdt
    )

    # ===========================================================
    #  TELEGRAM ALERT
    # ===========================================================
    direction_emoji = "🟢 LONG" if side == "buy" else "🔴 SHORT"
    tg_msg = f"""
🚀 TRADE OPENED — {SYMBOL}

Direction  : {direction_emoji}
Reason     : {reason}
Entry      : {p:.4f} USDT
Stop Loss  : {sl:.4f} USDT
Take Profit: {tp:.4f} USDT
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
    log("BOT", "  OKX SMC BOT — STARTED")
    log("BOT", f"  Pair     : {SYMBOL}")
    log("BOT", f"  Interval : {INTERVAL}")
    log("BOT", f"  Leverage : {LEVERAGE}x")
    log("BOT", f"  Position : {int(POSITION_PCT*100)}% of balance")
    log("BOT", "="*50)

    set_leverage()
    send_telegram(f"🤖 OKX SMC Bot Started\nPair: {SYMBOL} | {LEVERAGE}x | {int(POSITION_PCT*100)}% balance")

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
