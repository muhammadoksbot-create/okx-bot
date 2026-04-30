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

SYMBOLS = ["XRPUSDT", "DOGEUSDT", "HBARUSDT"]

CATEGORY = "linear"
ENTRY_INTERVAL = "5"
TREND_INTERVAL = "60"

LEVERAGE = 15
POSITION_PCT = 0.10
RR_RATIO = 1.5
SWING_LB = 3
ATR_PERIOD = 14
RECV_WINDOW = "5000"

VOLUME_MULTIPLIER = 1.10
COOLDOWN_MINUTES = 20

PARTIAL_AT_R = 1.0
PARTIAL_CLOSE_PCT = 0.50
MOVE_SL_TO_BE = True

# Soft fee-aware settings
TAKER_FEE_RATE = 0.00055
MIN_NET_PROFIT_USDT = 0.003
MIN_R_MULTIPLE = 1.0

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8756536068:AAFu7zrR5W-gu0Mv9bX4Tf9O7kokeqk6G5U")
CHAT_ID = os.getenv("CHAT_ID", "1118069943")

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
# HTTP with retry
# ============================================================
def req(method: str, path: str, params: dict | None = None, body: dict | None = None, retries: int = 3) -> dict:
    last_err = None

    for attempt in range(1, retries + 1):
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
                raise ValueError(f"Unsupported method: {method}")

            data = r.json()

            if data.get("retCode") not in (0, None):
                log("API_RET", f"{path} -> retCode={data.get('retCode')} retMsg={data.get('retMsg')}")

            return data

        except Exception as e:
            last_err = e
            log("API_ERR", f"{path} attempt {attempt}/{retries} -> {e}")
            if attempt < retries:
                time.sleep(1.5 * attempt)

    send_telegram(f"⚠️ API ERROR\n{path}\n{last_err}")
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
        "last_closed_at": None,
        "initial_risk": None,
        "partial_taken": False,
        "partial_qty": None,
        "remaining_qty": None,
        "breakeven_moved": False,
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
def get_candles(symbol: str, interval: str, limit: int = 300) -> list:
    r = req(
        "GET",
        "/v5/market/kline",
        params={
            "category": CATEGORY,
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
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

def ema(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    ema_val = values[0]
    for v in values[1:]:
        ema_val = v * k + ema_val * (1 - k)
    return ema_val

def iso_now() -> str:
    return datetime.now(UTC).isoformat()

def estimate_round_trip_fees(entry: float, exit_price: float, qty: float) -> float:
    entry_notional = entry * qty
    exit_notional = exit_price * qty
    return (entry_notional * TAKER_FEE_RATE) + (exit_notional * TAKER_FEE_RATE)

def gross_pnl(side: str, entry: float, exit_price: float, qty: float) -> float:
    if side == "Buy":
        return (exit_price - entry) * qty
    return (entry - exit_price) * qty

def net_pnl_estimate(side: str, entry: float, exit_price: float, qty: float) -> tuple[float, float, float]:
    gross = gross_pnl(side, entry, exit_price, qty)
    fees = estimate_round_trip_fees(entry, exit_price, qty)
    net = gross - fees
    return gross, fees, net

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
    atr = calc_atr(candles)
    atr_buffer = atr * 0.5 if atr > 0 else 0

    if side == "Buy":
        lows = [s[2] for s in swings if s[0] == "L"]
        if len(lows) >= 1:
            return lows[-1] - atr_buffer
        return current_price * 0.98

    highs = [s[2] for s in swings if s[0] == "H"]
    if len(highs) >= 1:
        return highs[-1] + atr_buffer
    return current_price * 1.02

def scan_symbol(symbol: str) -> dict | None:
    candles_5m = get_candles(symbol, ENTRY_INTERVAL, 300)
    candles_1h = get_candles(symbol, TREND_INTERVAL, 250)

    if not candles_5m or not candles_1h:
        log("SCAN", f"{symbol} -> missing candles")
        return None

    price = get_price(symbol)
    if not price:
        log("SCAN", f"{symbol} -> no price")
        return None

    closes_5m = [float(c[4]) for c in candles_5m]
    closes_1h = [float(c[4]) for c in candles_1h]

    swings = find_swings(closes_5m)
    bos = detect_bos(swings)
    choch = detect_choch(swings)
    fvg = detect_fvg(candles_5m)
    liq = detect_liquidity(candles_5m)

    ema200 = ema(closes_1h, 200)
    trend_up = ema200 is not None and price > ema200
    trend_down = ema200 is not None and price < ema200

    volumes = [float(c[5]) for c in candles_5m[-20:]]
    avg_vol = sum(volumes[:-1]) / max(len(volumes[:-1]), 1) if len(volumes) > 1 else 0
    current_vol = volumes[-1] if volumes else 0
    volume_surge = current_vol > avg_vol * VOLUME_MULTIPLIER if avg_vol > 0 else False

    log(
        "SMC",
        f"{symbol} | BOS={bos} | CHOCH={choch} | FVG={fvg[0] if fvg else None} | "
        f"Liq={liq} | Vol={volume_surge} | TrendUp={trend_up} | TrendDown={trend_down}"
    )

    side = None
    reason = None

    if trend_up and liq and volume_surge:
        if choch == "LONG" and fvg and fvg[0] == "bull":
            side = "Buy"
            reason = "HTF UP + CHOCH LONG + BULL FVG + LIQ + VOL"
        elif bos == "BOS_UP" and (fvg is None or fvg[0] == "bull"):
            side = "Buy"
            reason = "HTF UP + BOS UP + LIQ + VOL"

    elif trend_down and liq and volume_surge:
        if choch == "SHORT" and fvg and fvg[0] == "bear":
            side = "Sell"
            reason = "HTF DOWN + CHOCH SHORT + BEAR FVG + LIQ + VOL"
        elif bos == "BOS_DOWN" and (fvg is None or fvg[0] == "bear"):
            side = "Sell"
            reason = "HTF DOWN + BOS DOWN + LIQ + VOL"

    if not side:
        return None

    return {
        "symbol": symbol,
        "side": side,
        "reason": reason,
        "price": price,
        "candles": candles_5m,
        "swings": swings,
    }

# ============================================================
# ORDER MANAGEMENT
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

    return round(qty, 8)

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

def reduce_position(symbol: str, current_side: str, qty: float) -> dict:
    close_side = "Sell" if current_side == "Buy" else "Buy"
    body = {
        "category": CATEGORY,
        "symbol": symbol,
        "side": close_side,
        "orderType": "Market",
        "qty": str(qty),
        "positionIdx": 0,
        "reduceOnly": True,
    }
    return req("POST", "/v5/order/create", body=body)

def update_trading_stop(symbol: str, sl: float | None = None, tp: float | None = None) -> dict:
    body = {
        "category": CATEGORY,
        "symbol": symbol,
        "positionIdx": 0,
    }
    if sl is not None:
        body["stopLoss"] = str(sl)
        body["slTriggerBy"] = "MarkPrice"
    if tp is not None:
        body["takeProfit"] = str(tp)
        body["tpTriggerBy"] = "MarkPrice"
    return req("POST", "/v5/position/trading-stop", body=body)

# ============================================================
# DISPLAY / RESULT
# ============================================================
def print_trade_details(symbol: str, action: str, side: str, entry: float, sl: float, tp: float, qty: float, balance_usdt: float) -> None:
    risk_usdt = abs(entry - sl) * qty
    gross_tp, fees_tp, net_tp = net_pnl_estimate(side, entry, tp, qty)
    risk_pct = (risk_usdt / balance_usdt * 100) if balance_usdt else 0

    direction = "🟢 LONG" if side == "Buy" else "🔴 SHORT"

    print("\n" + "=" * 70)
    print(action)
    print("=" * 70)
    print(f"Pair      : {symbol}")
    print(f"Direction : {direction}")
    print(f"Entry     : {entry:.6f}")
    print(f"SL        : {sl:.6f}")
    print(f"TP        : {tp:.6f}")
    print(f"Qty       : {qty}")
    print(f"Leverage  : {LEVERAGE}x")
    print(f"Risk      : {risk_usdt:.6f} USDT ({risk_pct:.2f}%)")
    print(f"Est Gross : {gross_tp:.6f} USDT")
    print(f"Est Fees  : {fees_tp:.6f} USDT")
    print(f"Est Net   : {net_tp:.6f} USDT")
    print(f"Balance   : {balance_usdt:.6f} USDT")
    print("=" * 70 + "\n")

def estimate_close_result(state: dict, last_price: float | None) -> tuple[str, float | None, float | None, float | None]:
    symbol = state.get("symbol")
    side = state.get("pos")
    entry = state.get("entry")
    tp = state.get("tp")
    sl = state.get("sl")
    qty = state.get("remaining_qty") or state.get("size")

    if not symbol or not side or entry is None or qty is None or last_price is None:
        return ("Position Closed", None, None, None)

    reason = "Position Closed"

    if tp is not None and sl is not None:
        if side == "Buy":
            reason = "🎯 TP HIT" if abs(last_price - tp) <= abs(last_price - sl) else "❌ SL HIT"
        else:
            reason = "🎯 TP HIT" if abs(last_price - tp) <= abs(last_price - sl) else "❌ SL HIT"

    gross, fees, net = net_pnl_estimate(side, entry, last_price, qty)
    return reason, gross, fees, net

# ============================================================
# POSITION MANAGEMENT
# ============================================================
def manage_open_position(state: dict, actual_pos: dict) -> str:
    symbol = actual_pos["symbol"]
    side = actual_pos["side"]
    mark = float(actual_pos["markPrice"]) if actual_pos["markPrice"] else (get_price(symbol) or actual_pos["entry"])
    entry = float(state.get("entry") or actual_pos["entry"])
    size = float(actual_pos["size"])
    initial_risk = state.get("initial_risk")

    state["symbol"] = symbol
    state["pos"] = side
    state["size"] = size
    state["remaining_qty"] = size
    if state.get("entry") is None:
        state["entry"] = float(actual_pos["entry"])
    save_state(state)

    gross_now, fees_now, net_now = net_pnl_estimate(side, entry, mark, size)
    log("POSITION", f"{symbol} | {side} | entry={entry:.6f} now={mark:.6f} gross={gross_now:+.6f} fees={fees_now:.6f} net={net_now:+.6f}")

    if not initial_risk or initial_risk <= 0:
        return f"Position running on {symbol}"

    current_r = ((mark - entry) / initial_risk) if side == "Buy" else ((entry - mark) / initial_risk)

    if not state.get("partial_taken") and current_r >= PARTIAL_AT_R:
        instrument = get_instrument_info(symbol)
        if instrument:
            partial_qty = floor_to_step(size * PARTIAL_CLOSE_PCT, instrument["qty_step"])
            if partial_qty >= instrument["min_order_qty"] and partial_qty < size:
                r = reduce_position(symbol, side, partial_qty)
                if r.get("retCode") == 0:
                    gross_part, fees_part, net_part = net_pnl_estimate(side, entry, mark, partial_qty)

                    state["partial_taken"] = True
                    state["partial_qty"] = partial_qty
                    state["remaining_qty"] = max(size - partial_qty, instrument["min_order_qty"])
                    save_state(state)

                    send_telegram(
                        f"💰 PARTIAL TP TAKEN\n"
                        f"Pair: {symbol}\n"
                        f"Side: {side}\n"
                        f"Entry: {entry}\n"
                        f"Current Price: {mark}\n"
                        f"Closed Qty: {partial_qty}\n"
                        f"Remaining Qty: {state['remaining_qty']}\n"
                        f"Gross Partial: {gross_part:.6f} USDT\n"
                        f"Est. Fees: {fees_part:.6f} USDT\n"
                        f"Est. Net Partial: {net_part:.6f} USDT\n"
                        f"R Multiple: {current_r:.2f}R"
                    )

                    log("PARTIAL", f"{symbol} -> partial closed qty={partial_qty}")

                    if MOVE_SL_TO_BE and not state.get("breakeven_moved"):
                        be_sl = entry
                        update_trading_stop(symbol, sl=be_sl)
                        state["sl"] = be_sl
                        state["breakeven_moved"] = True
                        save_state(state)

                        send_telegram(
                            f"🛡️ SL MOVED TO BREAKEVEN\n"
                            f"Pair: {symbol}\n"
                            f"Side: {side}\n"
                            f"New SL: {be_sl}"
                        )
                        log("BE", f"{symbol} -> moved SL to BE")

    return f"Position running on {symbol}"

# ============================================================
# CORE
# ============================================================
def run() -> str:
    state = load_state()
    now_ts = time.time()

    actual_pos = find_any_open_position()
    if actual_pos:
        return manage_open_position(state, actual_pos)

    if state.get("pos"):
        symbol = state.get("symbol")
        last_price = get_price(symbol) if symbol else None
        close_reason, gross, fees, net = estimate_close_result(state, last_price)

        partial_text = ""
        if state.get("partial_taken"):
            partial_text = f"\nPartial Closed: Yes\nPartial Qty: {state.get('partial_qty')}"

        gross_text = "N/A" if gross is None else f"{gross:.6f} USDT"
        fees_text = "N/A" if fees is None else f"{fees:.6f} USDT"
        net_text = "N/A" if net is None else f"{net:.6f} USDT"

        send_telegram(
            f"{close_reason}\n"
            f"Pair: {state.get('symbol')}\n"
            f"Side: {state.get('pos')}\n"
            f"Entry: {state.get('entry')}\n"
            f"TP: {state.get('tp')}\n"
            f"SL: {state.get('sl')}\n"
            f"Exit Price: {last_price}\n"
            f"Qty: {state.get('size')}\n"
            f"Gross PnL: {gross_text}\n"
            f"Est. Fees: {fees_text}\n"
            f"Est. Net PnL: {net_text}"
            f"{partial_text}"
        )

        log("CLOSE", f"{close_reason} | {symbol} | gross={gross_text} fees={fees_text} net={net_text}")

        new_state = default_state()
        new_state["last_closed_at"] = now_ts
        save_state(new_state)
        state = new_state

    if state.get("last_closed_at"):
        elapsed = now_ts - float(state["last_closed_at"])
        cooldown_sec = COOLDOWN_MINUTES * 60
        if elapsed < cooldown_sec:
            remaining = int((cooldown_sec - elapsed) / 60)
            return f"Cooldown active ({remaining} min left)"

    balance_usdt = get_balance()
    if balance_usdt is None or balance_usdt <= 0:
        return "No balance"

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
            continue

        qty = build_order_qty(symbol, balance_usdt, price)
        if qty is None or qty <= 0:
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

        if sl <= 0 or tp <= 0:
            continue

        initial_risk = abs(price - sl)
        if initial_risk <= 0:
            continue

        gross_tp, fees_tp, net_tp = net_pnl_estimate(side, price, tp, qty)
        r_multiple = abs(tp - price) / initial_risk if initial_risk > 0 else 0

        if net_tp < MIN_NET_PROFIT_USDT:
            log("SKIP", f"{symbol} skipped: est net TP too small ({net_tp:.6f} USDT)")
            continue

        if r_multiple < MIN_R_MULTIPLE:
            log("SKIP", f"{symbol} skipped: R multiple too small ({r_multiple:.2f})")
            continue

        log(
            "DEBUG",
            f"{symbol} | side={side} | balance={balance_usdt:.4f} | price={price:.6f} | "
            f"qty={qty} | sl={sl} | tp={tp} | gross_tp={gross_tp:.6f} | fees_tp={fees_tp:.6f} | net_tp={net_tp:.6f}"
        )

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
            "remaining_qty": qty,
            "reason": reason,
            "opened": iso_now(),
            "initial_risk": initial_risk,
            "partial_taken": False,
            "partial_qty": None,
            "breakeven_moved": False,
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
            f"Balance: {balance_usdt:.4f} USDT\n"
            f"Est. Gross TP: {gross_tp:.6f} USDT\n"
            f"Est. Fees TP: {fees_tp:.6f} USDT\n"
            f"Est. Net TP: {net_tp:.6f} USDT\n"
            f"Partial Rule: {int(PARTIAL_CLOSE_PCT*100)}% at {PARTIAL_AT_R}R"
        )

        return f"Trade opened on {symbol}"

    return "No setup on any pair"

# ============================================================
# MAIN
# ============================================================
def main() -> None:
    log("BOT", "=" * 78)
    log("BOT", "Bybit Bot | Soft Fee-Aware | Partial TP + BE | 3 Pairs")
    log("BOT", f"Pairs: {', '.join(SYMBOLS)}")
    log("BOT", f"Wallet Usage: {int(POSITION_PCT * 100)}% | Leverage: {LEVERAGE}x | RR: 1:{RR_RATIO}")
    log("BOT", f"Cooldown: {COOLDOWN_MINUTES} min | Volume Multiplier: {VOLUME_MULTIPLIER}")
    log("BOT", f"Taker Fee Rate: {TAKER_FEE_RATE}")
    log("BOT", f"Min Net Profit: {MIN_NET_PROFIT_USDT} USDT | Min R: {MIN_R_MULTIPLE}")
    log("BOT", "=" * 78)

    for symbol in SYMBOLS:
        set_leverage(symbol)
        time.sleep(0.3)

    send_telegram(
        f"🤖 BOT STARTED\n"
        f"Pairs: {', '.join(SYMBOLS)}\n"
        f"Wallet: {int(POSITION_PCT * 100)}%\n"
        f"Leverage: {LEVERAGE}x\n"
        f"Cooldown: {COOLDOWN_MINUTES} min\n"
        f"Volume Multiplier: {VOLUME_MULTIPLIER}\n"
        f"Taker Fee Rate: {TAKER_FEE_RATE}\n"
        f"Min Net Profit: {MIN_NET_PROFIT_USDT} USDT\n"
        f"Min R: {MIN_R_MULTIPLE}\n"
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
