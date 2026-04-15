import requests, hmac, base64, json, os, math, time
from datetime import datetime
from config_okx import API_KEY, SECRET_KEY, PASSPHRASE

BASE_URL = "https://www.okx.com"
STATE_FILE = "state_okx.json"

SYMBOL = "LINK-USDT-SWAP"
INTERVAL = "5m"

LEVERAGE = 5
POSITION_PCT = 0.50

# 🔔 TELEGRAM
TELEGRAM_TOKEN = "8756536068:AAFu7zrR5W-gu0Mv9bX4Tf9O7kokeqk6G5U"
CHAT_ID = "okx_trade_alert_bot"

# ---------- TELEGRAM ----------
def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": CHAT_ID, "text": msg}, timeout=10)
    except:
        pass

# ---------- AUTH ----------
def sign(message):
    return base64.b64encode(
        hmac.new(SECRET_KEY.encode(), message.encode(), digestmod="sha256").digest()
    ).decode()

def headers(method, path, body=""):
    ts = datetime.utcnow().isoformat("T", "milliseconds") + "Z"
    msg = ts + method + path + body
    return {
        "OK-ACCESS-KEY": API_KEY,
        "OK-ACCESS-SIGN": sign(msg),
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": PASSPHRASE,
        "Content-Type": "application/json"
    }

# ---------- REQUEST ----------
def req(method, path, body=None):
    try:
        url = BASE_URL + path
        h = headers(method, path, body if body else "")
        if method == "GET":
            return requests.get(url, headers=h, timeout=10).json()
        return requests.post(url, headers=h, data=body, timeout=10).json()
    except Exception as e:
        send_telegram(f"⚠️ API ERROR: {e}")
        return {}

# ---------- LEVERAGE ----------
def set_leverage():
    body = json.dumps({
        "instId": SYMBOL,
        "lever": str(LEVERAGE),
        "mgnMode": "cross"
    })
    req("POST", "/api/v5/account/set-leverage", body)

# ---------- STATE ----------
def load():
    if not os.path.exists(STATE_FILE):
        return {"pos": None}
    return json.load(open(STATE_FILE))

def save(s):
    json.dump(s, open(STATE_FILE, "w"))

# ---------- DATA ----------
def candles():
    r = req("GET", f"/api/v5/market/candles?instId={SYMBOL}&bar={INTERVAL}&limit=200")
    return list(reversed(r["data"])) if r.get("code") == "0" else []

def price():
    r = req("GET", f"/api/v5/market/ticker?instId={SYMBOL}")
    try:
        return float(r["data"][0]["last"])
    except:
        return None

def balance():
    r = req("GET", "/api/v5/account/balance")
    try:
        for d in r["data"][0]["details"]:
            if d["ccy"] == "USDT":
                return float(d["availBal"])
    except:
        return None

# ---------- SMC ----------
def swings(closes, lb=3):
    s=[]
    for i in range(lb, len(closes)-lb):
        if all(closes[i]>closes[i-j] and closes[i]>closes[i+j] for j in range(1,lb+1)):
            s.append(("H",i,closes[i]))
        if all(closes[i]<closes[i-j] and closes[i]<closes[i+j] for j in range(1,lb+1)):
            s.append(("L",i,closes[i]))
    return s

def structure(sw):
    if len(sw)<3: return None
    a,b,c = sw[-3:]
    if b[0]=="L" and c[0]=="H" and c[2]>a[2]: return "BOS_UP"
    if b[0]=="H" and c[0]=="L" and c[2]<a[2]: return "BOS_DOWN"

def choch(sw):
    if len(sw)<4: return None
    a,b,c,d = sw[-4:]
    if a[0]=="H" and c[0]=="L" and d[2]<b[2]: return "SHORT"
    if a[0]=="L" and c[0]=="H" and d[2]>b[2]: return "LONG"

def fvg(c):
    if len(c)<3: return None
    a,b,c3 = c[-3],c[-2],c[-1]
    if float(c3[3])>float(a[2]):
        return ("bull", float(a[2]), float(c3[3]))
    if float(c3[2])<float(a[3]):
        return ("bear", float(c3[2]), float(a[3]))

def liquidity(c):
    last, prev = c[-1], c[-2]
    return float(last[2])>float(prev[2]) or float(last[3])<float(prev[3])

# ---------- ORDER ----------
def order(side, size):
    body=json.dumps({
        "instId":SYMBOL,
        "tdMode":"cross",
        "side":side,
        "ordType":"market",
        "sz":str(size)
    })
    return req("POST","/api/v5/trade/order",body)

# ---------- CORE ----------
def run():
    s = load()

    c = candles()
    if not c: return "No data"

    closes = [float(x[4]) for x in c]
    p = price()
    if not p: return "No price"

    # ---------- TP/SL CHECK ----------
    if s.get("pos"):
        pos = s["pos"]

        if pos == "buy":
            if p >= s["tp"]:
                order("sell", s["size"])
                send_telegram(f"🎯 TP HIT\n{SYMBOL}\nPrice: {p}")
                save({"pos": None})
                return "TP HIT"

            if p <= s["sl"]:
                order("sell", s["size"])
                send_telegram(f"❌ SL HIT\n{SYMBOL}\nPrice: {p}")
                save({"pos": None})
                return "SL HIT"

        if pos == "sell":
            if p <= s["tp"]:
                order("buy", s["size"])
                send_telegram(f"🎯 TP HIT\n{SYMBOL}\nPrice: {p}")
                save({"pos": None})
                return "TP HIT"

            if p >= s["sl"]:
                order("buy", s["size"])
                send_telegram(f"❌ SL HIT\n{SYMBOL}\nPrice: {p}")
                save({"pos": None})
                return "SL HIT"

        return "Position running"

    # ---------- ENTRY ----------
    sw = swings(closes)
    st = structure(sw)
    ch = choch(sw)
    fv = fvg(c)
    liq = liquidity(c)

    side=None

    if ch=="LONG" and liq:
        side="buy"
    elif ch=="SHORT" and liq:
        side="sell"
    elif st=="BOS_UP" and fv and fv[0]=="bull":
        side="buy"
    elif st=="BOS_DOWN" and fv and fv[0]=="bear":
        side="sell"

    if not side:
        return "No setup"

    bal = balance()
    if not bal: return "No balance"

    # ---------- SIZE ----------
    position_value = bal * POSITION_PCT
    exposure = position_value * LEVERAGE
    size = round(exposure / p, 1)

    # ---------- SL/TP ----------
    sl = min(closes[-5:]) if side=="buy" else max(closes[-5:])
    risk = abs(p - sl)
    if risk == 0:
        return "Invalid SL"

    tp = p + (risk * 2) if side=="buy" else p - (risk * 2)

    o = order(side, size)
    if o.get("code") != "0":
        send_telegram("❌ ORDER FAILED")
        return "Order failed"

    # SAVE
    s.update({
        "pos":side,
        "entry":p,
        "sl":sl,
        "tp":tp,
        "size":size
    })
    save(s)

    # ALERT
    msg = f"""
🚀 TRADE OPENED
Pair: {SYMBOL}
Side: {side.upper()}
Entry: {p}
SL: {sl}
TP: {tp}
Size: {size}
"""
    send_telegram(msg)

    return "Trade Opened"

# ---------- MAIN ----------
def main():
    print("BOT STARTED (FINAL VERSION)")
    set_leverage()

    while True:
        try:
            print(datetime.utcnow(), run())
            time.sleep(60)
        except Exception as e:
            send_telegram(f"⚠️ BOT ERROR: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main()
