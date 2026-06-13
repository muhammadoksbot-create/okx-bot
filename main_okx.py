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
VERSION = "SMC_BOS_RETEST_V1"

BASE_URL = os.getenv("BYBIT_BASE_URL", "https://api.bybit.com")
STATE_FILE = "state_smc_bos_retest.json"

SYMBOLS = ["DOGEUSDT", "XLMUSDT", "HBARUSDT", "BTCUSDT", "ETHUSDT", "AAVEUSDT"]
CATEGORY = "linear"

ENTRY_INTERVAL = "60"
TREND_INTERVAL = "240"

LEVERAGE = 5
POSITION_PCT = 0.15
RECV_WINDOW = "5000"

CHECK_INTERVAL_SECONDS = 15 * 60
COOLDOWN_HOURS = 4
HEARTBEAT_INTERVAL_SECONDS = 12 * 60 * 60

ATR_PERIOD = 14
RR_RATIO = 2.5
SWING_LEN = 3
RETEST_TOLERANCE_ATR = 0.75
SL_ATR_BUFFER_MULT = 0.25
EMA_TREND_PERIOD = 200
EMA_SLOPE_LOOKBACK = 5
VOLUME_SMA_PERIOD = 20
MIN_ATR_PCT = 0.003

TAKER_FEE_RATE = 0.00055
MAX_SL_RISK_PCT = 0.03
DAILY_LOSS_STOP_PCT = 0.08
LOSS_WARNING_THRESHOLD = 5

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")

SEEDED_PAIR_STATS = {
    "BTCUSDT": {"trades": 4, "tp": 2, "sl": 2, "net": -0.866242},
    "XLMUSDT": {"trades": 5, "tp": 1, "sl": 4, "net": 0.114879},
    "ETHUSDT": {"trades": 2, "tp": 1, "sl": 1, "net": 0.535746},
    "DOGEUSDT": {"trades": 2, "tp": 1, "sl": 1, "net": 0.122090},
    "HBARUSDT": {"trades": 1, "tp": 1, "sl": 0, "net": 0.445959},
    "SOLUSDT": {"trades": 1, "tp": 0, "sl": 1, "net": -0.126064},
    "AAVEUSDT": {"trades": 0, "tp": 0, "sl": 0, "net": 0.0},
}


# ============================================================
# LOGGING
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
        else:
            log("TG_OK", "Telegram message sent")

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
        hashlib.sha256,
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

    send_telegram(f"API ERROR\n{path}\n{last_err}")
    return {}


# ============================================================
# STATE
# ============================================================
def seeded_pair_stats() -> dict:
    return {symbol: stats.copy() for symbol, stats in SEEDED_PAIR_STATS.items()}


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
        "last_close_reason": None,
        "last_heartbeat_at": None,
        "daily_date": None,
        "daily_start_wallet": None,
        "daily_realized_pnl": 0.0,
        "daily_loss_stop_active": False,
        "total_closed_trades": 14,
        "total_tp_hits": 6,
        "total_sl_hits": 8,
        "total_net_pnl": 0.2264,
        "current_consecutive_losses": 5,
        "max_consecutive_losses_seen": 5,
        "latest_balance": 26.1750,
        "loss_pause_until": None,
        "loss_pause_applied_at_streak": None,
        "loss_warning_sent_at_streak": None,
        "pair_stats": seeded_pair_stats(),
    }


def migrate_state(state: dict) -> dict:
    migrated = default_state()
    migrated.update(state)

    migrated["total_closed_trades"] = int(migrated.get("total_closed_trades", 14) or 14)
    migrated["total_tp_hits"] = int(migrated.get("total_tp_hits", 6) or 6)
    migrated["total_sl_hits"] = int(migrated.get("total_sl_hits", 8) or 8)
    migrated["total_net_pnl"] = float(migrated.get("total_net_pnl", 0.2264) or 0.2264)
    migrated["latest_balance"] = float(migrated.get("latest_balance", 26.1750) or 26.1750)

    if "current_consecutive_losses" not in state:
        migrated["current_consecutive_losses"] = int(state.get("consecutive_losses", 5) or 5)
    else:
        migrated["current_consecutive_losses"] = int(migrated.get("current_consecutive_losses") or 0)

    migrated["max_consecutive_losses_seen"] = max(
        int(migrated.get("max_consecutive_losses_seen", 5) or 5),
        int(migrated.get("current_consecutive_losses", 0) or 0),
    )
    migrated["loss_pause_until"] = None
    migrated["loss_pause_applied_at_streak"] = None
    warning_at = migrated.get("loss_warning_sent_at_streak")
    migrated["loss_warning_sent_at_streak"] = None if warning_at is None else int(warning_at)

    pair_stats = migrated.get("pair_stats")
    if not isinstance(pair_stats, dict):
        pair_stats = {}

    for symbol, seeded in SEEDED_PAIR_STATS.items():
        existing = pair_stats.get(symbol)
        if not isinstance(existing, dict):
            pair_stats[symbol] = seeded.copy()
            continue

        for key, value in seeded.items():
            existing.setdefault(key, value)

        existing["trades"] = int(existing.get("trades") or 0)
        existing["tp"] = int(existing.get("tp") or 0)
        existing["sl"] = int(existing.get("sl") or 0)
        existing["net"] = float(existing.get("net") or 0.0)

    migrated["pair_stats"] = pair_stats

    return migrated


def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return default_state()

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return migrate_state(data)
    except Exception:
        return default_state()


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def iso_now() -> str:
    return datetime.now(UTC).isoformat()


def today_key() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def ensure_daily_state(state: dict, wallet_balance: float | None) -> None:
    today = today_key()

    if state.get("daily_date") != today:
        state["daily_date"] = today
        state["daily_start_wallet"] = wallet_balance
        state["daily_realized_pnl"] = 0.0
        state["daily_loss_stop_active"] = False
        save_state(state)
        return

    if state.get("daily_start_wallet") is None and wallet_balance is not None:
        state["daily_start_wallet"] = wallet_balance
        save_state(state)


def daily_loss_limit_hit(state: dict) -> bool:
    if state.get("daily_loss_stop_active"):
        return True

    start_wallet = state.get("daily_start_wallet")
    realized = float(state.get("daily_realized_pnl") or 0.0)

    if not start_wallet or start_wallet <= 0:
        return False

    if realized <= -(float(start_wallet) * DAILY_LOSS_STOP_PCT):
        state["daily_loss_stop_active"] = True
        save_state(state)
        send_telegram(
            f"DAILY LOSS STOP ACTIVE\n"
            f"Version: {VERSION}\n"
            f"Daily Start Wallet: {float(start_wallet):.4f} USDT\n"
            f"Realized Today: {realized:.4f} USDT\n"
            f"Limit: -{DAILY_LOSS_STOP_PCT * 100:.1f}%\n\n"
            f"{bot_record_text(state)}"
        )
        return True

    return False


def send_loss_streak_warning_if_needed(state: dict) -> None:
    current_losses = int(state.get("current_consecutive_losses") or 0)

    if current_losses < LOSS_WARNING_THRESHOLD:
        return

    warning_at = state.get("loss_warning_sent_at_streak")
    if warning_at is not None:
        try:
            if int(warning_at) == current_losses:
                return
        except Exception:
            pass

    state["loss_warning_sent_at_streak"] = current_losses
    save_state(state)

    send_telegram(
        f"LOSS STREAK WARNING\n"
        f"Consecutive Losses: {current_losses}\n"
        f"Bot is NOT paused\n"
        f"Trading will continue only if risk <= {MAX_SL_RISK_PCT * 100:.0f}%\n\n"
        f"{bot_record_text(state)}"
    )


def format_pause_until(state: dict) -> str:
    pause_until = state.get("loss_pause_until")
    if pause_until is None:
        return "N/A"

    try:
        return datetime.fromtimestamp(float(pause_until), UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return "N/A"


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
        lot_filter = info["lotSizeFilter"]
        price_filter = info["priceFilter"]
        min_notional = (
            lot_filter.get("minNotionalValue")
            or lot_filter.get("minOrderAmt")
            or lot_filter.get("minOrderValue")
            or 0
        )
        return {
            "qty_step": float(lot_filter["qtyStep"]),
            "min_order_qty": float(lot_filter["minOrderQty"]),
            "min_notional": float(min_notional or 0),
            "tick_size": float(price_filter["tickSize"]),
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
        log("LEVERAGE", f"Set {LEVERAGE}x on {symbol}")
    else:
        log("LEVERAGE", f"{symbol} -> {r.get('retMsg', r)}")


# ============================================================
# INDICATORS / HELPERS
# ============================================================
def floor_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    return math.floor(value / step) * step


def ceil_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    return math.ceil(value / step) * step


def round_price(price: float, tick_size: float) -> float:
    if tick_size <= 0:
        return price
    return round(round(price / tick_size) * tick_size, 8)


def ema_series(values: list[float], period: int) -> list[float]:
    if not values:
        return []

    k = 2 / (period + 1)
    out = [values[0]]

    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))

    return out


def sma(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


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


def gross_pnl(side: str, entry: float, exit_price: float, qty: float) -> float:
    if side == "Buy":
        return (exit_price - entry) * qty
    return (entry - exit_price) * qty


def estimate_round_trip_fees(entry: float, exit_price: float, qty: float) -> float:
    return (entry * qty * TAKER_FEE_RATE) + (exit_price * qty * TAKER_FEE_RATE)


def net_pnl_estimate(side: str, entry: float, exit_price: float, qty: float) -> tuple[float, float, float]:
    gross = gross_pnl(side, entry, exit_price, qty)
    fees = estimate_round_trip_fees(entry, exit_price, qty)
    net = gross - fees
    return gross, fees, net


def estimated_sl_loss(side: str, entry: float, sl: float, qty: float) -> float:
    gross, fees, net = net_pnl_estimate(side, entry, sl, qty)
    return abs(net) if net < 0 else fees


def get_last_confirmed_swing(highs: list[float], lows: list[float], length: int) -> tuple[float | None, float | None]:
    last_swing_high = None
    last_swing_low = None

    for pivot in range(length, len(highs) - length):
        high_window = highs[pivot - length:pivot + length + 1]
        low_window = lows[pivot - length:pivot + length + 1]

        if highs[pivot] == max(high_window):
            last_swing_high = highs[pivot]

        if lows[pivot] == min(low_window):
            last_swing_low = lows[pivot]

    return last_swing_high, last_swing_low


def symbol_4h_trend(symbol: str) -> tuple[bool, bool, str]:
    candles_raw = get_candles(symbol, TREND_INTERVAL, 300)

    if len(candles_raw) < EMA_TREND_PERIOD + EMA_SLOPE_LOOKBACK + 5:
        return False, False, "insufficient 4H candles"

    candles_4h = candles_raw[:-1]
    closes = [float(c[4]) for c in candles_4h]
    ema200 = ema_series(closes, EMA_TREND_PERIOD)

    if len(ema200) < EMA_SLOPE_LOOKBACK + 1:
        return False, False, "insufficient EMA200 data"

    close = closes[-1]
    ema_now = ema200[-1]
    ema_prev = ema200[-1 - EMA_SLOPE_LOOKBACK]

    trend_up = close > ema_now and ema_now > ema_prev
    trend_down = close < ema_now and ema_now < ema_prev

    detail = (
        f"4H close={close:.6f} ema200={ema_now:.6f} "
        f"ema200_{EMA_SLOPE_LOOKBACK}_ago={ema_prev:.6f}"
    )

    return trend_up, trend_down, detail


# ============================================================
# BOT RECORD / TELEGRAM REPORTING
# ============================================================
def win_rate(total: int, tp_hits: int) -> float:
    return (tp_hits / total * 100) if total > 0 else 0.0


def bot_record_text(state: dict) -> str:
    closed = int(state.get("total_closed_trades") or 0)
    tp_hits = int(state.get("total_tp_hits") or 0)
    sl_hits = int(state.get("total_sl_hits") or 0)
    loss_streak = int(state.get("current_consecutive_losses") or 0)

    lines = [
        "BOT RECORD",
        f"Closed: {closed}",
        f"TP: {tp_hits}",
        f"SL: {sl_hits}",
        f"Win Rate: {win_rate(closed, tp_hits):.2f}%",
        f"Total Net PnL: {float(state.get('total_net_pnl') or 0.0):.6f} USDT",
        f"Loss Streak: {loss_streak}",
        f"Max Loss Streak: {int(state.get('max_consecutive_losses_seen') or 0)}",
        f"Daily Stop: {state.get('daily_loss_stop_active')}",
        f"Risk Filter: {MAX_SL_RISK_PCT * 100:.0f}%",
        f"Loss Pause: Disabled",
        f"Active Pairs: {', '.join(SYMBOLS)}",
    ]

    return "\n".join(lines)


def pair_record_text(state: dict, symbol: str) -> str:
    pair_stats = state.get("pair_stats") or {}
    stats = pair_stats.get(symbol, {"trades": 0, "tp": 0, "sl": 0, "net": 0.0})
    trades = int(stats.get("trades") or 0)
    tp_hits = int(stats.get("tp") or 0)
    sl_hits = int(stats.get("sl") or 0)

    return (
        f"Pair Trades: {trades}\n"
        f"Pair TP: {tp_hits}\n"
        f"Pair SL: {sl_hits}\n"
        f"Pair Win Rate: {win_rate(trades, tp_hits):.2f}%\n"
        f"Pair Net PnL: {float(stats.get('net') or 0.0):.6f} USDT"
    )


# ============================================================
# SIGNAL LOGIC
# ============================================================
def scan_symbol(symbol: str) -> dict | None:
    candles_entry_raw = get_candles(symbol, ENTRY_INTERVAL, 300)

    if len(candles_entry_raw) < 120:
        log("SCAN", f"{symbol} -> insufficient 1H candles")
        return None

    candles_1h = candles_entry_raw[:-1]

    opens = [float(c[1]) for c in candles_1h]
    highs = [float(c[2]) for c in candles_1h]
    lows = [float(c[3]) for c in candles_1h]
    closes = [float(c[4]) for c in candles_1h]
    volumes = [float(c[5]) for c in candles_1h]

    if len(candles_1h) < ATR_PERIOD + VOLUME_SMA_PERIOD + SWING_LEN * 2 + 10:
        log("SCAN", f"{symbol} -> insufficient indicator candles")
        return None

    trend_up, trend_down, trend_detail = symbol_4h_trend(symbol)

    i = -1
    p = -2

    entry = closes[i]
    candle_open = opens[i]
    candle_high = highs[i]
    candle_low = lows[i]
    prev_close = closes[p]

    atr_1h = atr(candles_1h, ATR_PERIOD)
    atr_pct = atr_1h / entry if entry > 0 else 0.0

    vol_sma20 = sma(volumes[:-1], VOLUME_SMA_PERIOD)
    latest_volume = volumes[i]
    volume_ok = vol_sma20 is not None and latest_volume > vol_sma20

    atr_ok = atr_pct >= MIN_ATR_PCT

    swing_high, swing_low = get_last_confirmed_swing(
        highs[:p],
        lows[:p],
        SWING_LEN,
    )

    long_retest_distance = None
    short_retest_distance = None

    bullish = entry > candle_open
    bearish = entry < candle_open

    bos_long = swing_high is not None and prev_close > swing_high
    bos_short = swing_low is not None and prev_close < swing_low

    retest_long = False
    retest_short = False

    if swing_high is not None:
        long_retest_distance = abs(candle_low - swing_high)
        retest_long = long_retest_distance <= atr_1h * RETEST_TOLERANCE_ATR

    if swing_low is not None:
        short_retest_distance = abs(candle_high - swing_low)
        retest_short = short_retest_distance <= atr_1h * RETEST_TOLERANCE_ATR

    long_setup = trend_up and atr_ok and volume_ok and bos_long and retest_long and bullish
    short_setup = trend_down and atr_ok and volume_ok and bos_short and retest_short and bearish

    log(
        "SETUP",
        f"{symbol} | trend_up={trend_up} trend_down={trend_down} | {trend_detail} | "
        f"swing_high={swing_high} swing_low={swing_low} | "
        f"bos_long={bos_long} bos_short={bos_short} | "
        f"long_retest_dist={long_retest_distance} short_retest_dist={short_retest_distance} | "
        f"atr={atr_1h:.6f} atr_pct={atr_pct:.5f} atr_ok={atr_ok} | "
        f"volume={latest_volume:.2f} vol_sma20={vol_sma20} volume_ok={volume_ok} | "
        f"bullish={bullish} bearish={bearish} | "
        f"long_setup={long_setup} short_setup={short_setup}"
    )

    if long_setup:
        sl = candle_low - (atr_1h * SL_ATR_BUFFER_MULT)
        if sl >= entry:
            log("SKIP", f"{symbol} long skipped: SL >= entry")
            return None

        tp = entry + ((entry - sl) * RR_RATIO)

        return {
            "symbol": symbol,
            "side": "Buy",
            "reason": "SMC BOS RETEST LONG",
            "price": entry,
            "sl": sl,
            "tp": tp,
            "atr": atr_1h,
            "bos_level": swing_high,
        }

    if short_setup:
        sl = candle_high + (atr_1h * SL_ATR_BUFFER_MULT)
        if sl <= entry:
            log("SKIP", f"{symbol} short skipped: SL <= entry")
            return None

        tp = entry - ((sl - entry) * RR_RATIO)

        return {
            "symbol": symbol,
            "side": "Sell",
            "reason": "SMC BOS RETEST SHORT",
            "price": entry,
            "sl": sl,
            "tp": tp,
            "atr": atr_1h,
            "bos_level": swing_low,
        }

    return None


# ============================================================
# ORDER MANAGEMENT
# ============================================================
def build_order_qty_from_instrument(instrument: dict, sizing_balance_usdt: float, price: float) -> float | None:
    position_value = sizing_balance_usdt * POSITION_PCT
    exposure = position_value * LEVERAGE
    raw_qty = exposure / price

    qty = floor_to_step(raw_qty, instrument["qty_step"])

    if qty < instrument["min_order_qty"]:
        qty = instrument["min_order_qty"]

    min_notional = float(instrument.get("min_notional") or 0)
    if min_notional > 0 and qty * price < min_notional:
        qty = ceil_to_step(min_notional / price, instrument["qty_step"])

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


# ============================================================
# DISPLAY / RESULT
# ============================================================
def maybe_send_heartbeat(
    state: dict,
    balance: float | None = None,
    actual_pos: dict | None = None,
    status: str = "Running",
) -> None:
    now_ts = time.time()
    last_heartbeat = state.get("last_heartbeat_at")

    if last_heartbeat is not None:
        try:
            if now_ts - float(last_heartbeat) < HEARTBEAT_INTERVAL_SECONDS:
                return
        except Exception:
            pass

    balance_text = "N/A" if balance is None else f"{balance:.4f} USDT"

    if actual_pos:
        position_text = (
            f"{actual_pos.get('symbol')} {actual_pos.get('side')} | "
            f"Size: {actual_pos.get('size')} | "
            f"Entry: {actual_pos.get('entry')}"
        )
    else:
        position_text = "No open position"

    send_telegram(
        f"BOT HEARTBEAT\n"
        f"Version: {VERSION}\n"
        f"Status: {status}\n"
        f"Balance: {balance_text}\n"
        f"Position: {position_text}\n"
        f"Time: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}\n\n"
        f"{bot_record_text(state)}"
    )

    state["last_heartbeat_at"] = now_ts
    save_state(state)


def print_trade_details(
    symbol: str,
    action: str,
    side: str,
    entry: float,
    sl: float,
    tp: float,
    qty: float,
    sizing_balance_usdt: float,
    reason: str,
    est_sl_loss: float,
) -> None:
    risk_usdt = abs(entry - sl) * qty
    gross_tp, fees_tp, net_tp = net_pnl_estimate(side, entry, tp, qty)
    risk_pct = (risk_usdt / sizing_balance_usdt * 100) if sizing_balance_usdt else 0
    est_sl_risk_pct = (est_sl_loss / sizing_balance_usdt * 100) if sizing_balance_usdt else 0
    direction = "LONG" if side == "Buy" else "SHORT"

    print("\n" + "=" * 72)
    print(action)
    print("=" * 72)
    print(f"Version      : {VERSION}")
    print(f"Pair         : {symbol}")
    print(f"Direction    : {direction}")
    print(f"Reason       : {reason}")
    print(f"Entry        : {entry:.6f}")
    print(f"SL           : {sl:.6f}")
    print(f"TP           : {tp:.6f}")
    print(f"Qty          : {qty}")
    print(f"Leverage     : {LEVERAGE}x")
    print(f"Sizing Bal   : {sizing_balance_usdt:.6f} USDT")
    print(f"Risk         : {risk_usdt:.6f} USDT ({risk_pct:.2f}%)")
    print(f"Est SL Loss  : {est_sl_loss:.6f} USDT ({est_sl_risk_pct:.2f}%)")
    print(f"Max Risk     : {MAX_SL_RISK_PCT * 100:.2f}%")
    print(f"Est GrossTP  : {gross_tp:.6f} USDT")
    print(f"Est FeesTP   : {fees_tp:.6f} USDT")
    print(f"Est NetTP    : {net_tp:.6f} USDT")
    print("=" * 72 + "\n")


def estimate_close_result(state: dict, last_price: float | None) -> tuple[str, float | None, float | None, float | None]:
    symbol = state.get("symbol")
    side = state.get("pos")
    entry = state.get("entry")
    tp = state.get("tp")
    sl = state.get("sl")
    qty = state.get("size")

    if not symbol or not side or entry is None or qty is None or last_price is None:
        return "Position Closed", None, None, None

    reason = "Position Closed"

    if tp is not None and sl is not None:
        reason = "TP HIT" if abs(last_price - tp) <= abs(last_price - sl) else "SL HIT"

    gross, fees, net = net_pnl_estimate(side, entry, last_price, qty)
    return reason, gross, fees, net


def update_stats_on_close(state: dict, close_reason: str, net: float | None) -> None:
    symbol = state.get("symbol")
    if not symbol:
        return

    is_tp = "TP" in close_reason
    is_sl = "SL" in close_reason
    net_value = float(net or 0.0)

    state["total_closed_trades"] = int(state.get("total_closed_trades") or 0) + 1
    state["total_net_pnl"] = float(state.get("total_net_pnl") or 0.0) + net_value

    if is_tp:
        state["total_tp_hits"] = int(state.get("total_tp_hits") or 0) + 1
    if is_sl:
        state["total_sl_hits"] = int(state.get("total_sl_hits") or 0) + 1

    pair_stats = state.setdefault("pair_stats", seeded_pair_stats())
    stats = pair_stats.setdefault(symbol, {"trades": 0, "tp": 0, "sl": 0, "net": 0.0})
    stats["trades"] = int(stats.get("trades") or 0) + 1
    stats["net"] = float(stats.get("net") or 0.0) + net_value

    if is_tp:
        stats["tp"] = int(stats.get("tp") or 0) + 1
    if is_sl:
        stats["sl"] = int(stats.get("sl") or 0) + 1

    if net is not None:
        state["daily_realized_pnl"] = float(state.get("daily_realized_pnl") or 0.0) + net_value

        if net_value < 0:
            state["current_consecutive_losses"] = int(state.get("current_consecutive_losses") or 0) + 1
        else:
            state["current_consecutive_losses"] = 0
            state["loss_pause_applied_at_streak"] = None
            state["loss_warning_sent_at_streak"] = None

        state["max_consecutive_losses_seen"] = max(
            int(state.get("max_consecutive_losses_seen") or 0),
            int(state.get("current_consecutive_losses") or 0),
        )

    send_loss_streak_warning_if_needed(state)


def record_closed_trade(state: dict, symbol: str | None, side: str | None, last_price: float | None) -> None:
    close_reason, gross, fees, net = estimate_close_result(state, last_price)
    update_stats_on_close(state, close_reason, net)

    gross_text = "N/A" if gross is None else f"{gross:.6f} USDT"
    fees_text = "N/A" if fees is None else f"{fees:.6f} USDT"
    net_text = "N/A" if net is None else f"{net:.6f} USDT"

    send_telegram(
        f"{close_reason}\n"
        f"Version: {VERSION}\n"
        f"Pair: {state.get('symbol')}\n"
        f"Side: {state.get('pos')}\n"
        f"Entry: {state.get('entry')}\n"
        f"TP: {state.get('tp')}\n"
        f"SL: {state.get('sl')}\n"
        f"Exit Price: {last_price}\n"
        f"Qty: {state.get('size')}\n"
        f"Gross PnL: {gross_text}\n"
        f"Est. Fees: {fees_text}\n"
        f"Est. Net PnL: {net_text}\n\n"
        f"{pair_record_text(state, state.get('symbol'))}\n\n"
        f"{bot_record_text(state)}"
    )

    log("CLOSE", f"{close_reason} | {symbol} {side} | gross={gross_text} fees={fees_text} net={net_text}")


def reset_position_state_after_close(state: dict, last_price: float | None, close_reason: str | None = None) -> dict:
    now_ts = time.time()

    keep_keys = {
        "last_heartbeat_at",
        "daily_date",
        "daily_start_wallet",
        "daily_realized_pnl",
        "daily_loss_stop_active",
        "total_closed_trades",
        "total_tp_hits",
        "total_sl_hits",
        "total_net_pnl",
        "current_consecutive_losses",
        "max_consecutive_losses_seen",
        "latest_balance",
        "loss_pause_until",
        "loss_pause_applied_at_streak",
        "loss_warning_sent_at_streak",
        "pair_stats",
    }

    new_state = default_state()
    for key in keep_keys:
        new_state[key] = state.get(key, new_state.get(key))

    new_state["last_closed_at"] = now_ts
    new_state["last_closed_symbol"] = state.get("symbol")
    new_state["last_closed_side"] = state.get("pos")
    new_state["last_closed_price"] = last_price
    new_state["last_close_reason"] = close_reason or "Position Closed"

    save_state(new_state)
    return new_state


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

    state["symbol"] = symbol
    state["pos"] = side
    state["size"] = size

    if state.get("entry") is None:
        state["entry"] = float(actual_pos["entry"])

    save_state(state)

    gross_now, fees_now, net_now = net_pnl_estimate(side, entry, current_price, size)

    log(
        "POSITION",
        f"{symbol} | {side} | entry={entry:.6f} current={current_price:.6f} "
        f"gross={gross_now:+.6f} fees={fees_now:.6f} net={net_now:+.6f}"
    )

    return f"Position running on {symbol}"


# ============================================================
# CORE
# ============================================================
def run() -> str:
    state = load_state()

    actual_pos = find_any_open_position()

    if actual_pos:
        maybe_send_heartbeat(state, balance=None, actual_pos=actual_pos, status="Managing open position")
        return manage_open_position(state, actual_pos)

    if state.get("pos"):
        symbol = state.get("symbol")
        side = state.get("pos")
        last_price = get_price(symbol) if symbol else None

        record_closed_trade(state, symbol, side, last_price)

        close_reason, _, _, _ = estimate_close_result(state, last_price)
        state = reset_position_state_after_close(state, last_price, close_reason)

    actual_balance = get_balance()
    if actual_balance is None or actual_balance <= 0:
        return "No balance"

    state["latest_balance"] = actual_balance
    ensure_daily_state(state, actual_balance)
    maybe_send_heartbeat(state, balance=actual_balance, actual_pos=None, status="Running / scanning setups")

    if daily_loss_limit_hit(state):
        return "Daily loss stop active"

    if state.get("last_closed_at"):
        elapsed = time.time() - float(state["last_closed_at"])
        cooldown_sec = COOLDOWN_HOURS * 60 * 60

        if elapsed < cooldown_sec:
            remaining = int((cooldown_sec - elapsed) / 60)
            return f"Cooldown active ({remaining} min left)"

    sizing_balance = actual_balance

    for symbol in SYMBOLS:
        setup = scan_symbol(symbol)

        if not setup:
            continue

        instrument = get_instrument_info(symbol)
        if not instrument:
            log("SKIP", f"{symbol} skipped: instrument info failed")
            continue

        price = setup["price"]
        side = setup["side"]
        reason = setup["reason"]

        sl = round_price(setup["sl"], instrument["tick_size"])
        tp = round_price(setup["tp"], instrument["tick_size"])

        if sl <= 0 or tp <= 0:
            log("SKIP", f"{symbol} skipped: invalid SL/TP sl={sl} tp={tp}")
            continue

        if side == "Buy" and not (sl < price < tp):
            log("SKIP", f"{symbol} skipped: invalid long geometry entry={price} sl={sl} tp={tp}")
            continue

        if side == "Sell" and not (tp < price < sl):
            log("SKIP", f"{symbol} skipped: invalid short geometry entry={price} sl={sl} tp={tp}")
            continue

        qty = build_order_qty_from_instrument(instrument, sizing_balance, price)

        if qty is None or qty <= 0:
            log("SKIP", f"{symbol} skipped: invalid qty")
            continue

        est_sl_loss = estimated_sl_loss(side, price, sl, qty)
        est_sl_risk_pct = est_sl_loss / actual_balance if actual_balance > 0 else 999.0

        if est_sl_risk_pct > MAX_SL_RISK_PCT:
            direction = "LONG" if side == "Buy" else "SHORT"
            msg = (
                f"TRADE SKIPPED - Risk too high\n"
                f"Pair: {symbol}\n"
                f"Direction: {direction}\n"
                f"Entry: {price:.6f}\n"
                f"SL: {sl}\n"
                f"Qty: {qty}\n"
                f"Est SL Loss: {est_sl_loss:.6f} USDT\n"
                f"Wallet: {actual_balance:.6f} USDT\n"
                f"Risk %: {est_sl_risk_pct * 100:.2f}%\n"
                f"Max Risk %: {MAX_SL_RISK_PCT * 100:.2f}%"
            )
            log("RISK_SKIP", msg.replace("\n", " | "))
            send_telegram(msg)
            continue

        gross_tp, fees_tp, net_tp = net_pnl_estimate(side, price, tp, qty)

        log(
            "DEBUG",
            f"{symbol} | side={side} | balance={actual_balance:.4f} | price={price:.6f} | "
            f"qty={qty} | sl={sl} | tp={tp} | est_sl_loss={est_sl_loss:.6f} "
            f"est_sl_risk_pct={est_sl_risk_pct * 100:.2f}% | gross_tp={gross_tp:.6f} "
            f"fees_tp={fees_tp:.6f} net_tp={net_tp:.6f}"
        )

        order_res = place_order(symbol, side, qty, tp, sl)

        if order_res.get("retCode") != 0:
            log("ORDER_FAIL", f"{symbol} -> {order_res}")
            send_telegram(
                f"ORDER FAILED\n"
                f"Version: {VERSION}\n"
                f"Pair: {symbol}\n"
                f"Side: {side}\n"
                f"Response: {order_res}\n\n"
                f"{bot_record_text(state)}"
            )
            continue

        state.update({
            "symbol": symbol,
            "pos": side,
            "entry": price,
            "sl": sl,
            "tp": tp,
            "size": qty,
            "reason": reason,
            "opened": iso_now(),
            "latest_balance": actual_balance,
        })

        save_state(state)

        print_trade_details(symbol, "TRADE OPENED", side, price, sl, tp, qty, sizing_balance, reason, est_sl_loss)

        direction = "LONG" if side == "Buy" else "SHORT"

        send_telegram(
            f"TRADE OPENED\n"
            f"Version: {VERSION}\n"
            f"Pair: {symbol}\n"
            f"Direction: {direction}\n"
            f"Reason: {reason}\n"
            f"BOS Level: {setup.get('bos_level')}\n"
            f"Entry: {price:.6f}\n"
            f"SL: {sl}\n"
            f"TP: {tp}\n"
            f"Qty after rounding: {qty}\n"
            f"Leverage: {LEVERAGE}x\n"
            f"Actual Balance: {actual_balance:.4f} USDT\n"
            f"Position Sizing: {POSITION_PCT * 100:.1f}%\n"
            f"RR: {RR_RATIO}\n"
            f"Est SL Loss: {est_sl_loss:.6f} USDT\n"
            f"Est SL Risk %: {est_sl_risk_pct * 100:.2f}%\n"
            f"Max Risk %: {MAX_SL_RISK_PCT * 100:.2f}%\n"
            f"Est. Gross TP: {gross_tp:.6f} USDT\n"
            f"Est. Fees TP: {fees_tp:.6f} USDT\n"
            f"Est. Net TP: {net_tp:.6f} USDT\n\n"
            f"{bot_record_text(state)}"
        )

        return f"Trade opened on {symbol}"

    save_state(state)
    return "No setup"


# ============================================================
# MAIN
# ============================================================
def main() -> None:
    log("VERSION", VERSION)
    log("CONFIG", f"SYMBOLS={', '.join(SYMBOLS)}")
    log("CONFIG", f"ENTRY_INTERVAL={ENTRY_INTERVAL} | TREND_INTERVAL={TREND_INTERVAL}")
    log("CONFIG", f"LEVERAGE={LEVERAGE} | POSITION_PCT={POSITION_PCT} | RR={RR_RATIO}")
    log("CONFIG", f"SWING_LEN={SWING_LEN} | RETEST_TOLERANCE_ATR={RETEST_TOLERANCE_ATR}")
    log("CONFIG", f"ATR_PERIOD={ATR_PERIOD} | MIN_ATR_PCT={MIN_ATR_PCT} | VOLUME_SMA_PERIOD={VOLUME_SMA_PERIOD}")
    log("CONFIG", f"COOLDOWN_HOURS={COOLDOWN_HOURS} | CHECK_INTERVAL_SECONDS={CHECK_INTERVAL_SECONDS}")
    log("CONFIG", f"DAILY_LOSS_STOP_PCT={DAILY_LOSS_STOP_PCT} | LOSS_WARNING_THRESHOLD={LOSS_WARNING_THRESHOLD}")
    log("CONFIG", f"MAX_SL_RISK_PCT={MAX_SL_RISK_PCT}")
    log("BOT", "=" * 88)
    log("BOT", "SMC BOS RETEST | 1H STRUCTURE + 4H EMA200 SYMBOL TREND | BOTH SIDES")
    log("BOT", "One active trade only across all symbols")
    log("BOT", "=" * 88)

    state = load_state()
    save_state(state)

    for symbol in SYMBOLS:
        instrument = get_instrument_info(symbol)
        if instrument:
            log(
                "INSTRUMENT",
                f"{symbol} tick={instrument['tick_size']} qty_step={instrument['qty_step']} "
                f"min_qty={instrument['min_order_qty']} min_notional={instrument['min_notional']}"
            )
        else:
            log("INSTRUMENT", f"{symbol} instrument validation failed")

        set_leverage(symbol)
        time.sleep(0.8)

    balance = get_balance()
    if balance is not None:
        state["latest_balance"] = balance
    ensure_daily_state(state, balance)
    send_loss_streak_warning_if_needed(state)

    actual_pos = find_any_open_position()
    if actual_pos:
        state["symbol"] = actual_pos["symbol"]
        state["pos"] = actual_pos["side"]
        state["entry"] = actual_pos["entry"]
        state["size"] = actual_pos["size"]
        save_state(state)

        send_telegram(
            f"RESTART POSITION SYNC\n"
            f"Version: {VERSION}\n"
            f"Detected open position on exchange\n"
            f"Pair: {actual_pos['symbol']}\n"
            f"Side: {actual_pos['side']}\n"
            f"Size: {actual_pos['size']}\n"
            f"Entry: {actual_pos['entry']}\n\n"
            f"{bot_record_text(state)}"
        )
    else:
        save_state(state)

    send_telegram(
        f"BOT STARTED\n"
        f"Version: {VERSION}\n"
        f"Pairs: {', '.join(SYMBOLS)}\n"
        f"Strategy: SMC BOS Retest\n"
        f"Entry TF: {ENTRY_INTERVAL}m | Trend TF: {TREND_INTERVAL}m\n"
        f"Position Sizing: {POSITION_PCT * 100:.1f}% of actual wallet\n"
        f"Leverage: {LEVERAGE}x\n"
        f"RR: {RR_RATIO}\n"
        f"Cooldown: {COOLDOWN_HOURS}h\n"
        f"Max SL Risk: {MAX_SL_RISK_PCT * 100:.2f}%\n"
        f"Daily Loss Stop: {DAILY_LOSS_STOP_PCT * 100:.1f}%\n"
        f"Loss Streak Warning: {LOSS_WARNING_THRESHOLD} losses\n"
        f"Loss Pause: Disabled\n"
        f"Heartbeat: every {int(HEARTBEAT_INTERVAL_SECONDS / 3600)} hours\n"
        f"Mode: 1 active trade only\n\n"
        f"{bot_record_text(state)}"
    )

    while True:
        try:
            result = run()
            log("RUN", result)
            time.sleep(CHECK_INTERVAL_SECONDS)

        except Exception as e:
            log("ERROR", str(e))
            send_telegram(f"BOT ERROR\nVersion: {VERSION}\n{e}")
            time.sleep(30)


if __name__ == "__main__":
    main()
