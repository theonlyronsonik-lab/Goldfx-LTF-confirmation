import json
import os
import requests
import pandas as pd
import numpy as np
from flask import Flask, jsonify, request, render_template
from datetime import datetime, timezone

# ─────────────────────────────────────────────
# CONFIG  (mirrors bot.py exactly)
# ─────────────────────────────────────────────

API_KEY  = os.getenv("API_KEY", "")
INTERVAL = "5min"

RSI_OVERBOUGHT = 70
RSI_OVERSOLD   = 30

SIGNALS_FILE = "signals.json"

# Symbol-specific SL buffers (applied beyond the 5-candle wick)
SL_BUFFERS = {
    "XAU/USD": 0.50,
    "EUR/USD": 0.0003,
    "S&P 500": 0.10,
    "CAD/JPY": 0.10,
}

# Pip sizes per symbol
PIP_SIZES = {
    "XAU/USD": 0.1,
    "EUR/USD": 0.0001,
    "S&P 500": 0.01,
    "CAD/JPY": 0.01,
}

LOT_SIZE = 0.01

SESSIONS = {
    "Asia":     (1,  7),
    "London":   (7,  15),
    "New York": (14, 21),
}

# ─────────────────────────────────────────────
# IN-PROCESS STATE  (restored from signals.json on startup)
# ─────────────────────────────────────────────

# signal_stack holds the last two raw signals per symbol for double-confirmation
# { symbol: ["BUY"|"SELL", ...] }
signal_stack: dict  = {}

# last_div_time tracks the divergence candle already acted on to prevent re-firing
# { symbol: {"BULL": candle_dt_str, "BEAR": candle_dt_str} }
last_div_time: dict = {}

# active_trade mirrors bot.py's in-memory trade state
# { symbol: { type, entry, sl, pip_size, trend_aligned, label, session, rsi_alerted } }
active_trade: dict  = {}

# recent_signals / trades_history are the canonical lists written to signals.json
recent_signals: list = []
trades_history: list = []

# ─────────────────────────────────────────────
# SESSIONS
# ─────────────────────────────────────────────

def get_active_sessions() -> list:
    hour = datetime.now(timezone.utc).hour
    active = [name for name, (s, e) in SESSIONS.items() if s <= hour <= e]
    return active if active else ["Off-Hours"]


def session_label(sessions: list) -> str:
    return " / ".join(sessions) if sessions else "Off-Hours"


# ─────────────────────────────────────────────
# INDICATORS  (identical to bot.py)
# ─────────────────────────────────────────────

def calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta    = series.diff()
    gain     = delta.clip(lower=0)
    loss     = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs       = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calc_sma200(series: pd.Series) -> pd.Series:
    return series.rolling(200).mean()


def calc_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    h  = df["high"]
    l  = df["low"]
    c  = df["close"]
    tr = pd.concat([
        (h - l),
        (h - c.shift()).abs(),
        (l - c.shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


# ─────────────────────────────────────────────
# PIVOTS  (identical to bot.py)
# ─────────────────────────────────────────────

def pivot_low(series: pd.Series, left: int = 5, right: int = 5) -> list:
    pivots = []
    vals   = series.values
    for i in range(left, len(vals) - right):
        window = vals[i - left: i + right + 1]
        if vals[i] == np.min(window):
            pivots.append(i)
    return pivots


def pivot_high(series: pd.Series, left: int = 5, right: int = 5) -> list:
    pivots = []
    vals   = series.values
    for i in range(left, len(vals) - right):
        window = vals[i - left: i + right + 1]
        if vals[i] == np.max(window):
            pivots.append(i)
    return pivots


# ─────────────────────────────────────────────
# DIVERGENCE  (identical to bot.py)
# ─────────────────────────────────────────────

def bullish_div(df: pd.DataFrame):
    lows = pivot_low(df["low"])
    if len(lows) < 2:
        return False, None
    i1, i2  = lows[-2], lows[-1]
    price_ll = df["low"].iloc[i2] < df["low"].iloc[i1]
    rsi_hl   = df["rsi"].iloc[i2] > df["rsi"].iloc[i1]
    if price_ll and rsi_hl:
        return True, i2
    return False, None


def bearish_div(df: pd.DataFrame):
    highs = pivot_high(df["high"])
    if len(highs) < 2:
        return False, None
    i1, i2  = highs[-2], highs[-1]
    price_hh = df["high"].iloc[i2] > df["high"].iloc[i1]
    rsi_lh   = df["rsi"].iloc[i2]  < df["rsi"].iloc[i1]
    if price_hh and rsi_lh:
        return True, i2
    return False, None


# ─────────────────────────────────────────────
# DOUBLE CONFIRMATION  (identical to bot.py)
# ─────────────────────────────────────────────

def double_confirm(symbol: str, signal: str):
    if symbol not in signal_stack:
        signal_stack[symbol] = []
    signal_stack[symbol].append(signal)
    if len(signal_stack[symbol]) > 2:
        signal_stack[symbol].pop(0)
    if signal_stack[symbol] == ["BUY", "BUY"]:
        return "BUY"
    if signal_stack[symbol] == ["SELL", "SELL"]:
        return "SELL"
    return None


# ─────────────────────────────────────────────
# SL / PIPS / PROFIT HELPERS  (identical to bot.py)
# ─────────────────────────────────────────────

def get_sl_buy(df: pd.DataFrame, symbol: str) -> float:
    """SL = lowest low of last 5 candles minus buffer."""
    buf  = SL_BUFFERS.get(symbol, 0.0001)
    low5 = df["low"].iloc[-5:].min()
    return round(low5 - buf, 5)


def get_sl_sell(df: pd.DataFrame, symbol: str) -> float:
    """SL = highest high of last 5 candles plus buffer."""
    buf   = SL_BUFFERS.get(symbol, 0.0001)
    high5 = df["high"].iloc[-5:].max()
    return round(high5 + buf, 5)


def calc_pips(symbol: str, entry: float, close_price: float, direction: str) -> float:
    pip  = PIP_SIZES.get(symbol, 0.0001)
    diff = (close_price - entry) if direction == "BUY" else (entry - close_price)
    return round(diff / pip, 1)


def calc_profit(pips: float, lot_size: float = None) -> float:
    ls = lot_size if lot_size is not None else LOT_SIZE
    return round(pips * ls * 10, 2)


# ─────────────────────────────────────────────
# MARKET CONTEXT  (identical to bot.py)
# ─────────────────────────────────────────────

def get_market_context(symbol: str, price, rsi, sma200, atr, trend) -> str:
    tips = []

    if rsi is None:
        return "Insufficient data."

    if rsi >= RSI_OVERBOUGHT:
        tips.append(f"RSI {rsi:.1f} — overbought, momentum may be exhausting")
    elif rsi >= 70:
        tips.append(f"RSI {rsi:.1f} — elevated, strong momentum but watch for pullback")
    elif rsi <= RSI_OVERSOLD:
        tips.append(f"RSI {rsi:.1f} — oversold, potential bounce zone")
    elif rsi <= 30:
        tips.append(f"RSI {rsi:.1f} — weak, selling pressure present")
    else:
        tips.append(f"RSI {rsi:.1f} — neutral zone")

    if trend == "BULLISH":
        tips.append("Above SMA200 — long-term uptrend")
    elif trend == "BEARISH":
        tips.append("Below SMA200 — long-term downtrend")

    if atr and price:
        vol_pct = (atr / price) * 100
        if vol_pct > 1.0:
            tips.append("High volatility — consider reduced size")
        elif vol_pct < 0.2:
            tips.append("Low volatility — tight conditions")

    hour = datetime.now(timezone.utc).hour
    if 14 <= hour <= 20:
        tips.append("NY session active — peak liquidity window")
    elif 7 <= hour <= 10:
        tips.append("London/Asia overlap — elevated volatility possible")

    return " | ".join(tips)


# ─────────────────────────────────────────────
# DATA FETCH  (identical to bot.py)
# ─────────────────────────────────────────────

def get_data(symbol: str):
    url = (
        f"https://api.twelvedata.com/time_series"
        f"?symbol={symbol}&interval={INTERVAL}&outputsize=210&apikey={API_KEY}"
    )
    try:
        r = requests.get(url, timeout=15).json()
    except Exception as e:
        print(f"Fetch error {symbol}: {e}")
        return None

    if "values" not in r:
        print(f"No data for {symbol}: {r.get('message', '')}")
        return None

    df = pd.DataFrame(r["values"]).iloc[::-1].reset_index(drop=True)
    for c in ["open", "high", "low", "close"]:
        df[c] = df[c].astype(float)
    return df


# ─────────────────────────────────────────────
# PERSISTENCE  (shared signals.json with bot.py)
# ─────────────────────────────────────────────

def _empty_state() -> dict:
    return {
        "bot_status":       "starting",
        "last_scan":        None,
        "session_active":   False,
        "current_sessions": [],
        "symbols":          {},
        "recent_signals":   [],
        "trades_history":   [],
        "stats": {
            "total": 0, "wins": 0, "losses": 0,
            "pending": 0, "win_rate": 0,
            "by_asset": {}, "by_session": {},
        },
    }


def load_signals() -> dict:
    if not os.path.exists(SIGNALS_FILE):
        return _empty_state()
    try:
        with open(SIGNALS_FILE) as f:
            return json.load(f)
    except Exception:
        return _empty_state()


def save_signals(data: dict) -> None:
    try:
        with open(SIGNALS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"Save error: {e}")


def compute_stats(trades: list) -> dict:
    """Recompute stats from a trades list — mirrors bot.py's compute_stats()."""
    closed = [t for t in trades if t.get("outcome") in ("WIN", "LOSS")]
    wins   = [t for t in closed if t["outcome"] == "WIN"]

    symbols  = list({t["symbol"] for t in trades})
    sessions = list(SESSIONS.keys()) + ["Off-Hours"]

    by_asset = {}
    for sym in symbols:
        c = [t for t in closed if t["symbol"] == sym]
        w = sum(1 for t in c if t["outcome"] == "WIN")
        by_asset[sym] = {
            "total":    len(c),
            "wins":     w,
            "losses":   len(c) - w,
            "win_rate": round(w / len(c) * 100, 1) if c else 0,
        }

    by_session = {}
    for sess in sessions:
        c = [t for t in closed if sess in t.get("session", "")]
        w = sum(1 for t in c if t["outcome"] == "WIN")
        by_session[sess] = {
            "total":    len(c),
            "wins":     w,
            "losses":   len(c) - w,
            "win_rate": round(w / len(c) * 100, 1) if c else 0,
        }

    total = len(closed)
    return {
        "total":      total,
        "wins":       len(wins),
        "losses":     total - len(wins),
        "pending":    len([t for t in trades if t.get("outcome") == "OPEN"]),
        "win_rate":   round(len(wins) / total * 100, 1) if total else 0,
        "by_asset":   by_asset,
        "by_session": by_session,
    }


# ─────────────────────────────────────────────
# STARTUP STATE RESTORE
# ─────────────────────────────────────────────

def restore_state() -> None:
    """
    Load signals.json and restore in-process state so the API shares the same
    context as bot.py (active trades, recent signals, trades history).
    """
    global recent_signals, trades_history

    data = load_signals()
    recent_signals = data.get("recent_signals", [])
    trades_history = data.get("trades_history", [])

    # Restore active_trade from the most recent OPEN record per symbol
    seen_symbols = {t["symbol"] for t in trades_history}
    for sym in seen_symbols:
        open_trades = [
            t for t in trades_history
            if t["symbol"] == sym and t.get("outcome") == "OPEN"
        ]
        if open_trades:
            last = open_trades[-1]
            active_trade[sym] = {
                "type":          last["type"],
                "entry":         last["entry"],
                "sl":            last["sl"],
                "pip_size":      PIP_SIZES.get(sym, 0.0001),
                "trend_aligned": last.get("trend_aligned", False),
                "label":         last.get("label", ""),
                "session":       last.get("session", ""),
                "rsi_alerted":   False,
            }

    print(
        f"[app] State restored — "
        f"{len(trades_history)} trades, "
        f"{len(recent_signals)} signals, "
        f"active: {list(active_trade.keys())}"
    )


# ─────────────────────────────────────────────
# FLASK APP
# ─────────────────────────────────────────────

app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html")


# ── GET /data ────────────────────────────────
# Returns the full current state from signals.json, enriched with a
# signals_today count so the dashboard doesn't need to compute it.

@app.route("/data")
def api_data():
    data  = load_signals()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    data["signals_today"] = sum(
        1 for s in data.get("recent_signals", [])
        if s.get("time", "").startswith(today)
    )
    return jsonify(data)


# ── POST /close_trade ────────────────────────
# Body: { "symbol": "XAU/USD", "time": "2026-03-17 22:43 UTC",
#         "outcome": "WIN"|"LOSS"  (optional — auto-determined from price) }

@app.route("/close_trade", methods=["POST"])
def close_trade():
    body       = request.get_json(force=True) or {}
    symbol     = body.get("symbol")
    trade_time = body.get("time")
    manual_outcome = body.get("outcome")   # "WIN" | "LOSS" | None

    if not symbol or not trade_time:
        return jsonify({"ok": False, "error": "symbol and time required"}), 400

    data   = load_signals()
    trades = data.get("trades_history", [])

    # Find the matching OPEN trade
    target = None
    for t in trades:
        if (
            t.get("symbol")  == symbol
            and t.get("time") == trade_time
            and t.get("outcome") == "OPEN"
        ):
            target = t
            break

    if target is None:
        return jsonify({"ok": False, "error": "Trade not found or already closed"}), 404

    # Current price from symbols data (written by bot.py on each scan)
    sym_data  = (data.get("symbols") or {}).get(symbol, {})
    cur_price = sym_data.get("price")

    pip_size = PIP_SIZES.get(symbol, 0.0001)
    entry    = target.get("entry")
    dirn     = target.get("type")   # "BUY" | "SELL"
    now_str  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Calculate pips from current price if available
    if cur_price is not None and entry is not None:
        raw_pips = calc_pips(symbol, entry, cur_price, dirn)
    else:
        raw_pips = None

    # Determine outcome
    if manual_outcome in ("WIN", "LOSS"):
        outcome = manual_outcome
    else:
        outcome = "WIN" if (raw_pips is not None and raw_pips >= 0) else "LOSS"

    target["outcome"]     = outcome
    target["close_price"] = round(cur_price, 5) if cur_price is not None else None
    target["close_time"]  = now_str
    target["pips"]        = raw_pips
    target["profit"]      = calc_profit(raw_pips) if raw_pips is not None else None

    data["trades_history"] = trades
    data["stats"]          = compute_stats(trades)
    save_signals(data)

    # Keep in-process active_trade in sync
    if symbol in active_trade and active_trade[symbol].get("entry") == entry:
        del active_trade[symbol]

    return jsonify({
        "ok":          True,
        "outcome":     outcome,
        "pips":        raw_pips,
        "close_price": target["close_price"],
    })


# ── GET /signal/<symbol> ─────────────────────
# Fetches live OHLCV data, runs the full bot signal pipeline
# (indicators → divergence → double-confirmation → SL calc),
# and returns a structured signal object for MT5 to consume.
#
# Response when a confirmed signal fires:
#   { "signal": "BUY"|"SELL", "entry": ..., "sl": ..., "risk_pips": ...,
#     "symbol": ..., "session": ..., "trend": ..., "label": ...,
#     "rsi": ..., "context": ..., "time": ... }
#
# Response when no confirmed signal:
#   { "signal": "NONE", "symbol": ..., "reason": ...,
#     "price": ..., "rsi": ..., "trend": ..., "session": ... }

@app.route("/signal/<path:symbol>")
def get_signal(symbol: str):
    sessions  = get_active_sessions()
    sess_str  = session_label(sessions)
    now       = datetime.now(timezone.utc)
    now_str   = now.strftime("%Y-%m-%d %H:%M UTC")

    # ── Fetch & prepare data ──────────────────
    df = get_data(symbol)
    if df is None:
        return jsonify({
            "signal":  "NONE",
            "symbol":  symbol,
            "reason":  "data_unavailable",
            "session": sess_str,
        }), 503

    df["rsi"]    = calc_rsi(df["close"])
    df["sma200"] = calc_sma200(df["close"])
    df["atr"]    = calc_atr(df)

    price  = round(df["close"].iloc[-2], 5)
    rsi    = round(df["rsi"].iloc[-1], 2)
    sma200 = df["sma200"].iloc[-1]
    atr    = df["atr"].iloc[-1]

    sma200_val = round(sma200, 5) if not pd.isna(sma200) else None
    atr_val    = round(atr,    5) if not pd.isna(atr)    else None
    trend      = None
    if sma200_val:
        trend = "BULLISH" if price > sma200_val else "BEARISH"

    base_info = {
        "symbol":  symbol,
        "price":   price,
        "rsi":     rsi,
        "trend":   trend,
        "session": sess_str,
    }

    # ── Active trade guard ────────────────────
    # Reload active_trade from signals.json so we stay in sync with bot.py
    _sync_active_trade(symbol)

    if symbol in active_trade:
        at = active_trade[symbol]
        return jsonify({
            **base_info,
            "signal": "NONE",
            "reason": "active_trade",
            "active_trade": {
                "type":  at["type"],
                "entry": at["entry"],
                "sl":    at["sl"],
            },
        })

    # ── Divergence detection ──────────────────
    bull, bull_idx = bullish_div(df)
    bear, bear_idx = bearish_div(df)

    sym_div = last_div_time.setdefault(symbol, {})

    # Deduplicate: skip if we already acted on this exact divergence candle
    if bull and bull_idx is not None:
        bull_candle_dt = (
            str(df["datetime"].iloc[bull_idx])
            if "datetime" in df.columns
            else str(bull_idx)
        )
        if sym_div.get("BULL") == bull_candle_dt:
            bull = False
    else:
        bull_candle_dt = None

    if bear and bear_idx is not None:
        bear_candle_dt = (
            str(df["datetime"].iloc[bear_idx])
            if "datetime" in df.columns
            else str(bear_idx)
        )
        if sym_div.get("BEAR") == bear_candle_dt:
            bear = False
    else:
        bear_candle_dt = None

    # ── BUY path ──────────────────────────────
    if bull and bull_idx is not None:
        ds = double_confirm(symbol, "BUY")
        if ds == "BUY":
            entry         = price
            sl            = get_sl_buy(df, symbol)
            pip_size      = PIP_SIZES.get(symbol, 0.0001)
            trend_aligned = (trend == "BULLISH")
            label         = "Trend Aligned Signal" if trend_aligned else "Counter-Trend Signal"
            context       = get_market_context(symbol, price, rsi, sma200_val, atr_val, trend)
            risk_pips     = round((entry - sl) / pip_size, 1)

            # Persist signal + trade record into signals.json
            sig_rec = {
                "symbol":        symbol,
                "type":          "BUY",
                "time":          now_str,
                "entry":         entry,
                "sl":            sl,
                "trend_aligned": trend_aligned,
                "label":         label,
                "session":       sess_str,
                "rsi":           rsi,
                "trend":         trend,
                "context":       context,
            }
            _persist_signal(sig_rec, symbol, "BUY", entry, sl, trend_aligned, label, sess_str, pip_size)

            # Mark divergence candle as seen
            if bull_candle_dt:
                last_div_time[symbol]["BULL"] = bull_candle_dt

            return jsonify({
                **base_info,
                "signal":        "BUY",
                "entry":         entry,
                "sl":            sl,
                "risk_pips":     risk_pips,
                "pip_size":      pip_size,
                "lot_size":      LOT_SIZE,
                "trend_aligned": trend_aligned,
                "label":         label,
                "context":       context,
                "time":          now_str,
            })

    # ── SELL path ─────────────────────────────
    if bear and bear_idx is not None:
        ds = double_confirm(symbol, "SELL")
        if ds == "SELL":
            entry         = price
            sl            = get_sl_sell(df, symbol)
            pip_size      = PIP_SIZES.get(symbol, 0.0001)
            trend_aligned = (trend == "BEARISH")
            label         = "Trend Aligned Signal" if trend_aligned else "Counter-Trend Signal"
            context       = get_market_context(symbol, price, rsi, sma200_val, atr_val, trend)
            risk_pips     = round((sl - entry) / pip_size, 1)

            sig_rec = {
                "symbol":        symbol,
                "type":          "SELL",
                "time":          now_str,
                "entry":         entry,
                "sl":            sl,
                "trend_aligned": trend_aligned,
                "label":         label,
                "session":       sess_str,
                "rsi":           rsi,
                "trend":         trend,
                "context":       context,
            }
            _persist_signal(sig_rec, symbol, "SELL", entry, sl, trend_aligned, label, sess_str, pip_size)

            if bear_candle_dt:
                last_div_time[symbol]["BEAR"] = bear_candle_dt

            return jsonify({
                **base_info,
                "signal":        "SELL",
                "entry":         entry,
                "sl":            sl,
                "risk_pips":     risk_pips,
                "pip_size":      pip_size,
                "lot_size":      LOT_SIZE,
                "trend_aligned": trend_aligned,
                "label":         label,
                "context":       context,
                "time":          now_str,
            })

    # ── No confirmed signal ───────────────────
    pending_bull = "BUY"  in (signal_stack.get(symbol) or [])
    pending_bear = "SELL" in (signal_stack.get(symbol) or [])
    if pending_bull:
        reason = "awaiting_second_bull_confirmation"
    elif pending_bear:
        reason = "awaiting_second_bear_confirmation"
    elif bull or bear:
        reason = "divergence_detected_first_confirmation"
    else:
        reason = "no_divergence"

    return jsonify({
        **base_info,
        "signal": "NONE",
        "reason": reason,
        "atr":    atr_val,
        "sma200": sma200_val,
    })


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def _sync_active_trade(symbol: str) -> None:
    """
    Re-read signals.json to pick up any active trade that bot.py may have
    opened since the last API call, keeping in-process state current.
    """
    data = load_signals()
    sym_data = (data.get("symbols") or {}).get(symbol, {})
    at = sym_data.get("active_trade")
    if at and at.get("type") and at.get("entry") is not None:
        active_trade[symbol] = {
            "type":          at["type"],
            "entry":         at["entry"],
            "sl":            at.get("sl"),
            "pip_size":      PIP_SIZES.get(symbol, 0.0001),
            "trend_aligned": at.get("trend_aligned", False),
            "label":         at.get("label", ""),
            "session":       at.get("session", ""),
            "rsi_alerted":   False,
        }
    elif symbol in active_trade:
        # Bot closed the trade — remove from our cache
        open_trades = [
            t for t in data.get("trades_history", [])
            if t["symbol"] == symbol and t.get("outcome") == "OPEN"
        ]
        if not open_trades:
            del active_trade[symbol]


def _persist_signal(
    sig_rec: dict,
    symbol: str,
    sig_type: str,
    entry: float,
    sl: float,
    trend_aligned: bool,
    label: str,
    sess_str: str,
    pip_size: float,
) -> None:
    """
    Write a confirmed signal + open trade record into signals.json so that
    bot.py's dashboard and the API share the same state.
    """
    global recent_signals, trades_history

    data = load_signals()
    recent_signals = data.get("recent_signals", [])
    trades_history = data.get("trades_history", [])

    recent_signals.append(sig_rec)

    trade_rec = {
        "symbol":        symbol,
        "type":          sig_type,
        "time":          sig_rec["time"],
        "close_time":    None,
        "entry":         entry,
        "sl":            sl,
        "outcome":       "OPEN",
        "trend_aligned": trend_aligned,
        "label":         label,
        "session":       sess_str,
    }
    trades_history.append(trade_rec)

    # Update active_trade in-process
    active_trade[symbol] = {
        "type":          sig_type,
        "entry":         entry,
        "sl":            sl,
        "pip_size":      pip_size,
        "trend_aligned": trend_aligned,
        "label":         label,
        "session":       sess_str,
        "rsi_alerted":   False,
    }

    # Merge back into the full state document
    data["recent_signals"] = recent_signals[-50:]
    data["trades_history"] = trades_history[-200:]
    data["stats"]          = compute_stats(trades_history)

    # Update the symbol entry so the dashboard shows the new signal
    sym_entry = (data.get("symbols") or {}).get(symbol, {})
    sym_entry["active_trade"] = active_trade[symbol]
    sym_entry["last_signal"]  = sig_rec
    if "symbols" not in data:
        data["symbols"] = {}
    data["symbols"][symbol] = sym_entry

    save_signals(data)


# ─────────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────────

restore_state()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)

