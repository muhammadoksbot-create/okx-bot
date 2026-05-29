import hashlib
import hmac
import json
import math
import os
import time
from datetime import UTC, datetime
from urllib.parse import urlencode

import requests


# ============================================================
# CONFIG
# ============================================================
VERSION = "HIGHER_TF_SWING_V1"

BASE_URL = os.getenv("BYBIT_BASE_URL", "https://api.bybit.com")

# Bybit API keys are kept in config_okx.py, same as the old setup.
# Do NOT hardcode API keys here.
from config_okx import API_KEY, SECRET_KEY

RECV_WINDOW = "5000"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")

STATE_FILE = "state_higher_tf_swing.json"
CATEGORY = "linear"
SYMBOLS = ["DOGEUSDT", "XLMUSDT", "HBARUSDT", "BTCUSDT", "ETHUSDT", "SOLUSDT"]

TREND_INTERVAL = "240"
ENTRY_INTERVAL = "60"

LEVERAGE = 5
POSITION_PCT = 0.10
RR_RATIO = 3.0
ATR_PERIOD = 14
COOLDOWN_SECONDS = 3 * 60 * 60
CHECK_INTERVAL_SECONDS = 15 * 60
HEARTBEAT_INTERVAL_SECONDS = 12 * 60 * 60

EMA_SLOPE_LOOKBACK = 5
PULLBACK_TOLERANCE_ATR = 0.35
SL_ATR_MULT = 1.0

TAKER_FEE_RATE = 0.00055
DAILY_LOSS_STOP_PCT = 0.05
MAX_CONSECUTIVE_LOSSES = 8


# ============================================================
# LOGGING / TELEGRAM
# ============================================================
def log(tag: str, msg: str) -> None:
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{now}] [{tag}] {msg}", flush=True)


def send_telegram(msg: str) -> None:
    if not TELEGRAM_TOKEN or not CHAT_ID:
        log("TG_SKIP", "TELEGRAM_TOKEN or CHAT_ID missing")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        r = requests.post(url, data={"chat_id": CHAT_ID, "text": msg}, timeout=10)
        if r.status_code != 200:
            log("TG_ERR", f"status={r.status_code} response={r.text}")
    except Exception as e:
        log("TG_ERR", str(e))


# ============================================================
# AUTH / HTTP
# ============================================================
def require_api_keys() -> bool:
    if API_KEY and SECRET_KEY:
        return True
    log("KEYS", "API_KEY or SECRET_KEY missing in config_okx.py")
    send_telegram("⚠️ BOT NOT TRADING\nAPI_KEY or SECRET_KEY missing in config_okx.py")
    return False


def _sign(payload: str, timestamp: str) -> str:
    plain = f"{timestamp}{API_KEY}{RECV_WINDOW}{payload}"
    return hmac.new(SECRET_KEY.encode("utf-8"), plain.encode("utf-8"), hashlib.sha256).hexdigest()


def _headers(payload: str, timestamp: str) -> dict:
    return {
        "X-BAPI-API-KEY": API_KEY,
        "X-BAPI-SIGN": _sign(payload, timestamp),
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-RECV-WINDOW": RECV_WINDOW,
        "Content-Type": "application/json",
    }


def req(method: str, path: str, params: dict | None = None, body: dict | None = None, retries: int = 3) -> dict:
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            timestamp = str(int(time.time() * 1000))
            if method.upper() == "GET":
                payload = urlencode(params or {})
                url = f"{BASE_URL}{path}"
                if payload:
                    url += f"?{payload}"
                r = requests.get(url, headers=_headers(payload, timestamp), timeout=15)
            elif method.upper() == "POST":
                payload = json.dumps(body or {}, separators=(",", ":"))
                r = requests.post(f"{BASE_URL}{path}", headers=_headers(payload, timestamp), data=payload, timeout=15)
            else:
                raise ValueError(f"Unsupported method: {method}")

            data = r.json()
            if data.get("retCode") not in (0, None):
                log("API_RET", f"{path} -> retCode={data.get('retCode')} retMsg={data.get('retMsg')}")
            return data
        except Exception as e:
            last_err = e
            log("API_ERR", f"{path} attempt {attempt}/{retries}: {e}")
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
        "side": None,
        "entry": None,
        "sl": None,
        "tp": None,
        "qty": None,
        "opened_at": None,
        "last_closed_at": None,
        "last_heartbeat_at": None,
        "consecutive_losses": 0,
        "paused_after_losses": False,
        "daily_date": None,
        "daily_start_wallet": None,
        "daily_realized_pnl": 0.0,
        "daily_loss_stopped": False,
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
    except Exception as e:
        log("STATE_ERR", f"load failed: {e}")
        return default_state()


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def utc_day() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def reset_daily_if_needed(state: dict, wallet: float | None) -> None:
    today = utc_day()
    if state.get("daily_date") == today:
        return
    state["daily_date"] = today
    state["daily_start_wallet"] = wallet
    state["daily_realized_pnl"] = 0.0
    state["daily_loss_stopped"] = False
    save_state(state)
    log("DAILY", f"new day reset | start_wallet={wallet}")


# ============================================================
# MARKET / ACCOUNT
# ============================================================
def get_candles(symbol: str, interval: str, limit: int = 300) -> list:
    r = req(
        "GET",
        "/v5/market/kline",
        params={"category": CATEGORY, "symbol": symbol, "interval": interval, "limit": limit},
    )
    if r.get("retCode") == 0 and r.get("result", {}).get("list"):
        return list(reversed(r["result"]["list"]))
    return []


def get_price(symbol: str) -> float | None:
    r = req("GET", "/v5/market/tickers", params={"category": CATEGORY, "symbol": symbol})
    try:
        return float(r["result"]["list"][0]["lastPrice"])
    except Exception:
        return None


def get_balance() -> float | None:
    r = req("GET", "/v5/account/wallet-balance", params={"accountType": "UNIFIED", "coin": "USDT"})
    try:
        coin_list = r["result"]["list"][0]["coin"]
        usdt = coin_list[0]
        wallet_balance = float(usdt.get("walletBalance", 0))
        equity = float(usdt.get("equity", wallet_balance))
        log("BALANCE", f"walletBalance={wallet_balance:.6f} equity={equity:.6f}")
        return wallet_balance
    except Exception as e:
        log("BALANCE_ERR", f"{e} | RAW={r}")
        return None


def get_instrument_info(symbol: str) -> dict | None:
    r = req("GET", "/v5/market/instruments-info", params={"category": CATEGORY, "symbol": symbol})
    try:
        info = r["result"]["list"][0]
        return {
            "qty_step": float(info["lotSizeFilter"]["qtyStep"]),
            "min_order_qty": float(info["lotSizeFilter"]["minOrderQty"]),
            "tick_size": float(info["priceFilter"]["tickSize"]),
        }
    except Exception as e:
        log("INSTRUMENT_ERR", f"{symbol}: {e}")
        return None


def get_open_position(symbol: str) -> dict | None:
    r = req("GET", "/v5/position/list", params={"category": CATEGORY, "symbol": symbol})
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
        log("POS_ERR", f"{symbol}: {e}")
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
        log("LEVERAGE", f"{symbol} set to {LEVERAGE}x")
    else:
        log("LEVERAGE", f"{symbol}: {r.get('retMsg', r)}")


# ============================================================
# INDICATORS / HELPERS
# ============================================================
def floor_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    return math.floor(value / step) * step


def round_price(price: float, tick_size: float) -> float:
    if tick_size <= 0:
        return price
    return round(round(price / tick_size) * tick_size, 8)


def ema_series(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2 / (period + 1)
    out = [values[0]]
    for value in values[1:]:
        out.append(value * k + out[-1] * (1 - k))
    return out


def atr(candles: list, period: int = ATR_PERIOD) -> float:
    if len(candles) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        high = float(candles[i][2])
        low = float(candles[i][3])
        prev_close = float(candles[i - 1][4])
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return sum(trs[-period:]) / period if trs else 0.0


def gross_pnl(side: str, entry: float, exit_price: float, qty: float) -> float:
    if side == "Buy":
        return (exit_price - entry) * qty
    return (entry - exit_price) * qty


def estimate_round_trip_fees(entry: float, exit_price: float, qty: float) -> float:
    return entry * qty * TAKER_FEE_RATE + exit_price * qty * TAKER_FEE_RATE


def net_pnl_estimate(side: str, entry: float, exit_price: float, qty: float) -> tuple[float, float, float]:
    gross = gross_pnl(side, entry, exit_price, qty)
    fees = estimate_round_trip_fees(entry, exit_price, qty)
    return gross, fees, gross - fees


def build_order_qty(symbol: str, wallet: float, price: float) -> float | None:
    instrument = get_instrument_info(symbol)
    if not instrument:
        return None
    exposure = wallet * POSITION_PCT * LEVERAGE
    qty = floor_to_step(exposure / price, instrument["qty_step"])
    if qty < instrument["min_order_qty"]:
        qty = instrument["min_order_qty"]
    return round(qty, 8)


# ============================================================
# SIGNAL LOGIC
# ============================================================
def scan_symbol(symbol: str) -> dict | None:
    candles_1h_raw = get_candles(symbol, ENTRY_INTERVAL, 300)
    candles_4h_raw = get_candles(symbol, TREND_INTERVAL, 300)

    if len(candles_1h_raw) < 80 or len(candles_4h_raw) < 220:
        log("SKIP", f"{symbol}: insufficient candles 1h={len(candles_1h_raw)} 4h={len(candles_4h_raw)}")
        return None

    candles_1h = candles_1h_raw[:-1]
    candles_4h = candles_4h_raw[:-1]

    opens = [float(c[1]) for c in candles_1h]
    highs = [float(c[2]) for c in candles_1h]
    lows = [float(c[3]) for c in candles_1h]
    closes = [float(c[4]) for c in candles_1h]
    closes_4h = [float(c[4]) for c in candles_4h]

    ema20 = ema_series(closes, 20)
    ema50 = ema_series(closes, 50)
    ema200_4h = ema_series(closes_4h, 200)
    atr_1h = atr(candles_1h, ATR_PERIOD)

    if atr_1h <= 0:
        log("SKIP", f"{symbol}: ATR unavailable")
        return None

    i = -1
    p = -2
    price = closes[i]

    trend_up = closes_4h[-1] > ema200_4h[-1] and ema200_4h[-1] > ema200_4h[-1 - EMA_SLOPE_LOOKBACK]
    trend_down = closes_4h[-1] < ema200_4h[-1] and ema200_4h[-1] < ema200_4h[-1 - EMA_SLOPE_LOOKBACK]

    tol = atr_1h * PULLBACK_TOLERANCE_ATR
    long_pullback = lows[i] <= max(ema20[i], ema50[i]) + tol and closes[p] <= ema20[p] + tol
    short_pullback = highs[i] >= min(ema20[i], ema50[i]) - tol and closes[p] >= ema20[p] - tol

    bullish = closes[i] > opens[i] and closes[i] > ema20[i]
    bearish = closes[i] < opens[i] and closes[i] < ema20[i]

    log(
        "SETUP",
        f"{symbol} | trend_up={trend_up} trend_down={trend_down} "
        f"long_pullback={long_pullback} short_pullback={short_pullback} "
        f"bullish={bullish} bearish={bearish} price={price:.6f} atr1h={atr_1h:.6f}",
    )

    if trend_up and long_pullback and bullish:
        sl = lows[i] - atr_1h * SL_ATR_MULT
        if sl >= price:
            log("SKIP", f"{symbol}: long SL invalid sl={sl:.6f} price={price:.6f}")
            return None
        tp = price + abs(price - sl) * RR_RATIO
        return {
            "symbol": symbol,
            "side": "Buy",
            "price": price,
            "sl": sl,
            "tp": tp,
            "atr": atr_1h,
            "reason": "4H EMA200 UP + 1H EMA20/50 PULLBACK LONG",
        }

    if trend_down and short_pullback and bearish:
        sl = highs[i] + atr_1h * SL_ATR_MULT
        if sl <= price:
            log("SKIP", f"{symbol}: short SL invalid sl={sl:.6f} price={price:.6f}")
            return None
        tp = price - abs(price - sl) * RR_RATIO
        return {
            "symbol": symbol,
            "side": "Sell",
            "price": price,
            "sl": sl,
            "tp": tp,
            "atr": atr_1h,
            "reason": "4H EMA200 DOWN + 1H EMA20/50 PULLBACK SHORT",
        }

    log("SKIP", f"{symbol}: no valid setup")
    return None


# ============================================================
# ORDERS / ALERTS
# ============================================================
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


def update_state_open(state: dict, setup: dict, qty: float, sl: float, tp: float) -> None:
    state.update(
        {
            "symbol": setup["symbol"],
            "side": setup["side"],
            "entry": setup["price"],
            "sl": sl,
            "tp": tp,
            "qty": qty,
            "opened_at": datetime.now(UTC).isoformat(),
        }
    )
    save_state(state)


def send_open_alert(setup: dict, qty: float, wallet: float, sl: float, tp: float) -> None:
    gross_tp, fees_tp, net_tp = net_pnl_estimate(setup["side"], setup["price"], tp, qty)
    direction = "LONG" if setup["side"] == "Buy" else "SHORT"
    send_telegram(
        f"🚀 HIGHER TF SWING TRADE OPENED\n"
        f"Pair: {setup['symbol']}\n"
        f"Side: {direction}\n"
        f"Reason: {setup['reason']}\n"
        f"Entry: {setup['price']:.8f}\n"
        f"SL: {sl}\n"
        f"TP: {tp}\n"
        f"Qty: {qty}\n"
        f"Leverage: {LEVERAGE}x\n"
        f"Sizing: {POSITION_PCT*100:.0f}% wallet\n"
        f"Wallet: {wallet:.4f} USDT\n"
        f"Est Gross TP: {gross_tp:.6f} USDT\n"
        f"Est Fees TP: {fees_tp:.6f} USDT\n"
        f"Est Net TP: {net_tp:.6f} USDT"
    )


def estimate_close_from_state(state: dict, last_price: float | None) -> tuple[str, float, float, float]:
    if last_price is None:
        return "Position Closed", 0.0, 0.0, 0.0
    side = state.get("side")
    entry = float(state.get("entry") or 0)
    qty = float(state.get("qty") or 0)
    tp = state.get("tp")
    sl = state.get("sl")
    reason = "Position Closed"
    if tp is not None and sl is not None:
        reason = "TP HIT" if abs(last_price - float(tp)) <= abs(last_price - float(sl)) else "SL HIT"
    gross, fees, net = net_pnl_estimate(side, entry, last_price, qty)
    return reason, gross, fees, net


def handle_closed_position(state: dict, wallet: float | None) -> None:
    symbol = state.get("symbol")
    last_price = get_price(symbol) if symbol else None
    reason, gross, fees, net = estimate_close_from_state(state, last_price)

    new_state = default_state()
    new_state["last_heartbeat_at"] = state.get("last_heartbeat_at")
    new_state["last_closed_at"] = time.time()
    new_state["daily_date"] = state.get("daily_date")
    new_state["daily_start_wallet"] = state.get("daily_start_wallet")
    new_state["daily_realized_pnl"] = float(state.get("daily_realized_pnl") or 0.0) + net
    new_state["daily_loss_stopped"] = bool(state.get("daily_loss_stopped"))

    if net < 0:
        new_state["consecutive_losses"] = int(state.get("consecutive_losses") or 0) + 1
    else:
        new_state["consecutive_losses"] = 0

    daily_start = new_state.get("daily_start_wallet") or wallet
    if daily_start and new_state["daily_realized_pnl"] <= -(float(daily_start) * DAILY_LOSS_STOP_PCT):
        new_state["daily_loss_stopped"] = True

    if new_state["consecutive_losses"] >= MAX_CONSECUTIVE_LOSSES:
        new_state["paused_after_losses"] = True

    save_state(new_state)

    send_telegram(
        f"✅ HIGHER TF SWING POSITION CLOSED\n"
        f"Reason: {reason}\n"
        f"Pair: {symbol}\n"
        f"Side: {state.get('side')}\n"
        f"Entry: {state.get('entry')}\n"
        f"Exit Price: {last_price}\n"
        f"Qty: {state.get('qty')}\n"
        f"Gross PnL: {gross:.6f} USDT\n"
        f"Fees Est: {fees:.6f} USDT\n"
        f"Net PnL Est: {net:.6f} USDT\n"
        f"Daily Realized: {new_state['daily_realized_pnl']:.6f} USDT\n"
        f"Consecutive Losses: {new_state['consecutive_losses']}"
    )
    log("CLOSE", f"{symbol} {reason} gross={gross:.6f} fees={fees:.6f} net={net:.6f}")


def maybe_send_heartbeat(state: dict, wallet: float | None, actual_pos: dict | None, status: str) -> None:
    now_ts = time.time()
    last = state.get("last_heartbeat_at")
    try:
        if last is not None and now_ts - float(last) < HEARTBEAT_INTERVAL_SECONDS:
            return
    except Exception:
        pass

    wallet_text = "N/A" if wallet is None else f"{wallet:.4f} USDT"
    if actual_pos:
        pos_text = f"{actual_pos['symbol']} {actual_pos['side']} size={actual_pos['size']} entry={actual_pos['entry']}"
    else:
        pos_text = "No open position"

    send_telegram(
        f"✅ HIGHER TF SWING HEARTBEAT\n"
        f"Version: {VERSION}\n"
        f"Status: {status}\n"
        f"Pairs: {', '.join(SYMBOLS)}\n"
        f"Wallet: {wallet_text}\n"
        f"Position: {pos_text}\n"
        f"Daily Realized: {float(state.get('daily_realized_pnl') or 0.0):.6f} USDT\n"
        f"Consecutive Losses: {state.get('consecutive_losses')}\n"
        f"Time: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}"
    )
    state["last_heartbeat_at"] = now_ts
    save_state(state)


# ============================================================
# CORE
# ============================================================
def trading_allowed(state: dict) -> tuple[bool, str]:
    if state.get("paused_after_losses"):
        return False, "paused after max losing streak until manual state reset"
    if state.get("daily_loss_stopped"):
        return False, "daily loss stop active"
    if state.get("last_closed_at"):
        elapsed = time.time() - float(state["last_closed_at"])
        if elapsed < COOLDOWN_SECONDS:
            remaining = int((COOLDOWN_SECONDS - elapsed) / 60)
            return False, f"cooldown active ({remaining} min left)"
    return True, "allowed"


def sync_state_with_actual_position(state: dict, actual_pos: dict | None, wallet: float | None) -> dict:
    if actual_pos:
        state["symbol"] = actual_pos["symbol"]
        state["side"] = actual_pos["side"]
        state["qty"] = actual_pos["size"]
        if state.get("entry") is None:
            state["entry"] = actual_pos["entry"]
        save_state(state)
        return state

    if state.get("symbol") and state.get("side"):
        handle_closed_position(state, wallet)
        return load_state()

    return state


def run() -> str:
    if not require_api_keys():
        return "Missing API keys"

    state = load_state()
    wallet = get_balance()
    reset_daily_if_needed(state, wallet)

    actual_pos = find_any_open_position()
    state = sync_state_with_actual_position(state, actual_pos, wallet)

    if actual_pos:
        maybe_send_heartbeat(state, wallet, actual_pos, "Managing open position")
        log("POSITION", f"{actual_pos['symbol']} {actual_pos['side']} size={actual_pos['size']} entry={actual_pos['entry']}")
        return f"Position running on {actual_pos['symbol']}"

    maybe_send_heartbeat(state, wallet, None, "Scanning")

    allowed, reason = trading_allowed(state)
    if not allowed:
        log("GUARD", reason)
        return reason

    if wallet is None or wallet <= 0:
        return "No wallet balance"

    for symbol in SYMBOLS:
        setup = scan_symbol(symbol)
        if not setup:
            continue

        instrument = get_instrument_info(symbol)
        if not instrument:
            log("SKIP", f"{symbol}: instrument info unavailable")
            continue

        sl = round_price(setup["sl"], instrument["tick_size"])
        tp = round_price(setup["tp"], instrument["tick_size"])
        if sl <= 0 or tp <= 0:
            log("SKIP", f"{symbol}: invalid rounded SL/TP sl={sl} tp={tp}")
            continue

        qty = build_order_qty(symbol, wallet, setup["price"])
        if qty is None or qty <= 0:
            log("SKIP", f"{symbol}: invalid qty {qty}")
            continue

        gross_tp, fees_tp, net_tp = net_pnl_estimate(setup["side"], setup["price"], tp, qty)
        log(
            "TRADE_CHECK",
            f"{symbol} side={setup['side']} entry={setup['price']:.8f} sl={sl} tp={tp} "
            f"qty={qty} gross_tp={gross_tp:.6f} fees_tp={fees_tp:.6f} net_tp={net_tp:.6f}",
        )

        order = place_order(symbol, setup["side"], qty, tp, sl)
        if order.get("retCode") != 0:
            log("ORDER_FAIL", f"{symbol}: {order}")
            send_telegram(f"❌ HIGHER TF SWING ORDER FAILED\n{symbol}\n{order}")
            continue

        update_state_open(state, setup, qty, sl, tp)
        send_open_alert(setup, qty, wallet, sl, tp)
        return f"Trade opened on {symbol}"

    return "No setup"


def main() -> None:
    log("VERSION", VERSION)
    log("BOT", "=" * 88)
    log("BOT", "HIGHER TF SWING | 4H EMA200 TREND + 1H EMA20/50 PULLBACK | BOTH SIDES")
    log("BOT", f"Pairs: {', '.join(SYMBOLS)}")
    log("BOT", f"Leverage: {LEVERAGE}x | Position sizing: {POSITION_PCT*100:.0f}% wallet | RR: {RR_RATIO}")
    log("BOT", f"Cooldown: {int(COOLDOWN_SECONDS/3600)}h | Check interval: {int(CHECK_INTERVAL_SECONDS/60)}m")
    log("BOT", f"Daily loss stop: {DAILY_LOSS_STOP_PCT*100:.1f}% | Max consecutive losses: {MAX_CONSECUTIVE_LOSSES}")
    log("BOT", "=" * 88)

    if not require_api_keys():
        return

    for symbol in SYMBOLS:
        set_leverage(symbol)
        time.sleep(0.5)

    state = load_state()
    state["last_heartbeat_at"] = time.time()
    save_state(state)

    send_telegram(
        f"🤖 HIGHER TF SWING BOT STARTED\n"
        f"Version: {VERSION}\n"
        f"Pairs: {', '.join(SYMBOLS)}\n"
        f"Strategy: 4H EMA200 trend + 1H EMA20/50 pullback\n"
        f"Sides: Long and Short\n"
        f"Leverage: {LEVERAGE}x\n"
        f"Sizing: {POSITION_PCT*100:.0f}% actual wallet\n"
        f"RR: {RR_RATIO}\n"
        f"Cooldown: {int(COOLDOWN_SECONDS/3600)}h\n"
        f"Daily loss stop: {DAILY_LOSS_STOP_PCT*100:.1f}%\n"
        f"Max losing streak stop: {MAX_CONSECUTIVE_LOSSES}\n"
        f"Mode: one open trade only"
    )

    while True:
        try:
            result = run()
            log("RUN", result)
            time.sleep(CHECK_INTERVAL_SECONDS)
        except Exception as e:
            log("ERROR", str(e))
            send_telegram(f"⚠️ HIGHER TF SWING BOT ERROR\n{e}")
            time.sleep(30)


if __name__ == "__main__":
    main()
