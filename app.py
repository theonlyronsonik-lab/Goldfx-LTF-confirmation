from flask import Flask, jsonify, request
import json
import os
import logging

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

DATA_DIR = "/app/data"
SIGNALS_FILE = os.path.join(DATA_DIR, "signals.json")

def load_data():
    if not os.path.exists(SIGNALS_FILE):
        return {"recent_signals": [], "trades_history": []}
    try:
        with open(SIGNALS_FILE, "r") as f:
            return json.load(f)
    except:
        return {"recent_signals": [], "trades_history": []}

def save_data(data):
    try:
        with open(SIGNALS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        app.logger.error(f"Save error: {e}")

@app.route('/get_pending_signals', methods=['GET'])
def get_pending_signals():
    data = load_data()
    # We send ALL recent signals and let the MT5 EA decide if it wants to take them
    return jsonify(data.get("recent_signals", []))

@app.route('/execute_signal', methods=['POST'])
def execute_signal():
    """Called by MT5 once a trade is successfully opened"""
    req = request.json
    data = load_data()
    
    new_trade = {
        "symbol": req.get("symbol"),
        "type": req.get("type"),
        "entry": req.get("price"),
        "sl": req.get("sl"),
        "time": req.get("time"),
        "outcome": "OPEN"
    }
    
    data["trades_history"].append(new_trade)
    # Remove the signal from pending once it is executed
    data["recent_signals"] = [s for s in data["recent_signals"] if s["symbol"] != req.get("symbol")]
    
    save_data(data)
    return jsonify({"status": "success", "message": "Trade recorded as OPEN"})

@app.route('/close_trade', methods=['POST'])
def close_trade():
    """Called by MT5 when a trade hits TP or SL"""
    req = request.json
    data = load_data()
    
    for t in data["trades_history"]:
        if t["symbol"] == req.get("symbol") and t["outcome"] == "OPEN":
            t["outcome"] = req.get("outcome") # WIN or LOSS
            t["close_price"] = req.get("close_price")
            t["close_time"] = req.get("time")
            
    save_data(data)
    return jsonify({"status": "success"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
