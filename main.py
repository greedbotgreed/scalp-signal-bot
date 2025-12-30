import os
import time
import json
import math
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional, Any, Tuple

import requests

# =========================
# CONFIG
# =========================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("CHAT_ID", "").strip()  # channel id like -100...
TZ_OFFSET_HOURS = int(os.environ.get("TZ_OFFSET_HOURS", "2"))  # Ukraine +2 (winter). Change if needed.

# Bybit public endpoints (no keys)
BYBIT_BASE = "https://api.bybit.com"

# Market universe (Bybit symbols)
SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "TONUSDT"]

# Timeframes
TF = "15"  # 15m klines
CHECK_EVERY_SEC = 60  # scan once per minute

# Indicators
EMA_FAST = 20
EMA_SLOW = 50
RSI_LEN = 14

# Signal shaping (simple + readable)
MIN_EMA_GAP_PCT = 0.0015  # 0.15% between EMA20 and EMA50 to avoid noise
RSI_LONG_MIN = 35
RSI_SHORT_MAX = 65

# Risk template (for "setup", not an order)
SL_ATR_MULT = 1.2
TP_RR = 1.6  # takeprofit approx RR
ATR_LEN = 14

# Anti-spam / cooldown
COOLDOWN_MIN = 90  # per symbol+side cooldown
STATE_FILE = "state.json"

# Daily stats time (local, by TZ offset)
DAILY_STATS_HOUR = 21
DAILY_STATS_MINUTE = 0

# Request hardening
HTTP_TIMEOUT = 12
RETRY_SLEEP = 2.0

# =========================
# TELEGRAM
# =========================
def tg_send(text: str) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        print("Missing BOT_TOKEN or CHAT_ID env vars")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(url, json=payload, timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            print("Telegram error:", r.status_code, r.text[:300])
    except Exception as e:
        print("Telegram exception:", e)

# =========================
# STATE
# =========================
def load_state() -> Dict[str, Any]:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {
            "last_sent": {},     # key -> unix ts
            "signals": [],       # list of signals (for stats)
            "daily": {"last_stats_date": ""}  # YYYY-MM-DD
        }

def save_state(state: Dict[str, Any]) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("save_state error:", e)

def cooldown_ok(state: Dict[str, Any], key: str, now_ts: int) -> bool:
    last = int(state.get("last_sent", {}).get(key, 0))
    return (now_ts - last) >= (COOLDOWN_MIN * 60)

def mark_sent(state: Dict[str, Any], key: str, now_ts: int) -> None:
    state.setdefault("last_sent", {})[key] = now_ts

# =========================
# BYBIT DATA
# =========================
def bybit_klines(symbol: str, interval: str, limit: int = 200) -> List[Dict[str, float]]:
    """
    Returns list of candles oldest->newest.
    Bybit v5 market/kline returns:
    list: [ [start, open, high, low, close, volume, turnover], ... ]
    start is ms.
    """
    url = f"{BYBIT_BASE}/v5/market/kline"
    params = {
        "category": "linear",
        "symbol": symbol,
        "interval": interval,
        "limit": str(limit),
    }
    for _ in range(3):
        try:
            r = requests.get(url, params=params, timeout=HTTP_TIMEOUT)
            if r.status_code != 200:
                print("Bybit HTTP error", r.status_code, r.text[:200])
                time.sleep(RETRY_SLEEP)
                continue
            data = r.json()
            if data.get("retCode") != 0:
                print("Bybit retCode error", data.get("retCode"), data.get("retMsg"))
                time.sleep(RETRY_SLEEP)
                continue
            raw = data["result"]["list"]
            candles = []
            # raw is newest->oldest; convert to oldest->newest
            for row in reversed(raw):
                candles.append({
                    "t": int(row[0]),
                    "o": float(row[1]),
                    "h": float(row[2]),
                    "l": float(row[3]),
                    "c": float(row[4]),
                    "v": float(row[5]),
                })
            return candles
        except Exception as e:
            print("Bybit exception:", e)
            time.sleep(RETRY_SLEEP)
    return []

# =========================
# INDICATORS
# =========================
def ema(values: List[float], length: int) -> List[float]:
    if len(values) < length:
        return []
    k = 2 / (length + 1)
    out = [values[0]]
    for x in values[1:]:
        out.append(out[-1] + k * (x - out[-1]))
    return out

def rsi(values: List[float], length: int) -> List[float]:
    if len(values) < length + 1:
        return []
    gains, losses = [], []
    for i in range(1, len(values)):
        d = values[i] - values[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    # Wilder smoothing
    avg_gain = sum(gains[:length]) / length
    avg_loss = sum(losses[:length]) / length
    out = [50.0] * (length)  # padding for alignment
    for i in range(length, len(gains)):
        avg_gain = (avg_gain * (length - 1) + gains[i]) / length
        avg_loss = (avg_loss * (length - 1) + losses[i]) / length
        rs = (avg_gain / avg_loss) if avg_loss > 0 else 999999.0
        out.append(100 - (100 / (1 + rs)))
    return out

def true_range(c_prev: float, h: float, l: float) -> float:
    return max(h - l, abs(h - c_prev), abs(l - c_prev))

def atr(candles: List[Dict[str, float]], length: int) -> Optional[float]:
    if len(candles) < length + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        trs.append(true_range(candles[i - 1]["c"], candles[i]["h"], candles[i]["l"]))
    # simple ATR is fine for MVP
    return sum(trs[-length:]) / length

# =========================
# SETUP LOGIC
# =========================
def compute_setup(symbol: str) -> Optional[Dict[str, Any]]:
    candles = bybit_klines(symbol, TF, limit=250)
    if len(candles) < 120:
        return None

    closes = [c["c"] for c in candles]
    ema_fast = ema(closes, EMA_FAST)
    ema_slow = ema(closes, EMA_SLOW)
    r = rsi(closes, RSI_LEN)
    a = atr(candles, ATR_LEN)

    if not ema_fast or not ema_slow or not r or a is None:
        return None

    # align indices: ema outputs full length (starting from first), rsi padded;
    # use last values safely
    price = closes[-1]
    ef = ema_fast[-1]
    es = ema_slow[-1]
    rv = r[-1]

    gap_pct = abs(ef - es) / price

    # Trend filter + "pullback to EMA50"
    # LONG: EMA20 > EMA50, price near EMA50, RSI recovering
    # SHORT: EMA20 < EMA50, price near EMA50, RSI cooling
    near_ema50 = abs(price - es) / price <= 0.0025  # within 0.25%

    if gap_pct < MIN_EMA_GAP_PCT or not near_ema50:
        return None

    side = None
    reason = ""

    if ef > es and rv >= RSI_LONG_MIN and rv <= 60:
        side = "LONG"
        reason = f"тренд вгору (EMA{EMA_FAST}>{EMA_SLOW}), відкат до EMA{EMA_SLOW}, RSI {rv:.0f}"
    elif ef < es and rv <= RSI_SHORT_MAX and rv >= 40:
        side = "SHORT"
        reason = f"тренд вниз (EMA{EMA_FAST}<{EMA_SLOW}), відкат до EMA{EMA_SLOW}, RSI {rv:.0f}"
    else:
        return None

    # Risk levels from ATR
    # Use entry = current price (setup-level), SL/TP from ATR
    entry = price
    sl_dist = a * SL_ATR_MULT
    if side == "LONG":
        sl = entry - sl_dist
        tp = entry + sl_dist * TP_RR
    else:
        sl = entry + sl_dist
        tp = entry - sl_dist * TP_RR

    # Round nicely
    def r6(x: float) -> float:
        if x == 0:
            return 0.0
        # dynamic rounding
        p = max(0, 6 - int(math.floor(math.log10(abs(x)))) - 1)
        return round(x, min(max(p, 2), 6))

    return {
        "symbol": symbol,
        "tf": TF,
        "side": side,
        "entry": r6(entry),
        "sl": r6(sl),
        "tp": r6(tp),
        "ema_fast": r6(ef),
        "ema_slow": r6(es),
        "rsi": round(rv, 1),
        "reason": reason,
        "ts": int(time.time()),
        "t_ms": candles[-1]["t"],
    }

def format_setup(sig: Dict[str, Any]) -> str:
    # IMPORTANT: no "bot", no "render", no exchange naming.
    sym = sig["symbol"]
    side = "🟢 LONG" if sig["side"] == "LONG" else "🔴 SHORT"
    tf = sig["tf"]

    # Make it clear: it's a setup, and what is "confirmation"
    # Use a single confirmation rule:
    # LONG: close above EMA20 on 15m
    # SHORT: close below EMA20 on 15m
    confirm = "закріплення 15m вище EMA20" if sig["side"] == "LONG" else "закріплення 15m нижче EMA20"

    text = (
        f"⚡️ Сетап {sym} | {tf}m\n"
        f"{side}\n\n"
        f"Entry: {sig['entry']}\n"
        f"SL: {sig['sl']}\n"
        f"TP: {sig['tp']}\n\n"
        f"Умова підтвердження: {confirm}\n"
        f"Контекст: {sig['reason']}\n"
    )
    return text

# =========================
# STATS EVALUATION
# =========================
def local_now() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=TZ_OFFSET_HOURS)

def yyyy_mm_dd(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")

def eval_signal_hit(sig: Dict[str, Any]) -> str:
    """
    Evaluate signal outcome since its timestamp:
    WIN if TP touched at least once.
    LOSS if SL touched at least once before TP (best-effort with candles).
    OPEN if neither touched.
    """
    symbol = sig["symbol"]
    side = sig["side"]
    entry_ts = int(sig.get("t_ms", 0))  # candle start ms
    tp = float(sig["tp"])
    sl = float(sig["sl"])

    # fetch recent klines; Bybit doesn't accept startTime in this endpoint easily for all cases,
    # so we pull a chunk and filter by time.
    candles = bybit_klines(symbol, TF, limit=200)
    if not candles:
        return "OPEN"

    relevant = [c for c in candles if c["t"] >= entry_ts]
    if not relevant:
        relevant = candles[-80:]

    for c in relevant:
        hi = c["h"]
        lo = c["l"]
        # In same candle, if both touched, treat as LOSS (conservative)
        if side == "LONG":
            tp_hit = hi >= tp
            sl_hit = lo <= sl
            if tp_hit and sl_hit:
                return "LOSS"
            if sl_hit:
                return "LOSS"
            if tp_hit:
                return "WIN"
        else:
            tp_hit = lo <= tp
            sl_hit = hi >= sl
            if tp_hit and sl_hit:
                return "LOSS"
            if sl_hit:
                return "LOSS"
            if tp_hit:
                return "WIN"

    return "OPEN"

def send_daily_stats(state: Dict[str, Any]) -> None:
    today = yyyy_mm_dd(local_now())
    last = state.get("daily", {}).get("last_stats_date", "")
    if last == today:
        return

    # only send after 21:00 local
    now = local_now()
    if (now.hour, now.minute) < (DAILY_STATS_HOUR, DAILY_STATS_MINUTE):
        return

    # signals within "today" local window
    start_local = datetime(now.year, now.month, now.day, 0, 0, tzinfo=timezone.utc) - timedelta(hours=TZ_OFFSET_HOURS)
    end_local = start_local + timedelta(days=1)
    start_ts = int(start_local.timestamp())
    end_ts = int(end_local.timestamp())

    sigs = state.get("signals", [])
    todays = [s for s in sigs if start_ts <= int(s.get("ts", 0)) < end_ts]

    wins = 0
    losses = 0
    open_ = 0
    for s in todays:
        res = eval_signal_hit(s)
        s["result"] = res
        if res == "WIN":
            wins += 1
        elif res == "LOSS":
            losses += 1
        else:
            open_ += 1

    total = len(todays)
    if total == 0:
        text = (
            "📊 Підсумок дня (21:00)\n"
            "Сьогодні сетапів не було.\n"
            "Завтра продовжимо моніторинг."
        )
    else:
        text = (
            "📊 Підсумок дня (21:00)\n"
            f"Сетапів: {total}\n"
            f"✅ TP торкалось: {wins}\n"
            f"❌ SL торкалось: {losses}\n"
            f"⏳ В роботі: {open_}\n\n"
            "Критерій: якщо ціна хоч раз торкнулась TP/SL — зараховано."
        )

    tg_send(text)
    state.setdefault("daily", {})["last_stats_date"] = today

# =========================
# MAIN LOOP
# =========================
def announce_start() -> None:
    coins = "/".join([s.replace("USDT", "") for s in SYMBOLS])
    tf = f"{TF}m"
    # No "bot", no platform naming
    tg_send(f"✅ Моніторинг активовано — {tf} сетапи по {coins}")

def scanner_loop() -> None:
    state = load_state()
    announce_start()

    while True:
        now_ts = int(time.time())

        # daily stats
        try:
            send_daily_stats(state)
        except Exception as e:
            print("daily stats error:", e)

        # scan symbols
        for sym in SYMBOLS:
            try:
                sig = compute_setup(sym)
                if not sig:
                    continue

                key = f"{sym}:{sig['side']}:{TF}"
                if not cooldown_ok(state, key, now_ts):
                    continue

                # send setup
                tg_send(format_setup(sig))

                # store for stats
                state.setdefault("signals", []).append(sig)
                mark_sent(state, key, now_ts)
                save_state(state)

                time.sleep(0.25)
            except Exception as e:
                print("scan error:", sym, e)

        time.sleep(CHECK_EVERY_SEC)

if __name__ == "__main__":
    scanner_loop()
