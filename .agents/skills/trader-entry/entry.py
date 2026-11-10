#!/usr/bin/env python3
"""Entry Timer for trader-entry: watches a setup bar by bar and tells you
the moment to get in. Standard library only. Long side only. It never buys
anything.

    python3 entry.py check RELIANCE.NS                # 15-minute opening range, 5-minute bars
    python3 entry.py check RELIANCE.NS --write        # also save trader/entry.json
    python3 entry.py check RELIANCE.NS --setup level --level 1210.00
    python3 entry.py watch RELIANCE.NS --until 15:00  # one line per closed bar
    python3 entry.py watchlist                        # hits in trader/watchlist.json
"""

import argparse
import datetime
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/Jakeschincariol/day-trader-skill)"
PAUSE = 0.5      # seconds between requests to the free endpoint
BACKOFF = 5.0    # seconds before the one retry after an HTTP 429
GRACE = 2        # seconds after a bar closes before watch asks for it
SETTLE = 60      # seconds a closed bar may wait for Yahoo's data to catch up
HOSTS = ("query2", "query1")
INTERVALS = {"1m": 60, "5m": 300}
DEFAULTS = {"no_entries_before": None, "no_entries_after": None}
EPOCH = datetime.datetime(1970, 1, 1)
IST_OFFSET = datetime.timedelta(hours=5, minutes=30)

def _load_holidays():
    paths = [
        "trader/holidays.json",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "holidays.json"),
        os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "trader", "holidays.json")
    ]
    for p in paths:
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as fh:
                    doc = json.load(fh)
                    return doc.get("holidays", {}), tuple(doc.get("years", [2025, 2026, 2027]))
            except Exception:
                pass
    return {
        "2026-01-26": "Republic Day", "2026-02-17": "Mahashivratri", "2026-03-03": "Holi",
        "2026-03-20": "Id-Ul-Fitr", "2026-04-03": "Good Friday", "2026-04-14": "Dr. Ambedkar Jayanti",
        "2026-05-01": "Maharashtra Day", "2026-05-27": "Bakri Id", "2026-06-25": "Muharram",
        "2026-08-15": "Independence Day", "2026-10-02": "Mahatma Gandhi Jayanti", "2026-10-20": "Dussehra",
        "2026-11-08": "Diwali Laxmi Pujan", "2026-11-09": "Diwali Balipratipada", "2026-11-24": "Gurunanak Jayanti",
        "2026-12-25": "Christmas",
        "2027-01-26": "Republic Day", "2027-03-08": "Mahashivratri", "2027-03-22": "Holi",
        "2027-03-26": "Good Friday", "2027-04-09": "Id-Ul-Fitr", "2027-04-14": "Dr. Ambedkar Jayanti",
        "2027-05-01": "Maharashtra Day", "2027-08-15": "Independence Day", "2027-10-02": "Mahatma Gandhi Jayanti",
        "2027-10-29": "Diwali Laxmi Pujan", "2027-11-13": "Gurunanak Jayanti", "2027-12-25": "Christmas"
    }, (2025, 2026, 2027)

NSE_HOLIDAYS, CALENDAR_YEARS = _load_holidays()
NYSE_HOLIDAYS = {
    "2026-01-01": "New Year's Day",
    "2026-01-19": "Martin Luther King, Jr. Day",
    "2026-02-16": "Washington's Birthday",
    "2026-04-03": "Good Friday",
    "2026-05-25": "Memorial Day",
    "2026-06-19": "Juneteenth National Independence Day",
    "2026-07-03": "Independence Day (observed)",
    "2026-09-07": "Labor Day",
    "2026-11-26": "Thanksgiving Day",
    "2026-12-25": "Christmas Day",
    "2027-01-01": "New Year's Day",
    "2027-01-18": "Martin Luther King, Jr. Day",
    "2027-02-15": "Washington's Birthday",
    "2027-03-26": "Good Friday",
    "2027-05-31": "Memorial Day",
    "2027-06-18": "Juneteenth National Independence Day (observed)",
    "2027-07-05": "Independence Day (observed)",
    "2027-09-06": "Labor Day",
    "2027-11-25": "Thanksgiving Day",
    "2027-12-24": "Christmas Day (observed)",
}
NYSE_EARLY_CLOSE = {
    "2026-11-27": "day after Thanksgiving",
    "2026-12-24": "Christmas Eve",
    "2027-11-26": "day after Thanksgiving",
}
NSE_EARLY_CLOSE = {}
OPEN_NSE = datetime.time(9, 15)
CLOSE_NSE = datetime.time(15, 30)
OPEN_US = datetime.time(9, 30)
CLOSE_US = datetime.time(16, 0)
EARLY = datetime.time(13, 0)


class EntryError(Exception):
    pass


# ---------------------------------------------------------------- time

def _nth_sunday(year, month, n):
    d = datetime.date(year, month, 1)
    d += datetime.timedelta(days=(6 - d.weekday()) % 7)
    return d + datetime.timedelta(weeks=n - 1)


def eastern_offset(utc):
    y = utc.year
    start = datetime.datetime.combine(_nth_sunday(y, 3, 2), datetime.time(7, 0))
    end = datetime.datetime.combine(_nth_sunday(y, 11, 1), datetime.time(6, 0))
    naive = utc.replace(tzinfo=None)
    return -4 if start <= naive < end else -5


def to_eastern(utc):
    return utc.replace(tzinfo=None) + datetime.timedelta(hours=eastern_offset(utc))


def eastern_to_utc(local):
    for off in (-4, -5):
        utc = local - datetime.timedelta(hours=off)
        if eastern_offset(utc) == off:
            return utc
    return local + datetime.timedelta(hours=5)


def to_ist(utc):
    if utc.tzinfo is not None:
        utc = (utc - utc.utcoffset()).replace(tzinfo=None)
    return utc.replace(tzinfo=None) + IST_OFFSET


def ist_to_utc(local):
    if local.tzinfo is not None:
        local = (local - local.utcoffset()).replace(tzinfo=None)
    return local.replace(tzinfo=None) - IST_OFFSET


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)


def utc_of(ts):
    return EPOCH + datetime.timedelta(seconds=int(ts))


def ts_of(utc):
    return int((utc.replace(tzinfo=None) - EPOCH).total_seconds())


def iso(utc):
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_now(text):
    if not text:
        return utc_now()
    t = text.strip()
    is_et = t.upper().endswith(" ET")
    is_ist = t.upper().endswith(" IST")
    if is_et or is_ist:
        t = t[:-3].strip()
    if re.match(r"^\d{1,2}:\d{2}$", t):
        t = "%s %s" % (to_eastern(utc_now()).date().isoformat(), t.zfill(5))
    t = t.replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise EntryError('--now wants a time like "2026-10-19 10:15" (or ...Z for UTC)')
    if dt.tzinfo is None:
        return ist_to_utc(dt) if is_ist else eastern_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def parse_hhmm(text, what):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(text))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise EntryError("%s wants a time like 10:15 or 15:00, not %r" % (what, text))
    return datetime.time(int(m.group(1)), int(m.group(2)))


def short(t):
    return "%d:%02d" % (t.hour, t.minute)


def hm(t):
    return "%02d:%02d" % (t.hour, t.minute)


def ampm(t):
    h = t.hour % 12 or 12
    return "%d:%02d %s" % (h, t.minute, "AM" if t.hour < 12 else "PM")


def trading_day(d, us=False):
    if d.weekday() >= 5:
        return False, "weekend"
    hols = NYSE_HOLIDAYS if us else NSE_HOLIDAYS
    name = hols.get(d.isoformat())
    if name:
        return False, ("NYSE holiday: " if us else "Holiday: ") + name
    return True, ""


def is_us_chart(chart):
    sym = str(chart.get("symbol") or "").upper()
    if sym.endswith(".NS") or sym.endswith(".BO"):
        return False
    meta = chart.get("meta") or {}
    tz = str(meta.get("timezone") or meta.get("exchangeTimezoneName") or "").upper()
    if "EDT" in tz or "EST" in tz or "NEW_YORK" in tz or meta.get("currency") == "USD" or sym in ("TSLA", "AAPL", "KO", "DEMO"):
        return True
    return False


def session(d, us=False):
    ok, _ = trading_day(d, us=us)
    if not ok:
        return None
    if us:
        return OPEN_US, (EARLY if d.isoformat() in NYSE_EARLY_CLOSE else CLOSE_US)
    return OPEN_NSE, CLOSE_NSE


def next_open(d, us=False):
    nd = d + datetime.timedelta(days=1)
    for _ in range(15):
        s = session(nd, us)
        if s:
            return "%s %s" % (nd.isoformat(), s[0].strftime("%H:%M"))
        nd += datetime.timedelta(days=1)
    return None


# ---------------------------------------------------------------- Yahoo

def yahoo_symbol(sym):
    return sym.upper()


def chart_path(sym, rng, interval):
    s = sym.upper()
    if not (s.endswith(".NS") or s.endswith(".BO")):
        s = s.replace(".", "-")
    return "/v8/finance/chart/%s?range=%s&interval=%s" % (s, rng, interval)


def chart_range(day, today, interval):
    back = (today - day).days
    if back <= 0:
        return "1d"
    if back <= 6:
        return "5d"
    if interval == "1m":
        raise EntryError("Yahoo keeps 1-minute bars for about a week: use --interval 5m for %s" % day)
    if back <= 28:
        return "1mo"
    if back <= 58:
        return "60d"
    raise EntryError("Yahoo keeps 5-minute bars for about 60 days: %s is too far back" % day)


def _open_url(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _sleep(seconds):
    time.sleep(seconds)


def yahoo_get(path):
    last = "no answer"
    for host in HOSTS:
        url = "https://%s.finance.yahoo.com%s" % (host, path)
        for attempt in (0, 1):
            try:
                return _open_url(url)
            except urllib.error.HTTPError as e:
                code, body = e.code, None
                try:
                    if code == 404:
                        body = e.read().decode("utf-8", "replace")
                except Exception:
                    body = None
                finally:
                    try:
                        e.close()
                    except Exception:
                        pass
                if code == 404:
                    if body:
                        return body
                    raise EntryError("Yahoo has no chart at %s (HTTP 404)" % url.split("?")[0])
                last = "HTTP %d" % code
                if code == 429 and attempt == 0:
                    _sleep(BACKOFF)
                    continue
                if code == 429 or code >= 500:
                    break
                raise EntryError("Yahoo answered HTTP %d for %s" % (code, url.split("?")[0]))
            except urllib.error.URLError as e:
                last = "could not connect: %s" % (e.reason,)
                break
            except OSError as e:
                last = "network error: %s" % (e,)
                break
    raise EntryError("Yahoo did not answer (%s on query2 and query1): wait a minute and try again" % last)


def parse_chart(text, sym):
    try:
        data = json.loads(text)
        res = data["chart"]["result"]
    except (ValueError, KeyError, TypeError):
        raise EntryError("Yahoo returned something unexpected for %s" % sym)
    if not res:
        err = (data.get("chart") or {}).get("error") or {}
        raise EntryError("Yahoo has no data for %s: %s" % (sym, err.get("description") or "unknown ticker"))
    r = res[0] or {}
    meta = r.get("meta") or {}
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0] or {}

    def at(key, i):
        arr = q.get(key) or []
        return arr[i] if i < len(arr) else None

    bars = []
    for i, t in enumerate(r.get("timestamp") or []):
        c = at("close", i)
        if t is None or c is None:
            continue
        o, h, lo = at("open", i), at("high", i), at("low", i)
        bars.append({"t": int(t), "o": float(c if o is None else o), "h": float(c if h is None else h),
                     "l": float(c if lo is None else lo), "c": float(c), "v": int(at("volume", i) or 0)})
    return {"symbol": str(meta.get("symbol") or sym).upper(), "meta": meta, "bars": bars,
            "prev_close": meta.get("previousClose") or meta.get("chartPreviousClose"),
            "data_time": meta.get("regularMarketTime")}


def read_text(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def offline_file(path, sym):
    if os.path.isdir(path):
        candidates = [sym + ".json", sym + ".json.gz", sym.replace(".NS", "") + ".json",
                      sym.replace(".NS", "") + ".json.gz"]
        for name in candidates:
            p = os.path.join(path, name)
            if os.path.exists(p):
                return p
        raise EntryError("no offline file for %s in %s (want %s.json)" % (sym, path, sym))
    if not os.path.exists(path):
        raise EntryError("no offline file %s" % path)
    return path


def _load_broker():
    try:
        from trader import broker_data
        return broker_data
    except Exception:
        pass
    import sys
    d = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "trader"))
    if not os.path.exists(d):
        d = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "trader"))
    if os.path.exists(d) and d not in sys.path:
        sys.path.insert(0, d)
    try:
        import broker_data
        return broker_data
    except Exception:
        return None


def get_chart(sym, interval, day=None, offline=None, today=None):
    sym = sym.upper()
    if offline:
        f = offline_file(offline, sym)
        try:
            text = read_text(f)
        except (OSError, ValueError) as e:
            raise EntryError("could not read %s: %s" % (f, e))
        ch = parse_chart(text, sym)
        chart_sym = (ch.get("meta") or {}).get("symbol")
        if chart_sym and chart_sym.upper() != sym.upper() and not os.path.isdir(offline):
            raise EntryError("%s holds %s, not %s" % (offline, chart_sym, sym))
    else:
        today = today or to_ist(utc_now()).date()
        ch = None
        # Use broker only for same-day data (day is None or day == today)
        if day is None or day == today:
            broker = _load_broker()
            if broker and broker.active_broker():
                try:
                    ch = broker.get_realtime_candles(sym, interval)
                except Exception:
                    ch = None
        if ch is None:
            text = yahoo_get(chart_path(sym, chart_range(day or today, today, interval), interval))
            ch = parse_chart(text, sym)
            ch.setdefault("meta", {})["source"] = "yahoo"
    gran = ch["meta"].get("dataGranularity")
    if gran and gran != interval:
        raise EntryError("%s has %s bars, not %s: use --interval %s" % (sym, gran, interval, gran))
    return ch


# ---------------------------------------------------------------- the rule

def read_config(path):
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except ValueError as e:
            raise EntryError("%s is not valid JSON: %s" % (path, e))
        for k in DEFAULTS:
            if doc.get(k):
                cfg[k] = doc[k]
    for k in DEFAULTS:
        if cfg[k] is not None:
            parse_hhmm(cfg[k], k)
    return cfg


def r2(x):
    return None if x is None else round(float(x), 2) + 0.0


def evaluate(chart, now_utc, setup="orb", or_minutes=15, interval="5m", level=None,
              min_volume_x=None, cfg=None):
    cfg = cfg or dict(DEFAULTS)
    us = is_us_chart(chart)
    local_conv = to_eastern if us else to_ist
    to_utc_conv = eastern_to_utc if us else ist_to_utc
    cur = "$" if us else "₹"
    tz_label = "ET" if us else "IST"

    if interval not in INTERVALS:
        raise EntryError("--interval is 5m or 1m")
    step = INTERVALS[interval]
    if or_minutes <= 0 or (or_minutes * 60) % step:
        raise EntryError("--or-minutes must be a whole number of %s bars" % interval)
    if setup not in ("orb", "level"):
        raise EntryError("--setup is orb or level")
    if setup == "level" and level is None:
        raise EntryError("--setup level needs --level PRICE")
    n_or = or_minutes * 60 // step
    local = local_conv(now_utc)
    day = local.date()
    res = {"symbol": chart["symbol"], "setup": setup, "side": "buy", "date": day.isoformat(),
           "interval": interval, "or_minutes": or_minutes, "now": local.strftime("%Y-%m-%d %H:%M:%S"), "status": "waiting",
           "phase": "", "reason": "", "opening_range": None, "level": None, "trigger": None,
           "last": None, "forming": None, "min_volume_x": min_volume_x,
           "prev_close": chart.get("prev_close"), "calendar_known": day.year in CALENDAR_YEARS,
           "is_us": us, "currency_symbol": cur, "tz_label": tz_label,
           "source": (chart.get("meta") or {}).get("source", "yahoo"), "_bars": []}
    dt = chart.get("data_time")
    res["data_time"] = local_conv(utc_of(dt)).strftime("%Y-%m-%d %H:%M:%S") if dt else None
    ok, why = trading_day(day, us=us)
    if not ok:
        res.update(status="no-data", phase="closed", reason="the market is closed today (%s)" % why,
                   next_open=next_open(day, us))
        return res
    s_times = session(day, us)
    if not s_times:
        res.update(status="no-data", phase="closed", reason="the market is closed today (%s)" % why,
                   next_open=next_open(day, us))
        return res
    o, c = s_times
    open_dt = datetime.datetime.combine(day, o)
    close_dt = datetime.datetime.combine(day, c)

    default_before = "09:45" if us else "09:30"
    default_after = "15:30" if us else "15:00"
    cfg_before = cfg.get("no_entries_before") if cfg and cfg.get("no_entries_before") else default_before
    cfg_after = cfg.get("no_entries_after") if cfg and cfg.get("no_entries_after") else default_after
    before = datetime.datetime.combine(day, parse_hhmm(cfg_before, "no_entries_before"))
    cutoff = datetime.datetime.combine(day, parse_hhmm(cfg_after, "no_entries_after"))
    early_reason = NYSE_EARLY_CLOSE.get(day.isoformat()) if us else None
    if c == EARLY:
        orig_close = CLOSE_US if us else CLOSE_NSE
        shift_hrs = (datetime.datetime.combine(day, orig_close) - close_dt).total_seconds() / 3600.0
        if cutoff >= close_dt or not (cfg and cfg.get("no_entries_after")):
            cutoff = cutoff - datetime.timedelta(hours=shift_hrs)
    or_end = open_dt + datetime.timedelta(minutes=or_minutes)
    res.update(opens=hm(o), closes=hm(c), early_close=early_reason,
               no_entries_before=hm(before), no_entries_after=hm(cutoff))

    by_slot = {}
    for b in chart["bars"]:
        if b["t"] % step:
            continue
        et = local_conv(utc_of(b["t"]))
        if et.date() != day or not (open_dt <= et < close_dt):
            continue
        slot = int((et - open_dt).total_seconds()) // step
        by_slot[slot] = dict(b, et=et, end=et + datetime.timedelta(seconds=step), slot=slot)
    bars, pending = [], None
    now_ts = ts_of(now_utc)
    top = max(by_slot) if by_slot else -1
    for slot in sorted(by_slot):
        b = by_slot[slot]
        end_ts = b["t"] + step
        if end_ts <= now_ts:
            if dt is None or dt >= end_ts or slot < top or now_ts >= end_ts + SETTLE:
                bars.append(b)
            else:
                pending = b
                res["forming"] = {"bar_start": hm(b["et"]), "closes": ampm(b["end"]), "price": r2(b["c"])}
        elif b["et"] <= local:
            res["forming"] = {"bar_start": hm(b["et"]), "closes": ampm(b["end"]), "price": r2(b["c"])}
    res["_bars"] = bars
    res["bars_closed"] = len(bars)
    if pending:
        dt_str = local_conv(utc_of(dt)).strftime("%H:%M:%S") if dt else "unknown"
        res["lag_note"] = "the %s bar closed by the clock at %s, but Yahoo's last trade is %s: waiting up to a minute for its final tick" % (
            hm(pending["et"]), hm(pending["end"]), dt_str)
    else:
        res["lag_note"] = None

    if local < open_dt:
        res.update(phase="pre-open", reason="the market opens at %s%s" % (
            short(open_dt), (" and the %d-minute range is set at %s" % (or_minutes, short(or_end))) if setup == "orb" else ""))
        return res

    or_bars = [b for b in bars if b["slot"] < n_or]
    avg_vol = (sum(b["v"] for b in or_bars) / float(len(or_bars))) if or_bars else None
    if or_bars:
        res["opening_range"] = {"high": r2(max(b["h"] for b in or_bars)), "low": r2(min(b["l"] for b in or_bars)),
                                "minutes": or_minutes, "bars": len(or_bars), "of": n_or,
                                "complete": local >= or_end, "avg_volume": int(round(avg_vol))}
    if setup == "orb":
        if local < or_end:
            res.update(phase="forming", reason="%d of %d range bars closed; the range is set at %s" % (
                len(or_bars), n_or, short(or_end)))
            return res
        if not or_bars:
            res.update(status="no-data", phase="open", reason="Yahoo sent no bars for the opening range")
            return res
        lvl = max(b["h"] for b in or_bars)
    else:
        lvl = float(level)
    res["level"] = r2(lvl)

    first_slot = n_or if setup == "orb" else 0
    trig, thin = None, []
    for b in bars:
        if b["slot"] < first_slot or b["end"] < before or b["end"] > cutoff:
            continue
        if b["c"] > lvl:
            vx = (b["v"] / avg_vol) if avg_vol else None
            if min_volume_x and (vx is None or vx < min_volume_x):
                thin.append(b)
                continue
            trig = b
            break
    last = bars[-1] if bars else None
    if last:
        res["last"] = {"bar_start": hm(last["et"]), "close": r2(last["c"]),
                       "vs_level_pct": r2(100.0 * (last["c"] / lvl - 1))}

    if trig:
        vx = (trig["v"] / avg_vol) if avg_vol else None
        res.update(status="triggered", phase="open" if local < close_dt else "after-close",
                   reason="the %s bar closed above %s" % (short(trig["et"]), "the range high" if setup == "orb" else "the level"))
        res["trigger"] = {"time": ampm(trig["end"]), "close_time": hm(trig["end"]), "bar_start": hm(trig["et"]),
                          "bar_index": trig["slot"], "price": r2(trig["c"]), "close": trig["c"],
                          "volume": trig["v"], "volume_x": r2(vx), "pct_over": r2(100.0 * (trig["c"] / lvl - 1))}
        res["minutes_since"] = int((local - trig["end"]).total_seconds() // 60)
        return res
    if not bars:
        res.update(status="no-data", phase="open", reason="Yahoo sent no bars for %s yet" % day.isoformat())
        return res
    what = "close above the range high %s%.2f" % (cur, lvl) if setup == "orb" else "close above %s%.2f" % (cur, lvl)
    if local >= cutoff and not (pending and pending["end"] <= cutoff):
        early_note = " (early close at 1:00 p.m. today)" if early_reason else ""
        res.update(status="expired", phase="after-close" if local >= close_dt else "after-cutoff",
                   reason="no %s by %s, the last time for a new entry%s" % (what, short(cutoff), early_note))
        return res
    res["phase"] = "open"
    if last["c"] > lvl and last in thin:
        res["reason"] = "the %s bar closed above at %s%.2f but on %.2fx the range's volume, under your %.2fx" % (
            short(last["et"]), cur, last["c"], last["v"] / avg_vol if avg_vol else 0, min_volume_x)
    elif last["c"] > lvl and last["end"] < before:
        res["reason"] = "the %s bar closed above at %s%.2f, but no entries before %s" % (
            short(last["et"]), cur, last["c"], short(before))
    else:
        gap = lvl - last["c"]
        res["reason"] = "last close %s%.2f (%s bar), %s%.2f %s %s (%+.2f%%)" % (
            cur, last["c"], short(last["et"]), cur, abs(gap), "under" if gap >= 0 else "over",
            "the range high %s%.2f" % (cur, lvl) if setup == "orb" else "the level %s%.2f" % (cur, lvl),
            100.0 * (last["c"] / lvl - 1))
    return res


def public(res):
    return {k: v for k, v in res.items() if not k.startswith("_")}


def entry_doc(res, now_utc):
    t = res.get("trigger") or {}
    orr = res.get("opening_range")
    return {"created_by": "trader-entry", "symbol": res["symbol"], "setup": res["setup"], "side": "buy",
            "date": res["date"], "status": res["status"], "time": t.get("time"), "bar_start": t.get("bar_start"),
            "bar_index": t.get("bar_index"), "price": t.get("price"), "level": res.get("level"),
            "interval": res["interval"],
            "opening_range": {"high": orr["high"], "low": orr["low"], "minutes": orr["minutes"]} if orr else None,
            "source": res.get("source", "yahoo"), "bar_age": res.get("bar_age"),
            "checked_at": iso(now_utc), "reason": res.get("reason")}


def write_json(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")


# ---------------------------------------------------------------- words

LABEL = {"triggered": "TRIGGERED", "waiting": "WAITING", "expired": "EXPIRED", "no-data": "NO DATA"}
PHASE_LABEL = {"pre-open": "NOT OPEN YET", "forming": "RANGE FORMING", "closed": "MARKET CLOSED"}
SHORT = {"pre-open": "not open", "forming": "forming", "closed": "closed"}


def setup_name(res):
    cur = res.get("currency_symbol", "₹")
    if res["setup"] == "orb":
        return "opening-range breakout (%d-minute range)" % res["or_minutes"]
    return "close above %s%.2f" % (cur, res["level"]) if res["level"] is not None else "close above a level"


def describe(res):
    when = datetime.datetime.strptime(res["now"], "%Y-%m-%d %H:%M:%S")
    bars = "5-minute" if res["interval"] == "5m" else "1-minute"
    cur = res.get("currency_symbol", "₹")
    tz = res.get("tz_label", "IST")
    src = res.get("source", "yahoo")
    broker = _load_broker()
    bar_age_str = None
    if broker:
        last_t = None
        if res.get("_bars"):
            last_t = res["_bars"][-1].get("t")
        elif res.get("data_time"):
            try:
                dt_obj = datetime.datetime.strptime(res["data_time"], "%Y-%m-%d %H:%M:%S")
                last_t = int(dt_obj.replace(tzinfo=datetime.timezone.utc).timestamp())
            except Exception:
                pass
        if last_t:
            bar_age_str = broker.format_bar_age(last_t, source=src)
    if not bar_age_str:
        bar_age_str = "source: %s, free, can lag" % src
    head = "%s  %s %s %s  %s on %s bars (%s)" % (
        res["symbol"], when.strftime("%a %Y-%m-%d"), when.strftime("%H:%M"), tz, setup_name(res), bars, bar_age_str)
    if res.get("data_time") and res["data_time"] <= res["now"] and res["data_time"][:10] == res["date"]:
        head += ", data to %s" % res["data_time"][11:]
    out = [head]
    if res.get("early_close"):
        out.append("Early close today at %s (%s)." % (res["closes"], res["early_close"]))
    orr = res.get("opening_range")
    if orr:
        start = datetime.datetime.strptime(res["opens"], "%H:%M")
        end = start + datetime.timedelta(minutes=orr["minutes"])
        out.append("Opening range %s-%s: high %s%.2f, low %s%.2f (%d of %d bars%s)." % (
            short(start), short(end), cur, orr["high"], cur, orr["low"], orr["bars"], orr["of"],
            "" if orr["complete"] else ", still forming"))
    label = PHASE_LABEL.get(res["phase"]) or LABEL.get(res["status"], res["status"].upper())
    t = res.get("trigger")
    if t:
        out.append("TRIGGERED %s at %s%.2f: the %s bar (bar %d) closed above %s %s%.2f (%+.2f%%%s)." % (
            t["time"], cur, t["price"], short(datetime.datetime.strptime(t["bar_start"], "%H:%M")), t["bar_index"],
            "the range high" if res["setup"] == "orb" else "the level", cur, res["level"], t["pct_over"],
            (", %.2fx the range's volume" % t["volume_x"]) if res.get("min_volume_x") and t.get("volume_x") else ""))
        step = INTERVALS[res["interval"]] // 60
        if res.get("minutes_since", 0) > step and res.get("last"):
            out.append("That was %d minutes ago; the last close is %s%.2f (%s bar). A late entry is a different trade." % (
                res["minutes_since"], cur, res["last"]["close"], res["last"]["bar_start"]))
        out.append("Nothing was placed.")
        out.append("Next: set the stop and target BEFORE you buy: /trader-exit (exit.py plan --from trader/entry.json).")
    else:
        out.append("%s  %s." % (label, res["reason"]))
        f = res.get("forming")
        if f and res["status"] == "waiting" and res["phase"] in ("open", "forming"):
            out.append("The %s bar is still forming (%s%.2f so far, closes %s); it does not count until it closes." % (
                f["bar_start"].lstrip("0"), cur, f["price"], f["closes"]))
        if res["status"] == "expired":
            out.append("No entry on this setup today. Nothing was placed.")
        if res.get("next_open"):
            out.append("Next open: %s." % res["next_open"])
    return out


def bar_line(b, res):
    cur = res.get("currency_symbol", "₹")
    start = short(b["et"])
    orr = res.get("opening_range") or {}
    lvl = res.get("level")
    t = res.get("trigger")
    n_or = orr.get("of") or 0
    if res["setup"] == "orb" and b["slot"] < n_or:
        seen = [x for x in res["_bars"] if x["slot"] <= b["slot"]]
        note = "range so far %s%.2f to %s%.2f" % (cur, min(x["l"] for x in seen), cur, max(x["h"] for x in seen))
        if b["slot"] == n_or - 1:
            note = "opening range set: high %s%.2f, low %s%.2f" % (cur, orr["high"], cur, orr["low"])
    elif t and t["bar_index"] == b["slot"]:
        note = "TRIGGERED: closed above %s%.2f (%+.2f%%). Get in now: %s, %s%.2f" % (
            cur, lvl, t["pct_over"], t["time"], cur, t["price"])
    elif lvl is None:
        note = ""
    elif b["c"] > lvl:
        note = "above %s%.2f" % (cur, lvl)
    else:
        note = "%s%.2f under %s%.2f" % (cur, lvl - b["c"], cur, lvl)
    return "%5s bar  closed %s%.2f  %s" % (start, cur, b["c"], note)


# ---------------------------------------------------------------- watch

class SimClock(object):
    def __init__(self, start):
        self.t = start

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += datetime.timedelta(seconds=max(1, int(round(seconds))))


def watch_loop(provider, clock, sleep, emit, opts, every=30, until=None, max_errors=5, on_trigger=None):
    step = INTERVALS[opts.get("interval", "5m")]
    printed, errors, said_preopen, first = -1, 0, False, True
    next_fetch = None
    while True:
        now = clock()
        if next_fetch is not None and now < next_fetch:
            sleep(max(1, min(every, (next_fetch - now).total_seconds())))
            continue
        try:
            chart = provider(now)
            errors = 0
        except EntryError as e:
            errors += 1
            emit("Could not read bars: %s" % e)
            if errors >= max_errors:
                emit("Stopping after %d failed reads in a row." % errors)
                return 1, None
            next_fetch = now + datetime.timedelta(seconds=every)
            continue
        res = evaluate(chart, now, **opts)
        us = res.get("is_us")
        local_conv = to_eastern if us else to_ist
        local = local_conv(now)
        day = local.date()
        s = session(day, us)
        if not s:
            ok, why = trading_day(day, us)
            emit("%s: the market is closed today (%s). Nothing to watch." % (day.isoformat(), why))
            return 1, None
        open_dt = datetime.datetime.combine(day, s[0])
        close_dt = datetime.datetime.combine(day, s[1])

        if local < open_dt:
            if not said_preopen:
                emit("%s: not open yet (session is %s to %s). Waiting for the open." % (res["symbol"], res["opens"], res["closes"]))
                said_preopen = True
            sleep(min(every, (open_dt - local).total_seconds()))
            continue

        bars = res["_bars"]
        lines = describe(res)
        fresh = first
        if first:
            emit(lines[0])
            if len(lines) > 1 and lines[1].startswith("Opening range"):
                emit(lines[1])
            if bars and not res.get("trigger"):
                emit(bar_line(bars[-1], res))
            printed = bars[-1]["slot"] if bars else -1
            first = False
        else:
            for b in bars:
                if b["slot"] > printed:
                    emit(bar_line(b, res))
                    printed = b["slot"]
        if res["status"] == "triggered":
            for line in lines[1:]:
                if line.startswith("Opening range") or (line.startswith("TRIGGERED") and not fresh):
                    continue
                emit(line)
            if on_trigger:
                on_trigger(res, now)
            return 0, res
        if res["status"] == "expired" or local >= close_dt:
            emit("EXPIRED  %s." % (res["reason"] or "the market closed without a trigger"))
            return 1, res
        if until and local >= until:
            emit("Stopped at --until %s without a trigger. %s." % (short(until), res["reason"]))
            return 1, res
        cur_slot = int((local - open_dt).total_seconds()) // step
        expected_last = cur_slot - 1
        behind = (not bars) or bars[-1]["slot"] < expected_last
        wait = every if behind else (open_dt + datetime.timedelta(seconds=(cur_slot + 1) * step + GRACE) - local).total_seconds()
        next_fetch = now + datetime.timedelta(seconds=max(1, wait))


# ---------------------------------------------------------------- commands

def _opts(a, cfg):
    if a.side in ("sell", "short"):
        raise EntryError("v2 plans longs only: no short entries. Watch for a buy (--side buy) instead.")
    if a.setup == "orb" and a.level is not None:
        raise EntryError("--level is for --setup level (an orb's level is its range high)")
    if a.min_volume_x is not None and a.min_volume_x <= 0:
        raise EntryError("--min-volume-x must be above 0")
    if a.level is not None and a.level <= 0:
        raise EntryError("--level must be a price above 0")
    return {"setup": a.setup, "or_minutes": a.or_minutes, "interval": a.interval, "level": a.level,
            "min_volume_x": a.min_volume_x, "cfg": cfg}


def _load(a, sym, now):
    real_today = to_ist(utc_now()).date()
    if a.offline and now is None:
        ch = get_chart(sym, a.interval, offline=a.offline)
        return ch, (utc_of(ch["data_time"]) if ch.get("data_time") else utc_now())
    now = now or utc_now()
    return get_chart(sym, a.interval, to_ist(now).date(), a.offline, real_today), now


def cmd_check(a):
    cfg = read_config(a.config)
    opts = _opts(a, cfg)
    sym = a.symbol.upper()
    chart, now = _load(a, sym, parse_now(a.now) if a.now else None)
    res = evaluate(chart, now, **opts)
    if a.write:
        write_json(a.write, entry_doc(res, now))
    if a.json:
        print(json.dumps(public(res), indent=2))
    else:
        for line in describe(res):
            print(line)
        if a.write:
            print("Wrote %s (status %s)." % (a.write, res["status"]))
    return 0 if res["status"] == "triggered" else 1


def cmd_watch(a):
    cfg = read_config(a.config)
    opts = _opts(a, cfg)
    sym = a.symbol.upper()
    if a.every <= 0:
        raise EntryError("--every must be above 0 seconds")
    start = parse_now(a.now) if a.now else None
    if a.offline or start is not None:
        if start is None:
            start = _load(a, sym, None)[1]
        clock = SimClock(start)
        now_fn, sleep_fn = clock.now, clock.sleep
    else:
        now_fn, sleep_fn = utc_now, _sleep
    real_today = to_ist(utc_now()).date()

    def provider(now):
        return get_chart(sym, a.interval, to_ist(now).date(), a.offline, real_today)

    until = None
    if a.until:
        t = parse_hhmm(a.until, "--until")
        until = datetime.datetime.combine(to_ist(now_fn()).date(), t)

    def on_trigger(res, now):
        write_json(a.out, entry_doc(res, now))
        print("Wrote %s." % a.out, flush=True)

    def emit(line):
        print(line, flush=True)

    code, _ = watch_loop(provider, now_fn, sleep_fn, emit, opts, every=a.every, until=until,
                         on_trigger=on_trigger)
    return code


def cmd_watchlist(a):
    cfg = read_config(a.config)
    opts = _opts(a, cfg)
    if not os.path.exists(a.watchlist):
        raise EntryError("no watchlist at %s: run /trader-scanner first" % a.watchlist)
    try:
        with open(a.watchlist, encoding="utf-8") as fh:
            doc = json.load(fh)
    except ValueError as e:
        raise EntryError("%s is not valid JSON: %s" % (a.watchlist, e))
    hits = [h for h in (doc.get("hits") or []) if isinstance(h, dict) and h.get("symbol")]
    if not hits:
        raise EntryError("no hits in %s: the scanner found nothing to watch" % a.watchlist)
    if a.offline and not os.path.isdir(a.offline):
        raise EntryError("watchlist --offline wants a folder of <TICKER>.json files")
    now = parse_now(a.now) if a.now else None
    rows = []
    for i, h in enumerate(hits):
        sym = str(h["symbol"]).upper()
        o = dict(opts)
        if a.setup == "level" and o["level"] is None:
            o["level"] = h.get("level")
        try:
            if a.setup == "level" and o["level"] is None:
                raise EntryError("needs a level: --level PRICE or a \"level\" in the watchlist")
            if i and not a.offline:
                _sleep(PAUSE)
            chart, when = _load(a, sym, now)
            rows.append((sym, evaluate(chart, when, **o), None))
        except EntryError as e:
            rows.append((sym, None, str(e)))
    if a.json:
        print(json.dumps([{"symbol": s, "result": public(r) if r else None, "error": e} for s, r, e in rows], indent=2))
    else:
        print("%-6s  %-9s  %-8s  %-8s  %-8s  %-8s  %s" % ("SYMBOL", "STATUS", "TIME", "PRICE", "LEVEL", "LAST", "VS LEVEL"))
        for s, r, e in rows:
            if e:
                print("%-6s  error: %s" % (s, e))
                continue
            cur = r.get("currency_symbol", "₹")
            t = r.get("trigger") or {}
            last = r.get("last") or {}
            status = SHORT.get(r["phase"], r["status"]) if r["status"] != "triggered" else "triggered"
            print("%-6s  %-9s  %-8s  %-8s  %-8s  %-8s  %s" % (
                s, status[:9], t.get("time") or "-", ("%s%.2f" % (cur, t["price"])) if t else "-",
                ("%s%.2f" % (cur, r["level"])) if r.get("level") is not None else "-",
                ("%s%.2f" % (cur, last["close"])) if last else "-",
                ("%+.2f%%" % last["vs_level_pct"]) if last.get("vs_level_pct") is not None else "-"))
        n = sum(1 for _, r, _ in rows if r and r["status"] == "triggered")
        print("%d of %d triggered. Pick one, then: entry.py check <TICKER> --write, and /trader-exit." % (n, len(rows)))
    return 0 if any(r and r["status"] == "triggered" for _, r, _ in rows) else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="Entry Timer (trader-entry): watches a setup bar by bar.")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--setup", choices=["orb", "level"], default="orb")
        p.add_argument("--or-minutes", type=int, default=15, help="opening range length (default 15)")
        p.add_argument("--interval", choices=["5m", "1m"], default="5m")
        p.add_argument("--level", type=float, help="the price to close above (--setup level)")
        p.add_argument("--min-volume-x", type=float, help="trigger bar volume vs the range bars' average")
        p.add_argument("--side", default="buy", choices=["buy", "long", "sell", "short"], help="buy only")
        p.add_argument("--now", help='pretend it is this time (e.g. "2026-10-19 10:15")')
        p.add_argument("--offline", metavar="PATH", help="saved Yahoo chart JSON (.json/.json.gz) or a folder")
        p.add_argument("--config", default="trader/config.json")

    p = sub.add_parser("check", help="the setup's status right now")
    p.add_argument("symbol")
    common(p)
    p.add_argument("--write", nargs="?", const="trader/entry.json", metavar="PATH",
                   help="save the result (default trader/entry.json)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("watch", help="poll bar by bar until it triggers or expires")
    p.add_argument("symbol")
    common(p)
    p.add_argument("--every", type=float, default=30, help="seconds between polls")
    p.add_argument("--until", help="stop at this time (HH:MM)")
    p.add_argument("--out", default="trader/entry.json", help="where a trigger is written")
    p = sub.add_parser("watchlist", help="check every hit in trader/watchlist.json")
    common(p)
    p.add_argument("--watchlist", default="trader/watchlist.json")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"check": cmd_check, "watch": cmd_watch, "watchlist": cmd_watchlist}[a.cmd](a)
    except EntryError as e:
        print("entry.py: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("entry.py: stopped.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
