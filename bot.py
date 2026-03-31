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

# Standardized symbols (No slashes for MT5)
SYMBOLS  = ["XAUUSD", "EURUSD", "AUDCAD", "CADJPY", "EURJPY"]
INTERVAL = "5min"

DATA_DIR = "/app/data"
os.makedirs(DATA_DIR, exist_ok=True)
SIGNALS_FILE = os.path.join(DATA_DIR, "signals.json")

SL_BUFFERS = {
    "XAUUSD": 0.50, "EURUSD": 0.0003, "AUDCAD": 0.10, "CADJPY": 0.10, "EURJPY": 0.10,
}

PIP_SIZES = {
    "XAUUSD": 0.1, "EURUSD": 0.0001, "AUDCAD": 0.01, "CADJPY": 0.01, "EURJPY": 0.01,
}

LOT_SIZE = 0.01 
RSI_OVERBOUGHT = 70
RSI_OVERSOLD   = 30

# State
last_signal_time = {}
signal_stack     = {}
active_trade     = {}
recent_signals   = []
trades_history   = []
symbol_state     = {}
last_div_time    = {}

SESSIONS = {
    "Asia":      (2,  7),
    "London":    (7,  13),
    "New York": (13, 19),
}

# ─────────────────────────────────────────────
# PERSISTENCE & HELPERS
# ─────────────────────────────────────────────

def load_state():
    global recent_signals, trades_history, active_trade
    if not os.path.exists(SIGNALS_FILE): return
    try:
        with open(SIGNALS_FILE) as f:
            data = json.load(f)
        recent_signals = data.get("recent_signals", [])
        trades_history = data.get("trades_history", [])
        for t in trades_history:
            if t.get("outcome") == "OPEN":
                active_trade[t["symbol"]] = t
    except Exception as e: print(f"Load error: {e}")

def save_state(session_on, current_sessions):
    data = {
        "bot_status": "running",
        "last_scan": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "session_active": session_on,
        "current_sessions": current_sessions,
        "symbols": {},
        "recent_signals": recent_signals[-50:],
        "trades_history": trades_history[-200:],
    }
    for sym in SYMBOLS:
        st = symbol_state.get(sym, {})
        data["symbols"][sym] = {
            "price": st.get("price"), "rsi": st.get("rsi"),
            "trend": st.get("trend"), "active_trade": active_trade.get(sym)
        }
    try:
        with open(SIGNALS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e: print(f"Save error: {e}")

def get_active_sessions():
    hour = datetime.now(timezone.utc).hour
    active = [name for name, (s, e) in SESSIONS.items() if s <= hour <= e]
    return active if active else ["Off-Hours"]

# ─────────────────────────────────────────────
# INDICATORS
# ─────────────────────────────────────────────

def calc_rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calc_sma200(series): return series.rolling(200).mean()

def pivot_low(series, left=5, right=5):
    pivots = []
    vals = series.values
    for i in range(left, len(vals) - right):
        if vals[i] == np.min(vals[i - left: i + right + 1]): pivots.append(i)
    return pivots

def pivot_high(series, left=5, right=5):
    pivots = []
    vals = series.values
    for i in range(left, len(vals) - right):
        if vals[i] == np.max(vals[i - left: i + right + 1]): pivots.append(i)
    return pivots

# ─────────────────────────────────────────────
# DIVERGENCE & SIGNALS
# ─────────────────────────────────────────────

def bullish_div(df):
    lows = pivot_low(df["low"])
    if len(lows) < 2: return False, None
    i1, i2 = lows[-2], lows[-1]
    if df["low"].iloc[i2] < df["low"].iloc[i1] and df["rsi"].iloc[i2] > df["rsi"].iloc[i1]:
        return True, i2
    return False, None

def bearish_div(df):
    highs = pivot_high(df["high"])
    if len(highs) < 2: return False, None
    i1, i2 = highs[-2], highs[-1]
    if df["high"].iloc[i2] > df["high"].iloc[i1] and df["rsi"].iloc[i2] < df["rsi"].iloc[i1]:
        return True, i2
    return False, None

def double_confirm(symbol, signal):
    stack = signal_stack.setdefault(symbol, [])
    stack.append(signal)
    if len(stack) > 2: stack.pop(0)
    return signal if stack == [signal, signal] else None

def get_sl_buy(df, symbol):
    return round(df["low"].iloc[-5:].min() - SL_BUFFERS.get(symbol, 0.0001), 5)

def get_sl_sell(df, symbol):
    return round(df["high"].iloc[-5:].max() + SL_BUFFERS.get(symbol, 0.0001), 5)

# ─────────────────────────────────────────────
# DATA FETCH & ALERTS
# ─────────────────────────────────────────────

def get_data(symbol):
    # Convert back to API format for the request
    api_sym = symbol[:3] + "/" + symbol[3:] if "XAU" not in symbol else "XAU/USD"
    url = f"https://api.twelvedata.com/time_series?symbol={api_sym}&interval={INTERVAL}&outputsize=210&apikey={API_KEY}"
    try:
        r = requests.get(url, timeout=20).json()
        if "values" not in r: return None
        df = pd.DataFrame(r["values"]).iloc[::-1].reset_index(drop=True)
        for c in ["open", "high", "low", "close"]: df[c] = df[c].astype(float)
        return df
    except: return None

async def send_telegram(msg):
    if not BOT_TOKEN: return
    try: await Bot(token=BOT_TOKEN).send_message(chat_id=CHAT_ID, text=msg)
    except: pass

# ─────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────

async def main():
    load_state()
    print(f"Bot started | Symbols: {SYMBOLS}")
    await send_telegram("🤖 LTF Signal Bot Online")

    while True:
        try:
            sessions = get_active_sessions()
            sess_on = sessions != ["Off-Hours"]
            
            if not sess_on:
                await asyncio.sleep(60); continue

            for symbol in SYMBOLS:
                df = get_data(symbol)
                if df is None: continue

                df["rsi"] = calc_rsi(df["close"])
                df["sma200"] = calc_sma200(df["close"])
                
                price = round(df["close"].iloc[-2], 5)
                rsi = round(df["rsi"].iloc[-1], 2)
                sma = df["sma200"].iloc[-1]
                trend = "BULLISH" if price > sma else "BEARISH"
                symbol_state[symbol] = {"price": price, "rsi": rsi, "trend": trend}

                # Check for active trade lockout (Prevents spamming signals)
                if symbol in active_trade: continue

                bull, b_idx = bullish_div(df)
                bear, s_idx = bearish_div(df)

                if bull and double_confirm(symbol, "BUY") == "BUY":
                    sl = get_sl_buy(df, symbol)
                    pip_size = PIP_SIZES.get(symbol, 0.0001)
                    risk_pips = round((price - sl) / pip_size, 1)
                    sig = {
                        "symbol": symbol,
                        "type": "BUY",
                        "entry": price,
                        "sl": sl,
                        "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                        "risk_pips": risk_pips,
                        "trend_aligned": (trend == "BULLISH"),
                        "label": "Trend Aligned Signal" if trend == "BULLISH" else "Counter-Trend Signal",
                        "session": " / ".join(sessions),
                        "rsi": rsi,
                        "trend": trend,
                        "context": f"RSI: {rsi}, Trend: {trend}",
                    }
                    recent_signals.append(sig)
                    await send_telegram(f"🟢 BUY {symbol} @ {price}\nSL: {sl}")

                elif bear and double_confirm(symbol, "SELL") == "SELL":
                    sl = get_sl_sell(df, symbol)
                    pip_size = PIP_SIZES.get(symbol, 0.0001)
                    risk_pips = round((sl - price) / pip_size, 1)
                    sig = {
                        "symbol": symbol,
                        "type": "SELL",
                        "entry": price,
                        "sl": sl,
                        "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                        "risk_pips": risk_pips,
                        "trend_aligned": (trend == "BEARISH"),
                        "label": "Trend Aligned Signal" if trend == "BEARISH" else "Counter-Trend Signal",
                        "session": " / ".join(sessions),
                        "rsi": rsi,
                        "trend": trend,
                        "context": f"RSI: {rsi}, Trend: {trend}",
                    }
                    recent_signals.append(sig)
                    await send_telegram(f"🔴 SELL {symbol} @ {price}\nSL: {sl}")

            save_state(sess_on, sessions)
            await asyncio.sleep(300)
        except Exception as e:
            print(f"Error: {e}"); await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(main())
