from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import yfinance as yf
import pandas as pd
import datetime
import pyotp
import requests
from SmartApi import SmartConnect
import uvicorn
import logging
import os

logging.basicConfig(level=logging.INFO)

app = FastAPI()
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"]
)

# =================================================================
# 🔐 ANGEL ONE CREDENTIALS (PULLED FROM RAILWAY VARIABLES)
# =================================================================
ANGEL_API_KEY = os.environ.get("R1rnnYfT", "")
ANGEL_CLIENT_ID = os.environ.get("JAIG1059", "")
ANGEL_PIN = os.environ.get("1921", "")
ANGEL_TOTP_SECRET = os.environ.get("DJ6DDBZ2HEBPWGAEEEMM5RNTIE", "")

angel_session = None
opts_df = pd.DataFrame()
market_cache = {"data": None, "last_fetch": None, "interval": "1h"}

def init_angel():
    global angel_session
    if not ANGEL_API_KEY:
        logging.warning("Angel One API Keys not found in Railway Variables. Waiting...")
        return False
        
    try:
        angel = SmartConnect(api_key=ANGEL_API_KEY)
        totp = pyotp.TOTP(ANGEL_TOTP_SECRET).now()
        data = angel.generateSession(ANGEL_CLIENT_ID, ANGEL_PIN, totp)
        if data and data.get('status'):
            angel_session = angel
            logging.info("✅ Angel One Cloud Session Authenticated!")
            return True
        else:
            logging.error(f"❌ Angel Login Failed: {data}")
    except Exception as e:
        logging.error(f"❌ Angel Login Exception: {e}")
    return False

def load_instruments():
    global opts_df
    try:
        url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
        res = requests.get(url, timeout=30)
        df = pd.DataFrame(res.json())
        nifty_opts = df[(df['name'] == 'NIFTY') & (df['exch_seg'] == 'NFO') & (df['instrumenttype'] == 'OPTIDX')].copy()
        nifty_opts['expiry_dt'] = pd.to_datetime(nifty_opts['expiry'])
        nifty_opts['strike_price'] = (nifty_opts['strike'].astype(float) / 100).astype(int)
        nifty_opts = nifty_opts[nifty_opts['expiry_dt'] >= pd.Timestamp.now().normalize()]
        opts_df = nifty_opts.sort_values('expiry_dt')
        logging.info("✅ Options chain downloaded successfully.")
    except Exception as e:
        logging.error(f"Failed to load instruments: {e}")

@app.on_event("startup")
def startup_event():
    init_angel()
    load_instruments()

# ==========================================
# MARKET DATA LOGIC
# ==========================================
def fetch_nifty_market(interval: str):
    now = datetime.datetime.now()
    if market_cache["data"] is not None and market_cache["last_fetch"] is not None and market_cache["interval"] == interval:
        if (now - market_cache["last_fetch"]).total_seconds() < 60:
            return market_cache["data"]

    period = "60d"
    if interval in ["1m", "2m", "5m"]: period = "7d"
    
    df = yf.download("^NSEI", period=period, interval=interval, progress=False)
    if df.empty: return None
    
    if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
    df.columns = [str(c) for c in df.columns]
    
    df = df[~df.index.duplicated(keep='last')].sort_index()
    
    df['SMA_20'] = df['Close'].rolling(window=20).mean()
    delta = df['Close'].diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    avg_gain = gain.ewm(alpha=1/14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/14, adjust=False).mean()
    rs = avg_gain / avg_loss
    df['RSI_14'] = 100 - (100 / (1 + rs))
    df['RSI_SIGNAL'] = df['RSI_14'].ewm(span=14, adjust=False).mean()
    
    df = df.dropna()
    market_cache["data"] = df
    market_cache["last_fetch"] = now
    market_cache["interval"] = interval
    return df

@app.get("/api/market")
def get_market_data(interval: str = "1h"):
    global angel_session
    
    if not angel_session:
        init_angel()
        
    df = fetch_nifty_market(interval)
    if df is None or df.empty:
        raise HTTPException(status_code=500, detail="Failed to fetch market data")
    
    latest = df.iloc[-2]
    current = df.iloc[-1]
    
    close = float(latest['Close'])
    sma20 = float(latest['SMA_20'])
    rsi = float(latest['RSI_14'])
    rsi_sig = float(latest['RSI_SIGNAL'])
    
    spot = float(current['Close'])
    is_live = False
    
    if angel_session:
        try:
            tick = angel_session.ltpData("NSE", "Nifty 50", "26000")
            if tick and isinstance(tick, dict) and tick.get('status'):
                spot = float(tick['data']['ltp'])
                is_live = True
        except Exception: pass

    chart_data = []
    sma_data = []
    for ts, row in df.iterrows():
        t_stamp = int(pd.Timestamp(ts).timestamp()) + 19800 
        chart_data.append({"time": t_stamp, "open": float(row['Open']), "high": float(row['High']), "low": float(row['Low']), "close": float(row['Close'])})
        sma_data.append({"time": t_stamp, "value": float(row['SMA_20'])})
        
    if is_live and len(chart_data) > 0:
        chart_data[-1]['close'] = spot
        if spot > chart_data[-1]['high']: chart_data[-1]['high'] = spot
        if spot < chart_data[-1]['low']: chart_data[-1]['low'] = spot

    signal = "NEUTRAL (WAIT)"
    if rsi > rsi_sig and spot > sma20: signal = "🟢 ENTER BULL PUT"
    elif rsi < rsi_sig and spot < sma20: signal = "🔴 ENTER BEAR CALL"

    return {
        "spot": spot, "is_live": is_live,
        "indicators": {"sma20": sma20, "rsi": rsi, "rsi_sig": rsi_sig},
        "algo": {"signal": signal, "details": "Real-time Algo Evaluation"},
        "chart_data": chart_data, "sma_data": sma_data
    }

@app.get("/api/options_chain")
def get_options_chain():
    global opts_df
    
    if opts_df.empty: 
        load_instruments()
        
    if opts_df.empty: 
        return {"expiries": [], "strikes": []}
        
    return {"expiries": opts_df['expiry'].unique().tolist(), "strikes": sorted(opts_df['strike_price'].unique().tolist())}

class PriceRequest(BaseModel):
    expiry: str
    legs: list

@app.post("/api/live_prices")
def get_live_prices(req: PriceRequest):
    global angel_session, opts_df
    
    # 🔴 THE ROOT CAUSE FIX: Re-hydrate memory if a new Railway worker thread takes the request!
    if not angel_session:
        init_angel()
    if opts_df.empty:
        load_instruments()

    results = {}
    
    if opts_df.empty or not angel_session:
        return {"prices": {f"{l.get('strike')}_{l.get('type')}": 0.0 for l in req.legs}}
        
    exp_df = opts_df[opts_df['expiry'] == req.expiry]
    
    tokens_map = {} 
    nfo_tokens = []
    
    for leg in req.legs:
        key = f"{leg.get('strike')}_{leg.get('type')}"
        try:
            target_strike = int(leg['strike'])
            target_type = str(leg['type']).strip().upper()
            
            matches = exp_df[(exp_df['strike_price'] == target_strike) & (exp_df['symbol'].str.endswith(target_type))]
            
            if matches.empty:
                results[key] = 0.0
                continue
                
            row = matches.iloc[0]
            tok = str(row['token'])
            
            tokens_map[tok] = key
            nfo_tokens.append(tok)
            
        except Exception as e:
            results[key] = 0.0
            
    # 🔴 THE BATCH FETCH FIX: Fetch up to 50 options in 1 request to avoid Rate Limit crashes
    if nfo_tokens and angel_session:
        try:
            res = angel_session.getMarketData("LTP", {"NFO": nfo_tokens})
            
            if res and isinstance(res, dict) and res.get('status'):
                fetched_data = res.get('data', {}).get('fetched', [])
                for item in fetched_data:
                    tok = item.get('symbolToken')
                    ltp = float(item.get('ltp', 0.0))
                    if tok in tokens_map:
                        results[tokens_map[tok]] = ltp
            else:
                # Fallback to single fetches if the batch fails for any reason
                for tok in nfo_tokens:
                    sym = exp_df[exp_df['token'] == tok].iloc[0]['symbol']
                    tick = angel_session.ltpData("NFO", sym, tok)
                    if tick and isinstance(tick, dict) and tick.get('status'):
                        results[tokens_map[tok]] = float(tick['data']['ltp'])
                    else:
                        results[tokens_map[tok]] = 0.0
                        
        except Exception as e:
            logging.error(f"Live Price Batch Fetch Error: {e}")
            for tok in nfo_tokens:
                results[tokens_map[tok]] = 0.0
                
    return {"prices": results}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
