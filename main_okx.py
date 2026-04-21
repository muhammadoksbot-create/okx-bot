import requests
import hmac
import hashlib
import json
import os
import time
import math
from datetime import datetime, UTC
from urllib.parse import urlencode

from config_okx import API_KEY, SECRET_KEY

# ============================================================
# CONFIG
# ============================================================
BASE_URL = "https://api.bybit.com"
STATE_FILE = "state_bybit.json"

SYMBOL = "XRPUSDT"
CATEGORY = "linear"
INTERVAL = "5"          # 5-minute candles
LEVERAGE = 15
POSITION_PCT = 0.10     # 10% of wallet
RR_RATIO = 1.5
SWING_LB = 3
ATR_PERIOD = 14
ATR_MULTIPLIER = 2.0
RECV_WINDOW = "5000"

# Telegram
TELEGRAM_TOKEN = "8756536068:AAFu7zrR5W-gu0Mv9bX4Tf9O7kokeqk6G5U"
CHAT_ID = "1118069943"

# ============================================================
# LOGGING
# ============================================================
def log(tag: str, msg: str) -> None:
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{now}] [{tag}] {msg}", flush=True)

def send_telegram(msg: str) -> None:
    if not TELEGRAM_TOKEN or not CHAT_ID or "YOUR_" in TELEGRAM_TOKEN or "YOUR_" in CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": CHAT_ID, "text": msg}, timeout=10)
    except Exception as e:
        log("TG_ERR", str(e))

# ============================================================
# BYBIT AUTH
# ============================================================
def _sign(payload: str, timestamp: str) -> str:
    plain = f"{timestamp}{API_KEY}{RECV_WINDOW}{payload}"
    return hmac.new(
        SECRET_KEY.encode("utf-8"),
        plain.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

def _headers(payload: str, timestamp: str) -> dict:
    return {
        "X-BAPI-API-KEY": API_KEY,
        "X-BAPI-SIGN": _sign(payload, timestamp),
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-RECV-WINDOW": RECV_WINDOW,
        "Content-Type": "application/json",
    }

# ============================================================
# HTTP REQUESTS
# ============================================================
def req(method: str, path: str, params: dict | None = None, body: dict | None = None) -> dict:
    """
    GET signature payload = query string
    POST signature payload = json body string
    """
    try:
        timestamp = str(int(time.time() * 1000))

        if method.upper() == "GET":
            query_string = urlencode(params or {})
            payload = query_string
            headers = _headers(payload, timestamp)
            url = f"{BASE_URL}{path}"
            if query_string:
                url += f"?{query_string}"
            r = requests.get(url, headers=headers, timeout=15)

        elif method.upper() == "POST":
            body_str = json.dumps(body or {}, separators=(",", ":"))
            payload = body_str
            headers = _headers(payload, timestamp)
            url = f"{BASE_URL}{path}"
            r = requests.post(url, headers=headers, data=body_str, timeout=15)

        else:
            raise ValueError(f"Unsupported HTTP method: {method}")

        data = r.json()

        # Useful debug for Railway logs
        if data.get("retCode") not in (0, None):
            log("API_RET", f"{path} -> retCode={data.get('retCode')} retMsg={data.get('retMsg')}")

        return data

    except Exception as e:
        log("API_ERR", f"{path} -> {e}")
        send_telegram(f"⚠️ API ERROR\n{path}\n{e}")
        return {}

# ============================================================
# STATE
# ============================================================
def default_state() -> dict:
    return {
        "pos": None,
        "entry": None,
        "sl": None,
        "tp": None,
        "size": None,
        "reason": None,
        "opened": None,
    }

def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return default_state()
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        base = default_state()
        base.update(data)
        return base
    except Exception:
        return default_state()

def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

# ============================================================
# MARKET DATA
# ============================================================
def get_candles() -> list:
    r = req(
        "GET",
        "/v5/market/kline",
        params={
            "category": CATEGORY,
            "symbol": SYMBOL,
            "interval": INTERVAL,
            "limit": 300,
        },
    )
    if r.get("retCode") == 0 and r.get("result", {}).get("list"):
        # Bybit newest first -> reverse for oldest->newest
        return list(reversed(r["result"]["list"]))
    log("CANDLES", f"Failed: {r}")
    return []

def get_price() -> float | None:
    r = req(
        "GET",
        "/v5/market/tickers",
        params={
            "category": CATEGORY,
            "symbol": SYMBOL,
        },
    )
    try:
        return float(r["result"]["list"][0]["lastPrice"])
    except Exception:
        log("PRICE", f"Failed: {r}")
        return None

def get_balance() -> float | None:
    """
    Unified wallet balance for USDT coin.
    """
    r = req(
        "GET",
        "/v5/account/wallet-balance",
        params={
            "accountType": "UNIFIED",
            "coin": "USDT",
        },
    )

    try:
        coin_list = r["result"]["list"][0]["coin"]
        if not coin_list:
            log("BALANCE", f"No coin data in response: {r}")
            return None

        usdt = coin_list[0]
        wallet_balance = float(usdt.get("walletBalance", 0))
        equity = float(usdt.get("equity", wallet_balance))

        # For simple bot sizing, walletBalance is fine for coin balance visibility.
        log("BALANCE", f"walletBalance={wallet_balance} equity={equity}")
        return wallet_balance
    except Exception as e:
        log("BALANCE_ERR", f"{e} | RAW={r}")
        return None

def get_open_position() -> dict | None:
    r = req(
        "GET",
        "/v5/position/list",
        params={
            "category": CATEGORY,
            "symbol": SYMBOL,
        },
    )
    try:
        for pos in r["result"]["list"]:
            size = float(pos.get("size", 0))
            side = pos.get("side", "")
            if size > 0 and side in ("Buy", "Sell"):
                return {
                    "side": side,
                    "size": size,
                    "entry": float(pos.get("avgPrice", 0)),
                    "markPrice": float(pos.get("markPrice", 0) or 0),
                    "takeProfit": pos.get("takeProfit"),
                    "stopLoss": pos.get("stopLoss"),
                }
    except Exception as e:
        log("POS_ERR", f"{e} | RAW={r}")
    return None

def set_leverage() -> None:
    body = {
        "category": CATEGORY,
        "symbol": SYMBOL,
        "buyLeverage": str(LEVERAGE),
        "sellLeverage": str(LEVERAGE),
    }
    r = req("POST", "/v5/position/set-leverage", body=body)
    if r.get("retCode") == 0:
        log("LEVERAGE", f"✅ Set {LEVERAGE}x on {SYMBOL}")
    else:
        # retCode 110043/110044 etc can happen if already set; log only
        log("LEVERAGE", f"Response: {r}")

# ============================================================
# STRATEGY HELPERS
# ============================================================
def find_swings(closes: list[float], lb: int = SWING_LB) -> list[tuple]:
    swings = []
    for i in range(lb, len(closes) - lb):
        is_high = all(closes[i] > closes[i - j] and closes[i] > closes[i + j] for j in range(1, lb + 1))
        is_low = all(closes[i] < closes[i - j] and closes[i] < closes[i + j] for j in range(1, lb + 1))
        if is_high:
            swings.append(("H", i, closes[i]))
        elif is_low:
            swings.append(("L", i, closes[i]))
    return swings

def detect_bos(swings: list[tuple]) -> str | None:
    if len(swings) < 3:
        return None
    a, b, c = swings[-3], swings[-2], swings[-1]
    if b[0] == "L" and c[0] == "H" and c[2] > a[2]:
        return "BOS_UP"
    if b[0] == "H" and c[0] == "L" and c[2] < a[2]:
        return "BOS_DOWN"
    return None

def detect_choch(swings: list[tuple]) -> str | None:
    if len(swings) < 4:
        return None
    a, b, c, d = swings[-4], swings[-3], swings[-2], swings[-1]
    if a[0] == "H" and b[0] == "L" and c[0] == "H" and d[0] == "L" and d[2] < b[2]:
        return "SHORT"
    if a[0] == "L" and b[0] == "H" and c[0] == "L" and d[0] == "H" and d[2] > b[2]:
        return "LONG"
    return None

def detect_fvg(candles: list) -> tuple | None:
    if len(candles) < 3:
        return None
    c0, c2 = candles[-3], candles[-1]
    c0_high = float(c0[2])
    c0_low = float(c0[3])
    c2_high = float(c2[2])
    c2_low = float(c2[3])

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
    last_low = float(last[3])
    prev_high = float(prev[2])
    prev_low = float(prev[3])
    last_close = float(last[4])

    swept_high = (last_high > prev_high) and (last_close < prev_high)
    swept_low = (last_low < prev_low) and (last_close > prev_low)
    return swept_high or swept_low

def calc_atr(candles: list, period: int = ATR_PERIOD) -> float:
    if len(candles) < period + 1:
        return 0.0

    trs = []
    for i in range(1, len(candles)):
        high = float(candles[i][2])
        low = float(candles[i][3])
        prev_close = float(candles[i - 1][4])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)

    return sum(trs[-period:]) / period if trs else 0.0

def smart_stop_loss(side: str, swings: list[tuple], current_price: float, candles: list) -> float:
    if side == "Buy":
        lows = [s[2] for s in swings if s[0] == "L"]
        if len(lows) >= 2:
            return lows[-2] * 0.999
        if len(lows) == 1:
            return lows[-1] * 0.999
        atr = calc_atr(candles)
        return current_price - (atr * ATR_MULTIPLIER) if atr > 0 else current_price * 0.98
    else:
        highs = [s[2] for s in swings if s[0] == "H"]
        if len(highs) >= 2:
            return highs[-2] * 1.001
        if len(highs) == 1:
            return highs[-1] * 1.001
        atr = calc_atr(candles)
        return current_price + (atr * ATR_MULTIPLIER) if atr > 0 else current_price * 1.02

# ============================================================
# ORDER HELPERS
# ============================================================
def build_order_qty(balance_usdt: float, price: float) -> int:
    """
    10% wallet * 15x leverage -> exposure
    qty for XRPUSDT is in XRP contracts (base coin units).
    """
    position_value = balance_usdt * POSITION_PCT
    exposure = position_value * LEVERAGE
    qty = math.floor(exposure / price)
    return max(qty, 1)

def place_order(side: str, qty: int, tp: float, sl: float) -> dict:
    body = {
        "category": CATEGORY,
        "symbol": SYMBOL,
        "side": side,
        "orderType": "Market",
        "qty": str(qty),
        "timeInForce": "IOC",
        "positionIdx": 0,
        "takeProfit": str(round(tp, 4)),
        "stopLoss": str(round(sl, 4)),
        "tpTriggerBy": "MarkPrice",
        "slTriggerBy": "MarkPrice",
    }
    r = req("POST", "/v5/order/create", body=body)
    return r

def close_position(side: str, qty: int) -> dict:
    close_side = "Sell" if side == "Buy" else "Buy"
    body = {
        "category": CATEGORY,
        "symbol": SYMBOL,
        "side": close_side,
        "orderType": "Market",
        "qty": str(qty),
        "positionIdx": 0,
        "reduceOnly": True,
    }
    r = req("POST", "/v5/order/create", body=body)
    return r

# ============================================================
# DISPLAY
# ============================================================
def print_trade_details(action: str, side: str, entry: float, sl: float, tp: float, qty: int, balance_usdt: float) -> None:
    risk_usdt = abs(entry - sl) * qty
    reward_usdt = abs(tp - entry) * qty
    risk_pct = (risk_usdt / balance_usdt * 100) if balance_usdt else 0

    direction = "🟢 LONG" if side == "Buy" else "🔴 SHORT"

    print("\n" + "=" * 60)
    print(action)
    print("=" * 60)
    print(f"Pair      : {SYMBOL}")
    print(f"Direction : {direction}")
    print(f"Entry     : {entry:.4f}")
    print(f"SL        : {sl:.4f}")
    print(f"TP        : {tp:.4f}")
    print(f"Qty       : {qty}")
    print(f"Leverage  : {LEVERAGE}x")
    print(f"Risk      : {risk_usdt:.4f} USDT ({risk_pct:.2f}%)")
    print(f"Reward    : {reward_usdt:.4f} USDT")
    print(f"Balance   : {balance_usdt:.4f} USDT")
    print("=" * 60 + "\n")

# ============================================================
# CORE
# ============================================================
def run() -> str:
    state = load_state()

    candles = get_candles()
    if not candles:
        return "No candle data"

    price = get_price()
    if not price:
        return "No price"

    closes = [float(c[4]) for c in candles]

    # Sync with exchange position
    actual_pos = get_open_position()
    if actual_pos:
        if not state.get("pos"):
            state["pos"] = actual_pos["side"]
            state["size"] = int(float(actual_pos["size"]))
            state["entry"] = float(actual_pos["entry"])
            # keep previous TP/SL if state absent, but not fatal
            save_state(state)
            log("SYNC", f"Exchange position synced: {state['pos']} qty={state['size']} entry={state['entry']}")

        entry = float(state.get("entry") or actual_pos["entry"])
        pos_side = state["pos"]
        pnl = (price - entry) if pos_side == "Buy" else (entry - price)

        log("POSITION", f"{pos_side} | entry={entry:.4f} now={price:.4f} pnl={pnl:+.4f}")
        return "Position running"

    # If exchange position gone, reset local state
    if state.get("pos"):
        log("SYNC", "Position closed on exchange -> resetting local state")
        save_state(default_state())
        state = default_state()

    # Strategy
    swings = find_swings(closes)
    bos = detect_bos(swings)
    choch = detect_choch(swings)
    fvg = detect_fvg(candles)
    liq = detect_liquidity(candles)

    volumes = [float(c[5]) for c in candles[-20:]]
    avg_vol = sum(volumes[:-1]) / max(len(volumes[:-1]), 1)
    current_vol = volumes[-1] if volumes else 0
    volume_surge = current_vol > avg_vol * 1.2 if avg_vol > 0 else False

    log("SMC", f"BOS={bos} | CHOCH={choch} | FVG={fvg[0] if fvg else None} | Liq={liq} | Vol={volume_surge}")

    side = None
    reason = None

    if choch == "LONG" and volume_surge:
        side = "Buy"
        reason = "CHoCH LONG + Volume"
    elif choch == "SHORT" and volume_surge:
        side = "Sell"
        reason = "CHoCH SHORT + Volume"
    elif bos == "BOS_UP" and fvg and fvg[0] == "bull" and volume_surge:
        side = "Buy"
        reason = "BOS UP + Bullish FVG + Volume"
    elif bos == "BOS_DOWN" and fvg and fvg[0] == "bear" and volume_surge:
        side = "Sell"
        reason = "BOS DOWN + Bearish FVG + Volume"

    if not side:
        log("SIGNAL", "No setup")
        return "No setup"

    balance_usdt = get_balance()
    if balance_usdt is None or balance_usdt <= 0:
        log("BALANCE", "No balance")
        return "No balance"

    qty = build_order_qty(balance_usdt, price)
    sl = smart_stop_loss(side, swings, price, candles)

    if side == "Buy":
        if sl >= price:
            sl = price * 0.995
        tp = price + (abs(price - sl) * RR_RATIO)
    else:
        if sl <= price:
            sl = price * 1.005
        tp = price - (abs(price - sl) * RR_RATIO)

    if tp <= 0 or sl <= 0:
        log("RISK", f"Invalid TP/SL | price={price} sl={sl} tp={tp}")
        return "Invalid TP/SL"

    log("DEBUG", f"balance={balance_usdt:.4f} price={price:.4f} qty={qty} side={side} sl={sl:.4f} tp={tp:.4f}")

    order_res = place_order(side, qty, tp, sl)
    if order_res.get("retCode") != 0:
        log("ORDER_FAIL", str(order_res))
        send_telegram(f"❌ ORDER FAILED\n{SYMBOL}\n{order_res}")
        return "Order failed"

    state.update({
        "pos": side,
        "entry": price,
        "sl": sl,
        "tp": tp,
        "size": qty,
        "reason": reason,
        "opened": datetime.now(UTC).isoformat(),
    })
    save_state(state)

    print_trade_details("🚀 TRADE OPENED", side, price, sl, tp, qty, balance_usdt)

    direction = "🟢 LONG" if side == "Buy" else "🔴 SHORT"
    send_telegram(
        f"🚀 TRADE OPENED\n"
        f"Pair: {SYMBOL}\n"
        f"Direction: {direction}\n"
        f"Reason: {reason}\n"
        f"Entry: {price:.4f}\n"
        f"SL: {sl:.4f}\n"
        f"TP: {tp:.4f}\n"
        f"Qty: {qty}\n"
        f"Leverage: {LEVERAGE}x\n"
        f"Balance: {balance_usdt:.4f} USDT"
    )

    return "Trade opened"

# ============================================================
# MAIN
# ============================================================
def main() -> None:
    log("BOT", "=" * 56)
    log("BOT", f"Bybit SMC Bot | {SYMBOL}")
    log("BOT", f"Wallet Usage: {int(POSITION_PCT * 100)}% | Leverage: {LEVERAGE}x | RR: 1:{RR_RATIO}")
    log("BOT", "=" * 56)

    set_leverage()
    send_telegram(
        f"🤖 BOT STARTED\n"
        f"{SYMBOL}\n"
        f"Wallet: {int(POSITION_PCT * 100)}%\n"
        f"Leverage: {LEVERAGE}x\n"
        f"RR: 1:{RR_RATIO}"
    )

    while True:
        try:
            result = run()
            log("RUN", result)
            time.sleep(60)
        except Exception as e:
            log("ERROR", str(e))
            send_telegram(f"⚠️ BOT ERROR\n{e}")
            time.sleep(15)

if __name__ == "__main__":
    main()
