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

SYMBOLS = ["XRPUSDT", "DOGEUSDT", "TRXUSDT", "XLMUSDT", "HBARUSDT"]
CATEGORY = "linear"
INTERVAL = "5"

LEVERAGE = 15
POSITION_PCT = 0.10
RR_RATIO = 1.5
SWING_LB = 3
ATR_PERIOD = 14
ATR_MULTIPLIER = 2.0
RECV_WINDOW = "5000"

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
# AUTH
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
# HTTP
# ============================================================
def req(method: str, path: str, params: dict | None = None, body: dict | None = None) -> dict:
    try:
        timestamp = str(int(time.time() * 1000))

        if method.upper() == "GET":
            query_string = urlencode(params or {})
            headers = _headers(query_string, timestamp)
            url = f"{BASE_URL}{path}"
            if query_string:
                url += f"?{query_string}"
            r = requests.get(url, headers=headers, timeout=15)

        elif method.upper() == "POST":
            body_str = json.dumps(body or {}, separators=(",", ":"))
            headers = _headers(body_str, timestamp)
            url = f"{BASE_URL}{path}"
            r = requests.post(url, headers=headers, data=body_str, timeout=15)

        else:
            raise ValueError(f"Unsupported HTTP method: {method}")

        data = r.json()

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
        "symbol": None,
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
        state = default_state()
        state.update(data)
        return state
    except Exception:
        return default_state()

def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

# ============================================================
# MARKET DATA
# ============================================================
def get_candles(symbol: str) -> list:
    r = req(
        "GET",
        "/v5/market/kline",
        params={
            "category": CATEGORY,
            "symbol": symbol,
            "interval": INTERVAL,
            "limit": 300,
        },
    )
    if r.get("retCode") == 0 and r.get("result", {}).get("list"):
        return list(reversed(r["result"]["list"]))
    return []

def get_price(symbol: str) -> float | None:
    r = req(
        "GET",
        "/v5/market/tickers",
        params={
            "category": CATEGORY,
            "symbol": symbol,
        },
    )
    try:
        return float(r["result"]["list"][0]["lastPrice"])
    except Exception:
        return None

def get_balance() -> float | None:
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
            log("BALANCE", f"No coin data: {r}")
            return None
        usdt = coin_list[0]
        wallet_balance = float(usdt.get("walletBalance", 0))
        equity = float(usdt.get("equity", wallet_balance))
        log("BALANCE", f"walletBalance={wallet_balance} equity={equity}")
        return wallet_balance
    except Exception as e:
        log("BALANCE_ERR", f"{e} | RAW={r}")
        return None

def get_instrument_info(symbol: str) -> dict | None:
    r = req(
        "GET",
        "/v5/market/instruments-info",
        params={
            "category": CATEGORY,
            "symbol": symbol,
        },
    )
    try:
        info = r["result"]["list"][0]
        return {
            "qty_step": float(info["lotSizeFilter"]["qtyStep"]),
            "min_order_qty": float(info["lotSizeFilter"]["minOrderQty"]),
            "tick_size": float(info["priceFilter"]["tickSize"]),
        }
    except Exception:
        return None

def get_open_position(symbol: str) -> dict | None:
    r = req(
        "GET",
        "/v5/position/list",
        params={
            "category": CATEGORY,
            "symbol": symbol,
        },
    )
    try:
        for pos in r["result"]["list"]:
            size = float(pos.get("size", 0))
            side = pos.get("side", "")
            if size > 0 and side in ("Buy", "Sell"):
                return {
                    "symbol": symbol,
                    "side": side,
                    "size": size,
                    "entry": float(pos.get("avgPrice", 0)),
                    "markPrice": float(pos.get("markPrice", 0) or 0),
                    "takeProfit": pos.get("takeProfit"),
                    "stopLoss": pos.get("stopLoss"),
                }
    except Exception as e:
        log("POS_ERR", f"{symbol} -> {e}")
    return None

def find_any_open_position() -> dict | None:
    for symbol in SYMBOLS:
        pos = get_open_position(symbol)
        if pos:
            return pos
    return None

def set_leverage(symbol: str) -> None:
    body = {
        "category": CATEGORY,
        "symbol": symbol,
        "buyLeverage": str(LEVERAGE),
        "sellLeverage": str(LEVERAGE),
    }
    r = req("POST", "/v5/position/set-leverage", body=body)
    if r.get("retCode") == 0:
        log("LEVERAGE", f"✅ Set {LEVERAGE}x on {symbol}")
    else:
        log("LEVERAGE", f"{symbol} -> {r.get('retMsg', r)}")

# ============================================================
# HELPERS
# ============================================================
def floor_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    return math.floor(value / step) * step

def round_price(price: float, tick_size: float) -> float:
    if tick_size <= 0:
        return price
    return round(round(price / tick_size) * tick_size, 8)

# ============================================================
# STRATEGY
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

def scan_symbol(symbol: str) -> dict | None:
    candles = get_candles(symbol)
    if not candles:
        log("SCAN", f"{symbol} -> no candles")
        return None

    price = get_price(symbol)
    if not price:
        log("SCAN", f"{symbol} -> no price")
        return None

    closes = [float(c[4]) for c in candles]
    swings = find_swings(closes)
    bos = detect_bos(swings)
    choch = detect_choch(swings)
    fvg = detect_fvg(candles)
    liq = detect_liquidity(candles)

    volumes = [float(c[5]) for c in candles[-20:]]
    avg_vol = sum(volumes[:-1]) / max(len(volumes[:-1]), 1) if len(volumes) > 1 else 0
    current_vol = volumes[-1] if volumes else 0
    volume_surge = current_vol > avg_vol * 1.2 if avg_vol > 0 else False

    log("SMC", f"{symbol} | BOS={bos} | CHOCH={choch} | FVG={fvg[0] if fvg else None} | Liq={liq} | Vol={volume_surge}")

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
        return None

    return {
        "symbol": symbol,
        "side": side,
        "reason": reason,
        "price": price,
        "candles": candles,
        "swings": swings,
    }

# ============================================================
# ORDER
# ============================================================
def build_order_qty(symbol: str, balance_usdt: float, price: float) -> float | None:
    instrument = get_instrument_info(symbol)
    if not instrument:
        log("SIZE", f"{symbol} -> instrument info failed")
        return None

    position_value = balance_usdt * POSITION_PCT
    exposure = position_value * LEVERAGE
    raw_qty = exposure / price

    qty = floor_to_step(raw_qty, instrument["qty_step"])
    if qty < instrument["min_order_qty"]:
        qty = instrument["min_order_qty"]

    qty = round(qty, 8)
    return qty

def place_order(symbol: str, side: str, qty: float, tp: float, sl: float) -> dict:
    body = {
        "category": CATEGORY,
        "symbol": symbol,
        "side": side,
        "orderType": "Market",
        "qty": str(qty),
        "timeInForce": "IOC",
        "positionIdx": 0,
        "takeProfit": str(tp),
        "stopLoss": str(sl),
        "tpTriggerBy": "MarkPrice",
        "slTriggerBy": "MarkPrice",
    }
    return req("POST", "/v5/order/create", body=body)

# ============================================================
# DISPLAY
# ============================================================
def print_trade_details(symbol: str, action: str, side: str, entry: float, sl: float, tp: float, qty: float, balance_usdt: float) -> None:
    risk_usdt = abs(entry - sl) * qty
    reward_usdt = abs(tp - entry) * qty
    risk_pct = (risk_usdt / balance_usdt * 100) if balance_usdt else 0

    direction = "🟢 LONG" if side == "Buy" else "🔴 SHORT"

    print("\n" + "=" * 60)
    print(action)
    print("=" * 60)
    print(f"Pair      : {symbol}")
    print(f"Direction : {direction}")
    print(f"Entry     : {entry:.6f}")
    print(f"SL        : {sl:.6f}")
    print(f"TP        : {tp:.6f}")
    print(f"Qty       : {qty}")
    print(f"Leverage  : {LEVERAGE}x")
    print(f"Risk      : {risk_usdt:.6f} USDT ({risk_pct:.2f}%)")
    print(f"Reward    : {reward_usdt:.6f} USDT")
    print(f"Balance   : {balance_usdt:.6f} USDT")
    print("=" * 60 + "\n")

# ============================================================
# CORE
# ============================================================
def run() -> str:
    state = load_state()

    # Only 1 active trade total
    actual_pos = find_any_open_position()
    if actual_pos:
        if not state.get("pos"):
            state.update({
                "symbol": actual_pos["symbol"],
                "pos": actual_pos["side"],
                "size": actual_pos["size"],
                "entry": actual_pos["entry"],
            })
            save_state(state)
            log("SYNC", f"Synced exchange position: {actual_pos['symbol']} {actual_pos['side']} qty={actual_pos['size']}")
        else:
            state["symbol"] = actual_pos["symbol"]
            state["pos"] = actual_pos["side"]
            state["size"] = actual_pos["size"]
            state["entry"] = actual_pos["entry"]
            save_state(state)

        entry = float(state.get("entry") or actual_pos["entry"])
        pos_side = state["pos"]
        symbol = state["symbol"]
        mark = actual_pos.get("markPrice") or get_price(symbol) or entry
        pnl = (mark - entry) if pos_side == "Buy" else (entry - mark)

        log("POSITION", f"{symbol} | {pos_side} | entry={entry:.6f} now={mark:.6f} pnl={pnl:+.6f}")
        return f"Position running on {symbol}"

    # If exchange position gone, clear local state
    if state.get("pos"):
        log("SYNC", "No exchange position -> resetting local state")
        save_state(default_state())
        state = default_state()

    balance_usdt = get_balance()
    if balance_usdt is None or balance_usdt <= 0:
        return "No balance"

    # Scan pairs, take first valid setup only
    for symbol in SYMBOLS:
        setup = scan_symbol(symbol)
        if not setup:
            continue

        price = setup["price"]
        side = setup["side"]
        reason = setup["reason"]
        candles = setup["candles"]
        swings = setup["swings"]

        instrument = get_instrument_info(symbol)
        if not instrument:
            log("INFO", f"{symbol} -> no instrument info")
            continue

        qty = build_order_qty(symbol, balance_usdt, price)
        if qty is None or qty <= 0:
            log("SIZE", f"{symbol} -> invalid qty")
            continue

        sl = smart_stop_loss(side, swings, price, candles)

        if side == "Buy":
            if sl >= price:
                sl = price * 0.995
            tp = price + (abs(price - sl) * RR_RATIO)
        else:
            if sl <= price:
                sl = price * 1.005
            tp = price - (abs(price - sl) * RR_RATIO)

        sl = round_price(sl, instrument["tick_size"])
        tp = round_price(tp, instrument["tick_size"])

        if tp <= 0 or sl <= 0:
            log("RISK", f"{symbol} -> invalid TP/SL")
            continue

        log("DEBUG", f"{symbol} | balance={balance_usdt:.4f} price={price:.6f} qty={qty} side={side} sl={sl} tp={tp}")

        order_res = place_order(symbol, side, qty, tp, sl)
        if order_res.get("retCode") != 0:
            log("ORDER_FAIL", f"{symbol} -> {order_res}")
            send_telegram(f"❌ ORDER FAILED\n{symbol}\n{order_res}")
            continue

        state.update({
            "symbol": symbol,
            "pos": side,
            "entry": price,
            "sl": sl,
            "tp": tp,
            "size": qty,
            "reason": reason,
            "opened": datetime.now(UTC).isoformat(),
        })
        save_state(state)

        print_trade_details(symbol, "🚀 TRADE OPENED", side, price, sl, tp, qty, balance_usdt)

        direction = "🟢 LONG" if side == "Buy" else "🔴 SHORT"
        send_telegram(
            f"🚀 TRADE OPENED\n"
            f"Pair: {symbol}\n"
            f"Direction: {direction}\n"
            f"Reason: {reason}\n"
            f"Entry: {price:.6f}\n"
            f"SL: {sl}\n"
            f"TP: {tp}\n"
            f"Qty: {qty}\n"
            f"Leverage: {LEVERAGE}x\n"
            f"Balance: {balance_usdt:.4f} USDT"
        )

        return f"Trade opened on {symbol}"

    return "No setup on any pair"

# ============================================================
# MAIN
# ============================================================
def main() -> None:
    log("BOT", "=" * 64)
    log("BOT", "Bybit Multi-Pair SMC Bot")
    log("BOT", f"Pairs: {', '.join(SYMBOLS)}")
    log("BOT", f"Wallet Usage: {int(POSITION_PCT * 100)}% | Leverage: {LEVERAGE}x | RR: 1:{RR_RATIO}")
    log("BOT", "Mode: Scan 5 pairs, only 1 active trade total")
    log("BOT", "=" * 64)

    for symbol in SYMBOLS:
        set_leverage(symbol)
        time.sleep(0.3)

    send_telegram(
        f"🤖 BOT STARTED\n"
        f"Pairs: {', '.join(SYMBOLS)}\n"
        f"Wallet: {int(POSITION_PCT * 100)}%\n"
        f"Leverage: {LEVERAGE}x\n"
        f"Mode: 1 active trade only"
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
