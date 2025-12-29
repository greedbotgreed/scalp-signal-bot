import os
import time
import json
import math
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests


# ----------------------------
# CONFIG (ENV VARS)
# ----------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("CHAT_ID", "").strip()

# Futures symbols to scan (Binance futures uses e.g. BTCUSDT)
SYMBOLS = os.environ.get("SYMBOLS", "BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT,TONUSDT")
SYMBOLS = [s.strip().upper() for s in SYMBOLS.split(",") if s.strip()]

# Timeframes in Binance format: 1h, 4h, 15m, etc.
TIMEFRAMES = os.environ.get("TIMEFRAMES", "1h,4h")
TIMEFRAMES = [t.strip() for t in TIMEFRAMES.split(",") if t.strip()]

# How often to scan (seconds)
SCAN_EVERY_SEC = int(os.environ.get("SCAN_EVERY_SEC", "300"))  # 5 minutes by default

# Indicator params
EMA_FAST = int(os.environ.get("EMA_FAST", "50"))
EMA_SLOW = int(os.environ.get("EMA_SLOW", "200"))
RSI_PERIOD = int(os.environ.get("RSI_PERIOD", "14"))

# Setup thresholds (tune later)
RSI_LONG_MAX = float(os.environ.get("RSI_LONG_MAX", "45"))   # long if RSI <= 45 in uptrend
RSI_SHORT_MIN = float(os.environ.get("RSI_SHORT_MIN", "55")) # short if RSI >= 55 in downtrend

# How many candles to use for swing SL
SWING_LOOKBACK = int(os.environ.get("SWING_LOOKBACK", "20"))

# Dedup state file
STATE_FILE = "state.json"

# Binance Futures public endpoint for klines
BINANCE_FAPI = "https://fapi.binance.com/fapi/v1/klines"


# ----------------------------
# Minimal HTTP server (Render Web Service needs a port open)
# ----------------------------
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/healthz", "/health"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        return  # silence logs


def start_http_server():
    port = int(os.environ.get("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()


# ----------------------------
# Helpers
# ----------------------------
def tg_send(text: str):
    if not BOT_TOKEN or not CHAT_ID:
        print("❌ Missing BOT_TOKEN or CHAT_ID env vars")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": CHAT_ID, "text": text}, timeout=20)
        if r.status_code != 200:
            print("Telegram error:", r.status_code, r.text)
    except Exception as e:
        print("Telegram exception:", e)


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("State save error:", e)


def fetch_klines(symbol: str, interval: str, limit: int = 300):
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    r = requests.get(BINANCE_FAPI, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()

    # kline format:
    # [ open_time, open, high, low, close, volume, close_time, ... ]
    closes = [float(x[4]) for x in data]
    highs = [float(x[2]) for x in data]
    lows  = [float(x[3]) for x in data]
    return closes, highs, lows


def ema(values, period: int):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def rsi(values, period: int = 14):
    if len(values) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        diff = values[i] - values[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff

    avg_gain = gains / period
    avg_loss = losses / period

    for i in range(period + 1, len(values)):
        diff = values[i] - values[i - 1]
        gain = max(diff, 0.0)
        loss = max(-diff, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def fmt(x: float, digits: int = 4):
    if x is None:
        return "n/a"
    # nicer formatting for big prices
    if x >= 1000:
        return f"{x:.2f}"
    if x >= 10:
        return f"{x:.3f}"
    return f"{x:.{digits}f}"


def compute_signal(symbol: str, tf: str):
    closes, highs, lows = fetch_klines(symbol, tf, limit=300)
    last = closes[-1]

    e_fast = ema(closes, EMA_FAST)
    e_slow = ema(closes, EMA_SLOW)
    r = rsi(closes, RSI_PERIOD)

    if e_fast is None or e_slow is None or r is None:
        return None

    uptrend = e_fast > e_slow
    downtrend = e_fast < e_slow

    # Simple “pullback in trend” setup:
    # LONG: uptrend + RSI low-ish (<= 45)
    # SHORT: downtrend + RSI high-ish (>= 55)
    direction = None
    if uptrend and r <= RSI_LONG_MAX:
        direction = "LONG"
    elif downtrend and r >= RSI_SHORT_MIN:
        direction = "SHORT"

    if not direction:
        return None

    # SL by recent swing
    if len(lows) < SWING_LOOKBACK + 1:
        return None

    if direction == "LONG":
        sl = min(lows[-SWING_LOOKBACK:])
        entry = last
        risk = entry - sl
        if risk <= 0:
            return None
        tp1 = entry + risk * 1.0
        tp2 = entry + risk * 2.0
    else:
        sl = max(highs[-SWING_LOOKBACK:])
        entry = last
        risk = sl - entry
        if risk <= 0:
            return None
        tp1 = entry - risk * 1.0
        tp2 = entry - risk * 2.0

    # filter too tiny risk (avoid spam on flat moves)
    if risk / entry < 0.002:  # <0.2%
        return None

    return {
        "symbol": symbol,
        "tf": tf,
        "dir": direction,
        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "ema_fast": e_fast,
        "ema_slow": e_slow,
        "rsi": r,
    }


def main_loop():
    state = load_state()  # key -> {"active": bool}
    tg_send("✅ Bot online (Render) — scanning 1H/4H for BTC/ETH/BNB/SOL/TON")

    while True:
        try:
            changed = False

            for symbol in SYMBOLS:
                for tf in TIMEFRAMES:
                    key = f"{symbol}:{tf}"
                    active = bool(state.get(key, {}).get("active", False))

                    sig = None
                    try:
                        sig = compute_signal(symbol, tf)
                    except Exception as e:
                        print(f"Compute error {symbol} {tf}:", e)

                    if sig is None:
                        # setup not present -> reset active (so next time it appears, we alert once)
                        if active:
                            state[key] = {"active": False}
                            changed = True
                        continue

                    # setup present
                    if not active:
                        text = (
                            f"📣 SIGNAL ({sig['tf']}) — {sig['symbol']}\n"
                            f"{sig['dir']}\n\n"
                            f"Entry: {fmt(sig['entry'])}\n"
                            f"SL: {fmt(sig['sl'])}\n"
                            f"TP1: {fmt(sig['tp1'])}\n"
                            f"TP2: {fmt(sig['tp2'])}\n\n"
                            f"EMA{EMA_FAST}: {fmt(sig['ema_fast'])}\n"
                            f"EMA{EMA_SLOW}: {fmt(sig['ema_slow'])}\n"
                            f"RSI{RSI_PERIOD}: {sig['rsi']:.1f}\n"
                        )
                        tg_send(text)
                        state[key] = {"active": True, "last_dir": sig["dir"], "ts": int(time.time())}
                        changed = True

            if changed:
                save_state(state)

        except Exception as e:
            print("Loop error:", e)

        time.sleep(SCAN_EVERY_SEC)


if __name__ == "__main__":
    # Start HTTP server in background (keeps Render Web Service happy)
    t = threading.Thread(target=start_http_server, daemon=True)
    t.start()

    main_loop()