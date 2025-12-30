import os
import time
import json
import math
import threading
from typing import List, Dict, Optional

import requests

# =========================
# CONFIG
# =========================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("CHAT_ID", "").strip()

# Binance Futures public REST
BASE_URL = "https://fapi.binance.com"

SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "TONUSDT"]

# Timeframes
TF_TREND = "1h"   # trend filter
TF_ENTRY = "15m"  # main entry timeframe

# Indicators
EMA_FAST = 20
EMA_SLOW = 50
RSI_LEN = 14

# SL swing lookback on entry TF
SWING_LOOKBACK = 12  # candles
SL_BUFFER_PCT = 0.0015  # 0.15%

# Loop / anti-spam
CHECK_EVERY_SEC = 60
COOLDOWN_SEC = 6 * 60 * 60  # 6 hours per symbol+direction
STATE_FILE = "state.json"

# Alert limiting (soft cap)
MAX_ALERTS_PER_DAY = 8  # safety cap
DAY_KEY = "day_count_utc"

# =========================
# Telegram
# =========================
def tg_send(text: str) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        print("Missing BOT_TOKEN or CHAT_ID")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "disable_web_page_preview": True
    }
    r = requests.post(url, json=payload, timeout=20)
    if r.status_code != 200:
        print("tg_send error:", r.status_code, r.text)

# =========================
# Data helpers
# =========================
def fetch_klines(symbol: str, interval: str, limit: int = 200) -> List[Dict]:
    url = f"{BASE_URL}/fapi/v1/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    raw = r.json()
    out = []
    for k in raw:
        out.append({
            "open_time": int(k[0]),
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
            "close_time": int(k[6]),
        })
    return out

def ema(values: List[float], length: int) -> List[float]:
    if len(values) < length:
        return []
    k = 2 / (length + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(out[-1] + k * (v - out[-1]))
    return out

def rsi(closes: List[float], length: int) -> List[float]:
    if len(closes) < length + 1:
        return []
    gains = []
    losses = []
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        gains.append(max(0.0, ch))
        losses.append(max(0.0, -ch))

    # Wilder smoothing
    avg_gain = sum(gains[:length]) / length
    avg_loss = sum(losses[:length]) / length

    out = [50.0] * (length)  # pad
    for i in range(length, len(gains)):
        avg_gain = (avg_gain * (length - 1) + gains[i]) / length
        avg_loss = (avg_loss * (length - 1) + losses[i]) / length
        if avg_loss == 0:
            out.append(100.0)
        else:
            rs = avg_gain / avg_loss
            out.append(100.0 - (100.0 / (1.0 + rs)))
    # align length with closes
    while len(out) < len(closes):
        out.insert(0, 50.0)
    return out[-len(closes):]

def fmt_price(x: float, symbol: str) -> str:
    # simple formatting by typical price magnitude
    if symbol.startswith("BTC"):
        return f"{x:,.1f}"
    if x >= 100:
        return f"{x:,.2f}"
    if x >= 1:
        return f"{x:,.4f}"
    return f"{x:,.6f}"

# =========================
# State (cooldown / daily cap)
# =========================
def load_state() -> Dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(state: Dict) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception as e:
        print("save_state error:", e)

def utc_day() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())

def can_alert_today(state: Dict) -> bool:
    day = utc_day()
    if state.get(DAY_KEY) != day:
        state[DAY_KEY] = day
        state["alerts_today"] = 0
    return state.get("alerts_today", 0) < MAX_ALERTS_PER_DAY

def inc_alert_today(state: Dict) -> None:
    state["alerts_today"] = int(state.get("alerts_today", 0)) + 1

def cooldown_key(symbol: str, direction: str) -> str:
    return f"cooldown::{symbol}::{direction}"

def in_cooldown(state: Dict, symbol: str, direction: str) -> bool:
    k = cooldown_key(symbol, direction)
    last = float(state.get(k, 0))
    return (time.time() - last) < COOLDOWN_SEC

def mark_cooldown(state: Dict, symbol: str, direction: str) -> None:
    state[cooldown_key(symbol, direction)] = time.time()

# =========================
# Strategy
# =========================
def compute_setup(symbol: str) -> Optional[Dict]:
    # fetch data
    trend = fetch_klines(symbol, TF_TREND, 200)
    entry = fetch_klines(symbol, TF_ENTRY, 200)

    trend_closes = [c["close"] for c in trend]
    entry_closes = [c["close"] for c in entry]
    entry_highs = [c["high"] for c in entry]
    entry_lows = [c["low"] for c in entry]

    ema_fast_tr = ema(trend_closes, EMA_FAST)
    ema_slow_tr = ema(trend_closes, EMA_SLOW)

    ema_fast_en = ema(entry_closes, EMA_FAST)
    ema_slow_en = ema(entry_closes, EMA_SLOW)
    rsi_en = rsi(entry_closes, RSI_LEN)

    if not ema_fast_tr or not ema_slow_tr or not ema_fast_en or not ema_slow_en or not rsi_en:
        return None

    # current values
    tr_fast = ema_fast_tr[-1]
    tr_slow = ema_slow_tr[-1]

    en_fast = ema_fast_en[-1]
    en_slow = ema_slow_en[-1]
    r0 = rsi_en[-1]
    r1 = rsi_en[-2] if len(rsi_en) >= 2 else r0

    last_close = entry_closes[-1]
    last_low = entry_lows[-1]
    last_high = entry_highs[-1]

    # trend direction
    if tr_fast > tr_slow:
        direction = "LONG"
    elif tr_fast < tr_slow:
        direction = "SHORT"
    else:
        return None

    # distance to EMA50 (entry timeframe)
    dist_to_ema50 = abs(last_close - en_slow) / en_slow

    # We want "near EMA50" pullback. 0.35% default.
    near_ema = dist_to_ema50 <= 0.0035

    # RSI "turn"
    # LONG: RSI rising and coming out of low zone
    # SHORT: RSI falling and coming down from high zone
    if direction == "LONG":
        rsi_ok = (r0 > r1) and (r0 >= 32) and (r1 <= 35)
    else:
        rsi_ok = (r0 < r1) and (r0 <= 68) and (r1 >= 65)

    # structure filter: price not totally against EMA20 on entry TF
    if direction == "LONG":
        struct_ok = last_close >= en_fast * 0.995
    else:
        struct_ok = last_close <= en_fast * 1.005

    if not (near_ema and rsi_ok and struct_ok):
        return None

    # SL by swing
    if len(entry_lows) < SWING_LOOKBACK + 2:
        return None

    if direction == "LONG":
        swing = min(entry_lows[-(SWING_LOOKBACK + 1):-1])
        sl = swing * (1 - SL_BUFFER_PCT)
        entry_zone = (en_slow * 0.999, en_slow * 1.001)  # tight zone around EMA50
        entry_ref = last_close
        risk = max(entry_ref - sl, entry_ref * 0.002)  # avoid tiny risk
        tp1 = entry_ref + risk * 1.0
        tp2 = entry_ref + risk * 2.0
        invalid = f"Сценарій скасовано, якщо ціна закріпиться нижче {fmt_price(sl, symbol)}"
    else:
        swing = max(entry_highs[-(SWING_LOOKBACK + 1):-1])
        sl = swing * (1 + SL_BUFFER_PCT)
        entry_zone = (en_slow * 0.999, en_slow * 1.001)
        entry_ref = last_close
        risk = max(sl - entry_ref, entry_ref * 0.002)
        tp1 = entry_ref - risk * 1.0
        tp2 = entry_ref - risk * 2.0
        invalid = f"Сценарій скасовано, якщо ціна закріпиться вище {fmt_price(sl, symbol)}"

    return {
        "symbol": symbol,
        "direction": direction,
        "tf": TF_ENTRY,
        "trend_tf": TF_TREND,
        "price": entry_ref,
        "ema50": en_slow,
        "rsi": r0,
        "entry_zone": entry_zone,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "invalid": invalid,
        "ts": int(time.time()),
    }

def format_signal(sig: Dict) -> str:
    sym = sig["symbol"]
    direction = "LONG" if sig["direction"] == "LONG" else "SHORT"

    ez1, ez2 = sig["entry_zone"]
    price = sig["price"]

    # One-line EN (optional). Keep it minimal.
    en_hint = "(Plan: entry zone → SL → TP1/TP2)"

    text = (
        f"🟡 {sym} | {sig['tf']} | {direction}\n"
        f"Ціна: {fmt_price(price, sym)}\n\n"
        f"План (зона входу): {fmt_price(min(ez1, ez2), sym)} – {fmt_price(max(ez1, ez2), sym)}\n"
        f"SL: {fmt_price(sig['sl'], sym)}\n"
        f"TP1: {fmt_price(sig['tp1'], sym)}\n"
        f"TP2: {fmt_price(sig['tp2'], sym)}\n\n"
        f"{sig['invalid']}\n"
        f"{en_hint}"
    )
    return text

# =========================
# Scanner loop
# =========================
def scanner_loop():
    state = load_state()

    # Start message (no "bot/render" words)
    tg_send("✅ Моніторинг запущено — 15m сетапи по BTC/ETH/BNB/SOL/TON")

    while True:
        try:
            # reset daily counter
            can_alert_today(state)

            for sym in SYMBOLS:
                sig = compute_setup(sym)
                if not sig:
                    time.sleep(0.25)
                    continue

                direction = sig["direction"]
                if in_cooldown(state, sym, direction):
                    time.sleep(0.25)
                    continue

                if not can_alert_today(state):
                    # daily cap reached
                    break

                tg_send(format_signal(sig))
                mark_cooldown(state, sym, direction)
                inc_alert_today(state)
                save_state(state)

                time.sleep(0.5)  # small spacing between sends

        except Exception as e:
            print("scanner error:", e)

        time.sleep(CHECK_EVERY_SEC)

if __name__ == "__main__":
    # run scanner in foreground (Background Worker is fine)
    scanner_loop()
