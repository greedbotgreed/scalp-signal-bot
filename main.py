import os
import time
import json
import math
import threading
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import requests


# =========================
# CONFIG
# =========================
TG_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("CHAT_ID", "").strip()

# Symbols (USDT Perp)
SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "TONUSDT"]

# Timeframes
TF_ENTRY = "15m"   # основний
TF_TREND = "1h"    # фільтр тренду

# Indicators
EMA_FAST = 20
EMA_SLOW = 50
RSI_LEN = 14

# Risk/Targets (просте правило)
R_MULT_TP1 = 1.0
R_MULT_TP2 = 2.0

# How often to scan (seconds)
CHECK_EVERY_SEC = 60

# Anti-spam
COOLDOWN_SEC = 60 * 45          # мін. пауза між однаковими сетапами на символ
MAX_ALERTS_PER_DAY = 7          # щоб було “5–7/день”
STATE_FILE = "state.json"

# Daily stats schedule
LOCAL_TZ = timezone(timedelta(hours=2))   # UTC+2
DAILY_REPORT_HOUR = 21
DAILY_REPORT_MIN = 0

# Binance data endpoint (важливо, щоб не ловити 451)
BASE_URL = "https://data.binance.com"

# HTTP
HTTP_TIMEOUT = 12
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0"})


# =========================
# STORAGE
# =========================
def load_state() -> Dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {
            "last_sent": {},        # key -> ts
            "daily": {},            # yyyy-mm-dd -> {"count": int}
            "signals": [],          # list of signals for stats
            "last_report_date": None
        }


def save_state(state: Dict) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# =========================
# TELEGRAM
# =========================
def tg_send(text: str) -> None:
    if not TG_TOKEN or not TG_CHAT_ID:
        print("TG creds missing (BOT_TOKEN/CHAT_ID).")
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }
    try:
        r = SESSION.post(url, json=payload, timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            print("TG send error:", r.status_code, r.text[:200])
    except Exception as e:
        print("TG exception:", e)


# =========================
# BINANCE DATA
# =========================
def _safe_get(path: str, params: Dict) -> Optional[dict]:
    url = f"{BASE_URL}{path}"
    for attempt in range(4):
        try:
            r = SESSION.get(url, params=params, timeout=HTTP_TIMEOUT)
            if r.status_code == 200:
                return r.json()
            # простий backoff
            if r.status_code in (418, 429, 500, 502, 503, 504):
                time.sleep(1.2 * (attempt + 1))
                continue
            print("Binance HTTP:", r.status_code, r.text[:180])
            return None
        except Exception as e:
            print("Binance exception:", e)
            time.sleep(1.2 * (attempt + 1))
    return None


def get_klines(symbol: str, interval: str, limit: int = 300) -> Optional[List[List]]:
    data = _safe_get("/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": limit})
    if isinstance(data, list):
        return data
    return None


# =========================
# INDICATORS
# =========================
def ema(values: List[float], period: int) -> List[float]:
    if len(values) < period:
        return []
    k = 2 / (period + 1)
    out = []
    # start with SMA
    sma = sum(values[:period]) / period
    out.append(sma)
    prev = sma
    for v in values[period:]:
        prev = v * k + prev * (1 - k)
        out.append(prev)
    # align to original length (prepend Nones)
    pad = [math.nan] * (period - 1)
    return pad + out


def rsi(values: List[float], period: int) -> List[float]:
    if len(values) < period + 1:
        return []
    gains = []
    losses = []
    for i in range(1, len(values)):
        diff = values[i] - values[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    out = [math.nan] * period
    def rs_to_rsi(rs: float) -> float:
        return 100 - (100 / (1 + rs))

    rs = (avg_gain / avg_loss) if avg_loss != 0 else float("inf")
    out.append(rs_to_rsi(rs))

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rs = (avg_gain / avg_loss) if avg_loss != 0 else float("inf")
        out.append(rs_to_rsi(rs))

    # out length == len(values)
    return out


def to_ohlc(klines: List[List]) -> Tuple[List[int], List[float], List[float], List[float], List[float]]:
    ts = [int(k[0]) for k in klines]
    o = [float(k[1]) for k in klines]
    h = [float(k[2]) for k in klines]
    l = [float(k[3]) for k in klines]
    c = [float(k[4]) for k in klines]
    return ts, o, h, l, c


# =========================
# SETUP LOGIC
# =========================
def trend_filter_1h(symbol: str) -> Optional[str]:
    kl = get_klines(symbol, TF_TREND, 220)
    if not kl:
        return None
    _, _, _, _, c = to_ohlc(kl)

    e20 = ema(c, EMA_FAST)
    e50 = ema(c, EMA_SLOW)
    if len(e20) != len(c) or len(e50) != len(c):
        return None

    last = len(c) - 1
    if math.isnan(e20[last]) or math.isnan(e50[last]):
        return None

    # simple filter
    if e20[last] > e50[last]:
        return "LONG"
    if e20[last] < e50[last]:
        return "SHORT"
    return None


def make_setup(symbol: str) -> Optional[Dict]:
    # trend
    trend = trend_filter_1h(symbol)
    if not trend:
        return None

    # entry TF
    kl = get_klines(symbol, TF_ENTRY, 300)
    if not kl:
        return None
    ts, o, h, l, c = to_ohlc(kl)

    e20 = ema(c, EMA_FAST)
    e50 = ema(c, EMA_SLOW)
    r = rsi(c, RSI_LEN)
    if not e20 or not e50 or not r:
        return None

    i = len(c) - 2  # беремо передостанню свічку, щоб не ловити “недозакриту”
    price = c[i]
    if any(math.isnan(x) for x in [e20[i], e50[i], r[i]]):
        return None

    # Distance to EMA50 (mean reversion area)
    dist = abs(price - e50[i]) / price

    # Swing for SL (lookback)
    lookback = 20
    low_swing = min(l[i - lookback:i]) if i - lookback >= 0 else min(l[:i])
    high_swing = max(h[i - lookback:i]) if i - lookback >= 0 else max(h[:i])

    # Entry “zone” around EMA20/50
    entry_low = min(e20[i], e50[i]) * 0.999
    entry_high = max(e20[i], e50[i]) * 1.001

    # TRIGGERS (чітко і зрозуміло)
    # LONG: тренд LONG + RSI>50 і ціна не “занадто далеко” від EMA50
    # SHORT: тренд SHORT + RSI<50 і ціна не “занадто далеко” від EMA50
    if trend == "LONG":
        if r[i] < 50:
            return None
        if dist > 0.018:  # ~1.8%
            return None

        sl = low_swing * 0.999  # трохи нижче свінгу
        risk = max(price - sl, price * 0.002)  # мінімальний R
        tp1 = price + risk * R_MULT_TP1
        tp2 = price + risk * R_MULT_TP2

        return {
            "symbol": symbol,
            "tf": TF_ENTRY,
            "dir": "LONG",
            "trend_tf": TF_TREND,
            "price": price,
            "entry_zone": (entry_low, entry_high),
            "sl": sl,
            "tp1": tp1,
            "tp2": tp2,
            "ts": int(ts[i]),
            "rsi": r[i],
            "ema20": e20[i],
            "ema50": e50[i],
        }

    if trend == "SHORT":
        if r[i] > 50:
            return None
        if dist > 0.018:
            return None

        sl = high_swing * 1.001
        risk = max(sl - price, price * 0.002)
        tp1 = price - risk * R_MULT_TP1
        tp2 = price - risk * R_MULT_TP2

        return {
            "symbol": symbol,
            "tf": TF_ENTRY,
            "dir": "SHORT",
            "trend_tf": TF_TREND,
            "price": price,
            "entry_zone": (entry_low, entry_high),
            "sl": sl,
            "tp1": tp1,
            "tp2": tp2,
            "ts": int(ts[i]),
            "rsi": r[i],
            "ema20": e20[i],
            "ema50": e50[i],
        }

    return None


def fmt(x: float, symbol: str) -> str:
    # простий формат під різні ціни
    if symbol.startswith("BTC"):
        return f"{x:,.1f}"
    if symbol.startswith("ETH"):
        return f"{x:,.2f}"
    return f"{x:,.4f}"


def format_message(sig: Dict) -> str:
    sym = sig["symbol"]
    direction = "🟢 LONG" if sig["dir"] == "LONG" else "🔴 SHORT"
    zl, zh = sig["entry_zone"]
    # Чіткий тригер без “чекаємо підтвердження”
    trigger = (
        "Тригер: закриття 15m в зоні EMA + RSI по напрямку тренду"
    )
    return (
        f"{direction} | {sym} | {sig['tf']} (фільтр {sig['trend_tf']})\n"
        f"Зона входу: {fmt(zl, sym)} – {fmt(zh, sym)}\n"
        f"SL: {fmt(sig['sl'], sym)}\n"
        f"TP1: {fmt(sig['tp1'], sym)} | TP2: {fmt(sig['tp2'], sym)}\n"
        f"{trigger}\n"
        f"RSI: {sig['rsi']:.1f}"
    )


# =========================
# ANTI-SPAM / DAILY LIMIT
# =========================
def today_key() -> str:
    return datetime.now(LOCAL_TZ).date().isoformat()


def can_send(state: Dict, sig: Dict) -> bool:
    key = f"{sig['symbol']}|{sig['dir']}|{sig['tf']}"
    now = int(time.time())
    last = int(state.get("last_sent", {}).get(key, 0))
    if now - last < COOLDOWN_SEC:
        return False

    d = today_key()
    daily = state.setdefault("daily", {}).setdefault(d, {"count": 0})
    if daily["count"] >= MAX_ALERTS_PER_DAY:
        return False

    return True


def mark_sent(state: Dict, sig: Dict) -> None:
    key = f"{sig['symbol']}|{sig['dir']}|{sig['tf']}"
    now = int(time.time())
    state.setdefault("last_sent", {})[key] = now
    d = today_key()
    daily = state.setdefault("daily", {}).setdefault(d, {"count": 0})
    daily["count"] = int(daily.get("count", 0)) + 1

    # store for stats
    state.setdefault("signals", []).append({
        "id": f"{sig['symbol']}:{sig['dir']}:{sig['ts']}",
        "symbol": sig["symbol"],
        "dir": sig["dir"],
        "tf": sig["tf"],
        "ts": sig["ts"],
        "price": sig["price"],
        "sl": sig["sl"],
        "tp1": sig["tp1"],
        "tp2": sig["tp2"],
        "status": "OPEN",     # OPEN / HIT / FAIL / MIXED
        "resolved_ts": None
    })


# =========================
# STATS EVALUATION
# =========================
def eval_signal_outcome(sig: Dict) -> str:
    """
    Простий критерій як ти хотів:
    - якщо хоч раз TP1 торкнулися -> HIT
    - якщо SL торкнулися раніше -> FAIL
    - якщо в одній свічці і SL і TP1 -> MIXED
    """
    symbol = sig["symbol"]
    dirn = sig["dir"]
    since_ms = int(sig["ts"])  # kline open time ms
    # беремо останні 200 15m свічок — достатньо для доби+ (200*15m=50h)
    kl = get_klines(symbol, TF_ENTRY, 200)
    if not kl:
        return sig["status"]

    # залишаємо тільки після сигналу
    candles = [k for k in kl if int(k[0]) >= since_ms]
    if len(candles) < 2:
        return sig["status"]

    sl = float(sig["sl"])
    tp1 = float(sig["tp1"])

    for k in candles[1:]:  # після тієї, на якій згенерували
        high = float(k[2])
        low = float(k[3])
        t = int(k[0])

        if dirn == "LONG":
            hit_tp = high >= tp1
            hit_sl = low <= sl
        else:
            hit_tp = low <= tp1
            hit_sl = high >= sl

        if hit_tp and hit_sl:
            sig["resolved_ts"] = t
            return "MIXED"
        if hit_tp:
            sig["resolved_ts"] = t
            return "HIT"
        if hit_sl:
            sig["resolved_ts"] = t
            return "FAIL"

    return "OPEN"


def build_daily_report(state: Dict, date_iso: str) -> Optional[str]:
    # беремо сигнали за дату
    try:
        day = datetime.fromisoformat(date_iso).date()
    except Exception:
        return None

    sigs = state.get("signals", [])
    day_sigs = []
    for s in sigs:
        dt = datetime.fromtimestamp(int(s["ts"]) / 1000, tz=LOCAL_TZ).date()
        if dt == day:
            day_sigs.append(s)

    if not day_sigs:
        return f"📊 Підсумок дня ({date_iso}):\nСетапів: 0"

    # оновлюємо статуси
    hit = fail = mixed = open_ = 0
    for s in day_sigs:
        if s["status"] == "OPEN":
            new_status = eval_signal_outcome(s)
            s["status"] = new_status

        if s["status"] == "HIT":
            hit += 1
        elif s["status"] == "FAIL":
            fail += 1
        elif s["status"] == "MIXED":
            mixed += 1
        else:
            open_ += 1

    total = len(day_sigs)

    text = (
        f"📊 Підсумок дня ({date_iso})\n"
        f"Сетапів: {total}\n"
        f"TP1 торкнулися: {hit}\n"
        f"SL торкнулися: {fail}\n"
        f"Спірні (TP1+SL в одній свічці): {mixed}\n"
        f"Ще в роботі: {open_}\n\n"
        f"Примітка: оцінка = факт торкання рівня (без гарантії виконання)."
    )
    return text


def daily_report_loop(state: Dict) -> None:
    while True:
        now = datetime.now(LOCAL_TZ)
        target = now.replace(hour=DAILY_REPORT_HOUR, minute=DAILY_REPORT_MIN, second=0, microsecond=0)
        if now >= target:
            target = target + timedelta(days=1)

        sleep_for = (target - now).total_seconds()
        time.sleep(max(5, sleep_for))

        # звіт за попередній день (щоб день закрився)
        report_date = (datetime.now(LOCAL_TZ) - timedelta(days=1)).date().isoformat()
        if state.get("last_report_date") == report_date:
            continue

        msg = build_daily_report(state, report_date)
        if msg:
            tg_send(msg)
            state["last_report_date"] = report_date
            save_state(state)


# =========================
# MAIN SCANNER LOOP
# =========================
def scanner_loop() -> None:
    state = load_state()
    save_state(state)

    # стартове повідомлення (без “bot/render”)
    tg_send(f"✅ Моніторинг запущено — {TF_ENTRY}\nСетапи по BTC/ETH/BNB/SOL/TON")

    # окремий потік під щоденну статистику
    t = threading.Thread(target=daily_report_loop, args=(state,), daemon=True)
    t.start()

    while True:
        try:
            for sym in SYMBOLS:
                sig = make_setup(sym)
                if sig and can_send(state, sig):
                    tg_send(format_message(sig))
                    mark_sent(state, sig)
                    save_state(state)
                time.sleep(0.25)  # мікропаузи, щоб не “лупити”
        except Exception as e:
            print("scanner error:", e)

        time.sleep(CHECK_EVERY_SEC)


if __name__ == "__main__":
    scanner_loop()
