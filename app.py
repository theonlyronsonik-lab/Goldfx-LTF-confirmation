# app.py

import json
import os
from flask import Flask, jsonify, request
from datetime import datetime, timezone

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

    active_symbols = {
        t["symbol"] for t in trades if t.get("outcome") == "OPEN"
    }

    pending = []

    for s in signals:
        if s["time"] in executed_signal_times:
            continue
        if s["symbol"] in active_symbols:
            continue

        pending.append(s)

    return jsonify({"signals": pending})


@app.route("/execute_signal", methods=["POST"])
def execute():
    body = request.json
    signal_time = body["signal_time"]

    data = load_data()
    signals = data.get("recent_signals", [])

    signal = next((s for s in signals if s["time"] == signal_time), None)
    if not signal:
        return jsonify({"error": "not found"}), 404

    trade = {
        "symbol": signal["symbol"],
        "type": signal["type"],
        "entry": signal["entry"],
        "sl": signal["sl"],
        "time": signal_time,
        "outcome": "OPEN",
        "tp1_hit": False,
        "tp2_hit": False,
        "sl_moved": False,
    }

    data["trades_history"].append(trade)
    executed_signal_times.add(signal_time)

    data["symbols"][signal["symbol"]]["active_trade"] = trade

    save_data(data)

    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
