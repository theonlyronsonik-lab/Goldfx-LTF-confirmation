import json
import os
from flask import Flask, jsonify, request, render_template
from datetime import datetime, timezone

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

SIGNALS_FILE = "signals.json"

# Pip sizes per symbol (used for pips/profit calculation on close)
PIP_SIZES = {
    "XAU/USD": 0.1,
    "EUR/USD": 0.0001,
    "S&P 500": 0.01,
    "CAD/JPY": 0.01,
}

LOT_SIZE = 0.01

# ─────────────────────────────────────────────
# IN-PROCESS STATE
# ─────────────────────────────────────────────

# Set of signal "time" strings that have already been converted to trades.
# Seeded from trades_history on startup so we never re-execute old signals.
executed_signal_times: set = set()

# ─────────────────────────────────────────────
# PERSISTENCE HELPERS
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


# ─────────────────────────────────────────────
# STATS HELPER
# ─────────────────────────────────────────────

def compute_stats(trades: list) -> dict:
    """Recompute stats from a trades list."""
    closed = [t for t in trades if t.get("outcome") in ("WIN", "LOSS")]
    wins   = [t for t in closed if t["outcome"] == "WIN"]

    symbols  = list({t["symbol"] for t in trades})
    sessions = ["Asia", "London", "New York", "Off-Hours"]

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
# PIPS / PROFIT HELPERS
# ─────────────────────────────────────────────

def calc_pips(symbol: str, entry: float, close_price: float, direction: str) -> float:
    pip  = PIP_SIZES.get(symbol, 0.0001)
    diff = (close_price - entry) if direction == "BUY" else (entry - close_price)
    return round(diff / pip, 1)


def calc_profit(pips: float, lot_size: float = None) -> float:
    ls = lot_size if lot_size is not None else LOT_SIZE
    return round(pips * ls * 10, 2)


# ─────────────────────────────────────────────
# ACTIVE TRADE HELPERS
# ─────────────────────────────────────────────

def _get_active_trade_symbols(trades: list) -> set:
    """Return the set of symbols that currently have an OPEN trade."""
    return {t["symbol"] for t in trades if t.get("outcome") == "OPEN"}


def _has_active_trade(symbol: str, trades: list) -> bool:
    return any(
        t["symbol"] == symbol and t.get("outcome") == "OPEN"
        for t in trades
    )


# ─────────────────────────────────────────────
# STARTUP STATE RESTORE
# ─────────────────────────────────────────────

def restore_state() -> None:
    """
    Seed executed_signal_times from existing trades so we never re-execute
    a signal that already has a trade record (open or closed).
    """
    global executed_signal_times

    data           = load_signals()
    trades_history = data.get("trades_history", [])

    # Every trade's "time" field corresponds to the signal that spawned it.
    # Mark all of them as already executed.
    for trade in trades_history:
        sig_time = trade.get("time")
        if sig_time:
            executed_signal_times.add(sig_time)

    recent_signals = data.get("recent_signals", [])
    active_symbols = _get_active_trade_symbols(trades_history)

    print(
        f"[app] State restored — "
        f"{len(trades_history)} trades, "
        f"{len(recent_signals)} signals, "
        f"{len(executed_signal_times)} executed signal times, "
        f"active symbols: {active_symbols}"
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


# ── GET /get_pending_signals ─────────────────
# Returns all signals from recent_signals that:
#   1. Have NOT already been executed as a trade
#   2. Do NOT have an active (OPEN) trade for that symbol
#
# MT5 polls this to discover new signals it should act on.

@app.route("/get_pending_signals")
def get_pending_signals():
    data           = load_signals()
    recent_signals = data.get("recent_signals", [])
    trades_history = data.get("trades_history", [])

    # Symbols with an active open trade right now
    active_symbols = _get_active_trade_symbols(trades_history)

    pending = []
    for sig in recent_signals:
        sig_time = sig.get("time")
        symbol   = sig.get("symbol")

        # Skip if already executed
        if sig_time in executed_signal_times:
            continue

        # Skip if the symbol already has an open trade
        if symbol in active_symbols:
            continue

        # Build a clean pending signal object for MT5
        pip_size = PIP_SIZES.get(symbol, 0.0001)
        entry    = sig.get("entry")
        sl       = sig.get("sl")
        sig_type = sig.get("type")

        if sig_type == "BUY" and entry is not None and sl is not None:
            risk_pips = round((entry - sl) / pip_size, 1)
        elif sig_type == "SELL" and entry is not None and sl is not None:
            risk_pips = round((sl - entry) / pip_size, 1)
        else:
            risk_pips = None

        pending.append({
            "symbol":        symbol,
            "type":          sig_type,
            "time":          sig_time,
            "entry":         entry,
            "sl":            sl,
            "risk_pips":     risk_pips,
            "pip_size":      pip_size,
            "lot_size":      LOT_SIZE,
            "trend_aligned": sig.get("trend_aligned"),
            "label":         sig.get("label"),
            "session":       sig.get("session"),
            "rsi":           sig.get("rsi"),
            "trend":         sig.get("trend"),
            "context":       sig.get("context"),
        })

    return jsonify({
        "ok":      True,
        "count":   len(pending),
        "signals": pending,
    })


# ── POST /execute_signal ─────────────────────
# MT5 calls this to execute a specific signal as a trade.
# Body: { "signal_time": "2026-03-17 22:43 UTC" }
#
# Guards:
#   - Signal must exist in recent_signals (bot.py sent it to Telegram)
#   - Signal must not have been executed already
#   - Symbol must not already have an active (OPEN) trade

@app.route("/execute_signal", methods=["POST"])
def execute_signal():
    body        = request.get_json(force=True) or {}
    signal_time = body.get("signal_time")

    if not signal_time:
        return jsonify({"ok": False, "error": "signal_time is required"}), 400

    # Check in-process executed set first (fast path)
    if signal_time in executed_signal_times:
        return jsonify({
            "ok":    False,
            "error": "signal already executed",
            "time":  signal_time,
        }), 409

    data           = load_signals()
    recent_signals = data.get("recent_signals", [])
    trades_history = data.get("trades_history", [])

    # Find the signal in recent_signals
    signal = next(
        (s for s in recent_signals if s.get("time") == signal_time),
        None,
    )

    if signal is None:
        return jsonify({
            "ok":    False,
            "error": "signal not found in recent_signals — bot.py must send it to Telegram first",
            "time":  signal_time,
        }), 404

    symbol   = signal.get("symbol")
    sig_type = signal.get("type")
    entry    = signal.get("entry")
    sl       = signal.get("sl")

    if not all([symbol, sig_type, entry is not None, sl is not None]):
        return jsonify({
            "ok":    False,
            "error": "signal record is incomplete (missing symbol, type, entry, or sl)",
            "time":  signal_time,
        }), 422

    # Guard: no active trade for this symbol
    if _has_active_trade(symbol, trades_history):
        return jsonify({
            "ok":     False,
            "error":  f"symbol {symbol} already has an active trade",
            "symbol": symbol,
            "time":   signal_time,
        }), 409

    # All guards passed — open the trade record
    now_str  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    pip_size = PIP_SIZES.get(symbol, 0.0001)

    trade_rec = {
        "symbol":        symbol,
        "type":          sig_type,
        "time":          signal_time,   # use signal time as trade identifier
        "close_time":    None,
        "entry":         entry,
        "sl":            sl,
        "outcome":       "OPEN",
        "trend_aligned": signal.get("trend_aligned", False),
        "label":         signal.get("label", ""),
        "session":       signal.get("session", ""),
    }

    trades_history.append(trade_rec)

    # Mark signal as executed in-process
    executed_signal_times.add(signal_time)

    # Compute risk_pips for the response
    if sig_type == "BUY":
        risk_pips = round((entry - sl) / pip_size, 1)
    else:
        risk_pips = round((sl - entry) / pip_size, 1)

    # Update symbols entry so the dashboard reflects the new active trade
    active_trade_entry = {
        "type":          sig_type,
        "entry":         entry,
        "sl":            sl,
        "pip_size":      pip_size,
        "trend_aligned": signal.get("trend_aligned", False),
        "label":         signal.get("label", ""),
        "session":       signal.get("session", ""),
    }

    symbols = data.get("symbols") or {}
    if symbol not in symbols:
        symbols[symbol] = {}
    symbols[symbol]["active_trade"] = active_trade_entry
    symbols[symbol]["last_signal"]  = signal

    data["trades_history"] = trades_history[-200:]
    data["symbols"]        = symbols
    data["stats"]          = compute_stats(trades_history)

    save_signals(data)

    print(
        f"[app] Trade opened — {symbol} {sig_type} @ {entry} "
        f"SL {sl} | signal_time={signal_time}"
    )

    return jsonify({
        "ok":            True,
        "symbol":        symbol,
        "type":          sig_type,
        "entry":         entry,
        "sl":            sl,
        "risk_pips":     risk_pips,
        "pip_size":      pip_size,
        "lot_size":      LOT_SIZE,
        "trend_aligned": signal.get("trend_aligned"),
        "label":         signal.get("label"),
        "session":       signal.get("session"),
        "time":          signal_time,
        "executed_at":   now_str,
    })


# ── POST /close_trade ────────────────────────
# Body: { "symbol": "XAU/USD", "time": "2026-03-17 22:43 UTC",
#         "outcome": "WIN"|"LOSS"  (optional — auto-determined from price),
#         "close_price": 4990.0    (optional) }

@app.route("/close_trade", methods=["POST"])
def close_trade():
    body           = request.get_json(force=True) or {}
    symbol         = body.get("symbol")
    trade_time     = body.get("time")
    manual_outcome = body.get("outcome")    # "WIN" | "LOSS" | None
    close_price    = body.get("close_price")

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

    # Use close_price from body, or fall back to current price in symbols data
    if close_price is None:
        sym_data    = (data.get("symbols") or {}).get(symbol, {})
        close_price = sym_data.get("price")

    entry    = target.get("entry")
    dirn     = target.get("type")   # "BUY" | "SELL"
    now_str  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Calculate pips if we have both prices
    if close_price is not None and entry is not None:
        raw_pips = calc_pips(symbol, entry, float(close_price), dirn)
    else:
        raw_pips = None

    # Determine outcome
    if manual_outcome in ("WIN", "LOSS"):
        outcome = manual_outcome
    else:
        outcome = "WIN" if (raw_pips is not None and raw_pips >= 0) else "LOSS"

    target["outcome"]     = outcome
    target["close_price"] = round(float(close_price), 5) if close_price is not None else None
    target["close_time"]  = now_str
    target["pips"]        = raw_pips
    target["profit"]      = calc_profit(raw_pips) if raw_pips is not None else None

    # Clear active_trade on the symbol entry in the dashboard
    symbols = data.get("symbols") or {}
    if symbol in symbols:
        symbols[symbol]["active_trade"] = None
    data["symbols"] = symbols

    data["trades_history"] = trades
    data["stats"]          = compute_stats(trades)
    save_signals(data)

    print(
        f"[app] Trade closed — {symbol} {dirn} | outcome={outcome} "
        f"pips={raw_pips} profit={target.get('profit')} | trade_time={trade_time}"
    )

    return jsonify({
        "ok":          True,
        "outcome":     outcome,
        "pips":        raw_pips,
        "close_price": target["close_price"],
        "profit":      target["profit"],
    })


# ─────────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────────

restore_state()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
