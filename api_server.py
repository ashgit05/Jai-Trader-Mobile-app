from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import yfinance as yf
import pandas as pd
import datetime
import pyotp
import requests
import math
from SmartApi import SmartConnect
import uvicorn
import logging
import os
import json
import time
import uuid
import hashlib
import jwt  # NEW: Cryptography Library

logging.basicConfig(level=logging.INFO)

app = FastAPI()
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"]
)

# =================================================================
# 🔴 CRITICAL: THE SECRET KEY 🔴
# This must perfectly match the key in your license_master.py
# =================================================================
SECRET_KEY = "NIFTY_PRO_SUPER_SECRET_KEY_2026_!@#$"

# ==========================================
# COMMERCIAL DATA STORAGE & HARDWARE LOCK
# ==========================================
CONFIG_FILE = "system_config.json"
DATA_FILE = "user_data.json"

raw_hwid = str(uuid.getnode()).encode()
HWID = hashlib.sha256(raw_hwid).hexdigest()[:12].upper()

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f: return json.load(f)
    return {"is_licensed": False, "license_key": None, "angel_creds": None}

def save_config(data):
    with open(CONFIG_FILE, "w") as f: json.dump(data, f)

def load_user_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f: return json.load(f)
    return {"balance": 500000, "active_trade": {"status": "NONE", "strategy": "", "legs": [], "entryTime": ""}, "trade_history": []}

def save_user_data(data):
    with open(DATA_FILE, "w") as f: json.dump(data, f)

config = load_config()
angel_session = None
opts_df = pd.DataFrame()
market_cache = {"data": None, "last_fetch": None, "interval": "1h"}

# ==========================================
# ANGEL ONE INITIALIZATION
# ==========================================
def init_angel():
    global angel_session, config
    creds = config.get("angel_creds")
    if not creds: return False
    
    try:
        angel = SmartConnect(api_key=creds["api_key"])
        totp = pyotp.TOTP(creds["totp_secret"]).now()
        data = angel.generateSession(creds["client_id"], creds["pin"], totp)
        if data and data.get('status'):
            angel_session = angel
            logging.info("Angel One Commercial Session Authenticated!")
            return True
    except Exception as e:
        logging.error(f"Angel Login Failed: {e}")
    return False

def load_instruments():
    global opts_df
    try:
        scrip_file = "scrip_master_cache.json"
        data = None
        if os.path.exists(scrip_file) and (time.time() - os.path.getmtime(scrip_file)) < 86400:
            with open(scrip_file, 'r') as f: data = json.load(f)
        else:
            url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
            res = requests.get(url, timeout=30)
            data = res.json()
            with open(scrip_file, 'w') as f: json.dump(data, f)
        
        df = pd.DataFrame(data)
        nifty_opts = df[(df['name'] == 'NIFTY') & (df['exch_seg'] == 'NFO') & (df['instrumenttype'] == 'OPTIDX')].copy()
        nifty_opts['expiry_dt'] = pd.to_datetime(nifty_opts['expiry'])
        nifty_opts['strike_price'] = (nifty_opts['strike'].astype(float) / 100).astype(int)
        nifty_opts = nifty_opts[nifty_opts['expiry_dt'] >= pd.Timestamp.now().normalize()]
        opts_df = nifty_opts.sort_values('expiry_dt')
    except Exception as e:
        logging.error(f"Failed to load instruments: {e}")

def verify_active_license():
    """Silently verifies the stored license on every startup."""
    global config
    if not config.get("license_key"):
        config["is_licensed"] = False
        return False
        
    try:
        # Decode checks the signature AND automatically checks if 'exp' (expiration) has passed
        payload = jwt.decode(config["license_key"], SECRET_KEY, algorithms=["HS256"])
        if payload["hwid"] != HWID:
            config["is_licensed"] = False
            return False
        
        # If we reach here, signature is valid, HWID matches, and it hasn't expired!
        config["is_licensed"] = True
        return True
    except jwt.ExpiredSignatureError:
        logging.error("License has expired!")
        config["is_licensed"] = False
        return False
    except jwt.InvalidTokenError:
        logging.error("License signature is invalid or tampered with!")
        config["is_licensed"] = False
        return False

@app.on_event("startup")
def startup_event():
    # Verify the license every time the server boots up
    is_valid = verify_active_license()
    save_config(config) # Save the state in case it expired
    
    if is_valid and config["angel_creds"]:
        init_angel()
    load_instruments()

# ==========================================
# SYSTEM SETUP & LICENSE ENDPOINTS
# ==========================================
@app.get("/api/system_status")
def get_system_status():
    # Force a silent check every time the React UI asks for status
    verify_active_license() 
    return {
        "hwid": HWID,
        "is_licensed": config["is_licensed"],
        "is_setup": config["angel_creds"] is not None
    }

class LicenseReq(BaseModel):
    key: str

@app.post("/api/activate_license")
def activate_license(req: LicenseReq):
    global config
    try:
        # Attempt to decode the provided key
        payload = jwt.decode(req.key.strip(), SECRET_KEY, algorithms=["HS256"])
        
        # Check Hardware Match
        if payload["hwid"] != HWID:
            return {"success": False, "message": "This license key is locked to a different computer."}
            
        # If it passes, it's valid and not expired
        config["is_licensed"] = True
        config["license_key"] = req.key.strip()
        save_config(config)
        return {"success": True}
        
    except jwt.ExpiredSignatureError:
        return {"success": False, "message": "This license key has expired."}
    except jwt.InvalidTokenError:
        return {"success": False, "message": "Invalid or tampered license key."}
    except Exception as e:
        return {"success": False, "message": str(e)}

# ... existing code for API Endpoints (Creds, Market, Options) ...
class CredsReq(BaseModel):
    api_key: str
    client_id: str
    pin: str
    totp_secret: str

@app.post("/api/setup_credentials")
def setup_credentials(req: CredsReq):
    global config
    config["angel_creds"] = req.dict()
    save_config(config)
    success = init_angel()
    if not success:
        # Clear them if invalid so they can try again
        config["angel_creds"] = None
        save_config(config)
        return {"success": False, "message": "Invalid Angel One Credentials. Check your TOTP or API key."}
    return {"success": True}

# ==========================================
# LOCAL DATABASE ENDPOINTS (Replacing LocalStorage)
# ==========================================
@app.get("/api/user_data")
def get_user_data():
    return load_user_data()

class UserDataReq(BaseModel):
    balance: float
    active_trade: dict
    trade_history: list

@app.post("/api/user_data")
def update_user_data(req: UserDataReq):
    save_user_data(req.dict())
    return {"success": True}

# ==========================================
# MARKET DATA LOGIC (Multi-Timeframe)
# ==========================================
def fetch_nifty_market(interval: str):
    now = datetime.datetime.now()
    if market_cache["data"] is not None and market_cache["last_fetch"] is not None and market_cache["interval"] == interval:
        if (now - market_cache["last_fetch"]).total_seconds() < 60:
            return market_cache["data"]

    # Yahoo Finance timeframe limitations
    period = "60d"
    if interval in ["1m", "2m"]: period = "7d"
    
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
    if opts_df.empty: return {"expiries": [], "strikes": []}
    return {"expiries": opts_df['expiry'].unique().tolist(), "strikes": sorted(opts_df['strike_price'].unique().tolist())}

class PriceRequest(BaseModel):
    expiry: str
    legs: list

@app.post("/api/live_prices")
def get_live_prices(req: PriceRequest):
    if opts_df.empty or not angel_session:
        return {"prices": {f"{l['strike']}_{l['type']}": 0.0 for l in req.legs}}
        
    exp_df = opts_df[opts_df['expiry'] == req.expiry]
    results = {}
    
    for leg in req.legs:
        key = f"{leg['strike']}_{leg['type']}"
        try:
            row = exp_df[(exp_df['strike_price'] == leg['strike']) & (exp_df['symbol'].str.endswith(leg['type']))].iloc[0]
            tick = angel_session.ltpData("NFO", row['symbol'], row['token'])
            if tick and isinstance(tick, dict) and tick.get('status'):
                results[key] = float(tick['data']['ltp'])
            else: results[key] = 0.0
        except: results[key] = 0.0
            
    return {"prices": results}

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)