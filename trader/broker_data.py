#!/usr/bin/env python3
"""Broker market data adapter for real-time NSE candles and quotes (Fyers & Upstox).

Data access only (read-only):
- Candle endpoints and quote endpoints only.
- No orders, positions, funds, or mutations.
- Credentials MUST come from environment variables, never hardcoded.
  - Fyers: FYERS_APP_ID, FYERS_ACCESS_TOKEN
  - Upstox: UPSTOX_ACCESS_TOKEN
- If credentials are not present or if an endpoint fails, falls back to Yahoo Finance.
- When the broker call fails or is skipped, prints one line saying why before falling back.
- Never prints tokens or any part of credentials.
"""

import datetime
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_PAUSE = 0.3    # seconds between broker calls to respect rate limits
DEFAULT_TIMEOUT = 5    # seconds timeout per request
UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/Skill-Trade)"

UPSTOX_INSTRUMENTS_CACHE = None
UPSTOX_MAP_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "upstox_instruments.json")

# Built-in fallback mappings for standard liquid NSE stocks
COMMON_UPSTOX_INSTRUMENTS = {
    "RELIANCE": "NSE_EQ|INE002A01018",
    "TCS": "NSE_EQ|INE467B01029",
    "INFY": "NSE_EQ|INE009A01021",
    "HDFCBANK": "NSE_EQ|INE040A01034",
    "ICICIBANK": "NSE_EQ|INE090A01021",
    "SBIN": "NSE_EQ|INE062A01020",
    "BHARTIARTL": "NSE_EQ|INE397D01024",
    "ITC": "NSE_EQ|INE154A01025",
    "TATAMOTORS": "NSE_EQ|INE155A01022",
    "KOTAKBANK": "NSE_EQ|INE237A01028",
    "LT": "NSE_EQ|INE018A01030",
    "AXISBANK": "NSE_EQ|INE238A01034",
    "BAJFINANCE": "NSE_EQ|INE296A01024",
    "MARUTI": "NSE_EQ|INE585B01010",
    "SUNPHARMA": "NSE_EQ|INE044A01036",
    "TITAN": "NSE_EQ|INE280A01028",
    "ULTRACEMCO": "NSE_EQ|INE481G01011",
    "WIPRO": "NSE_EQ|INE075A01022",
    "NTPC": "NSE_EQ|INE733E01010",
    "ONGC": "NSE_EQ|INE213A01029",
}

__all__ = [
    "active_broker",
    "get_realtime_quotes",
    "get_intraday_candles",
    "fetch_upstox_quotes",
    "fetch_upstox_candles",
    "fetch_fyers_quotes",
    "fetch_fyers_candles",
    "fetch_market_overview_quotes",
    "load_upstox_instruments",
]



def _load_env_file():
    """Load environment variables from a local .env file if not already set."""
    candidates = [
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".env")),
        os.path.abspath(os.path.join(os.path.dirname(__file__), ".env")),
        os.path.abspath(".env"),
    ]
    for p in candidates:
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            k, v = k.strip(), v.strip().strip("'\"")
                            if k and v and k not in os.environ:
                                os.environ[k] = v
                break
            except Exception:
                pass


_load_env_file()


def active_broker():
    """Detect which broker credentials are set in environment variables.
    Returns 'fyers', 'upstox', or None. Never returns or logs token values.
    """
    if os.environ.get("FYERS_ACCESS_TOKEN"):
        return "fyers"
    if os.environ.get("UPSTOX_ACCESS_TOKEN"):
        return "upstox"
    return None


def _sanitize(text):
    """Ensure no credentials or tokens ever appear in output logs."""
    if not text:
        return ""
    s = str(text)
    for env_k in ("FYERS_ACCESS_TOKEN", "UPSTOX_ACCESS_TOKEN", "FYERS_APP_ID"):
        val = os.environ.get(env_k)
        if val and len(val) >= 4 and val in s:
            s = s.replace(val, "[REDACTED]")
    return s


def _log_fallback(reason, sym=None, syms=None, broker=None):
    """Print one line explaining why the broker call failed or was skipped before fallback.
    Never prints or reveals credentials or tokens.
    """
    b_name = (broker or active_broker() or "broker").capitalize()
    target = ""
    if sym:
        target = " for %s" % sym
    elif syms:
        target = " for %s" % (syms[0] if len(syms) == 1 else "%d symbols" % len(syms))
    
    clean_reason = _sanitize(reason)
    is_failure = any(k in clean_reason.lower() for k in ("http", "expired", "error", "failed", "timeout", "exception"))
    action = "call failed" if is_failure else "skipped"
    print("[broker] %s %s%s: %s; falling back to Yahoo" % (b_name, action, target, clean_reason))


def format_bar_age(timestamp_sec, now_utc=None, source="yahoo"):
    """Format human-readable age of the latest candle bar.
    Example: 'source: fyers, bar age: 2m' or 'source: yahoo, bar age: 16m (delayed)'
    """
    if not timestamp_sec:
        return "source: %s, bar age: unknown" % source
    if now_utc is None:
        now_sec = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    elif isinstance(now_utc, (int, float)):
        now_sec = int(now_utc)
    else:
        now_sec = int(now_utc.replace(tzinfo=datetime.timezone.utc).timestamp()) if now_utc.tzinfo else int(now_utc.timestamp())

    diff = max(0, now_sec - int(timestamp_sec))
    if diff < 60:
        age_str = "%ds" % diff
    elif diff < 3600:
        age_str = "%dm" % (diff // 60)
    else:
        age_str = "%dh %dm" % (diff // 3600, (diff % 3600) // 60)

    delayed_tag = " (delayed)" if (source == "yahoo" or diff >= 900) else ""
    return "source: %s, bar age: %s%s" % (source, age_str, delayed_tag)


def _to_fyers_symbol(sym):
    s = sym.upper().strip()
    if s.endswith(".NS"):
        s = s[:-3]
    elif s.endswith(".BO"):
        return "BSE:%s-EQ" % s[:-3]
    return "NSE:%s-EQ" % s


def load_upstox_instruments():
    """Load symbol-to-instrument_key dictionary from cache or local file."""
    global UPSTOX_INSTRUMENTS_CACHE
    if UPSTOX_INSTRUMENTS_CACHE is not None:
        return UPSTOX_INSTRUMENTS_CACHE

    mapping = dict(COMMON_UPSTOX_INSTRUMENTS)
    if os.path.exists(UPSTOX_MAP_PATH):
        try:
            with open(UPSTOX_MAP_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                mapping.update(data)
        except Exception:
            pass
    UPSTOX_INSTRUMENTS_CACHE = mapping
    return mapping


def _to_upstox_instrument(sym):
    """Convert ticker symbol (e.g. 'RELIANCE.NS') to official Upstox instrument key (e.g. 'NSE_EQ|INE002A01018').
    Returns instrument key string, or None if the symbol cannot be mapped.
    """
    if not sym:
        return None
    s = str(sym).strip().upper()
    # Already formatted as an instrument key (e.g. NSE_EQ|INE002A01018)
    if "|" in s:
        return s

    raw_sym = s
    if raw_sym.endswith(".NS"):
        raw_sym = raw_sym[:-3]
    elif raw_sym.endswith(".BO"):
        raw_sym = raw_sym[:-3]

    mapping = load_upstox_instruments()
    if raw_sym in mapping:
        return mapping[raw_sym]

    # Check if raw_sym is itself an ISIN
    if raw_sym.startswith("INE") or raw_sym.startswith("INF"):
        return "NSE_EQ|%s" % raw_sym

    return None


# ---------------------------------------------------------------- Fyers

def fetch_fyers_candles(sym, interval="5m", range_from=None, range_to=None, timeout=DEFAULT_TIMEOUT):
    """Fetch real-time candles from Fyers API v3.
    Endpoint: GET https://api-t1.fyers.in/data/history
    """
    app_id = os.environ.get("FYERS_APP_ID", "")
    token = os.environ.get("FYERS_ACCESS_TOKEN", "")
    if not token:
        _log_fallback("token not set", sym=sym, broker="fyers")
        return None

    # Resolution mapping: 1m -> '1', 5m -> '5', 15m -> '15', 1d -> 'D'
    res_map = {"1m": "1", "5m": "5", "15m": "15", "1d": "D", "D": "D"}
    resolution = res_map.get(interval, "5")

    today = datetime.date.today().isoformat()
    r_from = range_from or today
    r_to = range_to or today

    fyers_sym = _to_fyers_symbol(sym)
    query = urllib.parse.urlencode({
        "symbol": fyers_sym,
        "resolution": resolution,
        "date_format": "1",
        "range_from": r_from,
        "range_to": r_to,
        "cont_flag": "1"
    })
    url = "https://api-t1.fyers.in/data/history?%s" % query
    auth_header = "%s:%s" % (app_id, token) if app_id else token
    req = urllib.request.Request(url, headers={
        "Authorization": auth_header,
        "User-Agent": UA,
        "Accept": "application/json"
    })

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        err_msg = ""
        try:
            body = e.read().decode("utf-8", "replace")
            err_json = json.loads(body)
            err_msg = err_json.get("message") or ""
        except Exception:
            pass
        if e.code == 401 or "token" in err_msg.lower() or "expired" in err_msg.lower():
            reason = "token expired (HTTP 401)"
        elif err_msg:
            reason = "HTTP %d: %s" % (e.code, err_msg)
        else:
            reason = "HTTP %d %s" % (e.code, e.reason)
        _log_fallback(reason, sym=sym, broker="fyers")
        return None
    except urllib.error.URLError as e:
        _log_fallback("connection error: %s" % e.reason, sym=sym, broker="fyers")
        return None
    except Exception as e:
        _log_fallback("error: %s" % e, sym=sym, broker="fyers")
        return None

    if not isinstance(data, dict) or data.get("s") != "ok":
        msg = data.get("message") if isinstance(data, dict) else "unsuccessful response"
        if "token" in str(msg).lower() or "expired" in str(msg).lower():
            reason = "token expired"
        elif "symbol" in str(msg).lower() or "not found" in str(msg).lower():
            reason = "symbol not found"
        else:
            reason = str(msg) if msg else "unsuccessful response"
        _log_fallback(reason, sym=sym, broker="fyers")
        return None

    raw_candles = data.get("candles") or []
    bars = []
    for c in raw_candles:
        if len(c) >= 6:
            bars.append({
                "t": int(c[0]),
                "o": float(c[1]),
                "h": float(c[2]),
                "l": float(c[3]),
                "c": float(c[4]),
                "v": int(c[5])
            })

    latest_t = bars[-1]["t"] if bars else None
    return {
        "symbol": sym.upper(),
        "meta": {
            "symbol": sym.upper(),
            "dataGranularity": interval,
            "source": "fyers",
            "regularMarketTime": latest_t
        },
        "bars": bars,
        "prev_close": None,
        "data_time": latest_t
    }


def fetch_fyers_quotes(syms, timeout=DEFAULT_TIMEOUT):
    """Fetch real-time quotes from Fyers API v3.
    Endpoint: GET https://api-t1.fyers.in/data/quotes
    """
    app_id = os.environ.get("FYERS_APP_ID", "")
    token = os.environ.get("FYERS_ACCESS_TOKEN", "")
    if not token:
        _log_fallback("token not set", syms=syms, broker="fyers")
        return None
    if not syms:
        return {}

    fyers_symbols = [_to_fyers_symbol(s) for s in syms]
    query = urllib.parse.urlencode({"symbols": ",".join(fyers_symbols)})
    url = "https://api-t1.fyers.in/data/quotes?%s" % query
    auth_header = "%s:%s" % (app_id, token) if app_id else token
    req = urllib.request.Request(url, headers={
        "Authorization": auth_header,
        "User-Agent": UA,
        "Accept": "application/json"
    })

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        err_msg = ""
        try:
            body = e.read().decode("utf-8", "replace")
            err_json = json.loads(body)
            err_msg = err_json.get("message") or ""
        except Exception:
            pass
        if e.code == 401 or "token" in err_msg.lower() or "expired" in err_msg.lower():
            reason = "token expired (HTTP 401)"
        elif err_msg:
            reason = "HTTP %d: %s" % (e.code, err_msg)
        else:
            reason = "HTTP %d %s" % (e.code, e.reason)
        _log_fallback(reason, syms=syms, broker="fyers")
        return None
    except urllib.error.URLError as e:
        _log_fallback("connection error: %s" % e.reason, syms=syms, broker="fyers")
        return None
    except Exception as e:
        _log_fallback("error: %s" % e, syms=syms, broker="fyers")
        return None

    if not isinstance(data, dict) or data.get("s") != "ok":
        msg = data.get("message") if isinstance(data, dict) else "unsuccessful response"
        reason = "token expired" if ("token" in str(msg).lower() or "expired" in str(msg).lower()) else (msg or "error")
        _log_fallback(reason, syms=syms, broker="fyers")
        return None

    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = {}
    sym_map = {fyers_symbols[i]: syms[i].upper() for i in range(len(syms))}

    for item in data.get("d") or []:
        n = item.get("n")
        orig_sym = sym_map.get(n)
        v = item.get("v") or {}
        if orig_sym and "lp" in v:
            lp = float(v["lp"])
            prev = float(v.get("prev_close_price") or lp)
            tt = v.get("tt")
            t_str = datetime.datetime.fromtimestamp(tt, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if tt else now_iso
            vol = int(v.get("volume") or 0)
            chg_pct = round((lp - prev) / prev * 100, 2) if prev else 0.0
            out[orig_sym] = {
                "symbol": orig_sym,
                "price": lp,
                "prev_close": prev,
                "open": float(v.get("open_price") or lp),
                "high": float(v.get("high_price") or lp),
                "low": float(v.get("low_price") or lp),
                "volume": vol,
                "change_pct": chg_pct,
                "net_change": round(lp - prev, 2),
                "time": t_str,
                "source": "fyers",
                "timestamp": tt
            }
    return out


# ---------------------------------------------------------------- Upstox

def fetch_upstox_candles(sym, interval="5m", timeout=DEFAULT_TIMEOUT):
    """Fetch real-time intraday candles from Upstox API v3/v2.
    Endpoint: GET https://api.upstox.com/v3/historical-candle/intraday/{instrument_key}/minutes/{interval}
    """
    token = os.environ.get("UPSTOX_ACCESS_TOKEN", "")
    if not token:
        _log_fallback("token not set", sym=sym, broker="upstox")
        return None

    inst = _to_upstox_instrument(sym)
    if not inst:
        _log_fallback("symbol not found", sym=sym, broker="upstox")
        return None

    minute_val = "1" if interval == "1m" else "5"
    url = "https://api.upstox.com/v3/historical-candle/intraday/%s/minutes/%s" % (
        urllib.parse.quote(inst), minute_val
    )
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer %s" % token,
        "User-Agent": UA,
        "Accept": "application/json"
    })

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        err_msg = ""
        try:
            body = e.read().decode("utf-8", "replace")
            err_json = json.loads(body)
            errors = err_json.get("errors") or []
            if errors and isinstance(errors[0], dict):
                err_msg = errors[0].get("message") or ""
        except Exception:
            pass
        if e.code == 401 or "expired" in err_msg.lower() or "token" in err_msg.lower():
            reason = "token expired (HTTP 401)"
        elif "symbol" in err_msg.lower() or "instrument" in err_msg.lower() or e.code == 404:
            reason = "symbol not found"
        elif err_msg:
            reason = "HTTP %d: %s" % (e.code, err_msg)
        else:
            reason = "HTTP %d %s" % (e.code, e.reason)
        _log_fallback(reason, sym=sym, broker="upstox")
        return None
    except urllib.error.URLError as e:
        _log_fallback("connection error: %s" % e.reason, sym=sym, broker="upstox")
        return None
    except Exception as e:
        _log_fallback("error: %s" % e, sym=sym, broker="upstox")
        return None

    if not isinstance(data, dict) or data.get("status") != "success":
        errors = (data.get("errors") or []) if isinstance(data, dict) else []
        msg = errors[0].get("message") if (errors and isinstance(errors[0], dict)) else "unsuccessful response"
        if "expired" in msg.lower() or "token" in msg.lower():
            reason = "token expired"
        elif "symbol" in msg.lower() or "instrument" in msg.lower():
            reason = "symbol not found"
        else:
            reason = msg
        _log_fallback(reason, sym=sym, broker="upstox")
        return None

    candles_data = (data.get("data") or {}).get("candles") or []
    if not candles_data:
        _log_fallback("no candles returned", sym=sym, broker="upstox")
        return None

    bars = []
    for c in reversed(candles_data):  # Upstox returns newest first; reverse for chronological
        if len(c) >= 6:
            try:
                dt = datetime.datetime.fromisoformat(c[0])
                ts = int(dt.timestamp())
            except Exception:
                continue
            bars.append({
                "t": ts,
                "o": float(c[1]),
                "h": float(c[2]),
                "l": float(c[3]),
                "c": float(c[4]),
                "v": int(c[5])
            })

    latest_t = bars[-1]["t"] if bars else None
    return {
        "symbol": sym.upper(),
        "meta": {
            "symbol": sym.upper(),
            "dataGranularity": interval,
            "source": "upstox",
            "regularMarketTime": latest_t
        },
        "bars": bars,
        "prev_close": None,
        "data_time": latest_t
    }


def fetch_upstox_quotes(syms, timeout=DEFAULT_TIMEOUT):
    """Fetch real-time quotes from Upstox API v2.
    Endpoint: GET https://api.upstox.com/v2/market-quote/quotes
    """
    token = os.environ.get("UPSTOX_ACCESS_TOKEN", "")
    if not token:
        _log_fallback("token not set", syms=syms, broker="upstox")
        return None
    if not syms:
        return {}

    instruments = []
    inst_map = {}
    for s in syms:
        inst = _to_upstox_instrument(s)
        if not inst:
            _log_fallback("symbol not found", sym=s, broker="upstox")
        else:
            instruments.append(inst)
            inst_map[inst] = s.upper()

    if not instruments:
        return None

    query = urllib.parse.urlencode({"instrument_key": ",".join(instruments)})
    url = "https://api.upstox.com/v2/market-quote/quotes?%s" % query
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer %s" % token,
        "User-Agent": UA,
        "Accept": "application/json"
    })

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        err_msg = ""
        try:
            body = e.read().decode("utf-8", "replace")
            err_json = json.loads(body)
            errors = err_json.get("errors") or []
            if errors and isinstance(errors[0], dict):
                err_msg = errors[0].get("message") or ""
        except Exception:
            pass
        if e.code == 401 or "expired" in err_msg.lower() or "token" in err_msg.lower():
            reason = "token expired (HTTP 401)"
        elif "symbol" in err_msg.lower() or "instrument" in err_msg.lower() or e.code == 404:
            reason = "symbol not found"
        elif err_msg:
            reason = "HTTP %d: %s" % (e.code, err_msg)
        else:
            reason = "HTTP %d %s" % (e.code, e.reason)
        _log_fallback(reason, syms=syms, broker="upstox")
        return None
    except urllib.error.URLError as e:
        _log_fallback("connection error: %s" % e.reason, syms=syms, broker="upstox")
        return None
    except Exception as e:
        _log_fallback("error: %s" % e, syms=syms, broker="upstox")
        return None

    if not isinstance(data, dict) or data.get("status") != "success":
        errors = (data.get("errors") or []) if isinstance(data, dict) else []
        msg = errors[0].get("message") if (errors and isinstance(errors[0], dict)) else "unsuccessful response"
        reason = "token expired" if ("expired" in msg.lower() or "token" in msg.lower()) else msg
        _log_fallback(reason, syms=syms, broker="upstox")
        return None

    res_data = data.get("data") or {}
    out = {}

    for key, v in res_data.items():
        norm_key = key.replace(":", "|")
        orig_sym = inst_map.get(key) or inst_map.get(norm_key)
        if not orig_sym:
            for inst_k, sym_v in inst_map.items():
                if norm_key in inst_k or inst_k in norm_key:
                    orig_sym = sym_v
                    break
        if orig_sym and isinstance(v, dict):
            lp = float(v.get("last_price") or 0.0)
            ohlc = v.get("ohlc") or {}
            prev = float(ohlc.get("close") or lp)
            ts_str = v.get("last_trade_time") or ""
            vol = int(v.get("volume") or 0)
            chg_pct = round((lp - prev) / prev * 100, 2) if prev else 0.0
            clean_s = orig_sym.replace(".NS", "")
            q_obj = {
                "symbol": clean_s,
                "price": lp,
                "prev_close": prev,
                "open": float(ohlc.get("open") or lp),
                "high": float(ohlc.get("high") or lp),
                "low": float(ohlc.get("low") or lp),
                "volume": vol,
                "change_pct": chg_pct,
                "net_change": round(lp - prev, 2),
                "time": ts_str,
                "source": "upstox"
            }
            out[clean_s] = q_obj
            out[orig_sym] = q_obj
    return out


# ---------------------------------------------------------------- Dispatcher

def get_realtime_candles(sym, interval="5m", range_from=None, range_to=None, timeout=DEFAULT_TIMEOUT):
    """Dispatch to Fyers or Upstox based on active environment credentials.
    Returns normalized chart dict or None if broker is unavailable.
    """
    broker = active_broker()
    if not broker:
        _log_fallback("token not set", sym=sym, broker="broker")
        return None
    if broker == "fyers":
        return fetch_fyers_candles(sym, interval, range_from, range_to, timeout=timeout)
    elif broker == "upstox":
        return fetch_upstox_candles(sym, interval, timeout=timeout)
    return None


def get_realtime_quotes(syms, timeout=DEFAULT_TIMEOUT):
    """Dispatch to Fyers or Upstox quotes based on active environment credentials.
    Returns dict of {sym: quote_dict} or None if broker is unavailable.
    """
    broker = active_broker()
    if not broker:
        _log_fallback("token not set", syms=syms, broker="broker")
        return None
    if broker == "fyers":
        return fetch_fyers_quotes(syms, timeout=timeout)
    elif broker == "upstox":
        return fetch_upstox_quotes(syms, timeout=timeout)
    return None


def candles_to_series(chart):
    """Convert normalized chart dict with bars list to parallel column lists series."""
    if not chart or not chart.get("bars"):
        return None
    bars = chart["bars"]
    return {
        "t": [b["t"] for b in bars],
        "o": [b["o"] for b in bars],
        "h": [b["h"] for b in bars],
        "l": [b["l"] for b in bars],
        "c": [b["c"] for b in bars],
        "v": [b["v"] for b in bars],
        "meta": chart.get("meta", {})
    }


def _generate_sample_quotes(symbols):
    """Generate deterministic, realistic sample market quotes for offline testing."""
    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sample_base = {
        "RELIANCE": (1265.50, 1250.00, 8450000),
        "TCS": (4180.00, 4120.00, 2100000),
        "HDFCBANK": (1735.00, 1720.00, 9200000),
        "INFY": (1875.00, 1850.00, 4300000),
        "SBIN": (840.50, 832.00, 11500000),
        "BHARTIARTL": (1610.00, 1595.00, 3800000),
        "ITC": (475.00, 478.50, 6200000),
        "TATAMOTORS": (892.00, 880.25, 5900000),
        "SUNPHARMA": (1780.00, 1795.00, 1800000),
        "TITAN": (3350.00, 3310.00, 1400000),
        "LT": (3620.00, 3590.00, 1950000),
        "MARUTI": (12400.00, 12250.00, 750000),
        "DLF": (865.00, 852.00, 3100000),
        "TATASTEEL": (152.00, 154.50, 14000000),
        "CIPLA": (1540.00, 1555.00, 1600000),
        "NTPC": (392.00, 388.00, 7800000),
    }
    out = {}
    for s in symbols:
        sym_clean = s.replace(".NS", "").upper()
        if sym_clean in sample_base:
            lp, prev, vol = sample_base[sym_clean]
        else:
            h = sum(ord(c) for c in sym_clean)
            base_p = 500.0 + (h % 2000)
            chg = ((h % 11) - 5) * 0.65
            prev = round(base_p, 2)
            lp = round(prev * (1.0 + chg / 100.0), 2)
            vol = 1000000 + (h % 50) * 100000

        chg_pct = round((lp - prev) / prev * 100, 2) if prev else 0.0
        out[sym_clean] = {
            "symbol": sym_clean,
            "price": lp,
            "prev_close": prev,
            "open": round(prev * 1.002, 2),
            "high": round(max(lp, prev) * 1.008, 2),
            "low": round(min(lp, prev) * 0.994, 2),
            "volume": vol,
            "change_pct": chg_pct,
            "net_change": round(lp - prev, 2),
            "time": now_iso,
            "source": "sample"
        }
    return out


def fetch_market_overview_quotes(symbols, is_sample_mode=False):
    """Fetch live or sample quotes for a batch of symbols with clear provenance.

    - In sample mode (is_sample_mode=True): returns deterministic sample fixture quotes.
    - In live mode (is_sample_mode=False): queries Upstox API v2 using official instrument keys,
      or Fyers if configured, falling back cleanly to Yahoo Finance if broker credentials are not set.
      Never silently substitutes sample data when live fetching fails.

    Returns:
        dict: {symbol: quote_dict}
    """
    if symbols is None:
        mc_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "market_caps.json")
        if os.path.exists(mc_path):
            try:
                with open(mc_path, "r", encoding="utf-8") as fh:
                    d = json.load(fh)
                    symbols = list(d.get("stocks", {}).keys())
            except Exception:
                symbols = list(COMMON_UPSTOX_INSTRUMENTS.keys())
        else:
            symbols = list(COMMON_UPSTOX_INSTRUMENTS.keys())

    # 1. Sample Mode: explicitly requested by user or test fixture
    if is_sample_mode:
        return _generate_sample_quotes(symbols)

    # 2. Live Mode: query real broker API (Upstox / Fyers)
    broker = active_broker()
    quotes = {}
    if broker:
        # Batch in chunks of 100 symbols (strictly within Upstox batch limit of 500)
        chunk_size = 100
        for i in range(0, len(symbols), chunk_size):
            chunk = symbols[i:i + chunk_size]
            b_quotes = get_realtime_quotes(chunk, timeout=DEFAULT_TIMEOUT)
            if b_quotes:
                quotes.update(b_quotes)

    # For symbols missing from broker or if broker credentials are not set, query Yahoo fallback
    missing = [s for s in symbols if s not in quotes and f"{s}.NS" not in quotes]
    if missing and not broker:
        alerts = None
        try:
            import importlib.util
            p = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".agents", "skills", "trader-alerts", "alerts.py"))
            if not os.path.exists(p):
                p = os.path.abspath(os.path.join(os.path.dirname(__file__), "alerts.py"))
            if os.path.exists(p):
                spec = importlib.util.spec_from_file_location("trader_alerts_mod", p)
                alerts_mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(alerts_mod)
                alerts = alerts_mod
        except Exception:
            alerts = None

        if alerts:
            try:
                y_quotes, _ = alerts.get_quotes(missing[:60], source="yahoo", pause=0.05)
                for s, yq in y_quotes.items():
                    s_clean = s.replace(".NS", "").upper()
                    lp = float(yq.get("price") or 0.0)
                    prev = float(yq.get("prev_close") or lp)
                    chg_pct = round((lp - prev) / prev * 100, 2) if prev else 0.0
                    q_obj = {
                        "symbol": s_clean,
                        "price": lp,
                        "prev_close": prev,
                        "open": yq.get("open"),
                        "high": yq.get("high"),
                        "low": yq.get("low"),
                        "volume": 0,
                        "change_pct": chg_pct,
                        "net_change": round(lp - prev, 2),
                        "time": yq.get("time") or "",
                        "source": "yahoo"
                    }
                    quotes[s_clean] = q_obj
                    quotes[f"{s_clean}.NS"] = q_obj
            except Exception as e:
                _log_fallback("Yahoo fallback error: %s" % e, syms=missing[:60], broker="yahoo")

    # In Live Mode, return strictly what was legitimately retrieved (never silently fake sample data)
    return quotes

