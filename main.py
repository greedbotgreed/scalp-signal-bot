import os
import time
import json
import math
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional, Any, Tuple

import requests

# =========================
# VERSION
# =========================
VERSION = "v2026.01.17-a (v1 flow + traffic-light routing)"

# =========================
# CONFIG
# =========================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
CHAT_ID_SIGNALS = os.environ.get("CHAT_ID_SIGNALS", "").strip()  # main channel
CHAT_ID_LAB = os.environ.get("CHAT_ID_LAB", "").strip()          # lab channel
TZ_OFFSET_HOURS = int(os.environ.get("TZ_OFFSET_HOURS", "2"))

BYBIT_BASE = "https://api.bybit.com"

SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "TONUSDT"]

# Base TF for setups
TF = "15"              # 15m
HTF = "60"             # 1h filter for scoring
CHECK_EVERY_SEC = 20   # can be 20-60, doesn't matter, we gate by closed candle

EMA_FAST = 20
EMA_SLOW = 50
RSI_LEN = 14
ATR_LEN = 14

# --- Base v1 gates ---
MIN_EMA_GAP_PCT = 0.0015        # 0.15%
NEAR_EMA50_PCT = 0.0025         # pullback to EMA50
RSI_LONG_MIN = 35
RSI_SHORT_MAX = 65

# --- Stops/targets ---
SL_ATR_MULT = 1.2
TP1_R = 1.0
TP2_R = 2.0
TP3_R = 3.0

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

PENDING_TTL_MIN = 90   # allow more time for confirm (v1-like flow)
COOLDOWN_MIN = 60      # anti-spam for same symbol/side
STATE_FILE = "state.json"

DAILY_STATS_HOUR = 21
DAILY_STATS_MINUTE = 0

EVAL_TF = "1"
HTTP_TIMEOUT = 12
RETRY_SLEEP = 2.0

# =========================
# TRAFFIC LIGHT (v3-style but NOT too strict)
# (you can tune later)
# =========================
MIN_ATR_PCT = 0.0045           # 0.45% (looser than 0.6% to avoid "0 setups")
EMA_SLOPE_LOOKBACK = 10
EMA_SLOPE_MIN = 0.00045        # looser
MAX_FAST_EMA_CROSSES = 4       # looser
IMPULSE_ATR_MULT = 1.6

MIN_R_YELLOW = 1.6
MIN_R_GREEN = 2.2

MIN_SCORE_YELLOW = 5
MIN_SCORE_GREEN = 7

# We do NOT post candidates. We post only after CONFIRM.
POST_ON_CONFIRM_ONLY = True

# =========================
# TELEGRAM
# =========================
def tg_send(chat_id: str, text: str) -> None:
    if not BOT_TOKEN or not chat_id:
        print("Missing BOT_TOKEN or chat_id")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(url, json=payload, timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            print("Telegram error:", r.status_code, r.text[:300])
    except Exception as e:
        print("Telegram exception:", e)

def lab(text: str) -> None:
    tg_send(CHAT_ID_LAB, text)

def signals(text: str) -> None:
    tg_send(CHAT_ID_SIGNALS, text)

# =========================
# STATE
# =========================
def load_state() -> Dict[str, Any]:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            s = json.load(f)
    except Exception:
        s = {}

    s.setdefault("last_sent", {})          # cooldown by key
    s.setdefault("pending", {})            # setups waiting confirm
    s.setdefault("trades", [])             # confirmed signals (some become FILLED)
    s.setdefault("setups_all", [])         # all candidates found (for lab stats)
    s.setdefault("rejected_all", [])       # rejected candidates (for lab stats)
    s.setdefault("daily", {"last_stats_date": ""})
    s.setdefault("meta", {})
    s["meta"]["version"] = VERSION

    # gating by closed candle so we don't spam
    s.setdefault("last_closed_t_ms", {})   # per symbol last processed closed candle
    return s

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
                time.sleep(RETRY_SLEEP)
                continue
            data = r.json()
            if data.get("retCode") != 0:
                time.sleep(RETRY_SLEEP)
                continue
            raw = data["result"]["list"]
            candles = []
            for row in reversed(raw):
                candles.append({
                    "t": int(row[0]),         # ms
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
# TIME HELPERS
# =========================
def local_now() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=TZ_OFFSET_HOURS)

def yyyy_mm_dd(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")

# =========================
# CLOSED CANDLE ACCESS (ANTI-SPAM)
# =========================
def get_last_closed_15m(symbol: str) -> Optional[Tuple[Dict[str, float], List[Dict[str, float]]]]:
    candles = bybit_klines(symbol, TF, limit=260)
    if len(candles) < 120:
        return None
    # last closed candle is [-2]
    return candles[-2], candles

# =========================
# CONFIRM CHECK (uses last closed candle)
# =========================
def is_confirmed(sig: Dict[str, Any], last_closed: Dict[str, float], ema_fast_closed: float) -> bool:
    close = float(last_closed["c"])
    if sig["side"] == "LONG":
        return close > ema_fast_closed
    else:
        return close < ema_fast_closed

# =========================
# TRAFFIC LIGHT HELPERS
# =========================
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
    candles = bybit_klines(symbol, HTF, limit=260)
    if len(candles) < 120:
        return None
    closes = [c["c"] for c in candles]
    ef = ema(closes, EMA_FAST)
    es = ema(closes, EMA_SLOW)
    if not ef or not es:
        return None
    if ef[-2] > es[-2]:
        return "LONG"
    if ef[-2] < es[-2]:
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
    ef = ef_line[-2]
    es = es_line[-2]
    ema_spread = abs(ef - es) / price
    if ema_spread < MIN_EMA_GAP_PCT:
        return True, "EMA злиплись"
    closes = [c["c"] for c in candles]
    crosses = count_crosses(closes, ef_line, lookback=30)
    if crosses >= MAX_FAST_EMA_CROSSES:
        return True, "часті перетини EMA20"
    if len(ef_line) > EMA_SLOPE_LOOKBACK + 3:
        slope = (ef_line[-2] - ef_line[-(EMA_SLOPE_LOOKBACK+2)]) / price
        if abs(slope) < EMA_SLOPE_MIN:
            return True, "EMA20 без нахилу"
    return False, ""

def last_closed_impulse(candles: List[Dict[str, float]], a: float) -> bool:
    if len(candles) < 3 or a is None:
        return False
    c = candles[-2]  # last closed
    rng = float(c["h"] - c["l"])
    return rng > (IMPULSE_ATR_MULT * a)

def grade_setup(sig: Dict[str, Any], candles: List[Dict[str, float]], ef_line: List[float], es_line: List[float], r_line: List[float], a: float) -> Dict[str, Any]:
    # Use closed candle values
    price = float(candles[-2]["c"])
    ef = float(ef_line[-2])
    es = float(es_line[-2])
    rv = float(r_line[-2])

    side = sig["side"]

    chop, chop_reason = market_is_choppy(price, candles, ef_line, es_line, a)
    htf_side = htf_trend(sig["symbol"])
    struct_ok = structure_ok_15m(candles, side)
    impulse = last_closed_impulse(candles, a)

    # SL and target (R potential)
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
        reasons.append("HTF в ту ж сторону")
    else:
        reasons.append("HTF не підтверджує")

    if struct_ok:
        score += 2
        reasons.append("структура ок")
    else:
        reasons.append("структура слабка")

    score += 1
    reasons.append("відкат до EMA50")

    if (side == "LONG" and rv <= 60) or (side == "SHORT" and rv >= 40):
        score += 1
        reasons.append("RSI в нормі")

    if not impulse:
        score += 1
        reasons.append("без імпульсу")
    else:
        reasons.append("імпульс (ризик)")

    if rp >= 3.0:
        score += 2
        reasons.append(f"R потенціал {rp:.1f} (3+)")
    elif rp >= 2.0:
        score += 1
        reasons.append(f"R потенціал {rp:.1f}")

    color = "RED"
    market_mode = "пилка/флет" if chop else "тренд/рух"

    if chop:
        color = "REJECTED"
    else:
        if score >= MIN_SCORE_GREEN and rp >= MIN_R_GREEN:
            color = "GREEN"
        elif score >= MIN_SCORE_YELLOW and rp >= MIN_R_YELLOW:
            color = "YELLOW"
        else:
            color = "RED"

    return {
        "color": color,
        "score": int(score),
        "r_potential": round(float(rp), 2),
        "market_mode": market_mode,
        "htf_side": htf_side or "",
        "structure_ok": bool(struct_ok),
        "impulse": bool(impulse),
        "target_price": round_nice(float(target)) if target is not None else None,
        "grade_reasons": reasons,
        "chop_reason": chop_reason if chop else "",
        "entry_ref": round_nice(entry),
        "sl_ref": round_nice(sl),
    }

# =========================
# BASE v1 SETUP (FLOW GENERATOR)
# - Uses CLOSED candle values only
# =========================
def compute_v1_candidate(symbol: str, state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    got = get_last_closed_15m(symbol)
    if not got:
        return None
    last_closed, candles = got

    closed_t_ms = int(last_closed["t"])
    last_seen = int(state.get("last_closed_t_ms", {}).get(symbol, 0))
    if closed_t_ms <= last_seen:
        return None  # no new closed candle

    closes = [c["c"] for c in candles]
    ef_line = ema(closes, EMA_FAST)
    es_line = ema(closes, EMA_SLOW)
    r_line = rsi(closes, RSI_LEN)
    a = atr(candles, ATR_LEN)

    if not ef_line or not es_line or not r_line or a is None:
        state["last_closed_t_ms"][symbol] = closed_t_ms
        save_state(state)
        return None

    price = float(last_closed["c"])
    ef = float(ef_line[-2])
    es = float(es_line[-2])
    rv = float(r_line[-2])

    gap_pct = abs(ef - es) / price
    near_ema50 = abs(price - es) / price <= NEAR_EMA50_PCT

    side = None
    if gap_pct >= MIN_EMA_GAP_PCT and near_ema50:
        if ef > es and (RSI_LONG_MIN <= rv <= 60):
            side = "LONG"
        elif ef < es and (40 <= rv <= RSI_SHORT_MAX):
            side = "SHORT"

    # mark processed closed candle
    state["last_closed_t_ms"][symbol] = closed_t_ms

    if not side:
        # still useful for analysis? we keep only if you want; for now skip
        save_state(state)
        return None

    # base SL (v1)
    sl_dist = a * SL_ATR_MULT
    sl = price - sl_dist if side == "LONG" else price + sl_dist

    # cool-down by symbol/side to avoid duplicates
    now_ts = int(time.time())
    key = f"{symbol}:{side}:{TF}"
    if not cooldown_ok(state, key, now_ts):
        save_state(state)
        return None

    reason = (
        f"тренд вгору (EMA{EMA_FAST}>{EMA_SLOW}), відкат до EMA{EMA_SLOW}, RSI {rv:.0f}"
        if side == "LONG"
        else f"тренд вниз (EMA{EMA_FAST}<{EMA_SLOW}), відкат до EMA{EMA_SLOW}, RSI {rv:.0f}"
    )

    setup_id = f"{symbol}:{side}:{closed_t_ms}"
    sig = {
        "id": setup_id,
        "symbol": symbol,
        "tf": TF,
        "side": side,
        "entry": round_nice(price),   # reference (not limit)
        "sl": round_nice(sl),
        "rsi": round(rv, 1),
        "reason": reason,
        "ts": now_ts,
        "closed_t_ms": closed_t_ms,
        "expires_ts": now_ts + (PENDING_TTL_MIN * 60),
    }

    # grade it (traffic light), but DO NOT post yet
    grade = grade_setup(sig, candles, ef_line, es_line, r_line, a)
    sig.update(grade)

    # log ALL candidates for lab daily stats
    state.setdefault("setups_all", []).append({
        "id": setup_id,
        "ts": now_ts,
        "symbol": symbol,
        "side": side,
        "color": sig.get("color", ""),
        "score": sig.get("score", 0),
        "rp": sig.get("r_potential", 0.0),
        "reason": sig.get("chop_reason", "") if sig.get("color") == "REJECTED" else "",
    })

    # Route into buckets:
    if sig["color"] == "REJECTED":
        # don't spam, just store
        state.setdefault("rejected_all", []).append({
            "id": setup_id,
            "ts": now_ts,
            "symbol": symbol,
            "side": side,
            "why": sig.get("chop_reason", ""),
            "score": sig.get("score", 0),
            "rp": sig.get("r_potential", 0.0),
        })
        mark_sent(state, key, now_ts)
        save_state(state)
        return None

    # For RED/YELLOW/GREEN we still wait confirmation (ENTRY CONFIRMED step)
    state.setdefault("pending", {})[setup_id] = sig
    mark_sent(state, key, now_ts)
    save_state(state)
    return sig

# =========================
# TARGETS
# =========================
def build_targets(entry: float, sl: float, side: str) -> Tuple[float, float, float]:
    risk = abs(entry - sl)
    if risk <= 0:
        return entry, entry, entry
    if side == "LONG":
        return entry + risk * TP1_R, entry + risk * TP2_R, entry + risk * TP3_R
    return entry - risk * TP1_R, entry - risk * TP2_R, entry - risk * TP3_R

# =========================
# CONFIRM → TRADE (WITH OFFSET LIMIT)
# - evaluated only on NEW CLOSED candle as well
# =========================
def process_pending(state: Dict[str, Any]) -> None:
    now_ts = int(time.time())
    pending = state.get("pending", {})
    if not pending:
        return

    to_delete = []

    for pid, sig in list(pending.items()):
        if now_ts > int(sig.get("expires_ts", 0)):
            # expired w/o confirm
            to_delete.append(pid)
            # log to lab later in daily stats (no spam)
            continue

        symbol = sig["symbol"]

        got = get_last_closed_15m(symbol)
        if not got:
            continue
        last_closed, candles = got
        closed_t_ms = int(last_closed["t"])

        # confirm should happen only on candle after setup candle
        if closed_t_ms <= int(sig.get("closed_t_ms", 0)):
            continue

        closes = [c["c"] for c in candles]
        ef_line = ema(closes, EMA_FAST)
        if not ef_line or len(ef_line) < 5:
            continue
        ema_fast_closed = float(ef_line[-2])

        if not is_confirmed(sig, last_closed, ema_fast_closed):
            continue

        # confirmed close
        entry_confirm = float(last_closed["c"])

        # ATR now (using same candles, closed)
        a = atr(candles, ATR_LEN)
        if a is None:
            continue

        k = float(OFFSET_MULT.get(symbol, 0.35))
        offset = a * k

        if sig["side"] == "LONG":
            zone_high = entry_confirm
            zone_low = entry_confirm - offset
            entry_limit = zone_low
        else:
            zone_low = entry_confirm
            zone_high = entry_confirm + offset
            entry_limit = zone_high

        sl = float(sig["sl"])  # keep v1 SL reference
        tp1, tp2, tp3 = build_targets(entry_limit, sl, sig["side"])

        # trade object
        tr = {
            "id": pid,
            "symbol": symbol,
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
            "confirm_t_ms": closed_t_ms,

            # traffic light from candidate grading
            "color": sig.get("color", "RED"),
            "score": int(sig.get("score", 0)),
            "r_potential": float(sig.get("r_potential", 0.0)),
            "market_mode": sig.get("market_mode", ""),
            "impulse": bool(sig.get("impulse", False)),
            "chop_reason": sig.get("chop_reason", ""),
        }

        state.setdefault("trades", []).append(tr)

        # Route posting:
        # - GREEN/YELLOW to Signals
        # - RED to LAB only
        msg = format_entry_confirmed(tr)

        if tr["color"] in ("GREEN", "YELLOW"):
            signals(msg)
            lab("🧪 (copy) " + msg)
        else:
            lab("🔴 RED CONFIRMED (lab only)\n\n" + msg)

        to_delete.append(pid)

    for pid in to_delete:
        pending.pop(pid, None)

    save_state(state)

def traffic_block(tr: Dict[str, Any]) -> str:
    color = tr.get("color", "RED")
    score = int(tr.get("score", 0))
    rp = float(tr.get("r_potential", 0.0))
    mode = tr.get("market_mode", "")
    impulse = bool(tr.get("impulse", False))

    if color == "GREEN":
        badge = "🟢 Фільтр: зелений — ок"
    elif color == "YELLOW":
        badge = "🟡 Фільтр: жовтий — обережно"
    else:
        badge = "🔴 Фільтр: червоний — lab only"

    warn = " | ⚠️ імпульс" if impulse else ""
    return f"{badge}\nScore: {score}/10 | R потенціал: {rp:.2f} | Режим: {mode}{warn}"

def format_entry_confirmed(tr: Dict[str, Any]) -> str:
    sym = tr["symbol"]
    side = "🟢 LONG" if tr["side"] == "LONG" else "🔴 SHORT"
    tf = tr["tf"]

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
        f"{traffic_block(tr)}\n"
    )

# =========================
# FILL + OUTCOME (1m)
# =========================
def is_limit_touched(tr: Dict[str, Any]) -> Optional[int]:
    symbol = tr["symbol"]
    side = tr["side"]
    limit_price = float(tr["entry_limit"])
    start_ms = int(tr.get("confirm_t_ms", 0))

    candles = bybit_klines(symbol, EVAL_TF, limit=800)
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

    candles = bybit_klines(symbol, EVAL_TF, limit=1200)
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
        if status in ("FILLED", "NO_FILL"):
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
# DAILY STATS
# - LAB: full stats (all candidates + rejected + red/yellow/green)
# - SIGNALS: only posted (yellow/green confirmed)
# =========================
def send_daily_stats(state: Dict[str, Any]) -> None:
    today = yyyy_mm_dd(local_now())
    last = state.get("daily", {}).get("last_stats_date", "")
    if last == today:
        return

    now = local_now()
    if (now.hour, now.minute) < (DAILY_STATS_HOUR, DAILY_STATS_MINUTE):
        return

    # day boundaries in UTC seconds
    start_local = datetime(now.year, now.month, now.day, 0, 0, tzinfo=timezone.utc) - timedelta(hours=TZ_OFFSET_HOURS)
    end_local = start_local + timedelta(days=1)
    start_ts = int(start_local.timestamp())
    end_ts = int(end_local.timestamp())

    setups_all = [s for s in state.get("setups_all", []) if start_ts <= int(s.get("ts", 0)) < end_ts]
    rejected_all = [r for r in state.get("rejected_all", []) if start_ts <= int(r.get("ts", 0)) < end_ts]
    trades_today = [t for t in state.get("trades", []) if start_ts <= int(t.get("ts", 0)) < end_ts]

    # CONFIRMED buckets
    conf_green = [t for t in trades_today if t.get("color") == "GREEN"]
    conf_yellow = [t for t in trades_today if t.get("color") == "YELLOW"]
    conf_red = [t for t in trades_today if t.get("color") == "RED"]

    # Posted to signals: only green/yellow
    posted = conf_green + conf_yellow

    # Filled / no fill
    filled = [t for t in trades_today if t.get("status") == "FILLED"]
    no_fill = [t for t in trades_today if t.get("status") == "NO_FILL"]

    # Outcomes (only FILLED)
    sl = tp1 = tp2 = tp3 = open_ = 0
    sl_pub = tp1_pub = tp2_pub = tp3_pub = open_pub = 0

    for t in filled:
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

        # published outcomes subset
        if t.get("color") in ("GREEN", "YELLOW"):
            if res == "SL":
                sl_pub += 1
            elif res == "TP1":
                tp1_pub += 1
            elif res == "TP2":
                tp2_pub += 1
            elif res == "TP3":
                tp3_pub += 1
            else:
                open_pub += 1

    # summarize reject reasons
    reasons = {}
    for r in rejected_all:
        why = r.get("why", "unknown")
        reasons[why] = reasons.get(why, 0) + 1
    reasons_line = ", ".join([f"{k}:{v}" for k, v in sorted(reasons.items(), key=lambda x: -x[1])]) or "-"

    # LAB report (full)
    lab_text = (
        f"🧪 LAB Daily Report (21:00)\n"
        f"Версія: {VERSION}\n\n"
        f"Кандидатів (v1 flow) знайдено: {len(setups_all)}\n"
        f"REJECTED (флет/вола/пилка): {len(rejected_all)}\n"
        f"Причини REJECTED: {reasons_line}\n\n"
        f"CONFIRMED всього: {len(trades_today)}\n"
        f"  🟢 GREEN: {len(conf_green)}\n"
        f"  🟡 YELLOW: {len(conf_yellow)}\n"
        f"  🔴 RED: {len(conf_red)}\n\n"
        f"FILLED: {len([t for t in filled])}\n"
        f"NO_FILL: {len(no_fill)}\n\n"
        f"Результати (тільки FILLED):\n"
        f"❌ SL: {sl}\n"
        f"✅ TP1: {tp1}\n"
        f"✅ TP2: {tp2}\n"
        f"✅ TP3: {tp3}\n"
        f"⏳ В роботі: {open_}\n\n"
        f"Нотатка: в TP/SL входять тільки FILLED (коли ціна доторкнулась limit)."
    )
    lab(lab_text)

    # SIGNALS report (only published)
    if len(posted) == 0:
        sig_text = (
            "📊 Daily Report (21:00)\n"
            "Сетапів (🟢/🟡) не було.\n"
            "Продовжуємо моніторинг."
        )
    else:
        sig_text = (
            "📊 Daily Report (21:00)\n"
            f"Опубліковано (🟢/🟡): {len(posted)}\n"
            f"  🟢 GREEN: {len(conf_green)}\n"
            f"  🟡 YELLOW: {len(conf_yellow)}\n\n"
            f"FILLED: {len([t for t in filled if t.get('color') in ('GREEN','YELLOW')])}\n"
            f"NO_FILL: {len([t for t in no_fill if t.get('color') in ('GREEN','YELLOW')])}\n\n"
            f"Результати (тільки FILLED):\n"
            f"❌ SL: {sl_pub}\n"
            f"✅ TP1: {tp1_pub}\n"
            f"✅ TP2: {tp2_pub}\n"
            f"✅ TP3: {tp3_pub}\n"
            f"⏳ В роботі: {open_pub}\n\n"
            "У TP/SL входять тільки FILLED (коли ціна доторкнулась limit)."
        )
    signals(sig_text)

    state.setdefault("daily", {})["last_stats_date"] = today
    save_state(state)

# =========================
# START / LOOP
# =========================
def announce_start() -> None:
    coins = "/".join([s.replace("USDT", "") for s in SYMBOLS])
    msg = f"✅ Моніторинг активовано — {TF}m по {coins}\nВерсія: {VERSION}"
    lab("🧪 LAB активовано\n" + msg)
    signals(msg)

def scanner_loop() -> None:
    state = load_state()
    announce_start()

    while True:
        # 1) confirm pending -> route to signals/lab by color
        try:
            process_pending(state)
        except Exception as e:
            print("process_pending error:", e)

        # 2) track fills (limit touched)
        try:
            process_trade_fills(state)
        except Exception as e:
            print("process_trade_fills error:", e)

        # 3) daily stats (lab + signals)
        try:
            send_daily_stats(state)
        except Exception as e:
            print("daily stats error:", e)

        # 4) scan new candidates only on NEW closed 15m candle
        for sym in SYMBOLS:
            try:
                compute_v1_candidate(sym, state)
            except Exception as e:
                print("scan error:", sym, e)

        time.sleep(CHECK_EVERY_SEC)

if __name__ == "__main__":
    scanner_loop()
