# app.py

import json
import logging
import os
from flask import Flask, jsonify, request
from datetime import datetime, timezone

SIGNALS_FILE = "/app/data/signals.json"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

SIGNALS_FILE = "/app/data/signals.json"
if not os.path.exists("/app/data"):
    SIGNALS_FILE = "signals.json"

PIP_SIZES = {
     "XAU/USD": 0.1,
    "EUR/USD": 0.0001,
    "AUD/CAD":     0.01,
    "CAD/JPY":     0.01,
}

LOT_SIZE = 0.01

executed_signal_times = set()


def load_data():
    if not os.path.exists(SIGNALS_FILE):
        return {"recent_signals": [], "trades_history": [], "symbols": {}}
    with open(SIGNALS_FILE) as f:
        return json.load(f)


def save_data(data):
    with open(SIGNALS_FILE, "w") as f:
        json.dump(data, f, indent=2)


def calc_pips(symbol, entry, price, direction):
    pip = PIP_SIZES.get(symbol, 0.0001)
    diff = (price - entry) if direction == "BUY" else (entry - price)
    return round(diff / pip, 1)


def calc_profit(pips):
    return round(pips * LOT_SIZE * 10, 2)


# 🔥 REAL RSI TP LOGIC
def manage_trade(symbol_data, trade):
    price = symbol_data.get("price")
    rsi = symbol_data.get("rsi")

    if not price or not rsi:
        return trade

    entry = trade["entry"]
    direction = trade["type"]

    # TP1 → RSI condition
    if not trade.get("tp1_hit"):
        if direction == "BUY" and rsi >= 65:
            trade["tp1_hit"] = True
        elif direction == "SELL" and rsi <= 35:
            trade["tp1_hit"] = True

    # Move SL to BE
    if trade.get("tp1_hit") and not trade.get("sl_moved"):
        trade["sl"] = entry
        trade["sl_moved"] = True

    # TP2 → stronger RSI extreme
    if not trade.get("tp2_hit"):
        if direction == "BUY" and rsi >= 75:
            trade["tp2_hit"] = True
        elif direction == "SELL" and rsi <= 25:
            trade["tp2_hit"] = True

    # Close trade at TP2
    if trade.get("tp2_hit"):
        trade["outcome"] = "WIN"
        trade["close_price"] = price
        trade["close_time"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        trade["pips"] = calc_pips(trade["symbol"], entry, price, direction)
        trade["profit"] = calc_profit(trade["pips"])

    return trade


app = Flask(__name__)


@app.route("/data")
def data():
    data = load_data()

    symbols = data.get("symbols", {})
    trades = data.get("trades_history", [])

    for trade in trades:
        if trade.get("outcome") == "OPEN":
            sym = symbols.get(trade["symbol"], {})
            manage_trade(sym, trade)

            # SL hit
            price = sym.get("price")
            if price:
                if trade["type"] == "BUY" and price <= trade["sl"]:
                    trade["outcome"] = "LOSS"
                elif trade["type"] == "SELL" and price >= trade["sl"]:
                    trade["outcome"] = "LOSS"

                if trade.get("outcome") != "OPEN":
                    trade["close_price"] = price
                    trade["close_time"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                    trade["pips"] = calc_pips(trade["symbol"], trade["entry"], price, trade["type"])
                    trade["profit"] = calc_profit(trade["pips"])

                    symbols[trade["symbol"]]["active_trade"] = None

    save_data(data)
    return jsonify(data)


@app.route("/get_pending_signals")
def get_pending():
    data = load_data()
    trades = data.get("trades_history", [])
    signals = data.get("recent_signals", [])

    log.info("=== /get_pending_signals called ===")
    log.info("Total recent_signals in file: %d", len(signals))
    log.info("Total trades_history in file: %d", len(trades))

    active_symbols = {
        t["symbol"] for t in trades if t.get("outcome") == "OPEN"
    }
    log.info("Active (OPEN) symbols: %s", active_symbols)
    log.info("Already-executed signal times: %s", executed_signal_times)

    pending = []

    for s in signals:
        sig_time = s.get("time", "")
        sig_sym  = s.get("symbol", "")

        if sig_time in executed_signal_times:
            log.info("  SKIP (already executed): %s @ %s", sig_sym, sig_time)
            continue
        if sig_sym in active_symbols:
            log.info("  SKIP (active trade exists): %s @ %s", sig_sym, sig_time)
            continue

        log.info("  PENDING: %s %s @ %s  entry=%.5f  sl=%.5f",
                 sig_sym, s.get("type", "?"), sig_time,
                 s.get("entry", 0), s.get("sl", 0))
        pending.append({
            "symbol":        s.get("symbol", ""),
            "type":          s.get("type", ""),
            "time":          s.get("time", ""),
            "entry":         s.get("entry", 0),
            "sl":            s.get("sl", 0),
            "risk_pips":     s.get("risk_pips", 0),
            "trend_aligned": s.get("trend_aligned", False),
            "label":         s.get("label", ""),
            "session":       s.get("session", ""),
            "rsi":           s.get("rsi", 0),
            "trend":         s.get("trend", ""),
            "context":       s.get("context", ""),
        })

    log.info("Returning %d pending signal(s) to MT5", len(pending))
    return jsonify({"ok": True, "signals": pending})


@app.route("/execute_signal", methods=["POST"])
def execute():
    log.info("=== /execute_signal called ===")

    body = request.get_json(silent=True)
    if not body:
        resp = {"ok": False, "error": "invalid or missing JSON body"}
        log.warning("Response to MT5: %s", resp)
        return jsonify(resp), 400

    log.info("Request body from MT5: %s", body)

    signal_time = body.get("signal_time", "").strip()
    if not signal_time:
        resp = {"ok": False, "error": "signal_time is required"}
        log.warning("Response to MT5: %s", resp)
        return jsonify(resp), 400

    log.info("Executing signal_time: %s", signal_time)

    data = load_data()
    signals = data.get("recent_signals", [])

    log.info("Searching %d recent_signals for time=%s", len(signals), signal_time)
    log.debug("Full signals.json content: %s", json.dumps(data, indent=2))

    signal = next((s for s in signals if s.get("time", "") == signal_time), None)
    if not signal:
        available = [s.get("time", "") for s in signals]
        log.warning("Signal not found. Available times: %s", available)
        resp = {"ok": False, "error": f"signal not found for time={signal_time}"}
        log.warning("Response to MT5: %s", resp)
        return jsonify(resp), 404

    log.info("Found signal: %s", signal)

    # ── Validate required fields ──────────────────────────────────────────
    symbol = signal.get("symbol", "").strip()
    sig_type = signal.get("type", "").strip()
    entry = signal.get("entry")
    sl = signal.get("sl")

    if not symbol:
        resp = {"ok": False, "error": "signal has no symbol"}
        log.warning("Response to MT5: %s", resp)
        return jsonify(resp), 422

    if sig_type not in ("BUY", "SELL"):
        resp = {"ok": False, "error": f"invalid signal type: {sig_type!r}"}
        log.warning("Response to MT5: %s", resp)
        return jsonify(resp), 422

    try:
        entry = float(entry)
        sl = float(sl)
    except (TypeError, ValueError):
        resp = {"ok": False, "error": f"entry or sl is not a valid number: entry={entry!r}, sl={sl!r}"}
        log.warning("Response to MT5: %s", resp)
        return jsonify(resp), 422

    if entry <= 0 or sl <= 0:
        resp = {"ok": False, "error": f"entry and sl must be positive: entry={entry}, sl={sl}"}
        log.warning("Response to MT5: %s", resp)
        return jsonify(resp), 422

    # ── Build trade record ────────────────────────────────────────────────
    trade = {
        "symbol":        symbol,
        "type":          sig_type,
        "entry":         entry,
        "sl":            sl,
        "time":          signal_time,
        "outcome":       "OPEN",
        "tp1_hit":       False,
        "tp2_hit":       False,
        "sl_moved":      False,
        "trend_aligned": signal.get("trend_aligned", False),
        "label":         signal.get("label", ""),
        "session":       signal.get("session", ""),
    }

    log.info("Creating trade record: %s", trade)

    data["trades_history"].append(trade)
    executed_signal_times.add(signal_time)
    log.info("Added signal_time %s to executed set", signal_time)

    # ── Update symbols entry ──────────────────────────────────────────────
    if symbol not in data.get("symbols", {}):
        log.warning("Symbol %s not found in symbols dict — initialising entry", symbol)
        data.setdefault("symbols", {})[symbol] = {}

    data["symbols"][symbol]["active_trade"] = trade
    log.info("Updated symbols[%s][active_trade]", symbol)

    save_data(data)
    log.info("signals.json saved successfully")

    resp = {
        "ok":     True,
        "symbol": symbol,
        "type":   sig_type,
        "entry":  entry,
        "sl":     sl,
        "time":   signal_time,
    }
    log.info("Response to MT5: %s", resp)
    return jsonify(resp)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
