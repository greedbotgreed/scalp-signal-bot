import os
import time
import json
import threading
from typing import List, Dict, Tuple, Optional

import requests
from flask import Flask

# -----------------------------
# CONFIG
# -----------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("CHAT_ID", "").strip()  # для приватного чату - це ОК (твій chat_id)
PORT = int(os.environ.get("PORT", "10000"))

SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "TONUSDT"]
BASE_URL = "https://fapi.binance.com"  # Binance Futures public API

# Таймфрейми
TF_TREND = "4h"   # фільтр тренду
TF_ENTRY = "1h"   # тригер входу

# Індикатори
EMA_FAST = 20
EMA_SLOW = 50
RSI_LEN = 14

# Свінг для SL (по 1H)
SWING_LOOKBACK = 12
SL_BUFFER_PCT = 0.0015  # 0.15% буфер

# Як часто перевіряти (сек)
CHECK_EVERY_SEC = 60

# Щоб не спамив однаковими сигналами
COOLDOWN_SEC = 6 * 60 * 60  # 6 годин
STATE_FILE = "state.json"


# -----------------------------
# UTIL: Telegram
# -----------------------------
def tg_send(text: str) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        print("BOT_TOKEN/CHAT_ID not set")
        return

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text}
    try:
        r = requests.post(url, json=payload, timeout=20)
        if r.status_code != 200:
            print("Telegram error:", r.status_code, r.text[:500])
    except Exception as e:
        print("Telegram exception:", e)


# -----------------------------
# UTIL: Binance klines
# -----------------------------
def fetch_klines(symbol: str, interval: str, limit: int = 200) -> Optional[List[List]]:
    """
    Returns raw kline list:
    [
      [
        open_time, open, high, low, close, volume,
        close_time, quote_asset_volume, number_of_trades,
        taker_buy_base, taker_buy_quote, ignore
      ], ...
    ]
    """
    url = f"{BASE_URL}/fapi/v1/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    try:
        r = requests.get(url, params=params, timeout=20)
        if r.status_code != 200:
            print(f"Binance error {symbol} {interval}:", r.status_code, r.text[:300])
            return None
        return r.json()
    except Exception as e:
        print(f"Binance exception {symbol} {interval}:", e)
        return None


def closes_from_klines(klines: List[List]) -> List[float]:
    return [float(k[4]) for k in klines]


def highs_lows_from_klines(klines: List[List]) -> Tuple[List[float], List[float]]:
    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]
    return highs, lows


def last_closed_candle_time(klines: List[List]) -> int:
    # close_time мс останньої свічки
    return int(klines[-1][6])


# -----------------------------
# INDICATORS (no pandas)
# -----------------------------
def ema(series: List[float], length: int) -> List[float]:
    if len(series) < length:
        return []
    k = 2 / (length + 1)
    out = []
    # старт: SMA
    sma = sum(series[:length]) / length
    out.append(sma)
    prev = sma
    for price in series[length:]:
        cur = price * k + prev * (1 - k)
        out.append(cur)
        prev = cur
    # вирівняємо довжину до series (перед EMA значення None не треба — просто зсуваємо індекс)
    # out відповідає позиціям series[length-1:]
    return out


def rsi(series: List[float], length: int = 14) -> List[float]:
    if len(series) < length + 1:
        return []
    gains = []
    losses = []
    for i in range(1, length + 1):
        ch = series[i] - series[i - 1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    avg_gain = sum(gains) / length
    avg_loss = sum(losses) / length

    out = []
    # перше RSI відповідає series[length]
    rs = (avg_gain / avg_loss) if avg_loss != 0 else 999999
    out.append(100 - (100 / (1 + rs)))

    for i in range(length + 1, len(series)):
        ch = series[i] - series[i - 1]
        gain = max(ch, 0.0)
        loss = max(-ch, 0.0)
        avg_gain = (avg_gain * (length - 1) + gain) / length
        avg_loss = (avg_loss * (length - 1) + loss) / length
        rs = (avg_gain / avg_loss) if avg_loss != 0 else 999999
        out.append(100 - (100 / (1 + rs)))
    return out


def crossed_above(prev_a: float, a: float, prev_b: float, b: float) -> bool:
    return prev_a <= prev_b and a > b


def crossed_below(prev_a: float, a: float, prev_b: float, b: float) -> bool:
    return prev_a >= prev_b and a < b


# -----------------------------
# SIGNAL LOGIC
# -----------------------------
def compute_setup(symbol: str) -> Optional[Dict]:
    """
    Trend filter on 4H:
      LONG  if EMA20 > EMA50 and RSI > 50
      SHORT if EMA20 < EMA50 and RSI < 50

    Entry trigger on 1H:
      LONG  if close crosses ABOVE EMA20 and RSI crosses above 50 (soft)
      SHORT if close crosses BELOW EMA20 and RSI crosses below 50

    SL: swing low/high on last SWING_LOOKBACK candles (1H) + buffer
    TP1/TP2: 1R and 2R
    """
    k4 = fetch_klines(symbol, TF_TREND, 200)
    k1 = fetch_klines(symbol, TF_ENTRY, 200)
    if not k4 or not k1:
        return None

    c4 = closes_from_klines(k4)
    c1 = closes_from_klines(k1)
    h1, l1 = highs_lows_from_klines(k1)

    ema4_fast = ema(c4, EMA_FAST)
    ema4_slow = ema(c4, EMA_SLOW)
    rsi4 = rsi(c4, RSI_LEN)

    ema1_fast = ema(c1, EMA_FAST)
    rsi1 = rsi(c1, RSI_LEN)

    if not ema4_fast or not ema4_slow or not rsi4 or not ema1_fast or not rsi1:
        return None

    # Останні значення 4H
    ema4f = ema4_fast[-1]
    ema4s = ema4_slow[-1]
    r4 = rsi4[-1]

    trend_long = (ema4f > ema4s) and (r4 >= 50)
    trend_short = (ema4f < ema4s) and (r4 <= 50)

    # Для кросу на 1H беремо останні 2 точки
    # ema1_fast відповідає c1[EMA_FAST-1:]
    # rsi1 відповідає c1[RSI_LEN:]
    # Тому беремо "вирівняні" індекси через останні значення
    close_prev, close_now = c1[-2], c1[-1]

    ema1_now = ema1_fast[-1]
    ema1_prev = ema1_fast[-2] if len(ema1_fast) >= 2 else ema1_fast[-1]

    rsi1_now = rsi1[-1]
    rsi1_prev = rsi1[-2] if len(rsi1) >= 2 else rsi1[-1]

    entry_long = crossed_above(close_prev, close_now, ema1_prev, ema1_now) and (rsi1_prev <= 50 and rsi1_now > 50)
    entry_short = crossed_below(close_prev, close_now, ema1_prev, ema1_now) and (rsi1_prev >= 50 and rsi1_now < 50)

    direction = None
    if trend_long and entry_long:
        direction = "LONG"
    elif trend_short and entry_short:
        direction = "SHORT"
    else:
        return None

    entry = close_now

    # SL по свінгу (1H)
    look = min(SWING_LOOKBACK, len(l1) - 2)
    if look < 3:
        return None

    recent_lows = l1[-look-1:-1]
    recent_highs = h1[-look-1:-1]

    if direction == "LONG":
        swing = min(recent_lows)
        sl = swing * (1 - SL_BUFFER_PCT)
        risk = entry - sl
        if risk <= 0:
            return None
        tp1 = entry + 1.0 * risk
        tp2 = entry + 2.0 * risk
    else:
        swing = max(recent_highs)
        sl = swing * (1 + SL_BUFFER_PCT)
        risk = sl - entry
        if risk <= 0:
            return None
        tp1 = entry - 1.0 * risk
        tp2 = entry - 2.0 * risk

    return {
        "symbol": symbol,
        "direction": direction,
        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "tf": f"{TF_ENTRY}/{TF_TREND}",
        "candle_close_time": last_closed_candle_time(k1),  # контроль дублю
        "rsi4": r4,
        "ema4_fast": ema4f,
        "ema4_slow": ema4s,
        "rsi1": rsi1_now,
        "ema1": ema1_now,
    }


# -----------------------------
# STATE
# -----------------------------
def load_state() -> Dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"last_sent": {}}


def save_state(state: Dict) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception as e:
        print("save_state error:", e)


def should_send(state: Dict, sig: Dict) -> bool:
    """
    Block duplicates:
      - same symbol+direction+same candle_close_time
      - or within cooldown
    """
    key = f"{sig['symbol']}|{sig['direction']}"
    last = state.get("last_sent", {}).get(key)
    now = int(time.time())

    if not last:
        return True

    # last: {"ts":..., "candle":...}
    if last.get("candle") == sig["candle_close_time"]:
        return False
    if now - int(last.get("ts", 0)) < COOLDOWN_SEC:
        return False
    return True


def mark_sent(state: Dict, sig: Dict) -> None:
    key = f"{sig['symbol']}|{sig['direction']}"
    state.setdefault("last_sent", {})[key] = {
        "ts": int(time.time()),
        "candle": sig["candle_close_time"],
    }


def format_sig(sig: Dict) -> str:
    return (
        f"📣 SIGNAL ({sig['tf']})\n"
        f"{sig['symbol']} — {sig['direction']}\n\n"
        f"Entry: {sig['entry']:.4f}\n"
        f"SL: {sig['sl']:.4f}\n"
        f"TP1: {sig['tp1']:.4f}\n"
        f"TP2: {sig['tp2']:.4f}\n\n"
        f"Filters:\n"
        f"4H EMA{EMA_FAST}/{EMA_SLOW}: {sig['ema4_fast']:.4f} / {sig['ema4_slow']:.4f}\n"
        f"4H RSI{RSI_LEN}: {sig['rsi4']:.2f}\n"
        f"1H EMA{EMA_FAST}: {sig['ema1']:.4f}\n"
        f"1H RSI{RSI_LEN}: {sig['rsi1']:.2f}\n"
    )


# -----------------------------
# SCANNER LOOP
# -----------------------------
def scanner_loop():
    state = load_state()
    tg_send("✅ Bot online (Render) — scanning 1H/4H for BTC/ETH/BNB/SOL/TON")

    while True:
        try:
            for sym in SYMBOLS:
                sig = compute_setup(sym)
                if sig and should_send(state, sig):
                    tg_send(format_sig(sig))
                    mark_sent(state, sig)
                    save_state(state)
                time.sleep(0.25)  # мікропаузи щоб не душити API
        except Exception as e:
            print("scanner error:", e)

        time.sleep(CHECK_EVERY_SEC)


# -----------------------------
# HEALTH SERVER (for Render Web Service)
# -----------------------------
app = Flask(__name__)

@app.get("/")
def home():
    return "OK", 200

@app.get("/health")
def health():
    return "OK", 200


if __name__ == "__main__":
    # стартуємо сканер в фоні
    t = threading.Thread(target=scanner_loop, daemon=True)
    t.start()

    # і відкриваємо порт щоб Render не писав "No open ports detected"
    app.run(host="0.0.0.0", port=PORT)
