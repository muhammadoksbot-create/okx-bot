import requests, hmac, hashlib, json, os, time, math
from datetime import datetime
from config_okx import API_KEY, SECRET_KEY

# ================= CONFIG =================
BASE_URL = "https://api.bybit.com"
STATE_FILE = "state_bybit.json"

SYMBOL = "XRPUSDT"
CATEGORY = "linear"
INTERVAL = "5"

LEVERAGE = 15
POSITION_PCT = 0.10
RR_RATIO = 1.5

TELEGRAM_TOKEN = "8756536068:AAFu7zrR5W-gu0Mv9bX4Tf9O7kokeqk6G5U"
CHAT_ID = "1118069943"

# ================= TELEGRAM =================
def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": CHAT_ID, "text": msg}, timeout=10)
    except:
        pass

# ================= AUTH =================
def sign(ts, params):
    payload = ts + API_KEY + "5000" + params
    return hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()

def headers(body=""):
    ts = str(int(time.time() * 1000))
    return {
        "X-BAPI-API-KEY": API_KEY,
        "X-BAPI-SIGN": sign(ts, body),
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": "5000",
        "Content-Type": "application/json"
    }

# ================= REQUEST =================
def req(method, path, body=None):
    try:
        url = BASE_URL + path
        h = headers(body if body else "")
        if method == "GET":
            return requests.get(url, headers=h, timeout=10).json()
        else:
            return requests.post(url, headers=h, data=body, timeout=10).json()
    except Exception as e:
        send_telegram(f"API ERROR: {e}")
        return {}

# ================= STATE =================
def load():
    if not os.path.exists(STATE_FILE):
        return {"pos": None}
    return json.load(open(STATE_FILE))

def save(s):
    json.dump(s, open(STATE_FILE, "w"))

# ================= DATA =================
def candles():
    r = req("GET", f"/v5/market/kline?category={CATEGORY}&symbol={SYMBOL}&interval={INTERVAL}&limit=200")
    return list(reversed(r["result"]["list"])) if r.get("retCode")==0 else []

def price():
    r = req("GET", f"/v5/market/tickers?category={CATEGORY}&symbol={SYMBOL}")
    try:
        return float(r["result"]["list"][0]["lastPrice"])
    except:
        return None

def balance():
    r = req("GET", "/v5/account/wallet-balance?accountType=UNIFIED")
    try:
        for c in r["result"]["list"][0]["coin"]:
            if c["coin"]=="USDT":
                return float(c["walletBalance"])
    except:
        return None

# ================= LEVERAGE =================
def set_leverage():
    body = json.dumps({
        "category": CATEGORY,
        "symbol": SYMBOL,
        "buyLeverage": str(LEVERAGE),
        "sellLeverage": str(LEVERAGE)
    })
    req("POST", "/v5/position/set-leverage", body)

# ================= SMC =================
def swings(closes, lb=3):
    s=[]
    for i in range(lb, len(closes)-lb):
        if all(closes[i]>closes[i-j] and closes[i]>closes[i+j] for j in range(1,lb+1)):
            s.append(("H",closes[i]))
        if all(closes[i]<closes[i-j] and closes[i]<closes[i+j] for j in range(1,lb+1)):
            s.append(("L",closes[i]))
    return s

def signal(c):
    closes=[float(x[4]) for x in c]
    sw=swings(closes)

    if len(sw)<3:
        return None

    last=sw[-1]
    prev=sw[-2]

    if last[0]=="H" and prev[0]=="L":
        return "Buy"
    if last[0]=="L" and prev[0]=="H":
        return "Sell"
    return None

# ================= ORDER =================
def order(side, size, tp, sl):
    body=json.dumps({
        "category":CATEGORY,
        "symbol":SYMBOL,
        "side":side,
        "orderType":"Market",
        "qty":str(size),
        "takeProfit":str(round(tp,4)),
        "stopLoss":str(round(sl,4))
    })
    return req("POST","/v5/order/create",body)

# ================= CORE =================
def run():
    s=load()
    c=candles()
    if not c: return "No candles"

    p=price()
    if not p: return "No price"

    # TP SL check
    if s.get("pos"):
        if s["pos"]=="Buy" and p>=s["tp"]:
            order("Sell",s["size"],0,0)
            send_telegram("TP HIT")
            save({"pos":None})
            return "TP"
        if s["pos"]=="Buy" and p<=s["sl"]:
            order("Sell",s["size"],0,0)
            send_telegram("SL HIT")
            save({"pos":None})
            return "SL"
        if s["pos"]=="Sell" and p<=s["tp"]:
            order("Buy",s["size"],0,0)
            send_telegram("TP HIT")
            save({"pos":None})
            return "TP"
        if s["pos"]=="Sell" and p>=s["sl"]:
            order("Buy",s["size"],0,0)
            send_telegram("SL HIT")
            save({"pos":None})
            return "SL"
        return "Running"

    side=signal(c)
    if not side:
        return "No setup"

    bal=balance()
    if not bal:
        return "No balance"

    exposure=bal*POSITION_PCT*LEVERAGE
    size=round(exposure/p,0)
    if size<1: size=1

    sl=p*0.99 if side=="Buy" else p*1.01
    tp=p+(p-sl)*RR_RATIO if side=="Buy" else p-(sl-p)*RR_RATIO

    r=order(side,size,tp,sl)
    if r.get("retCode")!=0:
        send_telegram("Order failed")
        return "Fail"

    s.update({"pos":side,"entry":p,"sl":sl,"tp":tp,"size":size})
    save(s)

    send_telegram(f"TRADE {side}\nEntry:{p}\nSL:{sl}\nTP:{tp}\nSize:{size}")

    return "Trade Opened"

# ================= MAIN =================
def main():
    print("BOT STARTED FINAL")
    set_leverage()

    while True:
        try:
            print(datetime.utcnow(), run())
            time.sleep(60)
        except Exception as e:
            send_telegram(f"ERROR: {e}")
            time.sleep(10)

if __name__=="__main__":
    main()
