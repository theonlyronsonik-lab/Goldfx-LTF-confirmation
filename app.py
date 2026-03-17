import json
import os
from flask import Flask, render_template, jsonify, request
from datetime import datetime, timezone

app = Flask(__name__)
SIGNALS_FILE = "signals.json"

PIP_SIZES = {"XAU/USD": 0.1, "GBP/USD": 0.0001, "SPY": 0.01, "QQQ": 0.01, "EUR/JPY": 0.01}


def load_signals():
    if not os.path.exists(SIGNALS_FILE):
        return {
            "bot_status": "starting",
            "last_scan": None,
            "session_active": False,
            "current_sessions": [],
            "symbols": {},
            "recent_signals": [],
            "trades_history": [],
            "stats": {
                "total": 0, "wins": 0, "losses": 0,
                "pending": 0, "win_rate": 0,
                "by_asset": {}, "by_session": {}
            },
        }
    try:
        with open(SIGNALS_FILE) as f:
            return json.load(f)
    except Exception:
        return {
            "bot_status": "error",
            "symbols": {},
            "recent_signals": [],
            "trades_history": [],
            "stats": {}
        }


def save_signals(data):
    with open(SIGNALS_FILE, "w") as f:
        json.dump(data, f, indent=2)


def recompute_stats(data):
    trades = data.get("trades_history", [])
    closed = [t for t in trades if t.get("outcome") in ("WIN", "LOSS")]
    wins   = [t for t in closed if t.get("outcome") == "WIN"]
    losses = [t for t in closed if t.get("outcome") == "LOSS"]
    open_t = [t for t in trades if t.get("outcome") == "OPEN"]
    total  = len(closed)
    wr     = round(len(wins) / total * 100, 1) if total else 0

    symbols = list({t["symbol"] for t in trades})
    sessions = ["Asia", "London", "New York", "Off-Hours"]

    by_asset = {}
    for sym in symbols:
        c = [t for t in closed if t["symbol"] == sym]
        w = sum(1 for t in c if t["outcome"] == "WIN")
        by_asset[sym] = {
            "total": len(c), "wins": w, "losses": len(c) - w,
            "win_rate": round(w / len(c) * 100, 1) if c else 0
        }

    by_session = {}
    for sess in sessions:
        c = [t for t in closed if t.get("session") == sess]
        w = sum(1 for t in c if t["outcome"] == "WIN")
        by_session[sess] = {
            "total": len(c), "wins": w, "losses": len(c) - w,
            "win_rate": round(w / len(c) * 100, 1) if c else 0
        }

    data["stats"] = {
        "total": total, "wins": len(wins), "losses": len(losses),
        "pending": len(open_t), "win_rate": wr,
        "by_asset": by_asset, "by_session": by_session
    }
    return data


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/data")
def api_data():
    data = load_signals()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    signals_today = sum(
        1 for s in data.get("recent_signals", [])
        if s.get("time", "").startswith(today)
    )
    data["signals_today"] = signals_today
    return jsonify(data)


@app.route("/close_trade", methods=["POST"])
def close_trade():
    body = request.get_json(force=True) or {}
    symbol    = body.get("symbol")
    trade_time = body.get("time")
    manual_outcome = body.get("outcome")   # "WIN" or "LOSS" or None (auto)

    if not symbol or not trade_time:
        return jsonify({"ok": False, "error": "symbol and time required"}), 400

    data = load_signals()
    trades = data.get("trades_history", [])

    # Find the matching OPEN trade
    target = None
    for t in trades:
        if t.get("symbol") == symbol and t.get("time") == trade_time and t.get("outcome") == "OPEN":
            target = t
            break

    if target is None:
        return jsonify({"ok": False, "error": "Trade not found or already closed"}), 404

    # Get current price from symbols data
    sym_data  = (data.get("symbols") or {}).get(symbol, {})
    cur_price = sym_data.get("price")

    pip_size = PIP_SIZES.get(symbol, 0.0001)
    entry    = target.get("entry")
    sl       = target.get("sl")
    dirn     = target.get("type")  # BUY or SELL

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if cur_price is not None and entry is not None:
        if dirn == "BUY":
            raw_pips = (cur_price - entry) / pip_size
        else:
            raw_pips = (entry - cur_price) / pip_size
        raw_pips = round(raw_pips, 1)
    else:
        raw_pips = None

    # Auto-determine outcome if not specified
    if manual_outcome in ("WIN", "LOSS"):
        outcome = manual_outcome
    else:
        if raw_pips is not None:
            outcome = "WIN" if raw_pips >= 0 else "LOSS"
        else:
            outcome = "LOSS"

    target["outcome"]     = outcome
    target["close_price"] = round(cur_price, 5) if cur_price is not None else None
    target["close_time"]  = now_str
    target["pips"]        = raw_pips
    target["profit"]      = round(raw_pips * 0.01 * 10, 2) if raw_pips is not None else None

    data = recompute_stats(data)
    save_signals(data)

    return jsonify({"ok": True, "outcome": outcome, "pips": raw_pips, "close_price": target["close_price"]})


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
