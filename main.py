from fastapi import FastAPI, WebSocket, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel
import asyncio
import random
import json
import requests
import math
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo
import yfinance as yf
import pandas as pd

app = FastAPI()

# 🔴 [ใส่ LINE Token ของคุณตรงนี้เพื่อเปิดใช้งานระบบแจ้งเตือน] 🔴
LINE_ACCESS_TOKEN = "ใส่_LINE_TOKEN_ของคุณตรงนี้"

watchlist = ["NVDA", "AAPL", "TSLA", "AMD"]
logged_positions = []
live_prices = {}

class PositionModel(BaseModel):
    ticker: str
    strike_price: float
    option_type: str
    expiration: str
    premium_paid: float
    quantity: int


def send_line_alert(message: str):
    if LINE_ACCESS_TOKEN == "ใส่_LINE_TOKEN_ของคุณตรงนี้" or not LINE_ACCESS_TOKEN:
        return
    url = "https://notify-api.line.me/api/notify"
    headers = {"Authorization": f"Bearer {LINE_ACCESS_TOKEN}"}
    data = {"message": message}
    try:
        requests.post(url, headers=headers, data=data)
    except Exception as e:
        print(f"LINE Notify Error: {e}")


# ---------------------------------------------------------------------------
# 🕒 Market session helper (America/New_York)
# ---------------------------------------------------------------------------
def get_market_session() -> str:
    """Returns 'PRE', 'REGULAR', 'POST' or 'CLOSED' based on real NY time."""
    now_ny = datetime.now(ZoneInfo("America/New_York"))
    if now_ny.weekday() >= 5:  # เสาร์-อาทิตย์ ตลาดปิด
        return "CLOSED"
    t = now_ny.time()
    if dtime(4, 0) <= t < dtime(9, 30):
        return "PRE"
    if dtime(9, 30) <= t < dtime(16, 0):
        return "REGULAR"
    if dtime(16, 0) <= t < dtime(20, 0):
        return "POST"
    return "CLOSED"


def get_price_bundle(ticker: str) -> dict:
    session = get_market_session()
    stock = yf.Ticker(ticker)
    try:
        info = stock.info
    except Exception:
        info = {}

    reg_price = info.get('regularMarketPrice') or info.get('currentPrice')
    prev_close = info.get('previousClose') or info.get('regularMarketPreviousClose')
    pre_price = info.get('preMarketPrice')
    post_price = info.get('postMarketPrice')

    last_close = reg_price or prev_close
    if not last_close:
        try:
            hist = stock.history(period="5d", interval="1d")
            if not hist.empty:
                last_close = float(hist['Close'].iloc[-1])
        except Exception:
            pass

    last_close = float(last_close) if last_close else 100.0
    reg_price = float(reg_price) if reg_price else last_close

    if session == "REGULAR":
        current_price = reg_price
    elif session == "PRE":
        current_price = pre_price or last_close
    elif session == "POST":
        current_price = post_price or reg_price
    else:  
        current_price = last_close

    live_prices[ticker] = current_price

    return {
        "current_price": round(float(current_price), 2),
        "close_price": round(float(last_close), 2),
        "pre_price": round(float(pre_price), 2) if pre_price else None,
        "post_price": round(float(post_price), 2) if post_price else None,
        "market_session": session,
    }


def get_base_price(ticker: str) -> float:
    if ticker in live_prices:
        return live_prices[ticker]
    try:
        bundle = get_price_bundle(ticker)
        return bundle["current_price"]
    except Exception:
        live_prices[ticker] = 100.0
        return 100.0


def get_live_1m_price(ticker: str):
    try:
        fi = yf.Ticker(ticker).fast_info
        price = fi.get("last_price") if hasattr(fi, "get") else getattr(fi, "last_price", None)
        if price:
            return float(price)
    except Exception:
        pass
    try:
        hist = yf.Ticker(ticker).history(period="1d", interval="1m")
        if not hist.empty:
            return float(hist['Close'].iloc[-1])
    except Exception:
        pass
    return None


def calculate_rsi(series, period=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (100 + rs))


# ---------------------------------------------------------------------------
# 🧠 Call/Put Score Optimized (Technical 60% + Fundamental 40% แยกตรรกะอิสระ)
# ---------------------------------------------------------------------------
def calculate_option_scores(ticker: str, info: dict):
    call_technical_score = 50.0
    put_technical_score = 50.0
    
    # --- 1. Technical Analysis (60%) ---
    try:
        hist = yf.Ticker(ticker).history(period="6mo", interval="1d")
        if not hist.empty and len(hist) > 20:
            closes = hist['Close']
            rsi_series = calculate_rsi(closes, 14)
            last_rsi = float(rsi_series.iloc[-1]) if not pd.isna(rsi_series.iloc[-1]) else 50.0
            ema20 = closes.ewm(span=20, adjust=False).mean().iloc[-1]
            ema50 = closes.ewm(span=50, adjust=False).mean().iloc[-1] if len(closes) >= 50 else ema20
            last_close = closes.iloc[-1]

            # เจาะลึก RSI ฝั่ง Call (Bullish Momentum)
            if 50 <= last_rsi <= 70:
                call_rsi_score = 88.0
            elif last_rsi > 70:  
                call_rsi_score = 75.0
            elif last_rsi < 30:  
                call_rsi_score = 65.0
            else:
                call_rsi_score = last_rsi

            # เจาะลึก RSI ฝั่ง Put (Bearish Momentum)
            if 30 <= last_rsi <= 50:
                put_rsi_score = 88.0
            elif last_rsi < 30:  
                put_rsi_score = 75.0
            elif last_rsi > 70:  
                put_rsi_score = 65.0
            else:
                put_rsi_score = 100.0 - last_rsi

            # วิเคราะห์ Trend ด้วย EMA Crossover 
            if last_close > ema20 > ema50:         
                call_trend_score = 90.0
                put_trend_score = 10.0
            elif last_close > ema20 and ema20 <= ema50:  
                call_trend_score = 70.0
                put_trend_score = 30.0
            elif last_close < ema20 < ema50:       
                call_trend_score = 10.0
                put_trend_score = 90.0
            elif last_close < ema20 and ema20 >= ema50:  
                call_trend_score = 30.0
                put_trend_score = 70.0
            else:
                call_trend_score = 50.0
                put_trend_score = 50.0

            # ถ่วงน้ำหนักสายเทคนิค: Trend 60% และ RSI Momentum 40%
            call_technical_score = (call_rsi_score * 0.4) + (call_trend_score * 0.6)
            put_technical_score = (put_rsi_score * 0.4) + (put_trend_score * 0.6)
    except Exception:
        call_technical_score = 50.0
        put_technical_score = 50.0

    # --- 2. Fundamental Analysis (40%) ---
    call_fundamental_score = 50.0
    put_fundamental_score = 50.0
    try:
        rec_mean = info.get('recommendationMean')       
        target = info.get('targetMeanPrice')
        current = info.get('currentPrice') or info.get('regularMarketPrice')
        rev_growth = info.get('revenueGrowth')
        profit_margin = info.get('profitMargins')

        # === [คำนวณฝั่ง Call Fundamental] ===
        call_subs = []
        if rec_mean:
            call_subs.append(max(0, min(100, (5.0 - float(rec_mean)) * 25)))
        if target and current:
            upside = (float(target) - float(current)) / float(current)
            call_subs.append(max(0, min(100, 50 + upside * 150)))
        if rev_growth is not None:
            call_subs.append(max(0, min(100, 50 + float(rev_growth) * 100)))
        if profit_margin is not None:
            call_subs.append(max(0, min(100, 50 + float(profit_margin) * 100)))
        
        if call_subs:
            call_fundamental_score = sum(call_subs) / len(call_subs)

        # === [คำนวณฝั่ง Put Fundamental] ===
        put_subs = []
        if rec_mean:
            put_subs.append(max(0, min(100, (float(rec_mean) - 1.0) * 25)))
        if target and current:
            downside = (float(current) - float(target)) / float(current)
            put_subs.append(max(0, min(100, 50 + downside * 150)))
        if rev_growth is not None:
            put_subs.append(max(0, min(100, 50 - float(rev_growth) * 100)))
        if profit_margin is not None:
            put_subs.append(max(0, min(100, 50 - float(profit_margin) * 100)))

        if put_subs:
            put_fundamental_score = sum(put_subs) / len(put_subs)

    except Exception:
        pass

    # รวมคะแนนรวมถ่วงน้ำหนัก (Technical 60% + Fundamental 40%)
    call_score = round((call_technical_score * 0.6) + (call_fundamental_score * 0.4))
    call_score = int(max(0, min(100, call_score)))

    put_score = round((put_technical_score * 0.6) + (put_fundamental_score * 0.4))
    put_score = int(max(0, min(100, put_score)))

    return call_score, put_score


def calculate_fair_value(info: dict, current_price: float):
    methods = []   
    target = info.get('targetMeanPrice')
    if target and float(target) > 0:
        methods.append((float(target), 0.5, "analyst_target"))

    eps = info.get('trailingEps')
    bvps = info.get('bookValue')
    if eps and eps > 0 and bvps and bvps > 0:
        graham = (22.5 * float(eps) * float(bvps)) ** 0.5
        methods.append((graham, 0.3, "graham_number"))

    forward_eps = info.get('forwardEps')
    if forward_eps and forward_eps > 0:
        fpe = info.get('forwardPE')
        sector_pe = float(fpe) if fpe and 5 < float(fpe) < 60 else 20.0
        methods.append((float(forward_eps) * sector_pe, 0.2, "forward_pe"))

    if methods:
        total_w = sum(w for _, w, _ in methods)
        blended = sum(v * w for v, w, _ in methods) / total_w
        fair_value = round(blended, 2)
    elif eps and eps > 0:
        fair_value = round(float(eps) * 20, 2)
    else:
        fair_value = round(current_price, 2) if current_price else None

    upside_pct = None
    if fair_value and current_price:
        upside_pct = round(((fair_value - current_price) / current_price) * 100, 2)

    return fair_value, upside_pct


def calculate_iv_rank(ticker: str) -> int:
    try:
        stock = yf.Ticker(ticker)
        exps = stock.options
        if exps:
            chain = stock.option_chain(exps[0])
            calls = chain.calls
            current = get_base_price(ticker)
            if not calls.empty:
                calls = calls.copy()
                calls['diff'] = (calls['strike'] - current).abs()
                atm = calls.loc[calls['diff'].idxmin()]
                iv = atm.get('impliedVolatility', None)
                if iv is not None and not pd.isna(iv):
                    return int(round(min(100, max(0, float(iv) * 100))))
    except Exception:
        pass

    try:
        hist = yf.Ticker(ticker).history(period="1y", interval="1d")
        if hist.empty or len(hist) < 30:
            return 50
        returns = hist['Close'].pct_change().dropna()
        rolling_vol = (returns.rolling(window=20).std() * (252 ** 0.5) * 100).dropna()
        if rolling_vol.empty:
            return 50
        current_vol = rolling_vol.iloc[-1]
        rank = (rolling_vol < current_vol).sum() / len(rolling_vol) * 100
        return int(round(rank))
    except Exception:
        return 50


# ---------------------------------------------------------------------------
# 🎯 Support / Resistance System & ETA Indicators
# ---------------------------------------------------------------------------
BAR_SECONDS = {
    "1m": 60, "5m": 300, "10m": 600, "15m": 900,
    "1h": 3600, "4h": 14400, "1d": 86400, "week": 604800,
}

TIMEFRAME_CONFIG = {
    "1m":   {"period": "5d",  "interval": "1m"},
    "5m":   {"period": "1mo", "interval": "5m"},
    "10m":  {"period": "1mo", "interval": "5m",  "resample": "10min"},
    "15m":  {"period": "1mo", "interval": "15m"},
    "1h":   {"period": "3mo", "interval": "60m"},
    "4h":   {"period": "6mo", "interval": "60m", "resample": "4h"},
    "1d":   {"period": "1y",  "interval": "1d"},
    "week": {"period": "5y",  "interval": "1wk"},
}


def calculate_pivot_levels(h: float, l: float, c: float) -> dict:
    p = (h + l + c) / 3
    r1 = (2 * p) - l
    s1 = (2 * p) - h
    r2 = p + (h - l)
    s2 = p - (h - l)
    r3 = h + 2 * (p - l)
    s3 = l - 2 * (h - p)
    return {
        "pivot": round(p, 2),
        "resistance": [round(r1, 2), round(r2, 2), round(r3, 2)],
        "support": [round(s1, 2), round(s2, 2), round(s3, 2)],
    }


def get_pivot_source_bar(ticker: str, is_week: bool):
    stock = yf.Ticker(ticker)
    try:
        if is_week:
            hist = stock.history(period="2y", interval="1wk")
        else:
            hist = stock.history(period="3mo", interval="1d")
    except Exception:
        return None

    if hist is None or hist.empty:
        return None

    now_ny = datetime.now(ZoneInfo("America/New_York"))
    last_ts = hist.index[-1]
    try:
        last_ts_ny = last_ts.tz_convert("America/New_York") if last_ts.tzinfo else last_ts
    except Exception:
        last_ts_ny = last_ts

    if is_week:
        same_period = last_ts_ny.isocalendar()[:2] == now_ny.isocalendar()[:2]
    else:
        same_period = last_ts_ny.date() == now_ny.date()

    if same_period and len(hist) >= 2:
        bar = hist.iloc[-2]
    else:
        bar = hist.iloc[-1]

    if pd.isna(bar['High']) or pd.isna(bar['Low']) or pd.isna(bar['Close']):
        return None
    return bar


def get_atr_for_timeframe(ticker: str, timeframe: str, period: int = 14):
    cfg = TIMEFRAME_CONFIG.get(timeframe, TIMEFRAME_CONFIG["1d"])
    stock = yf.Ticker(ticker)
    try:
        hist = stock.history(period=cfg["period"], interval=cfg["interval"], prepost=True)
    except Exception:
        return None
    if hist is None or hist.empty or len(hist) < period + 1:
        return None

    if cfg.get("resample"):
        agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
        hist = hist.resample(cfg["resample"]).agg(agg).dropna(subset=["Close"])
        if len(hist) < period + 1:
            return None

    high, low, close = hist['High'], hist['Low'], hist['Close']
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = tr.rolling(window=period).mean().iloc[-1]
    if pd.isna(atr) or atr <= 0:
        return None
    return float(atr)


def format_duration(total_seconds: float) -> str:
    minutes = total_seconds / 60
    if minutes < 1:
        return "< 1 นาที"
    if minutes < 60:
        return f"~{round(minutes)} นาที"
    hours = minutes / 60
    if hours < 24:
        h = int(hours)
        m = int(round((hours - h) * 60))
        return f"~{h} ชม {m} นาที" if m else f"~{h} ชม"
    days = hours / 24
    if days < 30:
        d = int(days)
        h = int(round((days - d) * 24))
        return f"~{d} วัน {h} ชม" if h else f"~{d} วัน"
    months = days / 30
    return f"~{round(months)} เดือน+"


def estimate_eta(distance: float, atr, bar_seconds: int):
    if not atr or atr <= 0 or distance <= 0:
        return None
    bars_needed = distance / atr
    total_seconds = bars_needed * bar_seconds
    return format_duration(total_seconds)


# ---------------------------------------------------------------------------
# 📦 Tickers Database & Endpoints
# ---------------------------------------------------------------------------
TICKERS_DB = [
    {"symbol": "AAPL", "name": "Apple Inc."}, {"symbol": "MSFT", "name": "Microsoft Corp."},
    {"symbol": "GOOGL", "name": "Alphabet Inc. Class A"}, {"symbol": "GOOG", "name": "Alphabet Inc. Class C"},
    {"symbol": "AMZN", "name": "Amazon.com Inc."}, {"symbol": "NVDA", "name": "NVIDIA Corp."},
    {"symbol": "META", "name": "Meta Platforms Inc."}, {"symbol": "TSLA", "name": "Tesla Inc."},
    {"symbol": "AVGO", "name": "Broadcom Inc."}, {"symbol": "BRK-B", "name": "Berkshire Hathaway"},
    {"symbol": "JPM", "name": "JPMorgan Chase"}, {"symbol": "V", "name": "Visa Inc."},
    {"symbol": "UNH", "name": "UnitedHealth Group"}, {"symbol": "MA", "name": "Mastercard"},
    {"symbol": "HD", "name": "Home Depot"}, {"symbol": "PG", "name": "Procter & Gamble"},
    {"symbol": "COST", "name": "Costco Wholesale"}, {"symbol": "JNJ", "name": "Johnson & Johnson"},
    {"symbol": "ORCL", "name": "Oracle Corp."}, {"symbol": "MRK", "name": "Merck & Co."},
    {"symbol": "ABBV", "name": "AbbVie Inc."}, {"symbol": "CVX", "name": "Chevron Corp."},
    {"symbol": "CRM", "name": "Salesforce Inc."}, {"symbol": "KO", "name": "Coca-Cola Co."},
    {"symbol": "AMD", "name": "Advanced Micro Devices"}, {"symbol": "PEP", "name": "PepsiCo Inc."},
    {"symbol": "NFLX", "name": "Netflix Inc."}, {"symbol": "TMO", "name": "Thermo Fisher Scientific"},
    {"symbol": "ADBE", "name": "Adobe Inc."}, {"symbol": "WMT", "name": "Walmart Inc."},
    {"symbol": "BAC", "name": "Bank of America"}, {"symbol": "MCD", "name": "McDonald's Corp."},
    {"symbol": "CSCO", "name": "Cisco Systems"}, {"symbol": "ABT", "name": "Abbott Laboratories"},
    {"symbol": "PFE", "name": "Pfizer Inc."}, {"symbol": "LIN", "name": "Linde plc"},
    {"symbol": "ACN", "name": "Accenture"}, {"symbol": "DHR", "name": "Danaher Corp."},
    {"symbol": "TXN", "name": "Texas Instruments"}, {"symbol": "INTC", "name": "Intel Corp."},
    {"symbol": "WFC", "name": "Wells Fargo"}, {"symbol": "VZ", "name": "Verizon Communications"},
    {"symbol": "PM", "name": "Philip Morris International"}, {"symbol": "COP", "name": "ConocoPhillips"},
    {"symbol": "NKE", "name": "Nike Inc."}, {"symbol": "UNP", "name": "Union Pacific"},
    {"symbol": "RTX", "name": "RTX Corp."}, {"symbol": "IBM", "name": "IBM"},
    {"symbol": "QCOM", "name": "Qualcomm Inc."}, {"symbol": "GE", "name": "General Electric"},
    {"symbol": "CAT", "name": "Caterpillar Inc."}, {"symbol": "AMGN", "name": "Amgen Inc."},
    {"symbol": "SPGI", "name": "S&P Global"}, {"symbol": "BA", "name": "Boeing Co."},
    {"symbol": "SBUX", "name": "Starbucks Corp."}, {"symbol": "AMAT", "name": "Applied Materials"},
    {"symbol": "GS", "name": "Goldman Sachs"}, {"symbol": "BLK", "name": "BlackRock Inc."},
    {"symbol": "ISRG", "name": "Intuitive Surgical"}, {"symbol": "MDT", "name": "Medtronic plc"},
    {"symbol": "DE", "name": "Deere & Co."}, {"symbol": "LMT", "name": "Lockheed Martin"},
    {"symbol": "ADP", "name": "Automatic Data Processing"}, {"symbol": "MU", "name": "Micron Technology"},
    {"symbol": "T", "name": "AT&T Inc."}, {"symbol": "PLD", "name": "Prologis Inc."},
    {"symbol": "GILD", "name": "Gilead Sciences"}, {"symbol": "MO", "name": "Altria Group"},
    {"symbol": "TJX", "name": "TJX Companies"}, {"symbol": "SYK", "name": "Stryker Corp."},
    {"symbol": "ELV", "name": "Elevance Health"}, {"symbol": "REGN", "name": "Regeneron Pharma"},
    {"symbol": "BKNG", "name": "Booking Holdings"}, {"symbol": "VRTX", "name": "Vertex Pharmaceuticals"},
    {"symbol": "PANW", "name": "Palo Alto Networks"}, {"symbol": "ETN", "name": "Eaton Corp."},
    {"symbol": "NOW", "name": "ServiceNow Inc."}, {"symbol": "ANET", "name": "Arista Networks"},
    {"symbol": "MRVL", "name": "Marvell Technology"}, {"symbol": "PYPL", "name": "PayPal Holdings"},
    {"symbol": "CMG", "name": "Chipotle Mexican Grill"}, {"symbol": "SNOW", "name": "Snowflake Inc."},
    {"symbol": "SHOP", "name": "Shopify Inc."}, {"symbol": "UBER", "name": "Uber Technologies"},
    {"symbol": "ABNB", "name": "Airbnb Inc."}, {"symbol": "COIN", "name": "Coinbase Global"},
    {"symbol": "PLTR", "name": "Palantir Technologies"}, {"symbol": "SOFI", "name": "SoFi Technologies"},
    {"symbol": "RIVN", "name": "Rivian Automotive"}, {"symbol": "LCID", "name": "Lucid Group"},
    {"symbol": "SMCI", "name": "Super Micro Computer"}, {"symbol": "ARM", "name": "Arm Holdings"},
    {"symbol": "DELL", "name": "Dell Technologies"}, {"symbol": "MRNA", "name": "Moderna Inc."},
    {"symbol": "BABA", "name": "Alibaba Group"}, {"symbol": "JD", "name": "JD.com"},
    {"symbol": "PDD", "name": "PDD Holdings"}, {"symbol": "NIO", "name": "NIO Inc."},
    {"symbol": "TSM", "name": "Taiwan Semiconductor"}, {"symbol": "ASML", "name": "ASML Holding"},
    {"symbol": "SONY", "name": "Sony Group"}, {"symbol": "TM", "name": "Toyota Motor"},
    {"symbol": "F", "name": "Ford Motor Co."}, {"symbol": "GM", "name": "General Motors"},
    {"symbol": "DIS", "name": "Walt Disney Co."}, {"symbol": "CMCSA", "name": "Comcast Corp."},
    {"symbol": "WBD", "name": "Warner Bros. Discovery"}, {"symbol": "PARA", "name": "Paramount Global"},
    {"symbol": "SPOT", "name": "Spotify Technology"}, {"symbol": "RBLX", "name": "Roblox Corp."},
    {"symbol": "DASH", "name": "DoorDash Inc."}, {"symbol": "CRWD", "name": "CrowdStrike Holdings"},
    {"symbol": "ZS", "name": "Zscaler Inc."}, {"symbol": "DDOG", "name": "Datadog Inc."},
    {"symbol": "NET", "name": "Cloudflare Inc."}, {"symbol": "MDB", "name": "MongoDB Inc."},
    {"symbol": "TEAM", "name": "Atlassian Corp."}, {"symbol": "WDAY", "name": "Workday Inc."},
    {"symbol": "OKTA", "name": "Okta Inc."}, {"symbol": "TTD", "name": "The Trade Desk"},
    {"symbol": "ROKU", "name": "Roku Inc."}, {"symbol": "PINS", "name": "Pinterest Inc."},
    {"symbol": "SNAP", "name": "Snap Inc."}, {"symbol": "RKLB", "name": "Rocket Lab USA"},
    {"symbol": "ASTS", "name": "AST SpaceMobile"}, {"symbol": "CCJ", "name": "Cameco Corp."},
    {"symbol": "GEV", "name": "GE Vernova"}, {"symbol": "VRT", "name": "Vertiv Holdings"},
    {"symbol": "XOM", "name": "Exxon Mobil"}, {"symbol": "SLB", "name": "Schlumberger"},
    {"symbol": "OXY", "name": "Occidental Petroleum"}, {"symbol": "FCX", "name": "Freeport-McMoRan"},
    {"symbol": "NEM", "name": "Newmont Corp."}, {"symbol": "C", "name": "Citigroup Inc."},
    {"symbol": "MS", "name": "Morgan Stanley"}, {"symbol": "SCHW", "name": "Charles Schwab"},
    {"symbol": "AXP", "name": "American Express"}, {"symbol": "CVS", "name": "CVS Health"},
    {"symbol": "LLY", "name": "Eli Lilly and Co."}, {"symbol": "SPY", "name": "SPDR S&P 500 ETF"},
    {"symbol": "QQQ", "name": "Invesco QQQ Trust"}, {"symbol": "DIA", "name": "SPDR Dow Jones Industrial ETF"},
    {"symbol": "IWM", "name": "iShares Russell 2000 ETF"}, {"symbol": "GLD", "name": "SPDR Gold Shares"},
    {"symbol": "TLT", "name": "iShares 20+ Year Treasury Bond ETF"}, {"symbol": "VIX", "name": "CBOE Volatility Index"},
    {"symbol": "BTC-USD", "name": "Bitcoin USD"}, {"symbol": "ETH-USD", "name": "Ethereum USD"}
]


@app.get("/")
async def serve_home():
    return FileResponse('index.html')


@app.get("/api/tickers")
def search_tickers(q: str = ""):
    q = q.upper().strip()
    if not q:
        return TICKERS_DB[:60]
    matches = [t for t in TICKERS_DB if t["symbol"].startswith(q) or q in t["name"].upper()]
    return matches[:25]


@app.get("/api/watchlist")
def get_watchlist():
    return watchlist


@app.post("/api/watchlist")
def add_to_watchlist(ticker: str = Query(...)):
    ticker = ticker.upper().strip()
    if ticker not in watchlist:
        watchlist.append(ticker)
    return watchlist


@app.delete("/api/watchlist/{ticker}")
def remove_from_watchlist(ticker: str):
    global watchlist
    ticker = ticker.upper().strip()
    watchlist = [t for t in watchlist if t != ticker]
    return watchlist


@app.get("/api/stats")
def get_stats(ticker: str = "NVDA"):
    stock = yf.Ticker(ticker)
    try:
        info = stock.info
    except Exception:
        info = {}

    bundle = get_price_bundle(ticker)
    call_score, put_score = calculate_option_scores(ticker, info)
    iv_rank = calculate_iv_rank(ticker)

    mcap = info.get('marketCap', 0)
    vol = info.get('volume', 0)
    fair_value, fair_value_upside_pct = calculate_fair_value(info, bundle["current_price"])

    return {
        "ticker": ticker,
        "current_price": bundle["current_price"],
        "close_price": bundle["close_price"],
        "pre_price": bundle["pre_price"],
        "post_price": bundle["post_price"],
        "market_session": bundle["market_session"],
        "pe_ratio": round(info.get('trailingPE', 0), 2) if info.get('trailingPE') else "-",
        "market_cap": f"{mcap / 1e12:.2f}T" if mcap else "-",
        "fair_value": fair_value,
        "fair_value_upside_pct": fair_value_upside_pct,
        "volume": f"{vol / 1e6:.2f}M" if vol else "-",
        "iv_rank": iv_rank,
        "call_score": call_score,
        "put_score": put_score,
        "put_call_ratio": round(put_score / max(call_score, 1), 2)
    }


@app.get("/api/indicators")
def get_indicators(ticker: str = "NVDA", timeframe: str = "1d"):
    current_price = get_base_price(ticker)
    is_week = (timeframe == "week")
    basis = "week" if is_week else "1d"

    bar = get_pivot_source_bar(ticker, is_week)
    if bar is not None:
        levels = calculate_pivot_levels(float(bar['High']), float(bar['Low']), float(bar['Close']))
    else:
        p = current_price
        levels = {
            "pivot": round(p, 2),
            "resistance": [round(p * 1.02, 2), round(p * 1.04, 2), round(p * 1.06, 2)],
            "support": [round(p * 0.98, 2), round(p * 0.96, 2), round(p * 0.94, 2)],
        }

    atr = get_atr_for_timeframe(ticker, timeframe)
    bar_seconds = BAR_SECONDS.get(timeframe, 86400)

    def build_level(price: float, kind: str, idx: int):
        distance = abs(price - current_price)
        distance_pct = round((distance / current_price) * 100, 2) if current_price else 0
        return {
            "label": f"{kind}{idx}",
            "level": round(price, 2),
            "distance_pct": distance_pct,
            "eta": estimate_eta(distance, atr, bar_seconds),
        }

    supports = [build_level(p, "S", i + 1) for i, p in enumerate(levels["support"])]
    resistances = [build_level(p, "R", i + 1) for i, p in enumerate(levels["resistance"])]

    all_levels = supports + resistances
    closest = min(all_levels, key=lambda x: x["distance_pct"]) if all_levels else None

    return {
        "ticker": ticker,
        "current_price": round(current_price, 2),
        "timeframe_requested": timeframe,
        "basis_timeframe": basis,          
        "pivot": levels["pivot"],
        "support": supports,               
        "resistance": resistances,         
        "closest_alert": closest,          
        "s1": supports[0]["level"] if len(supports) > 0 else None,
        "s2": supports[1]["level"] if len(supports) > 1 else None,
        "r1": resistances[0]["level"] if len(resistances) > 0 else None,
        "r2": resistances[1]["level"] if len(resistances) > 1 else None,
    }


@app.get("/api/chart-data")
def get_chart_data(ticker: str = "NVDA", timeframe: str = "1d"):
    cfg = TIMEFRAME_CONFIG.get(timeframe, TIMEFRAME_CONFIG["1d"])
    stock = yf.Ticker(ticker)

    try:
        hist = stock.history(period=cfg["period"], interval=cfg["interval"], prepost=True)
    except Exception:
        hist = pd.DataFrame()

    if hist.empty:
        return []

    if cfg.get("resample"):
        agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
        if "Volume" in hist.columns:
            agg["Volume"] = "sum"
        hist = hist.resample(cfg["resample"]).agg(agg).dropna(subset=["Close"])

    hist['EMA20'] = hist['Close'].ewm(span=20, adjust=False).mean()
    hist['EMA50'] = hist['Close'].ewm(span=50, adjust=False).mean()
    hist['RSI'] = calculate_rsi(hist['Close'], 14)

    is_intraday = cfg["interval"] not in ("1d", "1wk")

    data = []
    for date, row in hist.iterrows():
        if pd.isna(row['Close']):
            continue
        t = int(date.timestamp()) if is_intraday else date.strftime("%Y-%m-%d")
        vol = row['Volume'] if 'Volume' in hist.columns and not pd.isna(row.get('Volume')) else 0
        data.append({
            "time": t,
            "open": round(row['Open'], 2), "high": round(row['High'], 2),
            "low": round(row['Low'], 2), "close": round(row['Close'], 2),
            "volume": int(vol) if vol else 0,
            "ema20": round(row['EMA20'], 2) if not pd.isna(row['EMA20']) else None,
            "ema50": round(row['EMA50'], 2) if not pd.isna(row['EMA50']) else None,
            "rsi": round(row['RSI'], 2) if not pd.isna(row['RSI']) else 50
        })
    return data


@app.get("/api/analysis")
def get_analysis(ticker: str = "NVDA"):
    return {
        "summary": f"🤖 [AI Real-Time Evaluation: {ticker}]\nระบบประมวลผลแยกสาย Call Option และ Put Option เป็นอิสระเรียบร้อยแล้ว ดาต้าพร้อมเสิร์ฟบนเกจวัดความเสี่ยง"
    }


# ---------------------------------------------------------------------------
# 👜 Smart Option Pocket Endpoints (คำนวณกำไรขาดทุนถูกต้องตามกลไกตลาดออปชัน)
# ---------------------------------------------------------------------------
@app.get("/api/positions")
def get_positions():
    for pos in logged_positions:
        tk = pos["ticker"]
        strike = float(pos["strike_price"])
        opt_type = pos["option_type"].upper()  
        exp = pos["expiration"]                
        premium_paid = float(pos["premium_paid"])
        qty = int(pos["quantity"])

        curr_underlying = get_base_price(tk)
        total_cost = premium_paid * 100 * qty
        current_premium = None

        # --- ขั้นที่ 1: ดึงราคาพรีเมียมจริงของออปชันสัญญาแม่จากตลาดหลักทรัพย์ ---
        try:
            stock = yf.Ticker(tk)
            chain = stock.option_chain(exp)
            df = chain.calls if opt_type == "CALL" else chain.puts
            
            opt_row = df[df['strike'] == strike]
            if not opt_row.empty:
                row = opt_row.iloc[0]
                bid = row.get('bid')
                ask = row.get('ask')
                # ใช้ค่าเฉลี่ย Bid/Ask (Mid Price) เพื่อสะท้อนราคาตลาดยุติธรรมที่สุด
                if bid and ask and bid > 0 and ask > 0:
                    current_premium = (bid + ask) / 2
                else:
                    current_premium = row.get('lastPrice')
        except Exception:
            pass  

        # --- ขั้นที่ 2: ประมวลผล P&L ---
        if current_premium is not None and current_premium > 0:
            # ใช้ราคาพรีเมียมจาก Real Market Chain
            pnl = (current_premium - premium_paid) * 100 * qty
        else:
            # Fallback หาก API หนาแน่น ดึง Chain ไม่ทัน จะคิดจากมูลค่าที่แท้จริง (Intrinsic Value)
            if opt_type == "CALL":
                intrinsic_value = max(0.0, curr_underlying - strike)
            else:
                intrinsic_value = max(0.0, strike - curr_underlying)
            
            pnl = (intrinsic_value - premium_paid) * 100 * qty
            
            # กฎเหล็กคนซื้อ Option: ขาดทุนสูงสุดต้องไม่เกินค่าพรีเมียมที่จ่าย (Risk Cap)
            if pnl < -total_cost:
                pnl = -total_cost

        pnl_percent = (pnl / total_cost) * 100 if total_cost > 0 else 0

        # แนบข้อมูลกลับไปอัปเดตบนหน้า UI Dashboard
        pos["current_underlying_price"] = round(curr_underlying, 2)
        pos["current_option_premium"] = round(current_premium, 2) if current_premium else "N/A (Intrinsic Calc)"
        pos["pnl"] = round(pnl, 2)
        pos["pnl_percent"] = round(pnl_percent, 2)
    return logged_positions


@app.post("/api/positions")
def add_position(pos: PositionModel):
    entry_price = get_base_price(pos.ticker)
    new_pos = pos.dict()
    new_pos["id"] = random.randint(1000, 9999)
    new_pos["entry_underlying_price"] = entry_price
    new_pos["pnl"] = 0.0
    new_pos["pnl_percent"] = 0.0
    logged_positions.append(new_pos)

    msg = f"\n🟢 [เปิดออปชัน]\nหุ้น: {pos.ticker} ({pos.option_type})\nStrike: ${pos.strike_price}\nจำนวน: {pos.quantity} สัญญา"
    send_line_alert(msg)
    return new_pos


@app.delete("/api/positions/{pos_id}")
def close_position(pos_id: int):
    global logged_positions
    pos = next((p for p in logged_positions if p["id"] == pos_id), None)
    if pos:
        msg = f"\n🔴 [ปิดออปชัน]\nหุ้น: {pos['ticker']} ({pos['option_type']})\nP&L: ${pos['pnl']} ({pos['pnl_percent']}%)"
        send_line_alert(msg)
    logged_positions = [p for p in logged_positions if p["id"] != pos_id]
    return {"status": "success"}


@app.websocket("/ws/price/{ticker}")
async def websocket_endpoint(websocket: WebSocket, ticker: str):
    await websocket.accept()
    ticker = ticker.upper()
    current_price = await asyncio.to_thread(get_base_price, ticker)

    try:
        tick = 0
        while True:
            session = get_market_session()
            if session == "REGULAR":
                if tick % 3 == 0:
                    live = await asyncio.to_thread(get_live_1m_price, ticker)
                    if live:
                        current_price = live
            live_prices[ticker] = current_price

            await websocket.send_text(json.dumps({
                "ticker": ticker,
                "price": round(current_price, 2),
                "market_session": session
            }))
            tick += 1
            await asyncio.sleep(1)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# 🔮 Options What-If Simulator (Black-Scholes Model)
# ---------------------------------------------------------------------------
def norm_cdf(x):
    """ฟังก์ชันสะสมความน่าจะเป็นแบบปกติ (Standard Normal CDF)"""
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0

def black_scholes(S, K, T, r, sigma, option_type="CALL"):
    """
    S: ราคาหุ้นเป้าหมาย (Target Price)
    K: ราคา Strike
    T: เวลาที่เหลือจนกว่าจะหมดอายุ (เป็นสัดส่วนของปี เช่น 30 วัน = 30/365)
    r: อัตราดอกเบี้ยปลอดความเสี่ยง (ใช้ 0.05 หรือ 5%)
    sigma: ค่าความผันผวนแฝง (IV - Implied Volatility)
    """
    if T <= 0:
        return max(0.0, S - K) if option_type == "CALL" else max(0.0, K - S)
    
    # ป้องกันกรณี IV เป็น 0
    sigma = max(sigma, 0.001) 
    
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    
    if option_type == "CALL":
        return S * norm_cdf(d1) - K * math.exp(-r * T) * norm_cdf(d2)
    else:
        return K * math.exp(-r * T) * norm_cdf(-d2) - S * norm_cdf(-d1)

class SimulatorModel(BaseModel):
    strike_price: float
    option_type: str
    expiration: str
    premium_paid: float
    current_iv: float
    target_price: float
    target_date: str

@app.post("/api/simulate")
def simulate_option(data: SimulatorModel):
    try:
        # คำนวณวันหมดอายุ และจำนวนวันที่เหลือจนถึงเป้าหมาย
        exp_date = datetime.strptime(data.expiration, "%Y-%m-%d")
        tgt_date = datetime.strptime(data.target_date, "%Y-%m-%d")
        
        days_to_exp = (exp_date - tgt_date).days
        if days_to_exp < 0:
            return {"error": "วันที่ตั้งเป้าหมาย ต้องไม่เกินวันหมดอายุของสัญญา!"}
        
        T = days_to_exp / 365.0
        r = 0.05  # กำหนด Risk-free rate ที่ 5%
        sigma = data.current_iv / 100.0  # แปลง IV จากเปอร์เซ็นต์เป็นทศนิยม

        # คำนวณราคาพรีเมียมตามเป้าหมาย (รับรู้ Time Decay อัตโนมัติจากค่า T ที่ลดลง)
        simulated_premium = black_scholes(data.target_price, data.strike_price, T, r, sigma, data.option_type)
        
        # คำนวณกำไร/ขาดทุน
        pnl_per_share = simulated_premium - data.premium_paid
        pnl_percent = (pnl_per_share / data.premium_paid) * 100 if data.premium_paid > 0 else 0
        
        # คำนวณจุดคุ้มทุน (Break-even)
        break_even = data.strike_price + data.premium_paid if data.option_type == "CALL" else data.strike_price - data.premium_paid

        return {
            "simulated_premium": round(simulated_premium, 2),
            "pnl_per_share": round(pnl_per_share, 2),
            "pnl_percent": round(pnl_percent, 2),
            "days_remaining": days_to_exp,
            "break_even": round(break_even, 2)
        }
    except Exception as e:
        return {"error": str(e)}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)