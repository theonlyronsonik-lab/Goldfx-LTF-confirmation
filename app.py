from flask import Flask, jsonify, request
import json
import os
import logging
from datetime import datetime, timezone

app = Flask(__name__)

# Configure logging to see MT5 connections in Railway logs
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DATA_DIR = "/app/data"
os.makedirs(DATA_DIR, exist_ok=True)
SIGNALS_FILE = os.path.join(DATA_DIR, "signals.json")

# ─────────────────────────────────────────────
# DATA HELPERS
# ─────────────────────────────────────────────

def load_data():
    """Reads the shared JSON state file."""
    if not os.path.exists(SIGNALS_FILE):
        return {"recent_signals": [], "trades_history": []}
    try:
        with open(SIGNALS_FILE, "r") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error reading JSON: {e}")
        return {"recent_signals": [], "trades_history": []}

def save_data(data):
    """Saves the state back to the JSON file."""
    try:
        with open(SIGNALS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving JSON: {e}")

# ─────────────────────────────────────────────
# API ROUTES
# ─────────────────────────────────────────────

@app.route('/', methods=['GET'])
def health_check():
    return jsonify({"status": "online", "message": "Signal Bridge is active"}), 200

@app.route('/get_pending_signals', methods=['GET'])
def get_pending_signals():
    """MT5 calls this to see if the Bot found any trades."""
    data = load_data()
    signals = data.get("recent_signals", [])
    
    # Log the request so you can see MT5 is actually reaching out
    logger.info(f"MT5 Polling: Found {len(signals)} signals.")
    
    return jsonify(signals)

@app.route('/execute_signal', methods=['POST'])
def execute_signal():
    """MT5 calls this AFTER it successfully opens a trade."""
    req = request.json
    if not req:
        return jsonify({"status": "error", "message": "No data provided"}), 400

    data = load_data()
    symbol = req.get("symbol")
    
    # Create the trade record
    new_trade = {
        "symbol": symbol,
        "type": req.get("type"),
        "entry": req.get("price"),
        "sl": req.get("sl"),
        "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "outcome": "OPEN"
    }
    
    # 1. Add to history
    data.setdefault("trades_history", []).append(new_trade)
    
    # 2. Clear this specific signal from pending so MT5 doesn't double-buy
    data["recent_signals"] = [s for s in data.get("recent_signals", []) if s.get("symbol") != symbol]
    
    save_data(data)
    logger.info(f"TRADE EXECUTED: {symbol} {req.get('type')}")
    
    return jsonify({"status": "success", "message": f"Recorded {symbol} as OPEN"})

@app.route('/close_trade', methods=['POST'])
def close_trade():
    """MT5 calls this when a trade hits TP or SL."""
    req = request.json
    data = load_data()
    symbol = req.get("symbol")
    
    found = False
    for t in data.get("trades_history", []):
        if t["symbol"] == symbol and t["outcome"] == "OPEN":
            t["outcome"] = req.get("outcome") # Should be "WIN" or "LOSS"
            t["close_price"] = req.get("close_price")
            t["close_time"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            found = True
            break
            
    if found:
        save_data(data)
        logger.info(f"TRADE CLOSED: {symbol} result: {req.get('outcome')}")
        return jsonify({"status": "success"})
    
    return jsonify({"status": "not_found", "message": "No open trade found for this symbol"}), 404

if __name__ == "__main__":
    # Ensure port is pulled from environment for Railway compatibility
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
