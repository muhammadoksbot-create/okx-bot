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
VERSION = "DOGE_V3_PULLBACK_RECLAIM_01"

BASE_URL = os.getenv("BYBIT_BASE_URL", "https://api.bybit.com")
STATE_FILE = "state_bybit_v3_reclaim.json"

SYMBOLS = ["DOGEUSDT"]
CATEGORY = "linear"

TREND_INTERVAL = "60"   # 1h trend
ENTRY_INTERVAL = "5"    # 5m entry

LEVERAGE = 15
POSITION_PCT = 0.10
RECV_WINDOW = "5000"

ATR_PERIOD = 14
RR_RATIO = 1.50

CHECK_INTERVAL_SECONDS = 15
COOLDOWN_MINUTES = 15

BE_TRIGGER_R = 0.50
PARTIAL_AT_R = 1.00
PARTIAL_CLOSE_PCT = 0.50

TAKER_FEE_RATE = 0.00055
MIN_NET_PROFIT_USDT = 0.003
MIN_R_MULTIPLE = 1.0

# same-zone re-entry block
REENTRY_BLOCK_MINUTES = 120
REENTRY_ZONE_ATR_MULT = 0.75

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
# HTTP WITH RETRY
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
        "last_closed_symbol": None,
        "last_closed_side": None,
        "last_closed_price": None,
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
        params={"category": CATEGORY, "symbol": symbol},
    )
    try:
        return float(r["result"]["list"][0]["lastPrice"])
    except Exception:
        return None


def get_balance() -> float | None:
    r = req(
        "GET",
        "/v5/account/wallet-balance",
        params={"accountType": "UNIFIED", "coin": "USDT"},
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
        params={"category": CATEGORY, "symbol": symbol},
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
        params={"category": CATEGORY, "symbol": symbol},
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


def iso_now() -> str:
    return datetime.now(UTC).isoformat()


def ema_series(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


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


def atr(candles: list, period: int = 14) -> float:
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


# ============================================================
# SIGNAL LOGIC
# ============================================================
def scan_symbol(symbol: str) -> dict | None:
    candles_5m_raw = get_candles(symbol, ENTRY_INTERVAL, 300)
    candles_1h_raw = get_candles(symbol, TREND_INTERVAL, 300)

    if len(candles_5m_raw) < 80 or len(candles_1h_raw) < 220:
        log("SCAN", f"{symbol} -> insufficient candles")
        return None

    # exclude current forming candles
    candles_5m = candles_5m_raw[:-1]
    candles_1h = candles_1h_raw[:-1]

    closes_5m = [float(c[4]) for c in candles_5m]
    highs_5m = [float(c[2]) for c in candles_5m]
    lows_5m = [float(c[3]) for c in candles_5m]
    opens_5m = [float(c[1]) for c in candles_5m]

    closes_1h = [float(c[4]) for c in candles_1h]

    price = closes_5m[-1]

    # 1h trend filter
    ema200_1h = ema_series(closes_1h, 200)
    trend_up = closes_1h[-1] > ema200_1h[-1]
    trend_down = closes_1h[-1] < ema200_1h[-1]

    # 5m structure
    ema20_5m = ema_series(closes_5m, 20)
    ema50_5m = ema_series(closes_5m, 50)

    i = -1
    p = -2

    ema20_now = ema20_5m[i]
    ema20_prev = ema20_5m[p]
    ema50_now = ema50_5m[i]
    ema50_prev = ema50_5m[p]

    bull_micro = ema20_now > ema50_now
    bear_micro = ema20_now < ema50_now

    # previous candle pullback into EMA20 zone
    pullback_long = lows_5m[p] <= ema20_prev
    pullback_short = highs_5m[p] >= ema20_prev

    # reclaim candle on last closed candle
    bullish_reclaim = (
        closes_5m[i] > opens_5m[i]
        and closes_5m[i] > ema20_now
        and closes_5m[i] > highs_5m[p]
    )

    bearish_reclaim = (
        closes_5m[i] < opens_5m[i]
        and closes_5m[i] < ema20_now
        and closes_5m[i] < lows_5m[p]
    )

    signal_atr = atr(candles_5m, ATR_PERIOD)

    log(
        "SETUP",
        f"{symbol} | trend_up={trend_up} trend_down={trend_down} | "
        f"bull_micro={bull_micro} bear_micro={bear_micro} | "
        f"pb_long={pullback_long} pb_short={pullback_short} | "
        f"bull_reclaim={bullish_reclaim} bear_reclaim={bearish_reclaim} | "
        f"ema20={ema20_now:.6f} ema50={ema50_now:.6f} atr={signal_atr:.6f}"
    )

    side = None
    reason = None
    sl = None
    tp = None

    if trend_up and bull_micro and pullback_long and bullish_reclaim:
        side = "Buy"
        reason = "1H EMA200 UP + 5M PULLBACK RECLAIM LONG"
        recent_low = min(lows_5m[-6:])
        sl = recent_low - (signal_atr * 0.20)
        if sl >= price:
            return None
        tp = price + (abs(price - sl) * RR_RATIO)

    elif trend_down and bear_micro and pullback_short and bearish_reclaim:
        side = "Sell"
        reason = "1H EMA200 DOWN + 5M PULLBACK RECLAIM SHORT"
        recent_high = max(highs_5m[-6:])
        sl = recent_high + (signal_atr * 0.20)
        if sl <= price:
            return None
        tp = price - (abs(price - sl) * RR_RATIO)

    if not side:
        return None

    return {
        "symbol": symbol,
        "side": side,
        "reason": reason,
        "price": price,
        "sl": sl,
        "tp": tp,
        "atr": signal_atr,
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

    print("\n" + "=" * 72)
    print(action)
    print("=" * 72)
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
    print("=" * 72 + "\n")


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

    last_price = get_price(symbol)
    mark_price = float(actual_pos["markPrice"]) if actual_pos["markPrice"] else None
    current_price = last_price if last_price is not None else (mark_price if mark_price is not None else actual_pos["entry"])

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

    gross_now, fees_now, net_now = net_pnl_estimate(side, entry, current_price, size)
    log(
        "POSITION",
        f"{symbol} | {side} | entry={entry:.6f} current={current_price:.6f} "
        f"gross={gross_now:+.6f} fees={fees_now:.6f} net={net_now:+.6f}"
    )

    if not initial_risk or initial_risk <= 0:
        return f"Position running on {symbol}"

    current_r = ((current_price - entry) / initial_risk) if side == "Buy" else ((entry - current_price) / initial_risk)

    # BE at 0.5R
    if not state.get("breakeven_moved") and current_r >= BE_TRIGGER_R:
        be_sl = entry
        r = update_trading_stop(symbol, sl=be_sl)
        if r.get("retCode") == 0:
            state["sl"] = be_sl
            state["breakeven_moved"] = True
            save_state(state)

            send_telegram(
                f"🛡️ SL MOVED TO BREAKEVEN\n"
                f"Pair: {symbol}\n"
                f"Side: {side}\n"
                f"New SL: {be_sl}\n"
                f"Triggered At: {current_r:.2f}R"
            )
            log("BE", f"{symbol} -> moved SL to BE at {current_r:.2f}R")

    # partial at 1R
    if not state.get("partial_taken") and current_r >= PARTIAL_AT_R:
        instrument = get_instrument_info(symbol)
        if instrument:
            partial_qty = floor_to_step(size * PARTIAL_CLOSE_PCT, instrument["qty_step"])
            if partial_qty >= instrument["min_order_qty"] and partial_qty < size:
                r = reduce_position(symbol, side, partial_qty)
                if r.get("retCode") == 0:
                    gross_part, fees_part, net_part = net_pnl_estimate(side, entry, current_price, partial_qty)

                    state["partial_taken"] = True
                    state["partial_qty"] = partial_qty
                    state["remaining_qty"] = max(size - partial_qty, instrument["min_order_qty"])
                    save_state(state)

                    send_telegram(
                        f"💰 PARTIAL TP TAKEN\n"
                        f"Pair: {symbol}\n"
                        f"Side: {side}\n"
                        f"Entry: {entry}\n"
                        f"Current Price: {current_price}\n"
                        f"Closed Qty: {partial_qty}\n"
                        f"Remaining Qty: {state['remaining_qty']}\n"
                        f"Gross Partial: {gross_part:.6f} USDT\n"
                        f"Est. Fees: {fees_part:.6f} USDT\n"
                        f"Est. Net Partial: {net_part:.6f} USDT\n"
                        f"Triggered At: {current_r:.2f}R"
                    )

                    log("PARTIAL", f"{symbol} -> partial closed qty={partial_qty} at {current_r:.2f}R")

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

    # position just closed
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
        new_state["last_closed_symbol"] = state.get("symbol")
        new_state["last_closed_side"] = state.get("pos")
        new_state["last_closed_price"] = last_price
        save_state(new_state)
        state = new_state

    # cooldown after any close
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
        signal_atr = setup["atr"]

        instrument = get_instrument_info(symbol)
        if not instrument:
            continue

        sl = round_price(setup["sl"], instrument["tick_size"])
        tp = round_price(setup["tp"], instrument["tick_size"])

        if sl <= 0 or tp <= 0:
            continue

        initial_risk = abs(price - sl)
        if initial_risk <= 0:
            continue

        qty = build_order_qty(symbol, balance_usdt, price)
        if qty is None or qty <= 0:
            continue

        # same-zone re-entry block
        if (
            state.get("last_closed_symbol") == symbol
            and state.get("last_closed_side") == side
            and state.get("last_closed_price") is not None
            and state.get("last_closed_at") is not None
        ):
            elapsed_close = now_ts - float(state["last_closed_at"])
            if elapsed_close <= REENTRY_BLOCK_MINUTES * 60:
                if abs(price - float(state["last_closed_price"])) <= signal_atr * REENTRY_ZONE_ATR_MULT:
                    log(
                        "REENTRY_SKIP",
                        f"{symbol} {side} skipped near same zone | "
                        f"price={price:.6f} last_close={float(state['last_closed_price']):.6f} atr={signal_atr:.6f}"
                    )
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
            f"BE: {BE_TRIGGER_R}R | Partial: {int(PARTIAL_CLOSE_PCT*100)}% at {PARTIAL_AT_R}R"
        )

        return f"Trade opened on {symbol}"

    return "No setup"


# ============================================================
# MAIN
# ============================================================
def main() -> None:
    log("VERSION", VERSION)
    log("CONFIG", f"ATR_PERIOD={ATR_PERIOD}")
    log("BOT", "=" * 86)
    log("BOT", "DOGE V3 | 1H EMA200 TREND + 5M PULLBACK RECLAIM | SIMPLE MODE")
    log("BOT", f"Base URL: {BASE_URL}")
    log("BOT", f"Pairs: {', '.join(SYMBOLS)}")
    log("BOT", f"Trend TF: {TREND_INTERVAL}m | Entry TF: {ENTRY_INTERVAL}m")
    log("BOT", f"Wallet Usage: {int(POSITION_PCT * 100)}% | Leverage: {LEVERAGE}x | RR: 1:{RR_RATIO}")
    log("BOT", f"Cooldown: {COOLDOWN_MINUTES} min | Check Interval: {CHECK_INTERVAL_SECONDS}s")
    log("BOT", f"BE Trigger: {BE_TRIGGER_R}R | Partial: {int(PARTIAL_CLOSE_PCT*100)}% at {PARTIAL_AT_R}R")
    log("BOT", f"Reentry Block: {REENTRY_BLOCK_MINUTES} min | Zone ATR Mult: {REENTRY_ZONE_ATR_MULT}")
    log("BOT", "=" * 86)

    for symbol in SYMBOLS:
        set_leverage(symbol)
        time.sleep(0.3)

    send_telegram(
        f"🤖 DOGE V3 BOT STARTED\n"
        f"Version: {VERSION}\n"
        f"Pairs: {', '.join(SYMBOLS)}\n"
        f"Trend TF: {TREND_INTERVAL}m | Entry TF: {ENTRY_INTERVAL}m\n"
        f"Wallet: {int(POSITION_PCT * 100)}%\n"
        f"Leverage: {LEVERAGE}x\n"
        f"Cooldown: {COOLDOWN_MINUTES} min\n"
        f"Check Interval: {CHECK_INTERVAL_SECONDS}s\n"
        f"BE Trigger: {BE_TRIGGER_R}R\n"
        f"Partial: {int(PARTIAL_CLOSE_PCT*100)}% at {PARTIAL_AT_R}R\n"
        f"ATR_PERIOD: {ATR_PERIOD}\n"
        f"Mode: 1 active trade only"
    )

    while True:
        try:
            result = run()
            log("RUN", result)
            time.sleep(CHECK_INTERVAL_SECONDS)
        except Exception as e:
            log("ERROR", str(e))
            send_telegram(f"⚠️ BOT ERROR\n{e}")
            time.sleep(10)


if __name__ == "__main__":
    main()
