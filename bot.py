import requests
import pandas as pd
import numpy as np
import os
import json
import asyncio
import smtplib
import time
from email.mime.text import MIMEText
from datetime import datetime, timedelta, timezone
from telegram import Bot
from telegram.error import TelegramError

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

API_KEY   = os.getenv("API_KEY", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID   = os.getenv("CHAT_ID", "")

SMTP_HOST   = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT   = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER   = os.getenv("SMTP_USER", "")
SMTP_PASS   = os.getenv("SMTP_PASS", "")
ALERT_EMAIL = os.getenv("ALERT_EMAIL", "")

# Standardized symbols for MT5 compatibility
SYMBOLS  = ["XAUUSD", "EURUSD", "AUDCAD", "CADJPY", "EURJPY"]
INTERVAL = "5min"

DATA_DIR = "/app/data"
os.makedirs(DATA_DIR, exist_ok=True)
SIGNALS_FILE = os.path.join(DATA_DIR, "signals.json")

SL_BUFFERS = {
    "XAUUSD": 0.50,
    "EURUSD": 0.0003,
    "AUDCAD": 0.10,
    "CADJPY": 0.10,
    "EURJPY": 0.10,
}

PIP_SIZES = {
    "XAUUSD": 0.1,
    "EURUSD": 0.0001,
    "AUDCAD": 0.01,
    "CADJPY": 0.01,
    "EURJPY": 0.01,
}

LOT_SIZE = 0.01 
RSI_OVERBOUGHT = 70
RSI_OVERSOLD   = 30

# State variables
last_signal_time = {}
signal_stack     = {}
active_trade     = {}
recent_signals   = []
trades_history   = []
symbol_state     = {}
last_div_time    = {}

SESSIONS = {
    "Asia":      (0,  7),
    "London":    (7,  15),
    "New York": (14, 20),
}

# ─────────────────────────────────────────────
# PERSISTENCE
# ─────────────────────────────────────────────

def load_state():
    global recent_signals, trades_history, active_trade
    if not os.path.exists(SIGNALS_FILE):
        return
    try:
        with open(SIGNALS_FILE) as f:
            data = json.load(f)
        recent_signals = data.get("recent_signals", [])
        trades_history = data.get("trades_history", [])
        
        # Sync active trades from the file so Bot knows what MT5 is doing
        for t in trades_history:
            if t.get("outcome") == "OPEN":
                sym = t["symbol"]
                active_trade[sym] = t
    except Exception as e:
        print(f"State load error: {e}")

def save_state(session_on, current_sessions):
    # Always reload before saving to avoid overwriting MT5 updates
    existing_data = {"recent_signals": [], "trades_history": []}
    if os.path.exists(SIGNALS_FILE):
        try:
            with open(SIGNALS_FILE, "r") as f:
                existing_data = json.load(f)
        except: pass

    data = {
        "bot_status": "running",
        "last_scan": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "session_active": session_on,
        "current_sessions": current_sessions,
        "symbols": {},
        "recent_signals": recent_signals[-50:], 
        "trades_history": existing_data.get("trades_history", [])[-200:],
        "pip_sizes": PIP_SIZES,
        "lot_size": LOT_SIZE,
    }
    
    for sym in SYMBOLS:
        st = symbol_state.get(sym, {})
        data["symbols"][sym] = {
            "price": st.get("price"),
            "rsi": st.get("rsi"),
            "trend": st.get("trend"),
            "active_trade": active_trade.get(sym)
        }
        
    try:
        with open(SIGNALS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"Save error: {e}")

# ... [Keep your existing indicator/data/alert functions here] ...
# (Specifically calc_rsi, calc_sma200, calc_atr, pivot_low/high, get_data, etc.)

async def main():
    load_state()
    print(f"Bot started | Symbols: {SYMBOLS}")

    while True:
        try:
            sessions = get_active_sessions()
            sess_on = sessions != ["Off-Hours"]
            
            if not sess_on:
                await asyncio.sleep(60)
                continue

            for symbol in SYMBOLS:
                df = get_data(symbol)
                if df is None: continue

                # Indicator Calculations
                df["rsi"] = calc_rsi(df["close"])
                df["sma200"] = calc_sma200(df["close"])
                price = round(df["close"].iloc[-2], 5)
                rsi = round(df["rsi"].iloc[-1], 2)
                trend = "BULLISH" if price > df["sma200"].iloc[-1] else "BEARISH"

                symbol_state[symbol] = {"price": price, "rsi": rsi, "trend": trend}

                bull, bull_idx = bullish_div(df)
                bear, bear_idx = bearish_div(df)

                # BUY Logic
                if bull and bull_idx is not None:
                    ds = double_confirm(symbol, "BUY")
                    if ds == "BUY":
                        sl = get_sl_buy(df, symbol)
                        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                        
                        sig_rec = {
                            "symbol": symbol, "type": "BUY", "time": ts,
                            "entry": price, "sl": sl, "rsi": rsi
                        }
                        # We ONLY add to recent_signals. 
                        # We DO NOT call open_trade_record here. MT5 handles that.
                        recent_signals.append(sig_rec)
                        await send_telegram(f"🟢 BUY {symbol} @ {price}")

                # SELL Logic
                if bear and bear_idx is not None:
                    ds = double_confirm(symbol, "SELL")
                    if ds == "SELL":
                        sl = get_sl_sell(df, symbol)
                        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                        
                        sig_rec = {
                            "symbol": symbol, "type": "SELL", "time": ts,
                            "entry": price, "sl": sl, "rsi": rsi
                        }
                        recent_signals.append(sig_rec)
                        await send_telegram(f"🔴 SELL {symbol} @ {price}")

            save_state(sess_on, sessions)
            await asyncio.sleep(300)

        except Exception as e:
            print(f"Runtime error: {e}")
            await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(main())
