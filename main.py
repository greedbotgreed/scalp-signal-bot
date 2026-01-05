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
ATR_LEN = 14

# Signal shaping
MIN_EMA_GAP_PCT = 0.0015  # 0.15% between EMA20 and EMA50 to avoid noise
RSI_LONG_MIN = 35
RSI_SHORT_MAX = 65

# Pullback rule
NEAR_EMA50_PCT = 0.0025  # within 0.25%

# Risk (ATR-based SL) and targets by R
SL_ATR_MULT = 1.2
TP1_R = 1.0
TP2_R = 2.0
TP3_R = 3.0

# Pending confirmation
PENDING_TTL_MIN = 60  # setup expires if not confirmed within 60 minutes

# Anti-spam / cooldown
COOLDOWN_MIN = 90  # per symbol+side cooldown for SETUP READY
STATE_FILE = "state.json"

# Daily stats time (local, by TZ offset)
DAILY_STATS_HOUR = 21
DAILY_STATS_MINUTE = 0

# Outcome evaluation TF (more accurate than 15m)
EVAL_TF = "1"  # 1m

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
            s = json.load(f)
            # migrate if older
            s.setdefault("last_sent", {})
            s.setdefault("pending", {})   # setup ready waiting confirm
            s.setdefault("trades", [])    # confirmed trades for stats
            s.setdefault("setups", [])    # for counting set-ups/day
            s.setdefault("daily", {"last_stats_date": ""})
            return s
    except Exception:
        return {
            "last_sent": {},
            "pending": {},
            "trades": [],
            "setups": [],
            "daily": {"last_stats_date": ""}
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
    return sum(trs[-length:]) / length

def round_nice(x: float) -> float:
    if x == 0:
        return 0.0
    p = max(0, 6 - int(math.floor(math.log10(abs(x)))) - 1)
    return round(x, min(max(p, 2), 6))

# =========================
# CONFIRM HELPERS
# =========================
def last_closed_candle(symbol: str, interval: str) -> Optional[Dict[str, float]]:
    candles = bybit_klines(symbol, interval, limit=3)
    if len(candles) < 3:
        return None
    return candles[-2]  # previous candle is closed

def ema_at_closed(symbol: str, interval: str, length: int) -> Optional[float]:
    candles = bybit_klines(symbol, interval, limit=250)
    if len(candles) < 120:
        return None
    closes = [c["c"] for c in candles]
    e = ema(closes, length)
    if not e or len(e) < 3:
        return None
    return float(e[-2])  # EMA value at closed candle

def is_confirmed(sig: Dict[str, Any]) -> bool:
    c = last_closed_candle(sig["symbol"], sig["tf"])
    if not c:
        return False
    ef = ema_at_closed(sig["symbol"], sig["tf"], EMA_FAST)
    if ef is None:
        return False
    close = float(c["c"])
    if sig["side"] == "LONG":
        return close > ef
    else:
        return close < ef

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

    price = float(closes[-1])
    ef = float(ema_fast[-1])
    es = float(ema_slow[-1])
    rv = float(r[-1])

    gap_pct = abs(ef - es) / price
    near_ema50 = abs(price - es) / price <= NEAR_EMA50_PCT

    if gap_pct < MIN_EMA_GAP_PCT or not near_ema50:
        return None

    side = None
    reason = ""

    # LONG
    if ef > es and (RSI_LONG_MIN <= rv <= 60):
        side = "LONG"
        reason = f"тренд вгору (EMA{EMA_FAST}>{EMA_SLOW}), відкат до EMA{EMA_SLOW}, RSI {rv:.0f}"
    # SHORT
    elif ef < es and (40 <= rv <= RSI_SHORT_MAX):
        side = "SHORT"
        reason = f"тренд вниз (EMA{EMA_FAST}<{EMA_SLOW}), відкат до EMA{EMA_SLOW}, RSI {rv:.0f}"
    else:
        return None

    # Setup-level entry is current price (orientation).
    entry = price
    sl_dist = a * SL_ATR_MULT

    if side == "LONG":
        sl = entry - sl_dist
    else:
        sl = entry + sl_dist

    return {
        "symbol": symbol,
        "tf": TF,
        "side": side,
        "entry": round_nice(entry),
        "sl": round_nice(sl),
        "ema_fast": round_nice(ef),
        "ema_slow": round_nice(es),
        "rsi": round(rv, 1),
        "reason": reason,
        "ts": int(time.time()),
        "t_ms": int(candles[-1]["t"]),
    }

def build_targets(entry: float, sl: float, side: str) -> Tuple[float, float, float]:
    risk = abs(entry - sl)
    if risk <= 0:
        return entry, entry, entry
    if side == "LONG":
        return (
            entry + risk * TP1_R,
            entry + risk * TP2_R,
            entry + risk * TP3_R
        )
    else:
        return (
            entry - risk * TP1_R,
            entry - risk * TP2_R,
            entry - risk * TP3_R
        )

# =========================
# MESSAGE FORMAT
# =========================
def format_setup_ready(sig: Dict[str, Any]) -> str:
    sym = sig["symbol"]
    side = "🟢 LONG" if sig["side"] == "LONG" else "🔴 SHORT"
    tf = sig["tf"]
    trigger = "15m close вище EMA20" if sig["side"] == "LONG" else "15m close нижче EMA20"
    return (
        f"👀 SETUP READY | {sym} | {tf}m\n"
        f"{side}\n\n"
        f"Орієнтир: {sig['entry']}\n"
        f"SL (орієнтир): {sig['sl']}\n\n"
        f"Тригер: {trigger}\n"
        f"Контекст: {sig['reason']}\n"
    )

def format_entry_confirmed(tr: Dict[str, Any]) -> str:
    sym = tr["symbol"]
    side = "🟢 LONG" if tr["side"] == "LONG" else "🔴 SHORT"
    tf = tr["tf"]
    return (
        f"✅ ENTRY CONFIRMED | {sym} | {tf}m\n"
        f"{side}\n\n"
        f"Entry: {tr['entry']}\n"
        f"SL: {tr['sl']}\n"
        f"TP1: {tr['tp1']}\n"
        f"TP2: {tr['tp2']}\n"
        f"TP3: {tr['tp3']}\n\n"
        f"Ризик: 1–2% (фікс)\n"
        f"Контекст: {tr.get('reason','')}\n"
    )

# =========================
# OUTCOME EVAL (1m)
# =========================
def eval_trade_hit(tr: Dict[str, Any]) -> str:
    symbol = tr["symbol"]
    side = tr["side"]
    start_ms = int(tr.get("confirm_t_ms", 0))

    sl = float(tr["sl"])
    tp1 = float(tr["tp1"])
    tp2 = float(tr["tp2"])
    tp3 = float(tr["tp3"])

    candles = bybit_klines(symbol, EVAL_TF, limit=500)
    if not candles:
        return "OPEN"

    relevant = [c for c in candles if c["t"] >= start_ms]
    if not relevant:
        relevant = candles[-200:]

    for c in relevant:
        hi, lo = c["h"], c["l"]

        # conservative: if both SL and any TP touched in same candle -> SL
        if side == "LONG":
            sl_hit = lo <= sl
            tp1_hit = hi >= tp1
            tp2_hit = hi >= tp2
            tp3_hit = hi >= tp3
            if sl_hit and (tp1_hit or tp2_hit or tp3_hit):
                return "SL"
            if sl_hit:
                return "SL"
            if tp3_hit:
                return "TP3"
            if tp2_hit:
                return "TP2"
            if tp1_hit:
                return "TP1"
        else:
            sl_hit = hi >= sl
            tp1_hit = lo <= tp1
            tp2_hit = lo <= tp2
            tp3_hit = lo <= tp3
            if sl_hit and (tp1_hit or tp2_hit or tp3_hit):
                return "SL"
            if sl_hit:
                return "SL"
            if tp3_hit:
                return "TP3"
            if tp2_hit:
                return "TP2"
            if tp1_hit:
                return "TP1"

    return "OPEN"

# =========================
# TIME HELPERS
# =========================
def local_now() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=TZ_OFFSET_HOURS)

def yyyy_mm_dd(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")

# =========================
# DAILY STATS
# =========================
def send_daily_stats(state: Dict[str, Any]) -> None:
    today = yyyy_mm_dd(local_now())
    last = state.get("daily", {}).get("last_stats_date", "")
    if last == today:
        return

    now = local_now()
    if (now.hour, now.minute) < (DAILY_STATS_HOUR, DAILY_STATS_MINUTE):
        return

    # Local day window
    start_local = datetime(now.year, now.month, now.day, 0, 0, tzinfo=timezone.utc) - timedelta(hours=TZ_OFFSET_HOURS)
    end_local = start_local + timedelta(days=1)
    start_ts = int(start_local.timestamp())
    end_ts = int(end_local.timestamp())

    setups_today = [s for s in state.get("setups", []) if start_ts <= int(s.get("ts", 0)) < end_ts]
    trades_today = [t for t in state.get("trades", []) if start_ts <= int(t.get("ts", 0)) < end_ts]

    setups_found = len(setups_today)
    activated = len(trades_today)

    sl = tp1 = tp2 = tp3 = open_ = 0
    for t in trades_today:
        res = eval_trade_hit(t)
        t["result"] = res
        if res == "SL":
            sl += 1
        elif res == "TP1":
            tp1 += 1
        elif res == "TP2":
            tp2 += 1
        elif res == "TP3":
            tp3 += 1
        else:
            open_ += 1

    if setups_found == 0:
        text = (
            "📊 Daily Report (21:00)\n"
            "Сетапів не було.\n"
            "Завтра продовжимо моніторинг."
        )
    else:
        text = (
            "📊 Daily Report (21:00)\n"
            f"Сетапів знайдено: {setups_found}\n"
            f"✅ Activated (confirmed): {activated}\n\n"
            f"❌ SL: {sl}\n"
            f"✅ TP1: {tp1}\n"
            f"✅ TP2: {tp2}\n"
            f"✅ TP3: {tp3}\n"
            f"⏳ В роботі: {open_}\n\n"
            "У статистику входять тільки ENTRY CONFIRMED."
        )

    tg_send(text)
    state.setdefault("daily", {})["last_stats_date"] = today
    save_state(state)

# =========================
# PENDING → CONFIRMED
# =========================
def process_pending(state: Dict[str, Any]) -> None:
    now_ts = int(time.time())
    pending = state.get("pending", {})
    if not pending:
        return

    to_delete = []
    for pid, sig in list(pending.items()):
        if now_ts > int(sig.get("expires_ts", 0)):
            to_delete.append(pid)
            continue

        if not is_confirmed(sig):
            continue

        # confirmed entry = close of last closed 15m candle
        c = last_closed_candle(sig["symbol"], sig["tf"])
        if not c:
            continue

        entry = float(c["c"])
        sl = float(sig["sl"])

        tp1, tp2, tp3 = build_targets(entry, sl, sig["side"])

        tr = {
            "id": pid,
            "symbol": sig["symbol"],
            "tf": sig["tf"],
            "side": sig["side"],
            "entry": round_nice(entry),
            "sl": round_nice(sl),
            "tp1": round_nice(tp1),
            "tp2": round_nice(tp2),
            "tp3": round_nice(tp3),
            "reason": sig.get("reason", ""),
            "ts": now_ts,              # confirm time
            "confirm_t_ms": int(c["t"]) # start ms of closed candle
        }

        state.setdefault("trades", []).append(tr)
        tg_send(format_entry_confirmed(tr))

        to_delete.append(pid)

    for pid in to_delete:
        pending.pop(pid, None)

    save_state(state)

# =========================
# MAIN LOOP
# =========================
def announce_start() -> None:
    coins = "/".join([s.replace("USDT", "") for s in SYMBOLS])
    tf = f"{TF}m"
    tg_send(f"✅ Моніторинг активовано — {tf} сетапи по {coins}")

def scanner_loop() -> None:
    state = load_state()
    announce_start()

    while True:
        now_ts = int(time.time())

        # 1) process confirmations
        try:
            process_pending(state)
        except Exception as e:
            print("process_pending error:", e)

        # 2) daily stats
        try:
            send_daily_stats(state)
        except Exception as e:
            print("daily stats error:", e)

        # 3) scan for new setups
        for sym in SYMBOLS:
            try:
                sig = compute_setup(sym)
                if not sig:
                    continue

                key = f"{sym}:{sig['side']}:{TF}"
                if not cooldown_ok(state, key, now_ts):
                    continue

                # Unique id based on candle time
                setup_id = f"{sig['symbol']}:{sig['side']}:{sig['t_ms']}"
                sig["id"] = setup_id
                sig["expires_ts"] = now_ts + (PENDING_TTL_MIN * 60)

                # Send SETUP READY
                tg_send(format_setup_ready(sig))

                # Store setup for daily counting
                state.setdefault("setups", []).append({"id": setup_id, "ts": now_ts})

                # Put into pending for confirmation
                state.setdefault("pending", {})[setup_id] = sig

                # Mark cooldown
                mark_sent(state, key, now_ts)
                save_state(state)

                time.sleep(0.25)

            except Exception as e:
                print("scan error:", sym, e)

        time.sleep(CHECK_EVERY_SEC)

if __name__ == "__main__":
    scanner_loop()
