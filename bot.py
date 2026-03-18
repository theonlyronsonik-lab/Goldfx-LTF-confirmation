import requests
import pandas as pd
import numpy as np
import os
import json
import asyncio
import smtplib
from email.mime.text import MIMEText
from datetime import datetime, timedelta, timezone
from telegram import Bot
from telegram.error import TelegramError

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

API_KEY    = os.getenv("API_KEY", "")
BOT_TOKEN  = os.getenv("BOT_TOKEN", "")
CHAT_ID    = os.getenv("CHAT_ID", "")

SMTP_HOST   = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT   = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER   = os.getenv("SMTP_USER", "")
SMTP_PASS   = os.getenv("SMTP_PASS", "")
ALERT_EMAIL = os.getenv("ALERT_EMAIL", "")

SYMBOLS  = ["XAU/USD", "GBP/USD", "SPY", "QQQ"]
INTERVAL = "5min"

COOLDOWN_MINUTES = 15
ATR_SL_MULT      = 1.5
ATR_TS_MULT      = 2.0

SIGNALS_FILE = "signals.json"

# State (in-memory, persisted to signals.json)
last_signal_time = {}
signal_stack     = {}
active_trade     = {}
recent_signals   = []
trades_history   = []
symbol_state     = {}

# Sessions (UTC hour ranges)
SESSIONS = {
    "Asia":     (1,  10),
    "London":   (7,  16),
    "New York": (13, 22),
}


# ─────────────────────────────────────────────
# PERSISTENCE
# ─────────────────────────────────────────────

def load_state():
    global recent_signals, trades_history
    if not os.path.exists(SIGNALS_FILE):
        return
    try:
        with open(SIGNALS_FILE) as f:
            data = json.load(f)
        recent_signals = data.get("recent_signals", [])
        trades_history = data.get("trades_history", [])
        print(f"Loaded {len(trades_history)} historical trades, {len(recent_signals)} recent signals")
    except Exception as e:
        print(f"State load error: {e}")


def compute_stats():
    closed = [t for t in trades_history if t.get("outcome") in ("WIN", "LOSS")]
    wins   = [t for t in closed if t["outcome"] == "WIN"]
    losses = [t for t in closed if t["outcome"] == "LOSS"]

    by_asset   = {}
    by_session = {}

    for sym in SYMBOLS:
        sym_trades = [t for t in closed if t["symbol"] == sym]
        sym_wins   = [t for t in sym_trades if t["outcome"] == "WIN"]
        by_asset[sym] = {
            "total":    len(sym_trades),
            "wins":     len(sym_wins),
            "losses":   len(sym_trades) - len(sym_wins),
            "win_rate": round(len(sym_wins) / len(sym_trades) * 100, 1) if sym_trades else 0,
        }

    for sess in list(SESSIONS.keys()) + ["Off-Hours"]:
        sess_trades = [t for t in closed if sess in t.get("session", "")]
        sess_wins   = [t for t in sess_trades if t["outcome"] == "WIN"]
        by_session[sess] = {
            "total":    len(sess_trades),
            "wins":     len(sess_wins),
            "losses":   len(sess_trades) - len(sess_wins),
            "win_rate": round(len(sess_wins) / len(sess_trades) * 100, 1) if sess_trades else 0,
        }

    total = len(closed)
    return {
        "total":      total,
        "wins":       len(wins),
        "losses":     len(losses),
        "pending":    len([t for t in trades_history if t.get("outcome") == "OPEN"]),
        "win_rate":   round(len(wins) / total * 100, 1) if total else 0,
        "by_asset":   by_asset,
        "by_session": by_session,
    }


def save_state(session_on, current_sessions):
    data = {
        "bot_status":       "running",
        "last_scan":        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "session_active":   session_on,
        "current_sessions": current_sessions,
        "symbols":          {},
        "recent_signals":   recent_signals[-50:],
        "trades_history":   trades_history[-200:],
        "stats":            compute_stats(),
    }
    for sym in SYMBOLS:
        st = symbol_state.get(sym, {})
        data["symbols"][sym] = {
            "price":        st.get("price"),
            "rsi":          st.get("rsi"),
            "sma200":       st.get("sma200"),
            "atr":          st.get("atr"),
            "trend":        st.get("trend"),
            "active_trade": active_trade.get(sym),
            "last_signal":  next(
                (s for s in reversed(recent_signals) if s["symbol"] == sym), None
            ),
        }
    try:
        with open(SIGNALS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"Save error: {e}")


def init_state():
    data = {
        "bot_status": "starting",
        "last_scan": None,
        "session_active": False,
        "current_sessions": [],
        "symbols": {sym: {"price": None, "rsi": None, "sma200": None,
                          "atr": None, "trend": None,
                          "active_trade": None, "last_signal": None}
                    for sym in SYMBOLS},
        "recent_signals": recent_signals,
        "trades_history": trades_history,
        "stats": compute_stats(),
    }
    with open(SIGNALS_FILE, "w") as f:
        json.dump(data, f, indent=2)


# ─────────────────────────────────────────────
# SESSIONS
# ─────────────────────────────────────────────

def get_active_sessions():
    hour = datetime.now(timezone.utc).hour
    active = [name for name, (s, e) in SESSIONS.items() if s <= hour <= e]
    return active if active else ["Off-Hours"]


def session_active():
    return get_active_sessions() != ["Off-Hours"]


def session_label(sessions):
    return " / ".join(sessions) if sessions else "Off-Hours"


# ─────────────────────────────────────────────
# ALERTS
# ─────────────────────────────────────────────

async def send_telegram(msg):
    if not BOT_TOKEN:
        print(msg)
        return
    try:
        bot = Bot(token=BOT_TOKEN)
        await bot.send_message(chat_id=CHAT_ID, text=msg)
    except TelegramError as e:
        print(f"Telegram error: {e}")


def send_email(subject, body):
    if not (SMTP_USER and SMTP_PASS and ALERT_EMAIL):
        return
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"]    = SMTP_USER
        msg["To"]      = ALERT_EMAIL
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.ehlo()
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(SMTP_USER, ALERT_EMAIL, msg.as_string())
        print("Email alert sent")
    except Exception as e:
        print(f"Email error: {e}")


def is_high_quality(trend_aligned):
    hour = datetime.now(timezone.utc).hour
    return trend_aligned and (14 <= hour <= 20)


# ─────────────────────────────────────────────
# DATA
# ─────────────────────────────────────────────

def get_data(symbol):
    url = (f"https://api.twelvedata.com/time_series"
           f"?symbol={symbol}&interval={INTERVAL}&outputsize=210&apikey={API_KEY}")
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
# INDICATORS
# ─────────────────────────────────────────────

def calc_rsi(series, period=14):
    delta    = series.diff()
    gain     = delta.clip(lower=0)
    loss     = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs       = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calc_sma200(series):
    return series.rolling(200).mean()


def calc_atr(df, period=14):
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
# PIVOTS  (true divergence detection)
# ─────────────────────────────────────────────

def pivot_low(series, left=5, right=5):
    pivots = []
    vals   = series.values
    for i in range(left, len(vals) - right):
        window = vals[i - left: i + right + 1]
        if vals[i] == np.min(window):
            pivots.append(i)
    return pivots


def pivot_high(series, left=5, right=5):
    pivots = []
    vals   = series.values
    for i in range(left, len(vals) - right):
        window = vals[i - left: i + right + 1]
        if vals[i] == np.max(window):
            pivots.append(i)
    return pivots


# ─────────────────────────────────────────────
# DIVERGENCE  (true RSI divergence)
# ─────────────────────────────────────────────

def bullish_div(df):
    """Bullish: price lower low AND RSI higher low"""
    lows = pivot_low(df["low"])
    if len(lows) < 2:
        return False, None
    i1, i2 = lows[-2], lows[-1]
    price_ll = df["low"].iloc[i2] < df["low"].iloc[i1]
    rsi_hl   = df["rsi"].iloc[i2] > df["rsi"].iloc[i1]
    if price_ll and rsi_hl:
        return True, i2
    return False, None


def bearish_div(df):
    """Bearish: price higher high AND RSI lower high"""
    highs = pivot_high(df["high"])
    if len(highs) < 2:
        return False, None
    i1, i2 = highs[-2], highs[-1]
    price_hh = df["high"].iloc[i2] > df["high"].iloc[i1]
    rsi_lh   = df["rsi"].iloc[i2]  < df["rsi"].iloc[i1]
    if price_hh and rsi_lh:
        return True, i2
    return False, None


# ─────────────────────────────────────────────
# DOUBLE CONFIRMATION
# ─────────────────────────────────────────────

def double_confirm(symbol, signal):
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
# TRADE OUTCOME TRACKING
# ─────────────────────────────────────────────

def open_trade_record(symbol, sig_type, entry, sl, trailing_stop,
                      trend_aligned, label, sess):
    rec = {
        "symbol":        symbol,
        "type":          sig_type,
        "time":          datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "close_time":    None,
        "entry":         entry,
        "sl":            sl,
        "trailing_stop": trailing_stop,
        "outcome":       "OPEN",
        "trend_aligned": trend_aligned,
        "label":         label,
        "session":       sess,
    }
    trades_history.append(rec)
    return rec


def close_trade_record(symbol, outcome):
    for t in reversed(trades_history):
        if t["symbol"] == symbol and t["outcome"] == "OPEN":
            t["outcome"]    = outcome
            t["close_time"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            print(f"Trade closed: {symbol} → {outcome}")
            return


# ─────────────────────────────────────────────
# TRAILING STOP CHECK
# ─────────────────────────────────────────────

async def check_trailing_stop(symbol, df):
    if symbol not in active_trade:
        return

    trade = active_trade[symbol]
    close = df["close"].iloc[-1]
    atr   = df["atr"].iloc[-1]
    if pd.isna(atr):
        return

    if trade["type"] == "BUY":
        new_ts = round(close - ATR_TS_MULT * atr, 5)
        if new_ts > trade.get("trailing_stop", 0):
            active_trade[symbol]["trailing_stop"] = new_ts

        if close < active_trade[symbol]["trailing_stop"]:
            msg = f"🛑 TRAILING STOP HIT — {symbol} BUY @ {trade['entry']}"
            print(msg)
            await send_telegram(msg)
            close_trade_record(symbol, "LOSS")
            del active_trade[symbol]

    elif trade["type"] == "SELL":
        new_ts = round(close + ATR_TS_MULT * atr, 5)
        if new_ts < trade.get("trailing_stop", float("inf")):
            active_trade[symbol]["trailing_stop"] = new_ts

        if close > active_trade[symbol]["trailing_stop"]:
            msg = f"🛑 TRAILING STOP HIT — {symbol} SELL @ {trade['entry']}"
            print(msg)
            await send_telegram(msg)
            close_trade_record(symbol, "LOSS")
            del active_trade[symbol]


# ─────────────────────────────────────────────
# TP CHECK (opposite signal)
# ─────────────────────────────────────────────

async def check_tp(symbol, signal):
    if symbol not in active_trade:
        return

    trade = active_trade[symbol]

    if signal == "BUY" and trade["type"] == "SELL":
        msg = f"✅ TP HIT — {symbol} | SELL trade closed"
        print(msg)
        await send_telegram(msg)
        close_trade_record(symbol, "WIN")
        del active_trade[symbol]

    elif signal == "SELL" and trade["type"] == "BUY":
        msg = f"✅ TP HIT — {symbol} | BUY trade closed"
        print(msg)
        await send_telegram(msg)
        close_trade_record(symbol, "WIN")
        del active_trade[symbol]


# ─────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────

async def main():
    load_state()
    init_state()
    print(f"Bot started | Symbols: {SYMBOLS}")

    await send_telegram(
        f"🤖 Signal Bot Online\n"
        f"Symbols: {', '.join(SYMBOLS)}\n"
        f"Interval: {INTERVAL} | ATR SL: {ATR_SL_MULT}x | ATR TS: {ATR_TS_MULT}x"
    )

    while True:
        try:
            sessions     = get_active_sessions()
            sess_on      = sessions != ["Off-Hours"]
            sess_str     = session_label(sessions)

            if not sess_on:
                now_str = datetime.now(timezone.utc).strftime("%H:%M")
                print(f"[{now_str} UTC] Off-hours, sleeping 60s…")
                save_state(False, sessions)
                await asyncio.sleep(60)
                continue

            for symbol in SYMBOLS:
                df = get_data(symbol)
                if df is None:
                    continue

                df["rsi"]   = calc_rsi(df["close"])
                df["sma200"] = calc_sma200(df["close"])
                df["atr"]   = calc_atr(df)

                price  = round(df["close"].iloc[-1], 5)
                rsi    = round(df["rsi"].iloc[-1], 2)
                sma200 = df["sma200"].iloc[-1]
                atr    = df["atr"].iloc[-1]

                sma200_val  = round(sma200, 5) if not pd.isna(sma200) else None
                atr_val     = round(atr,    5) if not pd.isna(atr)    else None
                trend       = None
                if sma200_val:
                    trend = "BULLISH" if price > sma200_val else "BEARISH"

                symbol_state[symbol] = {
                    "price":  price,
                    "rsi":    rsi,
                    "sma200": sma200_val,
                    "atr":    atr_val,
                    "trend":  trend,
                }

                # Check trailing stop on active trade
                await check_trailing_stop(symbol, df)

                bull, bull_idx = bullish_div(df)
                bear, bear_idx = bearish_div(df)

                now = datetime.now(timezone.utc)

                if symbol in last_signal_time:
                    if now - last_signal_time[symbol] < timedelta(minutes=COOLDOWN_MINUTES):
                        continue

                # ── BUY ──
                if bull:
                    await check_tp(symbol, "BUY")
                    ds = double_confirm(symbol, "BUY")

                    if ds == "BUY" and atr_val:
                        entry         = price
                        sl            = round(entry - ATR_SL_MULT * atr_val, 5)
                        trailing_stop = round(entry - ATR_TS_MULT * atr_val, 5)
                        trend_aligned = (trend == "BULLISH")
                        label         = "Trend Aligned Signal" if trend_aligned else "Counter-Trend Signal"
                        ts            = now.strftime("%Y-%m-%d %H:%M UTC")

                        tg_msg = (
                            f"🟢LTF 3 min BUY-signal — {symbol}\n"
                            f"Entry: {entry} | SL: {sl} | TS: {trailing_stop}\n"
                            f"RSI: {rsi} | Trend: {trend} | {label}\n"
                            f"Session: {sess_str} | {ts}"
                        )
                        print(tg_msg)
                        await send_telegram(tg_msg)

                        if is_high_quality(trend_aligned):
                            send_email(
                                f"⭐ HIGH QUALITY BUY — {symbol}",
                                tg_msg
                            )

                        sig_rec = {
                            "symbol": symbol, "type": "BUY", "time": ts,
                            "entry": entry, "sl": sl, "trailing_stop": trailing_stop,
                            "trend_aligned": trend_aligned, "label": label,
                            "session": sess_str, "rsi": rsi, "trend": trend,
                        }
                        recent_signals.append(sig_rec)

                        open_trade_record(symbol, "BUY", entry, sl, trailing_stop,
                                          trend_aligned, label, sess_str)
                        active_trade[symbol] = {
                            "type": "BUY", "entry": entry,
                            "sl": sl, "trailing_stop": trailing_stop,
                            "trend_aligned": trend_aligned, "label": label,
                            "session": sess_str,
                        }
                        last_signal_time[symbol] = now

                # ── SELL ──
                if bear:
                    await check_tp(symbol, "SELL")
                    ds = double_confirm(symbol, "SELL")

                    if ds == "SELL" and atr_val:
                        entry         = price
                        sl            = round(entry + ATR_SL_MULT * atr_val, 5)
                        trailing_stop = round(entry + ATR_TS_MULT * atr_val, 5)
                        trend_aligned = (trend == "BEARISH")
                        label         = "Trend Aligned Signal" if trend_aligned else "Counter-Trend Signal"
                        ts            = now.strftime("%Y-%m-%d %H:%M UTC")

                        tg_msg = (
                            f"🔴 LTF_3 min SELL-signal — {symbol}\n"
                            f"Entry: {entry} | SL: {sl} | TS: {trailing_stop}\n"
                            f"RSI: {rsi} | Trend: {trend} | {label}\n"
                            f"Session: {sess_str} | {ts}"
                        )
                        print(tg_msg)
                        await send_telegram(tg_msg)

                        if is_high_quality(trend_aligned):
                            send_email(
                                f"⭐ HIGH QUALITY SELL — {symbol}",
                                tg_msg
                            )

                        sig_rec = {
                            "symbol": symbol, "type": "SELL", "time": ts,
                            "entry": entry, "sl": sl, "trailing_stop": trailing_stop,
                            "trend_aligned": trend_aligned, "label": label,
                            "session": sess_str, "rsi": rsi, "trend": trend,
                        }
                        recent_signals.append(sig_rec)

                        open_trade_record(symbol, "SELL", entry, sl, trailing_stop,
                                          trend_aligned, label, sess_str)
                        active_trade[symbol] = {
                            "type": "SELL", "entry": entry,
                            "sl": sl, "trailing_stop": trailing_stop,
                            "trend_aligned": trend_aligned, "label": label,
                            "session": sess_str,
                        }
                        last_signal_time[symbol] = now

            save_state(sess_on, sessions)
            await asyncio.sleep(300)

        except Exception as e:
            print(f"Runtime error: {e}")
            await asyncio.sleep(90)


if __name__ == "__main__":
    asyncio.run(main())
