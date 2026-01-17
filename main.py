import os
import time
import json
import math
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional, Any, Tuple

import requests

# =========================
# VERSIONING
# =========================
BOT_VERSION = "v2026.01.13-c"
CONFIG_ID = "lab_rejected_paper_v1"
EXP_ID = "jan13c"

# =========================
# CONFIG
# =========================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("CHAT_ID", "").strip()          # main channel id like -100...
LAB_CHAT_ID = os.environ.get("LAB_CHAT_ID", "").strip()  # lab channel id like -100...
TZ_OFFSET_HOURS = int(os.environ.get("TZ_OFFSET_HOURS", "2"))

BYBIT_BASE = "https://api.bybit.com"

SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "TONUSDT"]

# Base TF for setups
TF = "15"              # 15m
HTF = "60"             # 1h (higher timeframe filter)
CHECK_EVERY_SEC = 60

EMA_FAST = 20
EMA_SLOW = 50
RSI_LEN = 14
ATR_LEN = 14

# --- Filters / scoring thresholds ---
MIN_EMA_GAP_PCT = 0.0015        # 0.15% gap between EMA20/50 (avoid flat)
NEAR_EMA50_PCT = 0.0025         # price near EMA50 (pullback)
MIN_ATR_PCT = 0.006             # ATR/price >= 0.6% (avoid dead market)
EMA_SLOPE_LOOKBACK = 10         # bars for slope check
EMA_SLOPE_MIN = 0.0006          # min slope magnitude (0.06% of price)
MAX_FAST_EMA_CROSSES = 3        # too many crosses -> chop
IMPULSE_ATR_MULT = 1.5          # last CLOSED candle range > 1.5*ATR -> impulse (risk)

# RSI gates (basic)
RSI_LONG_MIN = 35
RSI_SHORT_MAX = 65

# Stops/targets (risk-based)
SL_ATR_MULT = 1.2
TP1_R = 1.0
TP2_R = 2.0
TP3_R = 3.0

# R gating for traffic light
MIN_R_YELLOW = 1.8
MIN_R_GREEN = 2.5

# Score gating
MIN_SCORE_YELLOW = 5
MIN_SCORE_GREEN = 7

# ==== SMART ENTRY (OFFSET BY ATR) ====
OFFSET_MULT = {
    "BTCUSDT": 0.25,
    "ETHUSDT": 0.35,
    "BNBUSDT": 0.30,
    "SOLUSDT": 0.40,
    "TONUSDT": 0.45,
}

# If price doesn't touch the limit within this window -> NO FILL (skip, not SL)
FILL_TTL_MIN = 45

PENDING_TTL_MIN = 60
COOLDOWN_MIN = 90
STATE_FILE = "state.json"

DAILY_STATS_HOUR = 21
DAILY_STATS_MINUTE = 0

EVAL_TF = "1"
HTTP_TIMEOUT = 12
RETRY_SLEEP = 2.0

# Post only GREEN/YELLOW (requested)
POST_ONLY_GREEN_YELLOW = True

# =========================
# TELEGRAM
# =========================
def tg_send(text: str) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        print("Missing BOT_TOKEN or CHAT_ID env vars")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True}
    try:
        r = requests.post(url, json=payload, timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            print("Telegram error:", r.status_code, r.text[:300])
    except Exception as e:
        print("Telegram exception:", e)

def tg_send_lab(text: str) -> None:
    if not BOT_TOKEN or not LAB_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": LAB_CHAT_ID, "text": text, "disable_web_page_preview": True}
    try:
        r = requests.post(url, json=payload, timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            print("LAB Telegram error:", r.status_code, r.text[:300])
    except Exception as e:
        print("LAB Telegram exception:", e)

# =========================
# STATE
# =========================
def load_state() -> Dict[str, Any]:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            s = json.load(f)
            s.setdefault("last_sent", {})
            s.setdefault("pending", {})     # setups waiting confirm
            s.setdefault("trades", [])      # confirmed signals (some will become FILLED)
            s.setdefault("setups", [])      # count actionable setups/day (🟢🟡 only)
            s.setdefault("rejected", [])    # RED candidates (for LAB + paper eval)
            s.setdefault("daily", {"last_stats_date": ""})
            s.setdefault("paper", {"last_eval_date": ""})
            return s
    except Exception:
        return {
            "last_sent": {},
            "pending": {},
            "trades": [],
            "setups": [],
            "rejected": [],
            "daily": {"last_stats_date": ""},
            "paper": {"last_eval_date": ""},
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
    url = f"{BYBIT_BASE}/v5/market/kline"
    params = {"category": "linear", "symbol": symbol, "interval": interval, "limit": str(limit)}
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
    avg_gain = sum(gains[:length]) / length
    avg_loss = sum(losses[:length]) / length
    out = [50.0] * (length)
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
# HELPERS
# =========================
def last_closed_candle(symbol: str, interval: str) -> Optional[Dict[str, float]]:
    candles = bybit_klines(symbol, interval, limit=3)
    if len(candles) < 3:
        return None
    return candles[-2]

def ema_at_closed(symbol: str, interval: str, length: int) -> Optional[float]:
    candles = bybit_klines(symbol, interval, limit=250)
    if len(candles) < 120:
        return None
    closes = [c["c"] for c in candles]
    e = ema(closes, length)
    if not e or len(e) < 3:
        return None
    return float(e[-2])

def atr_now(symbol: str, interval: str) -> Optional[float]:
    candles = bybit_klines(symbol, interval, limit=250)
    if len(candles) < 120:
        return None
    return atr(candles, ATR_LEN)

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

def count_crosses(closes: List[float], ema_line: List[float], lookback: int = 30) -> int:
    if len(closes) < lookback + 2 or len(ema_line) < lookback + 2:
        return 0
    n = min(lookback + 1, len(closes) - 1, len(ema_line) - 1)
    crosses = 0
    prev = closes[-(n+1)] - ema_line[-(n+1)]
    for i in range(n, 0, -1):
        cur = closes[-i] - ema_line[-i]
        if (prev <= 0 < cur) or (prev >= 0 > cur):
            crosses += 1
        prev = cur
    return crosses

def structure_ok_15m(candles: List[Dict[str, float]], side: str) -> bool:
    if len(candles) < 5:
        return False
    c1 = candles[-2]  # last closed
    c2 = candles[-3]  # prev closed
    if side == "LONG":
        return (c1["h"] > c2["h"]) and (c1["l"] > c2["l"])
    else:
        return (c1["h"] < c2["h"]) and (c1["l"] < c2["l"])

def htf_trend(symbol: str) -> Optional[str]:
    candles = bybit_klines(symbol, HTF, limit=250)
    if len(candles) < 120:
        return None
    closes = [c["c"] for c in candles]
    ef = ema(closes, EMA_FAST)
    es = ema(closes, EMA_SLOW)
    if not ef or not es:
        return None
    if ef[-1] > es[-1]:
        return "LONG"
    if ef[-1] < es[-1]:
        return "SHORT"
    return None

def compute_target_price(candles: List[Dict[str, float]], side: str, lookback: int = 96) -> Optional[float]:
    if len(candles) < lookback + 5:
        return None
    recent = candles[-lookback:]
    hi = max(c["h"] for c in recent)
    lo = min(c["l"] for c in recent)
    return hi if side == "LONG" else lo

def r_potential(entry: float, sl: float, target: float) -> float:
    risk = abs(entry - sl)
    if risk <= 0:
        return 0.0
    reward = abs(target - entry)
    return reward / risk

def market_is_choppy(price: float, candles: List[Dict[str, float]], ef_line: List[float], es_line: List[float], a: float) -> Tuple[bool, str]:
    if a / price < MIN_ATR_PCT:
        return True, "низька волатильність"
    ef = ef_line[-1]
    es = es_line[-1]
    ema_spread = abs(ef - es) / price
    if ema_spread < MIN_EMA_GAP_PCT:
        return True, "EMA злиплись"
    closes = [c["c"] for c in candles]
    crosses = count_crosses(closes, ef_line, lookback=30)
    if crosses >= MAX_FAST_EMA_CROSSES:
        return True, "часті перетини EMA20"
    if len(ef_line) > EMA_SLOPE_LOOKBACK + 2:
        slope = (ef_line[-1] - ef_line[-(EMA_SLOPE_LOOKBACK+1)]) / price
        if abs(slope) < EMA_SLOPE_MIN:
            return True, "EMA20 без нахилу"
    return False, ""

def last_closed_candle_impulse(candles: List[Dict[str, float]], a: float) -> bool:
    # use last CLOSED candle
    if not candles or a is None or len(candles) < 3:
        return False
    c = candles[-2]
    rng = float(c["h"] - c["l"])
    return rng > (IMPULSE_ATR_MULT * a)

def compute_entry_zone_limit_proxy(symbol: str, side: str, tf: str) -> Optional[Tuple[float, float, float]]:
    """
    Proxy-confirm for RED (no real confirm):
    use last CLOSED 15m close as entry_confirm to build zone/limit like real.
    Returns (zone_low, zone_high, entry_limit).
    """
    c = last_closed_candle(symbol, tf)
    if not c:
        return None
    entry_confirm = float(c["c"])
    a = atr_now(symbol, tf)
    if a is None:
        return None
    k = float(OFFSET_MULT.get(symbol, 0.35))
    offset = a * k

    if side == "LONG":
        zone_high = entry_confirm
        zone_low = entry_confirm - offset
        entry_limit = zone_low
    else:
        zone_low = entry_confirm
        zone_high = entry_confirm + offset
        entry_limit = zone_high

    return (float(zone_low), float(zone_high), float(entry_limit))

# =========================
# SETUP LOGIC (TRAFFIC LIGHT)
# =========================
def compute_setup(symbol: str) -> Optional[Dict[str, Any]]:
    candles = bybit_klines(symbol, TF, limit=250)
    if len(candles) < 120:
        return None

    closes = [c["c"] for c in candles]
    ef_line = ema(closes, EMA_FAST)
    es_line = ema(closes, EMA_SLOW)
    r_line = rsi(closes, RSI_LEN)
    a = atr(candles, ATR_LEN)

    if not ef_line or not es_line or not r_line or a is None:
        return None

    price = float(closes[-1])
    ef = float(ef_line[-1])
    es = float(es_line[-1])
    rv = float(r_line[-1])

    near_ema50 = abs(price - es) / price <= NEAR_EMA50_PCT
    if not near_ema50:
        return None

    side = None
    if ef > es and (RSI_LONG_MIN <= rv <= 60):
        side = "LONG"
    elif ef < es and (40 <= rv <= RSI_SHORT_MAX):
        side = "SHORT"
    else:
        return None

    chop, chop_reason = market_is_choppy(price, candles, ef_line, es_line, a)
    htf_side = htf_trend(symbol)
    struct_ok = structure_ok_15m(candles, side)
    impulse = last_closed_candle_impulse(candles, a)

    entry = price
    sl_dist = a * SL_ATR_MULT
    sl = entry - sl_dist if side == "LONG" else entry + sl_dist
    target = compute_target_price(candles, side, lookback=96)

    rp = 0.0
    if target is not None:
        if (side == "LONG" and target > entry) or (side == "SHORT" and target < entry):
            rp = r_potential(entry, sl, target)

    score = 0
    reasons = []

    if htf_side == side:
        score += 2
        reasons.append("HTF ok")
    else:
        reasons.append("HTF mismatch")

    if struct_ok:
        score += 2
        reasons.append("structure ok")
    else:
        reasons.append("structure weak")

    score += 1
    reasons.append("pullback EMA50")

    if (side == "LONG" and rv <= 60) or (side == "SHORT" and rv >= 40):
        score += 1
        reasons.append("RSI ok")

    if not impulse:
        score += 1
        reasons.append("no impulse")
    else:
        reasons.append("impulse risk")

    if rp >= 3.0:
        score += 2
        reasons.append(f"rp {rp:.1f} (3+)")
    elif rp >= 2.0:
        score += 1
        reasons.append(f"rp {rp:.1f}")

    color = "RED"
    market_mode = "пилка/флет" if chop else "тренд/рух"
    if not chop:
        if score >= MIN_SCORE_GREEN and rp >= MIN_R_GREEN:
            color = "GREEN"
        elif score >= MIN_SCORE_YELLOW and rp >= MIN_R_YELLOW:
            color = "YELLOW"
        else:
            color = "RED"

    # reject_reason (for LAB diagnostics)
    reject_reason = ""
    if chop:
        reject_reason = f"chop:{chop_reason}"
    elif htf_side is not None and htf_side != side:
        reject_reason = "htf_mismatch"
    elif rp < MIN_R_YELLOW:
        reject_reason = "rp_low"
    elif score < MIN_SCORE_YELLOW:
        reject_reason = "score_low"
    elif impulse:
        reject_reason = "impulse_risk"
    else:
        reject_reason = "red"

    if side == "LONG":
        reason = f"тренд вгору (EMA{EMA_FAST}>{EMA_SLOW}), відкат до EMA{EMA_SLOW}, RSI {rv:.0f}"
    else:
        reason = f"тренд вниз (EMA{EMA_FAST}<{EMA_SLOW}), відкат до EMA{EMA_SLOW}, RSI {rv:.0f}"

    notes = []
    if chop:
        notes.append(f"🔴 Режим: {chop_reason}")
    if impulse:
        notes.append("⚠️ Імпульсна свічка: можливий хід проти")

    return {
        "symbol": symbol,
        "tf": TF,
        "side": side,
        "entry": round_nice(entry),
        "sl": round_nice(sl),
        "rsi": round(rv, 1),
        "reason": reason,
        "ts": int(time.time()),
        "t_ms": int(candles[-1]["t"]),

        "color": color,
        "score": int(score),
        "r_potential": round(float(rp), 2),
        "market_mode": market_mode,
        "htf_side": htf_side or "",
        "structure_ok": bool(struct_ok),
        "impulse": bool(impulse),
        "target_price": round_nice(float(target)) if target is not None else None,
        "notes": notes,
        "score_reasons": reasons,

        "reject_reason": reject_reason,
    }

def build_targets(entry: float, sl: float, side: str) -> Tuple[float, float, float]:
    risk = abs(entry - sl)
    if risk <= 0:
        return entry, entry, entry
    if side == "LONG":
        return entry + risk * TP1_R, entry + risk * TP2_R, entry + risk * TP3_R
    return entry - risk * TP1_R, entry - risk * TP2_R, entry - risk * TP3_R

# =========================
# MESSAGE FORMAT
# =========================
def traffic_block(color: str, score: int, rp: float, market_mode: str, impulse: bool) -> str:
    if color == "GREEN":
        badge = "🟢 Індикатор: зелений — можна працювати"
    else:
        badge = "🟡 Індикатор: жовтий — обережно"
    warn = " | ⚠️ імпульс" if impulse else ""
    return f"{badge}\nScore: {score}/10 | R потенціал: {rp:.2f} | Режим: {market_mode}{warn}"

def format_entry_confirmed_zone(tr: Dict[str, Any]) -> str:
    sym = tr["symbol"]
    side = "🟢 LONG" if tr["side"] == "LONG" else "🔴 SHORT"
    tf = tr["tf"]

    color = tr.get("color", "YELLOW")
    score = int(tr.get("score", 0))
    rp = float(tr.get("r_potential", 0.0))
    market_mode = tr.get("market_mode", "")
    impulse = bool(tr.get("impulse", False))

    extra_notes = tr.get("notes", [])
    extra = ""
    if extra_notes:
        extra = "\n" + "\n".join(extra_notes)

    return (
        f"✅ ENTRY CONFIRMED | {sym} | {tf}m\n"
        f"{side}\n\n"
        f"Entry zone: {tr['entry_zone_low']} – {tr['entry_zone_high']}\n"
        f"Limit (рекоменд.): {tr['entry_limit']}\n"
        f"SL: {tr['sl']}\n"
        f"TP1: {tr['tp1']}\n"
        f"TP2: {tr['tp2']}\n"
        f"TP3: {tr['tp3']}\n\n"
        f"Ризик: 1–2% (фікс)\n"
        f"Контекст: {tr.get('reason','')}\n\n"
        f"{traffic_block(color, score, rp, market_mode, impulse)}"
        f"{extra}\n"
    )

# =========================
# TRADE FILL + OUTCOME (1m)
# =========================
def is_limit_touched(tr: Dict[str, Any]) -> Optional[int]:
    """
    Returns fill_t_ms if limit touched after confirm candle (or proxy start), else None.
    """
    symbol = tr["symbol"]
    side = tr["side"]
    limit_price = float(tr["entry_limit"])
    start_ms = int(tr.get("confirm_t_ms", 0))

    candles = bybit_klines(symbol, EVAL_TF, limit=600)
    if not candles:
        return None

    relevant = [c for c in candles if c["t"] >= start_ms]
    if not relevant:
        relevant = candles[-300:]

    for c in relevant:
        hi, lo = c["h"], c["l"]
        if side == "LONG":
            if lo <= limit_price:
                return int(c["t"])
        else:
            if hi >= limit_price:
                return int(c["t"])
    return None

def eval_trade_hit_from(tr: Dict[str, Any], start_ms: int) -> str:
    symbol = tr["symbol"]
    side = tr["side"]

    sl = float(tr["sl"])
    tp1 = float(tr["tp1"])
    tp2 = float(tr["tp2"])
    tp3 = float(tr["tp3"])

    candles = bybit_klines(symbol, EVAL_TF, limit=800)
    if not candles:
        return "OPEN"

    relevant = [c for c in candles if c["t"] >= start_ms]
    if not relevant:
        relevant = candles[-300:]

    for c in relevant:
        hi, lo = c["h"], c["l"]

        if side == "LONG":
            sl_hit = lo <= sl
            tp1_hit = hi >= tp1
            tp2_hit = hi >= tp2
            tp3_hit = hi >= tp3
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
            if sl_hit:
                return "SL"
            if tp3_hit:
                return "TP3"
            if tp2_hit:
                return "TP2"
            if tp1_hit:
                return "TP1"

    return "OPEN"

def process_trade_fills(state: Dict[str, Any]) -> None:
    now_ts = int(time.time())
    trades = state.get("trades", [])
    if not trades:
        return

    changed = False

    for tr in trades:
        status = tr.get("status", "CONFIRMED")
        if status in ("FILLED", "NO_FILL", "CLOSED"):
            continue

        confirm_ts = int(tr.get("ts", 0))
        if confirm_ts <= 0:
            continue

        if now_ts > (confirm_ts + FILL_TTL_MIN * 60):
            tr["status"] = "NO_FILL"
            changed = True
            continue

        fill_t_ms = is_limit_touched(tr)
        if fill_t_ms is not None:
            tr["status"] = "FILLED"
            tr["fill_t_ms"] = int(fill_t_ms)
            changed = True

    if changed:
        save_state(state)

# =========================
# TIME HELPERS
# =========================
def local_now() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=TZ_OFFSET_HOURS)

def yyyy_mm_dd(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")

# =========================
# DAILY STATS (MAIN)
# =========================
def send_daily_stats(state: Dict[str, Any]) -> None:
    today = yyyy_mm_dd(local_now())
    last = state.get("daily", {}).get("last_stats_date", "")
    if last == today:
        return

    now = local_now()
    if (now.hour, now.minute) < (DAILY_STATS_HOUR, DAILY_STATS_MINUTE):
        return

    start_local = datetime(now.year, now.month, now.day, 0, 0, tzinfo=timezone.utc) - timedelta(hours=TZ_OFFSET_HOURS)
    end_local = start_local + timedelta(days=1)
    start_ts = int(start_local.timestamp())
    end_ts = int(end_local.timestamp())

    setups_today = [s for s in state.get("setups", []) if start_ts <= int(s.get("ts", 0)) < end_ts]
    trades_today = [t for t in state.get("trades", []) if start_ts <= int(t.get("ts", 0)) < end_ts]

    setups_found = len(setups_today)
    confirmed = len(trades_today)
    filled = len([t for t in trades_today if t.get("status") == "FILLED"])
    no_fill = len([t for t in trades_today if t.get("status") == "NO_FILL"])

    sl = tp1 = tp2 = tp3 = open_ = 0
    for t in trades_today:
        if t.get("status") != "FILLED":
            continue
        start_ms = int(t.get("fill_t_ms", 0)) or int(t.get("confirm_t_ms", 0))
        res = eval_trade_hit_from(t, start_ms)
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
            "Сетапів (🟢/🟡) не було.\n"
            "Завтра продовжимо моніторинг."
        )
    else:
        text = (
            "📊 Daily Report (21:00)\n"
            f"Сетапів (🟢/🟡) знайдено: {setups_found}\n"
            f"✅ Confirmed: {confirmed}\n"
            f"🎯 Filled (limit touched): {filled}\n"
            f"⏭ No fill (skip): {no_fill}\n\n"
            f"❌ SL: {sl}\n"
            f"✅ TP1: {tp1}\n"
            f"✅ TP2: {tp2}\n"
            f"✅ TP3: {tp3}\n"
            f"⏳ В роботі: {open_}\n\n"
            "У TP/SL входять тільки FILLED (коли ціна доторкнулась limit)."
        )

    tg_send(text)
    state.setdefault("daily", {})["last_stats_date"] = today
    save_state(state)

# =========================
# PAPER EVAL FOR REJECTED (LAB)
# =========================
def process_rejected_paper_daily(state: Dict[str, Any]) -> None:
    today = yyyy_mm_dd(local_now())
    last = state.get("paper", {}).get("last_eval_date", "")
    if last == today:
        return

    now = local_now()
    if (now.hour, now.minute) < (DAILY_STATS_HOUR, DAILY_STATS_MINUTE):
        return

    start_local = datetime(now.year, now.month, now.day, 0, 0, tzinfo=timezone.utc) - timedelta(hours=TZ_OFFSET_HOURS)
    end_local = start_local + timedelta(days=1)
    start_ts = int(start_local.timestamp())
    end_ts = int(end_local.timestamp())

    rej = state.get("rejected", [])
    todays = [r for r in rej if start_ts <= int(r.get("ts", 0)) < end_ts]

    if not todays:
        state.setdefault("paper", {})["last_eval_date"] = today
        save_state(state)
        return

    total = len(todays)
    no_fill = filled = 0
    sl = tp1 = tp2 = tp3 = open_ = 0
    by_reason: Dict[str, Dict[str, int]] = {}

    for r in todays:
        if r.get("paper_done"):
            continue

        tr_like = {
            "symbol": r["symbol"],
            "side": r["side"],
            "entry_limit": r["entry_limit"],
            "confirm_t_ms": int(r.get("t_ms", 0)),  # proxy start
        }

        fill_t_ms = is_limit_touched(tr_like)
        if fill_t_ms is None:
            r["paper_status"] = "NO_FILL"
            r["paper_done"] = True
            no_fill += 1
            res = "NO_FILL"
        else:
            r["paper_status"] = "FILLED"
            r["paper_fill_t_ms"] = int(fill_t_ms)
            filled += 1

            tr_eval = {
                "symbol": r["symbol"],
                "side": r["side"],
                "sl": r["sl"],
                "tp1": r["tp1"],
                "tp2": r["tp2"],
                "tp3": r["tp3"],
            }
            out = eval_trade_hit_from(tr_eval, int(fill_t_ms))
            r["paper_result"] = out
            r["paper_done"] = True
            res = out

            if out == "SL":
                sl += 1
            elif out == "TP1":
                tp1 += 1
            elif out == "TP2":
                tp2 += 1
            elif out == "TP3":
                tp3 += 1
            else:
                open_ += 1

        reason = (r.get("reject_reason") or "unknown")[:40]
        by_reason.setdefault(reason, {"n": 0, "NO_FILL": 0, "FILLED": 0, "SL": 0, "TP1": 0, "TP2": 0, "TP3": 0, "OPEN": 0})
        by_reason[reason]["n"] += 1
        if res == "NO_FILL":
            by_reason[reason]["NO_FILL"] += 1
        elif res in ("SL", "TP1", "TP2", "TP3", "OPEN"):
            by_reason[reason]["FILLED"] += 1
            by_reason[reason][res] += 1

    save_state(state)

    lines = []
    lines.append(f"🧪 LAB Paper Report (Rejected) | {today}")
    lines.append(f"{BOT_VERSION} | {CONFIG_ID} | {EXP_ID}")
    lines.append(f"Rejected: {total}")
    lines.append(f"NO_FILL: {no_fill} | FILLED: {filled}")
    if filled > 0:
        lines.append(f"FILLED outcomes → SL:{sl} TP1:{tp1} TP2:{tp2} TP3:{tp3} OPEN:{open_}")

    top = sorted(by_reason.items(), key=lambda kv: kv[1]["n"], reverse=True)[:3]
    if top:
        lines.append("")
        lines.append("Top reject reasons:")
        for reason, m in top:
            lines.append(f"- {reason}: n={m['n']} | NO_FILL={m['NO_FILL']} | SL={m['SL']} TP1={m['TP1']} TP2={m['TP2']}")

    tg_send_lab("\n".join(lines))

    state.setdefault("paper", {})["last_eval_date"] = today
    save_state(state)

# =========================
# PENDING → CONFIRMED (WITH ENTRY ZONE)
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

        c = last_closed_candle(sig["symbol"], sig["tf"])
        if not c:
            continue

        entry_confirm = float(c["c"])

        a = atr_now(sig["symbol"], sig["tf"])
        if a is None:
            continue

        k = float(OFFSET_MULT.get(sig["symbol"], 0.35))
        offset = a * k

        if sig["side"] == "LONG":
            zone_high = entry_confirm
            zone_low = entry_confirm - offset
            entry_limit = zone_low
        else:
            zone_low = entry_confirm
            zone_high = entry_confirm + offset
            entry_limit = zone_high

        sl = float(sig["sl"])
        tp1, tp2, tp3 = build_targets(entry_limit, sl, sig["side"])

        target_price = sig.get("target_price")
        rp = float(sig.get("r_potential", 0.0))
        if target_price is not None:
            tp = float(target_price)
            if (sig["side"] == "LONG" and tp > entry_limit) or (sig["side"] == "SHORT" and tp < entry_limit):
                rp = r_potential(entry_limit, sl, tp)

        score = int(sig.get("score", 0))
        market_mode = sig.get("market_mode", "")
        impulse = bool(sig.get("impulse", False))
        color = sig.get("color", "YELLOW")

        if color == "GREEN" and not (score >= MIN_SCORE_GREEN and rp >= MIN_R_GREEN):
            color = "YELLOW" if (score >= MIN_SCORE_YELLOW and rp >= MIN_R_YELLOW) else "RED"
        if color == "YELLOW" and not (score >= MIN_SCORE_YELLOW and rp >= MIN_R_YELLOW):
            color = "RED"
        if POST_ONLY_GREEN_YELLOW and color == "RED":
            to_delete.append(pid)
            continue

        tr = {
            "id": pid,
            "symbol": sig["symbol"],
            "tf": sig["tf"],
            "side": sig["side"],
            "status": "CONFIRMED",

            "entry_zone_low": round_nice(zone_low),
            "entry_zone_high": round_nice(zone_high),
            "entry_limit": round_nice(entry_limit),

            "sl": round_nice(sl),
            "tp1": round_nice(tp1),
            "tp2": round_nice(tp2),
            "tp3": round_nice(tp3),

            "reason": sig.get("reason", ""),
            "ts": now_ts,
            "confirm_t_ms": int(c["t"]),

            "color": color,
            "score": score,
            "r_potential": round(float(rp), 2),
            "market_mode": market_mode,
            "impulse": impulse,
            "notes": sig.get("notes", []),
        }

        state.setdefault("trades", []).append(tr)
        tg_send(format_entry_confirmed_zone(tr))
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
    tg_send(f"✅ Моніторинг активовано — {tf} сетапи (🟢🟡) по {coins}")
    tg_send_lab(f"🧪 LAB активний | {BOT_VERSION} | {CONFIG_ID} | {EXP_ID}")

def scanner_loop() -> None:
    state = load_state()
    announce_start()

    while True:
        now_ts = int(time.time())

        # 1) confirm setups -> trades with entry zone
        try:
            process_pending(state)
        except Exception as e:
            print("process_pending error:", e)

        # 2) track fills (limit touched)
        try:
            process_trade_fills(state)
        except Exception as e:
            print("process_trade_fills error:", e)

        # 3) daily stats (main)
        try:
            send_daily_stats(state)
        except Exception as e:
            print("daily stats error:", e)

        # 3b) daily paper evaluation for rejected (LAB)
        try:
            process_rejected_paper_daily(state)
        except Exception as e:
            print("paper rejected error:", e)

        # 4) scan for new setups
        for sym in SYMBOLS:
            try:
                sig = compute_setup(sym)
                if not sig:
                    continue

                color = sig.get("color", "RED")

                # --- RED: store to LAB + state["rejected"], then continue ---
                if color == "RED":
                    z = compute_entry_zone_limit_proxy(sig["symbol"], sig["side"], sig["tf"])
                    if z:
                        zone_low, zone_high, entry_limit = z
                        tp1, tp2, tp3 = build_targets(entry_limit, float(sig["sl"]), sig["side"])

                        rid = f"{sig['symbol']}:{sig['side']}:{sig['t_ms']}:RED"
                        rec = {
                            "id": rid,
                            "symbol": sig["symbol"],
                            "tf": sig["tf"],
                            "side": sig["side"],
                            "ts": int(time.time()),
                            "t_ms": int(sig["t_ms"]),

                            "entry_limit": round_nice(entry_limit),
                            "entry_zone_low": round_nice(zone_low),
                            "entry_zone_high": round_nice(zone_high),

                            "sl": round_nice(float(sig["sl"])),
                            "tp1": round_nice(tp1),
                            "tp2": round_nice(tp2),
                            "tp3": round_nice(tp3),

                            "score": int(sig.get("score", 0)),
                            "r_potential": float(sig.get("r_potential", 0.0)),
                            "reject_reason": sig.get("reject_reason", "") or "red",

                            "paper_done": False,
                        }

                        state.setdefault("rejected", []).append(rec)
                        save_state(state)

                        tg_send_lab(
                            f"🧪 REJECTED | {rec['symbol']} {rec['side']} | {rec['tf']}m\n"
                            f"Причина: {rec['reject_reason']}\n"
                            f"Score: {rec['score']} | Rp: {rec['r_potential']:.2f}\n"
                            f"Zone: {rec['entry_zone_low']}–{rec['entry_zone_high']} | SL: {rec['sl']}"
                        )
                    continue

                # 🟢🟡 only below (actionable)
                if POST_ONLY_GREEN_YELLOW and color not in ("GREEN", "YELLOW"):
                    continue

                key = f"{sym}:{sig['side']}:{TF}:{color}"
                if not cooldown_ok(state, key, now_ts):
                    continue

                setup_id = f"{sig['symbol']}:{sig['side']}:{sig['t_ms']}"
                sig["id"] = setup_id
                sig["expires_ts"] = now_ts + (PENDING_TTL_MIN * 60)

                state.setdefault("setups", []).append({"id": setup_id, "ts": now_ts, "color": color})
                state.setdefault("pending", {})[setup_id] = sig

                mark_sent(state, key, now_ts)
                save_state(state)

                time.sleep(0.25)

            except Exception as e:
                print("scan error:", sym, e)

        time.sleep(CHECK_EVERY_SEC)

if __name__ == "__main__":
    scanner_loop()
