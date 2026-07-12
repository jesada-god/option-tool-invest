from fastapi import FastAPI, WebSocket, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel
import asyncio
import random
import json
import requests
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo
import yfinance as yf
import pandas as pd


app = FastAPI()

# 🔴 [ใส่ LINE Token ของคุณตรงนี้] 🔴
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
# 🕒 Market session helper (America/New_York, regular NYSE/Nasdaq hours)
# ---------------------------------------------------------------------------
def get_market_session() -> str:
    """Returns 'PRE', 'REGULAR', 'POST' or 'CLOSED' based on real NY time."""
    now_ny = datetime.now(ZoneInfo("America/New_York"))
    if now_ny.weekday() >= 5:  # Sat/Sun
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
    """
    Figures out which price should be shown as the 'live' headline price:
      - Market REGULAR  -> real-time last traded price
      - PRE / POST      -> the pre/post market price if Yahoo has one
      - CLOSED          -> the ACTUAL closing price of the most recently
                            completed regular session — exactly that number,
                            nothing else, until the market reopens.
    Also returns pre/post prices separately for the header chips.
    """
    session = get_market_session()
    stock = yf.Ticker(ticker)
    try:
        info = stock.info
    except Exception:
        info = {}

    # 'regularMarketPrice' is the last traded price DURING the regular session.
    # Once the regular session ends, Yahoo freezes this field at that session's
    # final print — so it IS the correct "last close" to show while CLOSED.
    # 'previousClose' is one full session further back, so using it while
    # CLOSED would (incorrectly) show yesterday's close instead of today's.
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
    else:  # CLOSED -> ราคาปิดจริงของวันซื้อขายล่าสุดที่ปิดไปแล้ว เป๊ะๆ ไม่ขยับจนกว่าตลาดจะเปิดใหม่
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
    """
    Real near-real-time price used to drive the live ticker while the market
    is REGULAR — no more random-walk simulation. Tries the lightweight
    fast_info endpoint first, then falls back to the latest 1-minute bar close.
    Returns None if Yahoo has nothing (caller should just keep the last price).
    """
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
# 🧠 Call/Put score = Technical (60%) + Fundamental (40%)
# ---------------------------------------------------------------------------
def calculate_option_scores(ticker: str, info: dict):
    # --- Technical component ---
    technical_score = 50.0
    try:
        hist = yf.Ticker(ticker).history(period="6mo", interval="1d")
        if not hist.empty and len(hist) > 20:
            closes = hist['Close']
            rsi_series = calculate_rsi(closes, 14)
            last_rsi = rsi_series.iloc[-1]
            ema20 = closes.ewm(span=20, adjust=False).mean().iloc[-1]
            ema50 = closes.ewm(span=50, adjust=False).mean().iloc[-1] if len(closes) >= 50 else ema20
            last_close = closes.iloc[-1]

            rsi_score = float(last_rsi) if not pd.isna(last_rsi) else 50.0

            trend_score = 50.0
            if last_close > ema20 > ema50:
                trend_score = 75.0
            elif last_close > ema20 and ema20 <= ema50:
                trend_score = 60.0
            elif last_close < ema20 < ema50:
                trend_score = 25.0
            elif last_close < ema20 and ema20 >= ema50:
                trend_score = 40.0

            technical_score = (rsi_score * 0.5) + (trend_score * 0.5)
    except Exception:
        technical_score = 50.0

    # --- Fundamental component ---
    fundamental_score = 50.0
    try:
        rec_mean = info.get('recommendationMean')       # 1 (Strong Buy) -> 5 (Strong Sell)
        target = info.get('targetMeanPrice')
        current = info.get('currentPrice') or info.get('regularMarketPrice')
        rev_growth = info.get('revenueGrowth')
        profit_margin = info.get('profitMargins')

        sub_scores = []
        if rec_mean:
            sub_scores.append(max(0, min(100, 112.5 - float(rec_mean) * 22.5)))
        if target and current:
            upside = (float(target) - float(current)) / float(current)
            sub_scores.append(max(0, min(100, 50 + upside * 150)))
        if rev_growth is not None:
            sub_scores.append(max(0, min(100, 50 + float(rev_growth) * 100)))
        if profit_margin is not None:
            sub_scores.append(max(0, min(100, 50 + float(profit_margin) * 100)))

        if sub_scores:
            fundamental_score = sum(sub_scores) / len(sub_scores)
    except Exception:
        fundamental_score = 50.0

    call_score = round((technical_score * 0.6) + (fundamental_score * 0.4))
    call_score = int(max(0, min(100, call_score)))
    return call_score, 100 - call_score


def calculate_fair_value(info: dict, current_price: float):
    """
    'Fair value' estimate blended from internationally-recognized valuation methods
    (weighted average of whichever methods have usable data):

    1) Analyst consensus target price (targetMeanPrice) — weight 0.5
       The standard real-world "fair value" figure used by sell-side research desks.
    2) Graham Number — weight 0.3
       Classic Benjamin Graham value-investing formula: sqrt(22.5 x EPS x Book Value/Share).
       A globally-used conservative intrinsic-value benchmark.
    3) Forward-P/E valuation — weight 0.2
       Forward EPS x the stock's own forward P/E (falls back to a 20x broad-market
       multiple if forward P/E is missing or looks unreasonable).

    If none of the above have usable data, falls back to trailing EPS x 20 (broad
    market average multiple), then finally to the current price itself (neutral).
    Returns (fair_value, breakdown_dict) so callers can show upside/downside context.
    """
    methods = []   # list of (value, weight, label)

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
    """
    Best-effort IV read:
    1) Try real ATM implied volatility from the nearest options expiry.
    2) Fall back to a realized-volatility percentile-rank proxy.
    """
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
# 🔎 Large static ticker directory (for autocomplete / "search as many as possible")
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
    {"symbol": "ONDS", "name": "Ondas Holdings"}, {"symbol": "ASTS", "name": "AST SpaceMobile"},
    {"symbol": "JOBY", "name": "Joby Aviation"}, {"symbol": "ACHR", "name": "Archer Aviation"},
    {"symbol": "CCJ", "name": "Cameco Corp."}, {"symbol": "UEC", "name": "Uranium Energy Corp."},
    {"symbol": "GEV", "name": "GE Vernova"}, {"symbol": "VRT", "name": "Vertiv Holdings"},
    {"symbol": "FIX", "name": "Comfort Systems USA"}, {"symbol": "PWR", "name": "Quanta Services"},
    {"symbol": "ALAB", "name": "Astera Labs"}, {"symbol": "MOD", "name": "Modine Manufacturing"},
    {"symbol": "NVT", "name": "nVent Electric"}, {"symbol": "XOM", "name": "Exxon Mobil"},
    {"symbol": "SLB", "name": "Schlumberger"}, {"symbol": "OXY", "name": "Occidental Petroleum"},
    {"symbol": "FCX", "name": "Freeport-McMoRan"}, {"symbol": "NEM", "name": "Newmont Corp."},
    {"symbol": "MOS", "name": "Mosaic Co."}, {"symbol": "DOW", "name": "Dow Inc."},
    {"symbol": "C", "name": "Citigroup Inc."}, {"symbol": "MS", "name": "Morgan Stanley"},
    {"symbol": "SCHW", "name": "Charles Schwab"}, {"symbol": "AXP", "name": "American Express"},
    {"symbol": "USB", "name": "U.S. Bancorp"}, {"symbol": "PNC", "name": "PNC Financial"},
    {"symbol": "MET", "name": "MetLife Inc."}, {"symbol": "AIG", "name": "American International Group"},
    {"symbol": "CB", "name": "Chubb Ltd."}, {"symbol": "PGR", "name": "Progressive Corp."},
    {"symbol": "CVS", "name": "CVS Health"}, {"symbol": "CI", "name": "Cigna Group"},
    {"symbol": "HUM", "name": "Humana Inc."}, {"symbol": "LLY", "name": "Eli Lilly and Co."},
    {"symbol": "BMY", "name": "Bristol-Myers Squibb"}, {"symbol": "ZTS", "name": "Zoetis Inc."},
    {"symbol": "SPY", "name": "SPDR S&P 500 ETF"}, {"symbol": "QQQ", "name": "Invesco QQQ Trust"},
    {"symbol": "DIA", "name": "SPDR Dow Jones Industrial ETF"}, {"symbol": "IWM", "name": "iShares Russell 2000 ETF"},
    {"symbol": "VOO", "name": "Vanguard S&P 500 ETF"}, {"symbol": "VTI", "name": "Vanguard Total Stock Market ETF"},
    {"symbol": "ARKK", "name": "ARK Innovation ETF"}, {"symbol": "XLK", "name": "Technology Select Sector SPDR"},
    {"symbol": "XLF", "name": "Financial Select Sector SPDR"}, {"symbol": "XLE", "name": "Energy Select Sector SPDR"},
    {"symbol": "SMH", "name": "VanEck Semiconductor ETF"}, {"symbol": "SOXX", "name": "iShares Semiconductor ETF"},
    {"symbol": "GLD", "name": "SPDR Gold Shares"}, {"symbol": "SLV", "name": "iShares Silver Trust"},
    {"symbol": "TLT", "name": "iShares 20+ Year Treasury Bond ETF"}, {"symbol": "HYG", "name": "iShares High Yield Corp Bond ETF"},
    {"symbol": "VIX", "name": "CBOE Volatility Index"}, {"symbol": "BTC-USD", "name": "Bitcoin USD"},
    {"symbol": "ETH-USD", "name": "Ethereum USD"},

    # --- Expanded US-stock coverage (tech, healthcare, financials, energy,
    #     industrials, consumer, REITs, utilities, growth/cloud, ETFs, etc.) ---
    {"symbol": "INTU", "name": "Intuit Inc."}, {"symbol": "ADSK", "name": "Autodesk Inc."},
    {"symbol": "CDNS", "name": "Cadence Design Systems"}, {"symbol": "SNPS", "name": "Synopsys Inc."},
    {"symbol": "FTNT", "name": "Fortinet Inc."}, {"symbol": "CHKP", "name": "Check Point Software"},
    {"symbol": "PAYX", "name": "Paychex Inc."}, {"symbol": "PAYC", "name": "Paycom Software"},
    {"symbol": "FI", "name": "Fiserv Inc."}, {"symbol": "GPN", "name": "Global Payments Inc."},
    {"symbol": "MSCI", "name": "MSCI Inc."}, {"symbol": "MCO", "name": "Moody's Corp."},
    {"symbol": "ICE", "name": "Intercontinental Exchange"}, {"symbol": "CME", "name": "CME Group Inc."},
    {"symbol": "NDAQ", "name": "Nasdaq Inc."}, {"symbol": "TROW", "name": "T. Rowe Price Group"},
    {"symbol": "IVZ", "name": "Invesco Ltd."}, {"symbol": "STT", "name": "State Street Corp."},
    {"symbol": "BK", "name": "Bank of New York Mellon"}, {"symbol": "FITB", "name": "Fifth Third Bancorp"},
    {"symbol": "KEY", "name": "KeyCorp"}, {"symbol": "RF", "name": "Regions Financial"},
    {"symbol": "HBAN", "name": "Huntington Bancshares"}, {"symbol": "CFG", "name": "Citizens Financial Group"},
    {"symbol": "ZION", "name": "Zions Bancorporation"}, {"symbol": "MTB", "name": "M&T Bank Corp."},
    {"symbol": "TFC", "name": "Truist Financial"}, {"symbol": "ALLY", "name": "Ally Financial"},
    {"symbol": "DFS", "name": "Discover Financial Services"}, {"symbol": "SYF", "name": "Synchrony Financial"},
    {"symbol": "COF", "name": "Capital One Financial"}, {"symbol": "ADI", "name": "Analog Devices"},
    {"symbol": "MCHP", "name": "Microchip Technology"}, {"symbol": "ON", "name": "ON Semiconductor"},
    {"symbol": "SWKS", "name": "Skyworks Solutions"}, {"symbol": "QRVO", "name": "Qorvo Inc."},
    {"symbol": "KLAC", "name": "KLA Corp."}, {"symbol": "LRCX", "name": "Lam Research"},
    {"symbol": "TER", "name": "Teradyne Inc."}, {"symbol": "ENTG", "name": "Entegris Inc."},
    {"symbol": "MPWR", "name": "Monolithic Power Systems"}, {"symbol": "NXPI", "name": "NXP Semiconductors"},
    {"symbol": "CDW", "name": "CDW Corp."}, {"symbol": "ANSS", "name": "ANSYS Inc."},
    {"symbol": "TYL", "name": "Tyler Technologies"}, {"symbol": "ROP", "name": "Roper Technologies"},
    {"symbol": "KEYS", "name": "Keysight Technologies"}, {"symbol": "TDY", "name": "Teledyne Technologies"},
    {"symbol": "FTV", "name": "Fortive Corp."}, {"symbol": "AME", "name": "AMETEK Inc."},
    {"symbol": "EMR", "name": "Emerson Electric"}, {"symbol": "ITW", "name": "Illinois Tool Works"},
    {"symbol": "PH", "name": "Parker Hannifin"}, {"symbol": "DOV", "name": "Dover Corp."},
    {"symbol": "XYL", "name": "Xylem Inc."}, {"symbol": "IEX", "name": "IDEX Corp."},
    {"symbol": "IR", "name": "Ingersoll Rand"}, {"symbol": "GNRC", "name": "Generac Holdings"},
    {"symbol": "CARR", "name": "Carrier Global"}, {"symbol": "OTIS", "name": "Otis Worldwide"},
    {"symbol": "JCI", "name": "Johnson Controls"}, {"symbol": "HON", "name": "Honeywell International"},
    {"symbol": "LHX", "name": "L3Harris Technologies"}, {"symbol": "NOC", "name": "Northrop Grumman"},
    {"symbol": "GD", "name": "General Dynamics"}, {"symbol": "TXT", "name": "Textron Inc."},
    {"symbol": "HII", "name": "Huntington Ingalls Industries"}, {"symbol": "LDOS", "name": "Leidos Holdings"},
    {"symbol": "CACI", "name": "CACI International"}, {"symbol": "SAIC", "name": "Science Applications Intl"},
    {"symbol": "WM", "name": "Waste Management"}, {"symbol": "RSG", "name": "Republic Services"},
    {"symbol": "CTAS", "name": "Cintas Corp."}, {"symbol": "VRSK", "name": "Verisk Analytics"},
    {"symbol": "GWW", "name": "W.W. Grainger"}, {"symbol": "FAST", "name": "Fastenal Co."},
    {"symbol": "BR", "name": "Broadridge Financial Solutions"}, {"symbol": "EFX", "name": "Equifax Inc."},
    {"symbol": "FDS", "name": "FactSet Research Systems"}, {"symbol": "VLTO", "name": "Veralto Corp."},
    {"symbol": "IT", "name": "Gartner Inc."}, {"symbol": "BSX", "name": "Boston Scientific"},
    {"symbol": "BDX", "name": "Becton Dickinson"}, {"symbol": "EW", "name": "Edwards Lifesciences"},
    {"symbol": "HOLX", "name": "Hologic Inc."}, {"symbol": "RMD", "name": "ResMed Inc."},
    {"symbol": "ALGN", "name": "Align Technology"}, {"symbol": "IDXX", "name": "IDEXX Laboratories"},
    {"symbol": "IQV", "name": "IQVIA Holdings"}, {"symbol": "A", "name": "Agilent Technologies"},
    {"symbol": "WAT", "name": "Waters Corp."}, {"symbol": "MTD", "name": "Mettler-Toledo"},
    {"symbol": "RVTY", "name": "Revvity Inc."}, {"symbol": "CRL", "name": "Charles River Laboratories"},
    {"symbol": "DXCM", "name": "DexCom Inc."}, {"symbol": "PODD", "name": "Insulet Corp."},
    {"symbol": "ILMN", "name": "Illumina Inc."}, {"symbol": "INCY", "name": "Incyte Corp."},
    {"symbol": "BIIB", "name": "Biogen Inc."}, {"symbol": "VTRS", "name": "Viatris Inc."},
    {"symbol": "HCA", "name": "HCA Healthcare"}, {"symbol": "UHS", "name": "Universal Health Services"},
    {"symbol": "DVA", "name": "DaVita Inc."}, {"symbol": "CNC", "name": "Centene Corp."},
    {"symbol": "MOH", "name": "Molina Healthcare"}, {"symbol": "GEHC", "name": "GE HealthCare Technologies"},
    {"symbol": "ZBH", "name": "Zimmer Biomet Holdings"}, {"symbol": "BAX", "name": "Baxter International"},
    {"symbol": "COO", "name": "Cooper Companies"}, {"symbol": "ELAN", "name": "Elanco Animal Health"},
    {"symbol": "JAZZ", "name": "Jazz Pharmaceuticals"}, {"symbol": "EXEL", "name": "Exelixis Inc."},
    {"symbol": "NBIX", "name": "Neurocrine Biosciences"}, {"symbol": "UTHR", "name": "United Therapeutics"},
    {"symbol": "RARE", "name": "Ultragenyx Pharmaceutical"}, {"symbol": "BMRN", "name": "BioMarin Pharmaceutical"},
    {"symbol": "ALNY", "name": "Alnylam Pharmaceuticals"}, {"symbol": "IONS", "name": "Ionis Pharmaceuticals"},
    {"symbol": "SRPT", "name": "Sarepta Therapeutics"}, {"symbol": "FOLD", "name": "Amicus Therapeutics"},
    {"symbol": "ARWR", "name": "Arrowhead Pharmaceuticals"}, {"symbol": "XRAY", "name": "Dentsply Sirona"},
    {"symbol": "LULU", "name": "Lululemon Athletica"}, {"symbol": "ROST", "name": "Ross Stores"},
    {"symbol": "ULTA", "name": "Ulta Beauty"}, {"symbol": "TGT", "name": "Target Corp."},
    {"symbol": "LOW", "name": "Lowe's Companies"}, {"symbol": "KR", "name": "Kroger Co."},
    {"symbol": "DG", "name": "Dollar General"}, {"symbol": "DLTR", "name": "Dollar Tree"},
    {"symbol": "YUM", "name": "Yum! Brands"}, {"symbol": "DPZ", "name": "Domino's Pizza"},
    {"symbol": "DRI", "name": "Darden Restaurants"}, {"symbol": "BURL", "name": "Burlington Stores"},
    {"symbol": "GPS", "name": "Gap Inc."}, {"symbol": "RL", "name": "Ralph Lauren Corp."},
    {"symbol": "VFC", "name": "VF Corp."}, {"symbol": "HAS", "name": "Hasbro Inc."},
    {"symbol": "MAT", "name": "Mattel Inc."}, {"symbol": "EL", "name": "Estee Lauder Companies"},
    {"symbol": "CL", "name": "Colgate-Palmolive"}, {"symbol": "KMB", "name": "Kimberly-Clark"},
    {"symbol": "CHD", "name": "Church & Dwight"}, {"symbol": "CLX", "name": "Clorox Co."},
    {"symbol": "KHC", "name": "Kraft Heinz Co."}, {"symbol": "GIS", "name": "General Mills"},
    {"symbol": "K", "name": "Kellanova"}, {"symbol": "HRL", "name": "Hormel Foods"},
    {"symbol": "TSN", "name": "Tyson Foods"}, {"symbol": "CAG", "name": "Conagra Brands"},
    {"symbol": "SJM", "name": "J.M. Smucker Co."}, {"symbol": "MKC", "name": "McCormick & Co."},
    {"symbol": "STZ", "name": "Constellation Brands"}, {"symbol": "TAP", "name": "Molson Coors Beverage"},
    {"symbol": "MNST", "name": "Monster Beverage"}, {"symbol": "KDP", "name": "Keurig Dr Pepper"},
    {"symbol": "APD", "name": "Air Products and Chemicals"}, {"symbol": "ECL", "name": "Ecolab Inc."},
    {"symbol": "SHW", "name": "Sherwin-Williams"}, {"symbol": "PPG", "name": "PPG Industries"},
    {"symbol": "NUE", "name": "Nucor Corp."}, {"symbol": "STLD", "name": "Steel Dynamics"},
    {"symbol": "RS", "name": "Reliance Steel & Aluminum"}, {"symbol": "VMC", "name": "Vulcan Materials"},
    {"symbol": "MLM", "name": "Martin Marietta Materials"}, {"symbol": "ALB", "name": "Albemarle Corp."},
    {"symbol": "FMC", "name": "FMC Corp."}, {"symbol": "CF", "name": "CF Industries Holdings"},
    {"symbol": "LYB", "name": "LyondellBasell Industries"}, {"symbol": "EMN", "name": "Eastman Chemical"},
    {"symbol": "ETSY", "name": "Etsy Inc."}, {"symbol": "W", "name": "Wayfair Inc."},
    {"symbol": "CHWY", "name": "Chewy Inc."}, {"symbol": "CVNA", "name": "Carvana Co."},
    {"symbol": "CPNG", "name": "Coupang Inc."}, {"symbol": "MELI", "name": "MercadoLibre Inc."},
    {"symbol": "EOG", "name": "EOG Resources"}, {"symbol": "DVN", "name": "Devon Energy"},
    {"symbol": "HES", "name": "Hess Corp."}, {"symbol": "MRO", "name": "Marathon Oil"},
    {"symbol": "APA", "name": "APA Corp."}, {"symbol": "HAL", "name": "Halliburton Co."},
    {"symbol": "BKR", "name": "Baker Hughes Co."}, {"symbol": "WMB", "name": "Williams Companies"},
    {"symbol": "KMI", "name": "Kinder Morgan"}, {"symbol": "OKE", "name": "ONEOK Inc."},
    {"symbol": "PSX", "name": "Phillips 66"}, {"symbol": "VLO", "name": "Valero Energy"},
    {"symbol": "MPC", "name": "Marathon Petroleum"}, {"symbol": "TRGP", "name": "Targa Resources"},
    {"symbol": "MMM", "name": "3M Co."}, {"symbol": "UPS", "name": "United Parcel Service"},
    {"symbol": "FDX", "name": "FedEx Corp."}, {"symbol": "CSX", "name": "CSX Corp."},
    {"symbol": "NSC", "name": "Norfolk Southern"}, {"symbol": "ODFL", "name": "Old Dominion Freight Line"},
    {"symbol": "JBHT", "name": "J.B. Hunt Transport Services"}, {"symbol": "CHRW", "name": "C.H. Robinson Worldwide"},
    {"symbol": "EXPD", "name": "Expeditors International"}, {"symbol": "WAB", "name": "Westinghouse Air Brake"},
    {"symbol": "DAL", "name": "Delta Air Lines"}, {"symbol": "UAL", "name": "United Airlines Holdings"},
    {"symbol": "AAL", "name": "American Airlines Group"}, {"symbol": "LUV", "name": "Southwest Airlines"},
    {"symbol": "ALK", "name": "Alaska Air Group"}, {"symbol": "EXPE", "name": "Expedia Group"},
    {"symbol": "TRIP", "name": "TripAdvisor Inc."}, {"symbol": "MAR", "name": "Marriott International"},
    {"symbol": "HLT", "name": "Hilton Worldwide Holdings"}, {"symbol": "WYNN", "name": "Wynn Resorts"},
    {"symbol": "LVS", "name": "Las Vegas Sands"}, {"symbol": "MGM", "name": "MGM Resorts International"},
    {"symbol": "CCL", "name": "Carnival Corp."}, {"symbol": "RCL", "name": "Royal Caribbean Cruises"},
    {"symbol": "NCLH", "name": "Norwegian Cruise Line Holdings"}, {"symbol": "AMT", "name": "American Tower Corp."},
    {"symbol": "CCI", "name": "Crown Castle Inc."}, {"symbol": "SBAC", "name": "SBA Communications"},
    {"symbol": "EQIX", "name": "Equinix Inc."}, {"symbol": "DLR", "name": "Digital Realty Trust"},
    {"symbol": "PSA", "name": "Public Storage"}, {"symbol": "O", "name": "Realty Income Corp."},
    {"symbol": "SPG", "name": "Simon Property Group"}, {"symbol": "WELL", "name": "Welltower Inc."},
    {"symbol": "VTR", "name": "Ventas Inc."}, {"symbol": "ARE", "name": "Alexandria Real Estate Equities"},
    {"symbol": "AVB", "name": "AvalonBay Communities"}, {"symbol": "EQR", "name": "Equity Residential"},
    {"symbol": "ESS", "name": "Essex Property Trust"}, {"symbol": "MAA", "name": "Mid-America Apartment Communities"},
    {"symbol": "INVH", "name": "Invitation Homes"}, {"symbol": "VICI", "name": "VICI Properties"},
    {"symbol": "NEE", "name": "NextEra Energy"}, {"symbol": "DUK", "name": "Duke Energy"},
    {"symbol": "SO", "name": "Southern Co."}, {"symbol": "D", "name": "Dominion Energy"},
    {"symbol": "AEP", "name": "American Electric Power"}, {"symbol": "EXC", "name": "Exelon Corp."},
    {"symbol": "XEL", "name": "Xcel Energy"}, {"symbol": "ED", "name": "Consolidated Edison"},
    {"symbol": "PEG", "name": "Public Service Enterprise Group"}, {"symbol": "WEC", "name": "WEC Energy Group"},
    {"symbol": "ES", "name": "Eversource Energy"}, {"symbol": "FE", "name": "FirstEnergy Corp."},
    {"symbol": "ETR", "name": "Entergy Corp."}, {"symbol": "AEE", "name": "Ameren Corp."},
    {"symbol": "CMS", "name": "CMS Energy Corp."}, {"symbol": "DTE", "name": "DTE Energy Co."},
    {"symbol": "PPL", "name": "PPL Corp."}, {"symbol": "ATO", "name": "Atmos Energy"},
    {"symbol": "NI", "name": "NiSource Inc."}, {"symbol": "LNT", "name": "Alliant Energy"},
    {"symbol": "EVRG", "name": "Evergy Inc."}, {"symbol": "TRV", "name": "Travelers Companies"},
    {"symbol": "ALL", "name": "Allstate Corp."}, {"symbol": "HIG", "name": "Hartford Financial Services"},
    {"symbol": "LNC", "name": "Lincoln National Corp."}, {"symbol": "PFG", "name": "Principal Financial Group"},
    {"symbol": "AFL", "name": "Aflac Inc."}, {"symbol": "GL", "name": "Globe Life Inc."},
    {"symbol": "WRB", "name": "W.R. Berkley Corp."}, {"symbol": "RE", "name": "Everest Group"},
    {"symbol": "MMC", "name": "Marsh & McLennan Companies"}, {"symbol": "AON", "name": "Aon plc"},
    {"symbol": "WTW", "name": "Willis Towers Watson"}, {"symbol": "BRO", "name": "Brown & Brown Inc."},
    {"symbol": "AJG", "name": "Arthur J. Gallagher & Co."}, {"symbol": "TMUS", "name": "T-Mobile US Inc."},
    {"symbol": "CHTR", "name": "Charter Communications"}, {"symbol": "LYV", "name": "Live Nation Entertainment"},
    {"symbol": "EA", "name": "Electronic Arts"}, {"symbol": "TTWO", "name": "Take-Two Interactive Software"},
    {"symbol": "MTCH", "name": "Match Group"}, {"symbol": "IAC", "name": "IAC Inc."},
    {"symbol": "NWSA", "name": "News Corp Class A"}, {"symbol": "FOXA", "name": "Fox Corp Class A"},
    {"symbol": "U", "name": "Unity Software"}, {"symbol": "DOCU", "name": "DocuSign Inc."},
    {"symbol": "ZM", "name": "Zoom Video Communications"}, {"symbol": "TWLO", "name": "Twilio Inc."},
    {"symbol": "HUBS", "name": "HubSpot Inc."}, {"symbol": "BILL", "name": "Bill.com Holdings"},
    {"symbol": "PATH", "name": "UiPath Inc."}, {"symbol": "GTLB", "name": "GitLab Inc."},
    {"symbol": "CFLT", "name": "Confluent Inc."}, {"symbol": "S", "name": "SentinelOne Inc."},
    {"symbol": "TENB", "name": "Tenable Holdings"}, {"symbol": "RPD", "name": "Rapid7 Inc."},
    {"symbol": "QLYS", "name": "Qualys Inc."}, {"symbol": "VRNS", "name": "Varonis Systems"},
    {"symbol": "CYBR", "name": "CyberArk Software"}, {"symbol": "ESTC", "name": "Elastic N.V."},
    {"symbol": "PCTY", "name": "Paylocity Holding"}, {"symbol": "PCOR", "name": "Procore Technologies"},
    {"symbol": "APPF", "name": "AppFolio Inc."}, {"symbol": "SMAR", "name": "Smartsheet Inc."},
    {"symbol": "MNDY", "name": "Monday.com Ltd."}, {"symbol": "ASAN", "name": "Asana Inc."},
    {"symbol": "FROG", "name": "JFrog Ltd."}, {"symbol": "DBX", "name": "Dropbox Inc."},
    {"symbol": "BOX", "name": "Box Inc."}, {"symbol": "WDC", "name": "Western Digital"},
    {"symbol": "STX", "name": "Seagate Technology"}, {"symbol": "HPE", "name": "Hewlett Packard Enterprise"},
    {"symbol": "HPQ", "name": "HP Inc."}, {"symbol": "NTAP", "name": "NetApp Inc."},
    {"symbol": "JNPR", "name": "Juniper Networks"}, {"symbol": "FFIV", "name": "F5 Inc."},
    {"symbol": "CIEN", "name": "Ciena Corp."}, {"symbol": "LITE", "name": "Lumentum Holdings"},
    {"symbol": "COHR", "name": "Coherent Corp."}, {"symbol": "MSTR", "name": "MicroStrategy Inc."},
    {"symbol": "MARA", "name": "Marathon Digital Holdings"}, {"symbol": "RIOT", "name": "Riot Platforms"},
    {"symbol": "CLSK", "name": "CleanSpark Inc."}, {"symbol": "HUT", "name": "Hut 8 Mining Corp."},
    {"symbol": "HOOD", "name": "Robinhood Markets"}, {"symbol": "GME", "name": "GameStop Corp."},
    {"symbol": "AMC", "name": "AMC Entertainment Holdings"}, {"symbol": "BB", "name": "BlackBerry Ltd."},
    {"symbol": "IVV", "name": "iShares Core S&P 500 ETF"}, {"symbol": "MDY", "name": "SPDR S&P MidCap 400 ETF"},
    {"symbol": "IJH", "name": "iShares Core S&P Mid-Cap ETF"}, {"symbol": "IJR", "name": "iShares Core S&P Small-Cap ETF"},
    {"symbol": "EFA", "name": "iShares MSCI EAFE ETF"}, {"symbol": "EEM", "name": "iShares MSCI Emerging Markets ETF"},
    {"symbol": "VEA", "name": "Vanguard FTSE Developed Markets ETF"}, {"symbol": "VWO", "name": "Vanguard FTSE Emerging Markets ETF"},
    {"symbol": "AGG", "name": "iShares Core U.S. Aggregate Bond ETF"}, {"symbol": "BND", "name": "Vanguard Total Bond Market ETF"},
    {"symbol": "XLY", "name": "Consumer Discretionary Select Sector SPDR"}, {"symbol": "XLP", "name": "Consumer Staples Select Sector SPDR"},
    {"symbol": "XLV", "name": "Health Care Select Sector SPDR"}, {"symbol": "XLI", "name": "Industrial Select Sector SPDR"},
    {"symbol": "XLB", "name": "Materials Select Sector SPDR"}, {"symbol": "XLU", "name": "Utilities Select Sector SPDR"},
    {"symbol": "XLRE", "name": "Real Estate Select Sector SPDR"}, {"symbol": "XLC", "name": "Communication Services Select Sector SPDR"},
    {"symbol": "XBI", "name": "SPDR S&P Biotech ETF"}, {"symbol": "IBB", "name": "iShares Biotechnology ETF"},
    {"symbol": "KRE", "name": "SPDR S&P Regional Banking ETF"}, {"symbol": "KBE", "name": "SPDR S&P Bank ETF"},
    {"symbol": "JETS", "name": "US Global Jets ETF"}, {"symbol": "ITA", "name": "iShares U.S. Aerospace & Defense ETF"},
    {"symbol": "XRT", "name": "SPDR S&P Retail ETF"}, {"symbol": "XHB", "name": "SPDR S&P Homebuilders ETF"},
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


# ---------------------------------------------------------------------------
# 🎯 Support / Resistance system
#
# Rules implemented per user spec:
#   - D1 and Week each get their OWN pivot basis (last COMPLETED daily / weekly
#     candle), computed with the standard "Classic Pivot Points" formula —
#     a well-established, non-arbitrary technical-analysis method that
#     naturally produces 3 ordered levels on each side (S1..S3 / R1..R3).
#   - Every timeframe other than "week" (1m/5m/10m/15m/1h/4h/1d) is anchored
#     to the D1 basis. Only "week" uses the weekly basis.
#   - Each level also gets an estimated "time to reach" using that level's
#     distance from price divided by the ATR (Average True Range) of the
#     CURRENTLY VIEWED timeframe — i.e. a volatility-based ETA, not a promise.
# ---------------------------------------------------------------------------
BAR_SECONDS = {
    "1m": 60, "5m": 300, "10m": 600, "15m": 900,
    "1h": 3600, "4h": 14400, "1d": 86400, "week": 604800,
}


def calculate_pivot_levels(h: float, l: float, c: float) -> dict:
    """Classic (Floor Trader) Pivot Points — 3 support + 3 resistance levels."""
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
    """
    Returns the last FULLY COMPLETED daily or weekly candle to base pivots on.
    (If today's/this-week's candle is still forming, we deliberately skip it —
    pivots must be computed from a closed session, never a live/partial one.)
    """
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
    """ATR of the currently-viewed timeframe, used to project time-to-reach a level."""
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
    """Rough volatility-based ETA: bars-needed = distance / ATR, projected into real time."""
    if not atr or atr <= 0 or distance <= 0:
        return None
    bars_needed = distance / atr
    total_seconds = bars_needed * bar_seconds
    return format_duration(total_seconds)


@app.get("/api/indicators")
def get_indicators(ticker: str = "NVDA", timeframe: str = "1d"):
    current_price = get_base_price(ticker)
    is_week = (timeframe == "week")
    basis = "week" if is_week else "1d"

    bar = get_pivot_source_bar(ticker, is_week)
    if bar is not None:
        levels = calculate_pivot_levels(float(bar['High']), float(bar['Low']), float(bar['Close']))
    else:
        # Only used if Yahoo has literally no history for the ticker.
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
        "basis_timeframe": basis,          # "1d" or "week" — which candle the levels come from
        "pivot": levels["pivot"],
        "support": supports,               # [S1 nearest price ... S3 farthest]
        "resistance": resistances,         # [R1 nearest price ... R3 farthest]
        "closest_alert": closest,          # nearest S/R level overall, for the alert banner
        # Legacy flat fields kept for backward compatibility with older clients:
        "s1": supports[0]["level"] if len(supports) > 0 else None,
        "s2": supports[1]["level"] if len(supports) > 1 else None,
        "r1": resistances[0]["level"] if len(resistances) > 0 else None,
        "r2": resistances[1]["level"] if len(resistances) > 1 else None,
    }


# ---------------------------------------------------------------------------
# 📈 Timeframe configuration — 1m / 5m / 10m / 15m / 1h / 4h / 1d / week
# 10m and 4h aren't native Yahoo intervals, so they're built by resampling.
# ---------------------------------------------------------------------------
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
        "summary": f"🤖 [AI Real-Time Evaluation: {ticker}]\nระบบพร้อมรับคำสั่งและโหลดอินดิเคเตอร์เรียบร้อยแล้ว ราคาออปชันจะผูกติดกับสัญญาสินทรัพย์หลัก"
    }


@app.get("/api/positions")
def get_positions():
    for pos in logged_positions:
        tk = pos["ticker"]
        curr_underlying = get_base_price(tk)
        entry_underlying = pos["entry_underlying_price"]

        if pos["option_type"] == "CALL":
            pnl = (curr_underlying - entry_underlying) * 100 * pos["quantity"]
        else:
            pnl = (entry_underlying - curr_underlying) * 100 * pos["quantity"]

        total_cost = pos["premium_paid"] * 100 * pos["quantity"]
        pnl_percent = (pnl / total_cost) * 100 if total_cost > 0 else 0

        pos["current_underlying_price"] = round(curr_underlying, 2)
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
                # ตลาดเปิด -> ดึงราคาจริงจาก Yahoo (fast_info / แท่ง 1 นาทีล่าสุด) ทุก ~3 วิ
                # ไม่ใช่การจำลองราคาแบบสุ่มอีกต่อไป — ราคาที่เห็นวิ่งตามราคาตลาดจริงแบบ 1m
                if tick % 3 == 0:
                    live = await asyncio.to_thread(get_live_1m_price, ticker)
                    if live:
                        current_price = live
            # นอกเวลาตลาด (PRE/POST/CLOSED) -> คงราคาไว้ ไม่ให้ราคากระดิกมั่ว
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
# 📱 Run directly with: python main.py
# host="0.0.0.0" is what makes this reachable from your phone/iPad on the
# same Wi-Fi (127.0.0.1 / "localhost" only accepts connections from this
# same computer). See the setup notes shared alongside this file for the
# exact steps (find your PC's LAN IP, open the port on the firewall, etc).
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
