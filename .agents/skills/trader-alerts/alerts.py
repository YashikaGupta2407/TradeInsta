#!/usr/bin/env python3
"""Price alerts and the NSE clock for trader-alerts. Standard library only.

    python3 alerts.py clock                          # is the NSE open? next open / close
    python3 alerts.py quote RELIANCE.NS --write      # quotes -> trader/prices.json
    python3 alerts.py quote --holdings --write       # every ticker in your portfolio and plan
    python3 alerts.py add --from-plan                # stop, target and flat-by-close from trader/plan.json
    python3 alerts.py add --symbol RELIANCE.NS --stop 1250.00
    python3 alerts.py add --symbol RELIANCE.NS --target 1320.00
    python3 alerts.py add --at 15:15 --label "flat by close"
    python3 alerts.py add --symbol TCS.NS --pct 3
    python3 alerts.py add --opening-bell
    python3 alerts.py check                          # rings what hit, once; exit 1 if nothing rang
    python3 alerts.py list
    python3 alerts.py reset reliance-plan-stop

Rule types:
  above / target  the price is at or above "price"
  below / stop    the price is at or below "price"
  pct_move        the price moved "pct" percent or more from the previous close
                  (or from "ref" if you give one); "direction": "up" or "down"
  opening_bell    the NSE opened today (rings once per trading day at 9:15 AM IST)
  time            the Indian clock passed "at" (HH:MM IST) on a trading day
                  (rings once per trading day)

Quotes:
  yahoo   https://query2.finance.yahoo.com/v8/finance/chart/<TICKER> (defaults to .NS for NSE)
"""

import argparse
import csv
import datetime
import gzip
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/Jakeschincariol/day-trader-skill)"
PAUSE = 0.5    # seconds between requests: be polite to free endpoints
BACKOFF = 2.0  # seconds to wait before the one retry after a 429
YAHOO_HOSTS = ("query2.finance.yahoo.com", "query1.finance.yahoo.com")
HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

# Load NSE Holidays from trader/holidays.json or fallback
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
                    return doc.get("holidays", {}), doc.get("muhurat_trading", {}), tuple(doc.get("years", [2025, 2026, 2027]))
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
    }, {"2026-11-08": {"open": "18:15", "close": "19:15"}}, (2025, 2026, 2027)

NSE_HOLIDAYS, MUHURAT_TRADING, CALENDAR_YEARS = _load_holidays()
NYSE_HOLIDAYS = {
    "2026-01-01": "New Year's Day",
    "2026-01-19": "Martin Luther King Jr. Day",
    "2026-02-16": "Washington's Birthday",
    "2026-04-03": "Good Friday",
    "2026-05-25": "Memorial Day",
    "2026-06-19": "Juneteenth",
    "2026-07-03": "Independence Day (observed)",
    "2026-09-07": "Labor Day",
    "2026-11-26": "Thanksgiving Day",
    "2026-12-25": "Christmas Day",
    "2027-01-01": "New Year's Day",
    "2027-01-18": "Martin Luther King Jr. Day",
    "2027-02-15": "Washington's Birthday",
    "2027-03-26": "Good Friday",
    "2027-05-31": "Memorial Day",
    "2027-06-18": "Juneteenth (observed)",
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

OPEN = OPEN_US
CLOSE = CLOSE_US
IST_OFFSET = datetime.timedelta(hours=5, minutes=30)


class AlertError(Exception):
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


def parse_now(text):
    if not text:
        return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)
    t = text.strip()
    is_ist = t.upper().endswith(" IST")
    is_et = t.upper().endswith(" ET")
    if is_ist or is_et:
        t = t[:-3].strip()
    t = t.replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise AlertError('--now wants a time like "2026-10-19 10:15" (or ...Z for UTC)')
    if dt.tzinfo is None:
        return ist_to_utc(dt) if is_ist else eastern_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def trading_day(d, us=True):
    if d.weekday() >= 5:
        return False, "weekend"
    if us:
        name = NYSE_HOLIDAYS.get(d.isoformat())
        if name:
            return False, "NYSE holiday: " + name
        return True, ""
    name = NSE_HOLIDAYS.get(d.isoformat())
    if name:
        if d.isoformat() in MUHURAT_TRADING:
            return True, "Muhurat trading session (%s)" % name
        return False, "NSE holiday: " + name
    return True, ""


def session(d, us=True):
    ok, _ = trading_day(d, us=us)
    if not ok:
        return None
    if us:
        return OPEN_US, (EARLY if d.isoformat() in NYSE_EARLY_CLOSE else CLOSE_US)
    if d.isoformat() in MUHURAT_TRADING:
        m = MUHURAT_TRADING[d.isoformat()]
        op = list(map(int, m["open"].split(":")))
        cl = list(map(int, m["close"].split(":")))
        return datetime.time(op[0], op[1]), datetime.time(cl[0], cl[1])
    return OPEN_NSE, CLOSE_NSE


def market_status(now_utc, us=True):
    local = to_eastern(now_utc) if us else to_ist(now_utc)
    d = local.date()
    ok, reason = trading_day(d, us=us)
    early_map = NYSE_EARLY_CLOSE if us else NSE_EARLY_CLOSE
    st = {"now_ist": to_ist(now_utc).strftime("%Y-%m-%d %H:%M"),
          "now_new_york": to_eastern(now_utc).strftime("%Y-%m-%d %H:%M"),
          "date": d.isoformat(), "weekday": local.strftime("%A"),
          "calendar_known": d.year in CALENDAR_YEARS, "open": False, "reason": reason,
          "early_close": early_map.get(d.isoformat())}
    s = session(d, us=us)
    if s:
        o, c = s
        st["opens"], st["closes"] = o.strftime("%H:%M"), c.strftime("%H:%M")
        if o <= local.time() < c:
            st["open"] = True
            st["reason"] = "regular session"
        elif local.time() < o:
            st["reason"] = "before the open"
        else:
            st["reason"] = "after the close"
    nd = d if (s and local.time() < s[0]) else d + datetime.timedelta(days=1)
    for _ in range(15):
        s_next = session(nd, us=us)
        if s_next:
            st["next_open"] = "%s %s" % (nd.isoformat(), s_next[0].strftime("%H:%M"))
            break
        nd += datetime.timedelta(days=1)
    return st


# ---------------------------------------------------------------- quotes

def stooq_url(sym):
    s = sym.lower().replace(".", "-")
    return "https://stooq.com/q/l/?s=%s&f=sd2t2ohlcv&h&e=csv" % s


def yahoo_url(sym, host=YAHOO_HOSTS[0]):
    s = sym.upper()
    if not (s.endswith(".NS") or s.endswith(".BO")):
        s = s.replace(".", "-")
    return "https://%s/v8/finance/chart/%s?range=1d&interval=1d" % (host, s)


def parse_stooq(text, sym):
    rows = list(csv.DictReader(io.StringIO(text.strip())))
    if not rows:
        raise AlertError("Stooq returned no rows for %s" % sym)
    r = {k.strip().lower(): (v or "").strip() for k, v in rows[0].items() if k}
    close = r.get("close", "")
    if not close or close.upper() == "N/D":
        raise AlertError("Stooq has no data for %s (check the ticker)" % sym)

    def f(k):
        v = r.get(k, "")
        try:
            return float(v)
        except ValueError:
            return None
    return {"symbol": sym.upper(), "price": float(close), "open": f("open"), "high": f("high"),
            "low": f("low"), "prev_close": None, "time": ("%s %s" % (r.get("date", ""), r.get("time", ""))).strip(),
            "source": "stooq"}


def parse_yahoo(text, sym):
    try:
        data = json.loads(text)
        res = data["chart"]["result"]
    except (ValueError, KeyError, TypeError):
        raise AlertError("Yahoo returned something unexpected for %s" % sym)
    if not res:
        err = (data.get("chart") or {}).get("error") or {}
        raise AlertError("Yahoo has no data for %s: %s" % (sym, err.get("description", "unknown ticker")))
    m = res[0].get("meta", {})
    price = m.get("regularMarketPrice")
    if price is None:
        raise AlertError("Yahoo has no price for %s" % sym)
    t = m.get("regularMarketTime")
    when = (datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if t else "")
    q = (res[0].get("indicators", {}).get("quote") or [{}])[0]
    opens = [x for x in (q.get("open") or []) if x is not None]
    return {"symbol": sym.upper(), "price": float(price), "open": opens[0] if opens else None,
            "high": m.get("regularMarketDayHigh"), "low": m.get("regularMarketDayLow"),
            "prev_close": m.get("previousClose") or m.get("chartPreviousClose"), "time": when,
            "currency": m.get("currency") or "INR", "source": "yahoo"}


_urlopen = urllib.request.urlopen   # tests swap these two
_sleep = time.sleep


def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        with _urlopen(req, timeout=timeout) as r:
            return 200, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        finally:
            try:
                e.close()
            except Exception:
                pass
        return e.code, body
    except urllib.error.URLError as e:
        raise AlertError("could not reach %s: %s" % (url.split("/")[2], e.reason))


def fetch(url, timeout=20):
    code, text = _get(url, timeout)
    if code != 200:
        raise AlertError("%s answered HTTP %d" % (url.split("?")[0], code))
    return text


def fetch_yahoo(sym, timeout=20):
    last = None
    for host in YAHOO_HOSTS:
        url = yahoo_url(sym, host)
        code, text = _get(url, timeout)
        if code == 429:
            _sleep(BACKOFF)
            code, text = _get(url, timeout)
        if code == 200:
            return text
        if code == 404 and text.strip().startswith("{"):
            return text
        last = (url, code)
        if code != 429 and code < 500:
            break
    raise AlertError("%s answered HTTP %d" % (last[0].split("?")[0], last[1]))


def _offline_text(folder, source, sym):
    ext = "csv" if source == "stooq" else "json"
    candidates = [
        os.path.join(folder, "%s-%s.%s" % (source, sym, ext)),
        os.path.join(folder, "%s-%s.%s" % (source, sym.replace(".NS", ""), ext))
    ]
    for base in candidates:
        if os.path.exists(base):
            with open(base, encoding="utf-8") as fh:
                return fh.read()
        if os.path.exists(base + ".gz"):
            with gzip.open(base + ".gz", "rt", encoding="utf-8") as fh:
                return fh.read()
    raise AlertError("no offline quote file for %s in %s" % (sym, folder))


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


def get_quotes(symbols, source="yahoo", offline=None, pause=PAUSE):
    quotes, errors = {}, []
    broker = _load_broker()
    active_b = broker.active_broker() if (broker and not offline and source != "stooq") else None
    broker_quotes = None
    if active_b:
        try:
            broker_quotes = broker.get_realtime_quotes(symbols)
        except Exception:
            broker_quotes = None

    for i, sym in enumerate(symbols):
        sym = sym.upper()
        if broker_quotes and sym in broker_quotes:
            quotes[sym] = broker_quotes[sym]
            continue
        try:
            if offline:
                text = _offline_text(offline, source, sym)
            else:
                if i and pause:
                    _sleep(pause)
                text = fetch(stooq_url(sym)) if source == "stooq" else fetch_yahoo(sym)
            quotes[sym] = parse_stooq(text, sym) if source == "stooq" else parse_yahoo(text, sym)
        except AlertError as e:
            errors.append(str(e))
    return quotes, errors


def write_prices(path, quotes, now_utc, source):
    old = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                old = json.load(fh)
        except (ValueError, OSError):
            old = {}
    prices = dict(old.get("prices", {}))
    detail = dict(old.get("detail", {}))
    stamp = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    for s, q in quotes.items():
        prices[s] = q["price"]
        detail[s] = {"prev_close": q.get("prev_close"), "time": q.get("time"), "source": q.get("source"),
                     "fetched": stamp}
    doc = {"as_of": stamp, "source": source,
            "note": "Delayed quotes for alerts and sizing checks. detail.fetched is when each price was read.",
            "prices": prices, "detail": detail}
    _write(path, doc)
    return doc


def _write(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def _read(path, default):
    if not path or not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise AlertError("%s is not valid JSON: %s" % (path, e))


# ---------------------------------------------------------------- rules

PRICE_TYPES = {"above", "below", "stop", "target", "pct_move", "opening_bell", "time"}
NO_QUOTE = {"opening_bell", "time"}


def _hhmm(text):
    m = HHMM.match(str(text or "").strip())
    if not m:
        return None
    return datetime.time(int(m.group(1)), int(m.group(2)))


def validate(rule):
    t = rule.get("type")
    if t not in PRICE_TYPES:
        raise AlertError("rule %s: unknown type %r" % (rule.get("id"), t))
    if t not in NO_QUOTE and not rule.get("symbol"):
        raise AlertError("rule %s needs a symbol" % rule.get("id"))
    if t in ("above", "below", "stop", "target") and not isinstance(rule.get("price"), (int, float)):
        raise AlertError("rule %s needs a numeric price" % rule.get("id"))
    if t == "pct_move" and not isinstance(rule.get("pct"), (int, float)):
        raise AlertError("rule %s needs a numeric pct" % rule.get("id"))
    if t == "time" and _hhmm(rule.get("at")) is None:
        raise AlertError("rule %s needs \"at\" as a time like \"15:15\"" % rule.get("id"))
    if rule.get("date") and not re.match(r"^\d{4}-\d{2}-\d{2}$", str(rule["date"])):
        raise AlertError("rule %s: date must be YYYY-MM-DD" % rule.get("id"))


def active(rule, today):
    return not rule.get("date") or str(rule["date"]) == today


def _rang_today(stamp, today, us=False):
    if not stamp:
        return False
    try:
        utc = datetime.datetime.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return stamp[:10] == today
    local = to_eastern(utc) if us else to_ist(utc)
    return local.date().isoformat() == today


def ring_time(rule, status):
    at = _hhmm(rule.get("at"))
    if rule.get("before_close") and status.get("early_close"):
        at = (datetime.datetime.combine(datetime.date(2000, 1, 1), at) - datetime.timedelta(hours=3)).time()
    return at


def evaluate(rules, quotes, status, state, now_utc):
    state = json.loads(json.dumps(state or {}))
    rung = state.setdefault("rung", {})
    today = status["date"]
    has_ns = any(str(r.get("symbol", "")).upper().endswith(".NS") or str(r.get("symbol", "")).upper().endswith(".BO") for r in rules)
    if has_ns:
        is_us = False
    elif status.get("market") == "NYSE" or status.get("opens") == "09:30" or status.get("closes") == "16:00":
        is_us = True
    elif status.get("market") == "NSE" or status.get("opens") == "09:15" or status.get("closes") == "15:30":
        is_us = False
    else:
        is_us = True
    local = to_eastern(now_utc) if is_us else to_ist(now_utc)
    stamp = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    rings = []
    for r in rules:
        validate(r)
        rid = r.get("id") or "%s-%s" % (r.get("symbol", "market"), r["type"])
        if not active(r, today):
            continue
        typ = {"stop": "below", "target": "above"}.get(r["type"], r["type"])
        if typ == "opening_bell":
            if status["open"] and not _rang_today(rung.get(rid), today, is_us):
                mkt_name = "NYSE" if is_us else "NSE"
                now_s = status["now_new_york"] if is_us else status["now_ist"]
                tz_s = "New York" if is_us else "IST"
                rings.append({"id": rid, "symbol": "", "message": "Opening bell: the %s is open (%s %s)."
                              % (mkt_name, now_s, tz_s)})
                rung[rid] = stamp
            continue
        if typ == "time":
            at = ring_time(r, status)
            if status.get("opens") and local.time() >= at and not _rang_today(rung.get(rid), today, is_us):
                label = r.get("label") or "time alert"
                tz_s = "New York" if is_us else "IST"
                msg = "%s%s: it is %s %s (set for %s)" % (label[:1].upper(), label[1:],
                                                           local.strftime("%H:%M"), tz_s, at.strftime("%H:%M"))
                msg += (", the market closes at %s." % status["closes"]) if status["open"] else \
                    ", the market has closed."
                sym = str(r.get("symbol") or "").upper()
                if sym:
                    if is_us:
                        msg += " If %s is still open, close it with trader-broker flatten --symbol %s." % (sym, sym)
                    else:
                        msg += " If %s is still open, close it before market close." % sym
                rings.append({"id": rid, "symbol": sym, "message": msg})
                rung[rid] = stamp
            continue
        sym = r["symbol"].upper()
        sym_is_us = not (sym.endswith(".NS") or sym.endswith(".BO"))
        cur = "$" if sym_is_us else "₹"
        q = quotes.get(sym) or quotes.get(sym.replace(".NS", ""))
        if not q:
            continue
        p = q["price"]
        if typ in ("above", "below"):
            hit = p >= r["price"] if typ == "above" else p <= r["price"]
            if hit and rid not in rung:
                word = "at or above" if typ == "above" else "at or below"
                label = {"stop": "stop", "target": "target"}.get(r["type"], "alert level")
                rings.append({"id": rid, "symbol": sym, "message": "%s %s %s%.2f: %s your %s %s%s%s."
                              % (sym, "is" if status["open"] else "last", cur, p, word, label, cur, _n(r["price"]),
                                 (" (" + r["note"] + ")") if r.get("note") else "")})
                rung[rid] = stamp
            elif not hit and rid in rung and r.get("rearm"):
                del rung[rid]
        elif typ == "pct_move":
            ref = r.get("ref") or q.get("prev_close") or q.get("open")
            if not ref:
                continue
            move = 100.0 * (p / float(ref) - 1)
            want = r.get("direction")
            hit = abs(move) >= r["pct"] and (not want or (want == "up") == (move > 0))
            if hit and not _rang_today(rung.get(rid), today, is_us):
                rings.append({"id": rid, "symbol": sym, "message": "%s moved %+.2f%% to %s%.2f (from %s%.2f)."
                              % (sym, move, cur, p, cur, float(ref))})
                rung[rid] = stamp
    state["last_check"] = stamp
    return rings, state


def _n(x):
    x = float(x)
    return str(int(x)) if x == int(x) else "%.2f" % x


def rules_from_plan(plan, cfg_flat_by=None):
    if not isinstance(plan, dict) or not plan.get("symbol"):
        raise AlertError("the plan has no symbol (run trader-exit first)")
    sym = str(plan["symbol"]).upper()
    out = []
    for key, typ, word in (("stop", "stop", "plan stop"), ("target", "target", "plan target")):
        v = plan.get(key)
        if not isinstance(v, (int, float)) or v <= 0:
            raise AlertError("the plan has no %s (run trader-exit)" % key)
        label = plan.get("%s_label" % key)
        out.append({"id": "%s-plan-%s" % (sym.lower(), key), "symbol": sym, "type": typ, "price": float(v),
                    "note": word + (": " + label if label else "")})
    flat = plan.get("flat_by") or cfg_flat_by or "15:15"
    if _hhmm(flat) is None:
        raise AlertError("the plan's flat_by %r is not a time like 15:15" % flat)
    out.append({"id": "%s-flat-by" % sym.lower(), "symbol": sym, "type": "time", "at": flat,
                "label": "flat by close", "before_close": True})
    if plan.get("date"):
        for r in out:
            r["date"] = str(plan["date"])
    return out


# ---------------------------------------------------------------- commands

def holdings(a):
    syms = []
    pf = _read(a.portfolio, {})
    for p in pf.get("positions", []) if isinstance(pf, dict) else []:
        syms.append(str(p.get("ticker") or p.get("symbol") or "").upper())
    plan = _read(a.plan, {})
    if isinstance(plan, dict) and plan.get("symbol"):
        syms.append(str(plan["symbol"]).upper())
    return [s for s in syms if s]


def _dedupe(items):
    out = []
    for s in items:
        if s and s not in out:
            out.append(s)
    return out


def cmd_clock(a):
    st = market_status(parse_now(a.now))
    if a.json:
        print(json.dumps(st, indent=2))
    else:
        print("IST %s (%s): NSE %s, %s." % (st["now_ist"], st["weekday"],
                                            "OPEN" if st["open"] else "closed", st["reason"]))
        if st.get("opens"):
            print("Today's session: %s to %s." % (st["opens"], st["closes"]))
        if not st["open"] and st.get("next_open"):
            print("Next open: %s IST." % st["next_open"])
        if not st["calendar_known"]:
            print("Holiday list covers %s only: check nseindia.com for this date." % " and ".join(map(str, CALENDAR_YEARS)))
    return 0 if st["open"] else 1


def cmd_quote(a):
    syms = _dedupe([s.upper() for s in a.symbols] + (holdings(a) if a.holdings else []))
    if not syms:
        raise AlertError("which tickers? alerts.py quote RELIANCE.NS TCS.NS, or --holdings")
    now = parse_now(a.now)
    quotes, errors = get_quotes(syms, a.source, a.offline)
    if a.write and quotes:
        write_prices(a.write, quotes, now, a.source)
    if a.json:
        print(json.dumps({"quotes": quotes, "errors": errors}, indent=2))
    else:
        broker = _load_broker()
        active_b = broker.active_broker() if (broker and not a.offline) else None
        for s in syms:
            q = quotes.get(s)
            if q:
                cur = "$" if not (s.endswith(".NS") or s.endswith(".BO")) else "₹"
                chg = ""
                if q.get("prev_close"):
                    chg = " (%+.2f%% vs prev close %s%.2f)" % (100.0 * (q["price"] / q["prev_close"] - 1), cur, q["prev_close"])
                age_tag = ""
                if broker and q.get("time"):
                    try:
                        t_iso = q["time"].replace("Z", "+00:00")
                        dt_q = datetime.datetime.fromisoformat(t_iso)
                        age_tag = " (%s)" % broker.format_bar_age(int(dt_q.timestamp()), now, source=q["source"])
                    except Exception:
                        pass
                print("%-6s %s%.2f%s  %s  [%s]%s" % (s, cur, q["price"], chg, q.get("time", ""), q["source"], age_tag))
        for e in errors:
            print("error: " + e)
        if a.write and quotes:
            print("Wrote %d price(s) to %s." % (len(quotes), a.write))
        if active_b:
            print("Quotes from %s real-time feed: for alerts and sizing." % active_b.capitalize())
        else:
            print("Quotes are free, delayed and unofficial: for alerts and sizing only.")
    return 0 if quotes else 1


def cmd_check(a):
    doc = _read(a.rules, None)
    if not doc or not doc.get("rules"):
        raise AlertError("no rules in %s: add some with alerts.py add --from-plan" % a.rules)
    rules = doc["rules"]
    now = parse_now(a.now)
    is_us = not any(str(r.get("symbol", "")).upper().endswith(".NS") or str(r.get("symbol", "")).upper().endswith(".BO") for r in rules)
    status = market_status(now, us=is_us)
    watched = [r for r in rules if active(r, status["date"])]
    syms = _dedupe([str(r.get("symbol", "")).upper() for r in watched if r.get("type") not in NO_QUOTE])
    quotes, errors = (get_quotes(syms, a.source, a.offline) if syms else ({}, []))
    state = _read(a.state, {})
    rings, state = evaluate(rules, quotes, status, state, now)
    if not a.no_write:
        _write(a.state, state)
        if quotes:
            write_prices(a.prices_out, quotes, now, a.source)
    if a.json:
        print(json.dumps({"market": status, "rings": rings, "errors": errors,
                          "quotes": {s: q["price"] for s, q in quotes.items()}}, indent=2))
    else:
        mkt = "NYSE" if is_us else "NSE"
        tz_word = "ET" if is_us else "IST"
        now_str = status["now_new_york"] if is_us else status["now_ist"]
        print("%s %s (%s %s, %s)." % (mkt, "OPEN" if status["open"] else "closed", now_str,
                                        tz_word, status["reason"]))
        for r in rings:
            print("ALERT " + r["message"])
        if not rings:
            cur_fn = lambda s: "$" if not (s.endswith(".NS") or s.endswith(".BO")) else "₹"
            print("Nothing hit. %d rule(s) watched today%s." % (
                len(watched), (", " + ", ".join("%s %s%.2f" % (s, cur_fn(s), quotes[s]["price"]) for s in syms if s in quotes))
                if quotes else ""))
        for e in errors:
            print("error: " + e)
        if rings:
            print("An alert is information, not an order. Check your position and manage exits accordingly.")
    return 0 if rings else 1


def cmd_add(a):
    doc = _read(a.rules, {"rules": []})
    doc.setdefault("rules", [])
    if a.from_plan:
        plan = _read(a.plan, None)
        if plan is None:
            raise AlertError("no plan at %s (run trader-exit first)" % a.plan)
        cfg = _read(a.config, {}) if a.config else {}
        new = rules_from_plan(plan, (cfg or {}).get("flat_by"))
        ids = {r["id"] for r in new}
        doc["rules"] = [r for r in doc["rules"] if r.get("id") not in ids] + new
        state = _read(a.state, {})
        if state.get("rung"):
            for i in ids:
                state["rung"].pop(i, None)
            _write(a.state, state)
        _write(a.rules, doc)
        for r in new:
            print("Added %s: %s" % (r["id"], json.dumps(r)))
        print("Run alerts.py check while the trade is open. Free quotes are delayed.")
        return 0
    if a.opening_bell:
        rule = {"id": a.id or "opening-bell", "type": "opening_bell"}
    elif a.at:
        if _hhmm(a.at) is None:
            raise AlertError("--at wants an IST time like 15:15")
        rule = {"id": a.id or ("time-%s" % a.at.replace(":", "")), "type": "time", "at": a.at,
                "label": a.label or "time alert"}
        if a.symbol:
            rule["symbol"] = a.symbol.upper()
        if a.before_close:
            rule["before_close"] = True
    else:
        if not a.symbol:
            raise AlertError("--symbol is required (or --from-plan, --at, --opening-bell)")
        sym = a.symbol.upper()
        if a.above is not None:
            rule = {"type": "above", "price": a.above}
        elif a.below is not None:
            rule = {"type": "below", "price": a.below}
        elif a.stop is not None:
            rule = {"type": "stop", "price": a.stop}
        elif a.target is not None:
            rule = {"type": "target", "price": a.target}
        elif a.pct is not None:
            rule = {"type": "pct_move", "pct": a.pct}
            if a.direction:
                rule["direction"] = a.direction
        else:
            raise AlertError("give one of --above, --below, --stop, --target or --pct")
        val = rule.get("price", rule.get("pct"))
        rule = dict({"id": a.id or ("%s-%s-%s" % (sym.lower(), rule["type"], _n(val)))}, symbol=sym, **rule)
        if a.rearm:
            rule["rearm"] = True
        if a.note:
            rule["note"] = a.note
    if a.date:
        rule["date"] = a.date
    validate(rule)
    if any(r.get("id") == rule["id"] for r in doc["rules"]):
        raise AlertError("a rule with id %s already exists" % rule["id"])
    doc["rules"].append(rule)
    _write(a.rules, doc)
    print("Added %s: %s" % (rule["id"], json.dumps(rule)))
    return 0


def cmd_list(a):
    doc = _read(a.rules, {"rules": []})
    state = _read(a.state, {})
    if not doc.get("rules"):
        print("No rules yet.")
        return 1
    for r in doc["rules"]:
        rid = r.get("id")
        rung = state.get("rung", {}).get(rid)
        print("%-24s %s%s" % (rid, json.dumps({k: v for k, v in r.items() if k != "id"}),
                              ("  (rang %s)" % rung) if rung else ""))
    return 0


def cmd_reset(a):
    state = _read(a.state, {})
    if a.id not in state.get("rung", {}):
        print("%s has not rung (nothing to reset)." % a.id)
        return 1
    del state["rung"][a.id]
    _write(a.state, state)
    print("Re-armed %s." % a.id)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Price alerts and the NSE clock (trader-alerts).")
    sub = ap.add_subparsers(dest="cmd")

    def net(p):
        p.add_argument("--source", default="yahoo", choices=["yahoo", "stooq"])
        p.add_argument("--offline", metavar="DIR", help="read <source>-<TICKER>.json|csv files, no network")
        p.add_argument("--now", help="pretend it is this time (no offset = IST)")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("clock", help="is the NSE open?")
    p.add_argument("--now")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("quote", help="latest delayed quotes")
    net(p)
    p.add_argument("symbols", nargs="*")
    p.add_argument("--holdings", action="store_true", help="add tickers from your portfolio and plan")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--write", nargs="?", const="trader/prices.json", metavar="PATH",
                   help="save to prices.json (default trader/prices.json)")
    p = sub.add_parser("check", help="check every rule, ring what hit")
    net(p)
    p.add_argument("--rules", default="trader/alerts.json")
    p.add_argument("--state", default="trader/state/alerts.json")
    p.add_argument("--prices-out", default="trader/prices.json")
    p.add_argument("--no-write", action="store_true")
    p = sub.add_parser("add", help="add a rule")
    p.add_argument("--rules", default="trader/alerts.json")
    p.add_argument("--state", default="trader/state/alerts.json")
    p.add_argument("--symbol")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--above", type=float)
    g.add_argument("--below", type=float)
    g.add_argument("--stop", type=float)
    g.add_argument("--target", type=float)
    g.add_argument("--pct", type=float)
    g.add_argument("--opening-bell", action="store_true")
    g.add_argument("--at", metavar="HH:MM", help="a time alert, IST (e.g. 15:15)")
    g.add_argument("--from-plan", action="store_true", help="stop, target and flat-by-close from the plan")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--config", default="trader/config.json", help="flat_by when the plan has none")
    p.add_argument("--label", help="what a time alert says, e.g. \"flat by close\"")
    p.add_argument("--before-close", action="store_true",
                   help="a time alert before close")
    p.add_argument("--date", help="watch this rule on this date only (YYYY-MM-DD)")
    p.add_argument("--direction", choices=["up", "down"])
    p.add_argument("--rearm", action="store_true")
    p.add_argument("--note")
    p.add_argument("--id")
    p = sub.add_parser("list", help="list the rules")
    p.add_argument("--rules", default="trader/alerts.json")
    p.add_argument("--state", default="trader/state/alerts.json")
    p = sub.add_parser("reset", help="re-arm a rule that rang")
    p.add_argument("id")
    p.add_argument("--state", default="trader/state/alerts.json")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"clock": cmd_clock, "quote": cmd_quote, "check": cmd_check, "add": cmd_add,
                "list": cmd_list, "reset": cmd_reset}[a.cmd](a)
    except AlertError as e:
        print("alerts.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
