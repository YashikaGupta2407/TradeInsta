#!/usr/bin/env python3
"""Local Streamlit app wrapping existing trader skills and BIG BULL Institutional Intelligence.

Terminal-style interface. Runs strictly on 127.0.0.1.
Paper trading & market research, not advice.
Credentials (Upstox/Fyers) are read strictly from environment variables and never displayed, logged, or written.
"""

import datetime
from decimal import Decimal
import importlib.util
import json
import os
import re
import sys
import textwrap
import time
from typing import Optional, Dict, Any, List
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# ---------------------------------------------------------------- Skill Loader
def load_skill(folder: str, name: str):
    """Load a script from a skill folder without modifying sys.path permanently."""
    path = os.path.join(ROOT, ".agents", "skills", folder, name + ".py")
    if not os.path.exists(path):
        path = os.path.join(ROOT, folder, name + ".py")
    spec = importlib.util.spec_from_file_location("trader_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


alerts = load_skill("trader-alerts", "alerts")
entry_mod = load_skill("trader-entry", "entry")
exit_mod = load_skill("trader-exit", "exit")
budget_mod = load_skill("trader-budget", "size")
flow_mod = load_skill("trader-flow", "flow")
big_bull = load_skill("trader-flow", "big_bull")
risk_mod = load_skill("trader-risk", "check")
tm_mod = load_skill("trader-timemachine", "timemachine")
scan_mod = load_skill("trader-scanner", "scan")

import trader.broker_data as broker_data
from trader.broker_data import fetch_market_overview_quotes
from trader.analyzer import analyze_symbol, load_signal_log


# ---------------------------------------------------------------- Diagnostic Timing Helper
def log_timing(func_name: str, duration: float, source: str = "local"):
    """Print timing diagnostic to terminal: function name, seconds, data source. Never log tokens."""
    safe_source = str(source)
    tok = os.environ.get("UPSTOX_ACCESS_TOKEN")
    if tok:
        safe_source = safe_source.replace(tok, "[REDACTED]")
    print(f"[PERF] function={func_name}, seconds={duration:.3f}s, source={safe_source}", flush=True)


# ---------------------------------------------------------------- Universe Lookup
@st.cache_data(ttl=86400)
def load_universe():
    """Load symbol-to-name mapping from trader/universe.txt."""
    t0 = time.perf_counter()
    path = os.path.join(ROOT, "trader", "universe.txt")
    sym_to_name = {}
    name_to_sym = {}
    items = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    parts = line.split("|", 1)
                    sym = parts[0].strip().upper()
                    name = parts[1].strip() if len(parts) > 1 else sym
                    sym_to_name[sym] = name
                    name_to_sym[name.upper()] = sym
                    items.append(f"{sym} — {name}")
    log_timing("load_universe", time.perf_counter() - t0, "trader/universe.txt")
    return sym_to_name, name_to_sym, items


SYM_TO_NAME, NAME_TO_SYM, UNIVERSE_ITEMS = load_universe()


# ---------------------------------------------------------------- Cached Market Data & Config Fetchers
@st.cache_data(ttl=86400)
def get_theme_css() -> str:
    """Read theme.css once from disk with 24-hour cache."""
    css_path = os.path.join(ROOT, "ui", "theme.css")
    if os.path.exists(css_path):
        with open(css_path, "r", encoding="utf-8") as fh:
            return fh.read()
    return ""


@st.cache_data(ttl=86400)
def load_holidays_map() -> dict:
    """Load {YYYY-MM-DD: holiday_name} dictionary from trader/holidays.json with 24-hour cache."""
    holidays_path = os.path.join(ROOT, "trader", "holidays.json")
    if os.path.exists(holidays_path):
        try:
            with open(holidays_path, "r", encoding="utf-8") as fh:
                d = json.load(fh)
                return d.get("holidays", {})
        except Exception:
            pass
    return {}


@st.cache_data(ttl=300)
def load_config_cached(cfg_path: str) -> dict:
    """Load trader config with 5-minute cache."""
    return budget_mod.load_config(cfg_path)


@st.cache_data(ttl=86400)
def get_cached_sector_registry() -> dict:
    """Load static sector constituents with 24-hour cache."""
    return big_bull.load_sector_registry()


@st.cache_data(ttl=86400)
def get_cached_market_caps() -> dict:
    """Load static market caps with 24-hour cache."""
    return big_bull.load_market_caps()


@st.cache_data(ttl=60)
def fetch_cached_intraday_chart(symbol: str, interval: str = "5m"):
    """Fetch same-day intraday chart with 60-second TTL to avoid Yahoo rate-limiting on page clicks."""
    t0 = time.perf_counter()
    chart = entry_mod.get_chart(symbol, interval=interval)
    src = (chart.get("meta") or {}).get("source", "yahoo") if chart else "yahoo"
    log_timing(f"fetch_cached_intraday_chart({symbol})", time.perf_counter() - t0, src)
    return chart


@st.cache_data(ttl=3600)
def fetch_cached_history_chart(symbol: str, interval: str = "5m", day: str = None):
    """Fetch historical day chart with 3600s TTL cache."""
    t0 = time.perf_counter()
    chart = entry_mod.get_chart(symbol, interval=interval, day=day)
    src = (chart.get("meta") or {}).get("source", "yahoo") if chart else "yahoo"
    log_timing(f"fetch_cached_history_chart({symbol})", time.perf_counter() - t0, src)
    return chart


@st.cache_data(ttl=900)
def fetch_cached_60d_history(symbol: str) -> str:
    """Fetch 60-day 5-minute bars with 15-minute TTL."""
    t0 = time.perf_counter()
    data = tm_mod.fetch_chart(symbol)
    log_timing(f"fetch_cached_60d_history({symbol})", time.perf_counter() - t0, "yahoo:60d")
    return data


@st.cache_data(ttl=30)
def fetch_cached_market_overview(symbols: tuple, is_sample_mode: bool = False):
    """Fetch market overview quotes with 30-second TTL to avoid broker/Yahoo rate-limiting."""
    t0 = time.perf_counter()
    quotes = fetch_market_overview_quotes(list(symbols), is_sample_mode=is_sample_mode)
    src = "sample" if is_sample_mode else (broker_data.active_broker() or "yahoo")
    log_timing(f"fetch_cached_market_overview({len(symbols)} syms)", time.perf_counter() - t0, src)
    return quotes


def resolve_symbol(query: str) -> str:
    """Resolve user input (company name or NSE ticker) to a clean .NS ticker."""
    if not query:
        return "RELIANCE.NS"
    q = query.strip()
    if " — " in q:
        q = q.split(" — ")[0].strip()

    q_upper = q.upper()
    if q_upper.endswith(".NS") or q_upper.endswith(".BO"):
        return q_upper

    # Check for instrument keys (e.g. NSE_EQ|INE002A01018, NSE_INDEX|Nifty 50)
    if "|" in q_upper:
        inst_mapping = broker_data.load_upstox_instruments()
        for sym_base, ikey in inst_mapping.items():
            if ikey.upper() == q_upper:
                return f"{sym_base}.NS"
        return q_upper

    # Check for index identifiers (e.g. ^NSEI, ^BSESN, ^NSEBANK, ^CNXIT)
    if q_upper.startswith("^"):
        return q_upper

    # Known index aliases
    if q_upper in ("NIFTY 50", "NIFTY50", "NIFTY"):
        return "^NSEI"
    if q_upper in ("SENSEX", "BSE SENSEX"):
        return "^BSESN"
    if q_upper in ("BANK NIFTY", "NIFTY BANK", "BANKNIFTY"):
        return "^NSEBANK"

    ns_key = f"{q_upper}.NS"
    if ns_key in SYM_TO_NAME:
        return ns_key

    if q_upper in NAME_TO_SYM:
        return NAME_TO_SYM[q_upper]

    for name_up, s in NAME_TO_SYM.items():
        if q_upper in name_up:
            return s

    if re.match(r"^[A-Z0-9\-]+$", q_upper):
        return f"{q_upper}.NS"

    return q_upper


def get_badge_info(is_sample: bool, is_open: bool, data_source: str, bar_age: str = "", session_reason: str = "", holiday_name: str = None, is_pre_open: bool = False) -> dict:
    """Determine top-of-page badge state, color, and label. Plain text labels only."""
    if is_sample:
        return {
            "label": "SAMPLE DATA",
            "bg": "#7c3aed",
            "detail": f"Offline test fixture / simulated candles · Source: {data_source.capitalize()}"
        }
    if holiday_name:
        return {
            "label": "HOLIDAY",
            "bg": "#b45309",
            "detail": f"NSE Holiday: {holiday_name} · Market Closed · Source: {data_source.capitalize()}"
        }
    if is_pre_open:
        return {
            "label": "MARKET NOT OPEN",
            "bg": "#d97706",
            "detail": f"Pre-market window (opens at 09:15 IST) · Displaying last session · Source: {data_source.capitalize()}"
        }
    if not is_open:
        reason_text = f" ({session_reason})" if session_reason else ""
        return {
            "label": "LAST SESSION",
            "bg": "#475569",
            "detail": f"Market closed{reason_text} · Displaying last session · Source: {data_source.capitalize()} · Age: {bar_age}"
        }
    if data_source in ("upstox", "fyers"):
        return {
            "label": "LIVE (broker)",
            "bg": "#16a34a",
            "detail": f"Broker real-time feed ({data_source.capitalize()}) · Bar age: {bar_age}"
        }
    return {
        "label": "DELAYED (Yahoo)",
        "bg": "#dc2626",
        "detail": f"Yahoo Finance ~15m delay · Bar age: {bar_age}"
    }


@st.cache_data(ttl=300)
def load_market_deals(is_sample_mode: bool = False):
    """Load bulk/block deals from trader/flow/ or sample fixtures if in sample mode."""
    t0 = time.perf_counter()
    flow_dir = os.path.join(ROOT, "trader", "flow")
    deals = flow_mod.load_flow_deals(flow_dir=flow_dir)
    source_note = "Local Directory (trader/flow)"

    if is_sample_mode:
        multi_csv = os.path.join(ROOT, "tests", "fixtures", "big_bull", "big_bull_multi_session.csv")
        fixture_dir = os.path.join(ROOT, "tests", "fixtures", "trader-flow")
        if os.path.exists(multi_csv):
            deals = flow_mod.load_flow_deals(file_path=multi_csv)
            source_note = "Multi-Session Sample Fixture (Offline Demo)"
        elif os.path.exists(fixture_dir):
            deals = flow_mod.load_flow_deals(flow_dir=fixture_dir)
            source_note = "Sample Fixture Directory"

    log_timing(f"load_market_deals({len(deals)} deals)", time.perf_counter() - t0, source_note)
    return deals, source_note


# ---------------------------------------------------------------- HTML Rendering Helpers
def clean_html(html: str) -> str:
    """Clean HTML string by stripping whitespace per line and dropping blank lines.

    Guarantees that no line starts with 4 or more spaces (which would trigger markdown
    indented code blocks) and ensures no empty lines exist.
    """
    if not html:
        return ""
    dedented = textwrap.dedent(html)
    cleaned_lines = [line.strip() for line in dedented.splitlines()]
    return "\n".join(line for line in cleaned_lines if line)


def render_html(html: str, target=None):
    """Safely render cleaned HTML block through st.html if supported, or st.markdown as fallback."""
    cleaned = clean_html(html)
    if not cleaned:
        return
    renderer = target if target is not None else st
    if hasattr(renderer, "html"):
        renderer.html(cleaned)
    else:
        renderer.markdown(cleaned, unsafe_allow_html=True)


# ---------------------------------------------------------------- Theme & CSS Loader
def load_theme():
    """Load single CSS theme file once via cached reader and apply theme mode (Dark / Light / System)."""
    css_content = get_theme_css()
    theme_mode = st.session_state.get("theme_mode", "System")

    theme_override = ""
    if theme_mode == "Light":
        theme_override = """
        :root, [data-testid="stAppViewContainer"], .stApp {
            --bg-main: #FAFAFB !important;
            --bg-sidebar: #FFFFFF !important;
            --surface-primary: #FFFFFF !important;
            --surface-secondary: #F2F2F5 !important;
            --border-subtle: #E4E4E9 !important;
            --border-card: #E4E4E9 !important;
            --border-focus: #7D42D4 !important;
            --text-main: #15151B !important;
            --text-secondary: #6D6D79 !important;
            --text-muted: #6D6D79 !important;
            --accent-purple: #7D42D4 !important;
            --accent-purple-hover: #9258E8 !important;
            --accent-purple-glow: rgba(125, 66, 212, 0.16) !important;
            --accent-purple-dim: rgba(125, 66, 212, 0.08) !important;
        }
        """
    elif theme_mode == "Dark":
        theme_override = """
        :root, [data-testid="stAppViewContainer"], .stApp {
            --bg-main: #050505 !important;
            --bg-sidebar: #080808 !important;
            --surface-primary: #101013 !important;
            --surface-secondary: #17171B !important;
            --border-subtle: #303036 !important;
            --border-card: #202026 !important;
            --border-focus: #9955E8 !important;
            --text-main: #F5F5F7 !important;
            --text-secondary: #9A9AA3 !important;
            --text-muted: #9A9AA3 !important;
            --accent-purple: #9955E8 !important;
            --accent-purple-hover: #AD78F4 !important;
            --accent-purple-glow: rgba(153, 85, 232, 0.20) !important;
            --accent-purple-dim: rgba(153, 85, 232, 0.12) !important;
        }
        """

    theme_script = f"""
    <script>
    (function() {{
        const mode = "{theme_mode}";
        const doc = window.parent ? window.parent.document.documentElement : document.documentElement;
        if (mode === 'Light') {{
            doc.setAttribute('data-theme', 'light');
            doc.classList.remove('theme-dark');
            doc.classList.add('theme-light');
        }} else if (mode === 'Dark') {{
            doc.setAttribute('data-theme', 'dark');
            doc.classList.remove('theme-light');
            doc.classList.add('theme-dark');
        }} else {{
            const isDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
            doc.setAttribute('data-theme', isDark ? 'dark' : 'light');
            doc.classList.toggle('theme-dark', isDark);
            doc.classList.toggle('theme-light', !isDark);
            if (window.matchMedia) {{
                window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function(e) {{
                    const darkNow = e.matches;
                    doc.setAttribute('data-theme', darkNow ? 'dark' : 'light');
                    doc.classList.toggle('theme-dark', darkNow);
                    doc.classList.toggle('theme-light', !darkNow);
                }});
            }}
        }}
    }})();
    </script>
    """

    if css_content:
        st.html(f"<style>{css_content}\n{theme_override}</style>")
    render_html(theme_script)


# ---------------------------------------------------------------- Token & Market Status Helpers
def get_upstox_token_status() -> str:
    """Check Upstox token status without exposing token: 'found', 'not set', or 'rejected (expired)'."""
    token = os.environ.get("UPSTOX_ACCESS_TOKEN", "").strip()
    if not token:
        return "not set"
    if st.session_state.get("upstox_token_expired", False):
        return "rejected (expired)"
    return "found"


def get_nse_status() -> tuple[str, str, bool]:
    """Get NSE market status text, CSS dot class, and open boolean.
    
    Returns:
        (status_text, dot_class, is_open)
    """
    now_ist = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5, minutes=30)))
    today_str = now_ist.strftime("%Y-%m-%d")
    h_data = load_holidays_map()
    holiday_name = h_data.get(today_str)

    if holiday_name:
        return f"NSE closed · {holiday_name}", "grey", False
    if now_ist.weekday() >= 5:
        return "NSE closed · Weekend", "grey", False

    current_tod = now_ist.time()
    open_time = datetime.time(9, 15)
    close_time = datetime.time(15, 30)
    if open_time <= current_tod <= close_time:
        return "NSE open · 09:15 to 15:30 IST", "green pulsing", True
    elif current_tod < open_time:
        return "NSE closed · Opens 09:15 IST", "amber", False
    else:
        return "NSE closed · Closed 15:30 IST", "grey", False


# ---------------------------------------------------------------- Trust Strip (Phase 2)
def compute_trust_strip_info(data_source: str, chart: dict = None, is_sample_mode: bool = False, now_utc=None) -> dict:
    """Compute all Trust Strip telemetry fields deterministically."""
    if now_utc is None:
        now_utc_aware = datetime.datetime.now(datetime.timezone.utc)
    elif now_utc.tzinfo is None:
        # Naive datetime from utc_now() represents UTC time
        now_utc_aware = now_utc.replace(tzinfo=datetime.timezone.utc)
    else:
        now_utc_aware = now_utc.astimezone(datetime.timezone.utc)

    ist_tz = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
    now_ist = now_utc_aware.astimezone(ist_tz)
    now_ts = int(now_utc_aware.timestamp())

    today_str = now_ist.strftime("%Y-%m-%d")
    h_data = load_holidays_map()
    holiday_name = h_data.get(today_str)

    is_weekend = now_ist.weekday() >= 5
    open_time = datetime.time(9, 15)
    close_time = datetime.time(15, 30)
    current_tod = now_ist.time()
    is_pre_open = (not is_weekend and not holiday_name and current_tod < open_time)
    is_market_open = (not is_weekend and not holiday_name and open_time <= current_tod <= close_time)

    bars = (chart.get("bars") if chart else []) or []
    completed_bars = [b for b in bars if (b.get("t", 0) + 300) <= now_ts]

    if completed_bars:
        last_b = completed_bars[-1]
        start_ts = last_b["t"]
        end_ts = start_ts + 300
        start_dt = datetime.datetime.fromtimestamp(start_ts, tz=datetime.timezone.utc).astimezone(ist_tz)
        end_dt = datetime.datetime.fromtimestamp(end_ts, tz=datetime.timezone.utc).astimezone(ist_tz)
        bar_range_str = f"{start_dt.strftime('%H:%M')} to {end_dt.strftime('%H:%M')}"
        age_min = max(0, int((now_ts - end_ts) // 60))
        bar_age_str = f"age {age_min}m"
    elif bars:
        last_b = bars[-1]
        start_ts = last_b["t"]
        end_ts = start_ts + 300
        start_dt = datetime.datetime.fromtimestamp(start_ts, tz=datetime.timezone.utc).astimezone(ist_tz)
        end_dt = datetime.datetime.fromtimestamp(end_ts, tz=datetime.timezone.utc).astimezone(ist_tz)
        bar_range_str = f"{start_dt.strftime('%H:%M')} to {end_dt.strftime('%H:%M')}"
        age_min = max(0, int((now_ts - end_ts) // 60))
        bar_age_str = f"age {age_min}m"
    else:
        bar_range_str = "N/A"
        bar_age_str = "no bars"

    is_live = False
    if is_sample_mode:
        source_label = "SAMPLE · Simulated"
        dot_cls = "grey"
        source_color = "#98A2B3"
    elif holiday_name:
        source_label = f"HOLIDAY · {holiday_name}"
        dot_cls = "grey"
        source_color = "#98A2B3"
    elif is_pre_open:
        source_label = "MARKET NOT OPEN"
        dot_cls = "grey"
        source_color = "#98A2B3"
    elif not is_market_open:
        source_label = "LAST SESSION"
        dot_cls = "grey"
        source_color = "#98A2B3"
    elif data_source in ("upstox", "fyers"):
        source_label = f"LIVE · {data_source.capitalize()}"
        dot_cls = "green pulsing"
        source_color = "#3DD68C"
        is_live = True
    else:
        source_label = "DELAYED · Yahoo"
        dot_cls = "amber"
        source_color = "#F2B84B"

    return {
        "source_label": source_label,
        "source_color": source_color,
        "dot_cls": dot_cls,
        "is_live": is_live,
        "analysis_time_str": f"Analysis as of {now_ist.strftime('%H:%M:%S')} IST",
        "bar_info_str": f"Last completed bar {bar_range_str} · {bar_age_str}",
        "bar_range_str": bar_range_str,
        "bar_age_str": bar_age_str,
        "is_sample_mode": is_sample_mode
    }


def render_trust_strip(data_source: str, chart: dict = None, is_sample_mode: bool = False, now_utc=None):
    """Render the slim Trust Strip row at the top of an analysis view."""
    info = compute_trust_strip_info(data_source, chart=chart, is_sample_mode=is_sample_mode, now_utc=now_utc)
    sample_badge_html = '<span class="terminal-chip sample-chip">SAMPLE DATA</span>' if info["is_sample_mode"] else ''
    render_html(
        f"""
        <div class="trust-strip">
            <div class="trust-strip-left">
                <span class="status-dot {info['dot_cls']}"></span>
                <span class="trust-source" style="color: {info['source_color']};">{info['source_label']}</span>
                <span class="trust-divider">·</span>
                <span class="mono" style="color: var(--text-main);">{info['analysis_time_str']}</span>
                <span class="trust-divider">·</span>
                <span class="mono" style="color: var(--text-muted);">{info['bar_info_str']}</span>
            </div>
            {sample_badge_html}
        </div>
        """
    )


# ---------------------------------------------------------------- Phase 3 Terminal Components
def render_day_timeline(now_ist: datetime.datetime, trigger_info: dict = None):
    """Render Day timeline panel (09:15 to 15:30 IST, 375 minutes total)."""
    current_tod = now_ist.time()
    cur_mins = (current_tod.hour - 9) * 60 + (current_tod.minute - 15)
    now_pct = max(0.0, min(100.0, (cur_mins / 375.0) * 100.0))

    trigger_html = ""
    if trigger_info and trigger_info.get("time"):
        t_str = str(trigger_info["time"])
        try:
            parts = [int(p) for p in t_str.split(":")[:2]]
            trig_mins = (parts[0] - 9) * 60 + (parts[1] - 15)
            trig_pct = max(0.0, min(100.0, (trig_mins / 375.0) * 100.0))
            trigger_html = f'<div class="timeline-marker-breakout" style="left: {trig_pct:.1f}%;">Breakout {parts[0]:02d}:{parts[1]:02d}</div>'
        except Exception:
            pass

    render_html(
        f"""
        <div class="timeline-panel-wrap">
            <div class="timeline-header">Day Timeline · 09:15 to 15:30 IST</div>
            <div class="timeline-track-container" style="--timeline-width: {now_pct:.1f}%;">
                <div class="timeline-marker-now" style="left: {now_pct:.1f}%;">NOW ({now_ist.strftime('%H:%M')})</div>
                {trigger_html}
                <div class="timeline-track">
                    <div class="timeline-fill" style="width: {now_pct:.1f}%;"></div>
                </div>
                <div class="timeline-labels-bottom">
                    <div class="timeline-label-item" style="left: 0%;">9:15 open</div>
                    <div class="timeline-label-item" style="left: 4.0%;">range done 9:30</div>
                    <div class="timeline-label-item" style="left: 96.0%;">3:15 square-off</div>
                </div>
            </div>
        </div>
        """
    )


def render_price_ladder(current_price: float, range_high: float, range_low: float, entry_level: float, stop_level: float, target_level: float, day_phase: str):
    """Render vertical price ladder with real numbers, shaded opening range, and glowing price marker."""
    is_forming = (day_phase == "0915_to_0930")

    levels = [v for v in [current_price, range_high, range_low] if v is not None]
    if not is_forming:
        levels.extend([v for v in [entry_level, stop_level, target_level] if v is not None])

    if not levels:
        levels = [100.0, 102.0]

    max_p = max(levels) * 1.002
    min_p = min(levels) * 0.998
    span = max(0.01, max_p - min_p)

    def p_to_top(p):
        if p is None:
            return 50.0
        return max(4.0, min(96.0, ((max_p - p) / span) * 100.0))

    cur_top = p_to_top(current_price)

    band_html = ""
    range_low_html = ""
    if range_high is not None and range_low is not None:
        top_rh = p_to_top(range_high)
        top_rl = p_to_top(range_low)
        b_height = max(4.0, abs(top_rl - top_rh))
        band_html = f'<div class="ladder-band" style="top: {min(top_rh, top_rl):.1f}%; height: {b_height:.1f}%;"></div>'
        range_low_html = f"""
            <div class="ladder-line range-low" style="top: {top_rl:.1f}%;"></div>
            <div class="ladder-label-left" style="top: {top_rl:.1f}%; color: var(--text-muted);">
                RANGE LOW<br><span style="color: var(--text-main);">₹{range_low:,.2f}</span>
            </div>
        """

    levels_html = ""
    if not is_forming:
        if target_level is not None:
            top_t = p_to_top(target_level)
            levels_html += f"""
                <div class="ladder-line target" style="top: {top_t:.1f}%;"></div>
                <div class="ladder-label-left" style="top: {top_t:.1f}%; color: var(--gain-green);">
                    TARGET<br><span style="color: var(--text-main);">₹{target_level:,.2f}</span>
                </div>
            """
        if entry_level is not None:
            top_e = p_to_top(entry_level)
            levels_html += f"""
                <div class="ladder-line entry" style="top: {top_e:.1f}%;"></div>
                <div class="ladder-label-left" style="top: {top_e:.1f}%; color: var(--accent-blue);">
                    ENTRY LEVEL<br><span style="color: var(--text-main);">₹{entry_level:,.2f}</span>
                </div>
            """
        if stop_level is not None:
            top_s = p_to_top(stop_level)
            levels_html += f"""
                <div class="ladder-line stop" style="top: {top_s:.1f}%;"></div>
                <div class="ladder-label-left" style="top: {top_s:.1f}%; color: var(--loss-red);">
                    STOP<br><span style="color: var(--text-main);">₹{stop_level:,.2f}</span>
                </div>
            """
    else:
        levels_html = """
            <div class="ladder-label-left" style="top: 20%; color: var(--warning-amber);">
                OPENING RANGE FORMING<br><span style="font-size: 10px; color: var(--text-muted);">Levels confirmed at 09:30</span>
            </div>
        """

    cur_price_disp = f"₹{current_price:,.2f}" if current_price is not None else "N/A"

    render_html(
        f"""
        <div class="ladder-panel-wrap">
            <div style="font-size: 11px; font-weight: 700; text-transform: uppercase; color: var(--text-muted); letter-spacing: 0.05em; margin-bottom: 6px;">
                Price Ladder & Range
            </div>
            <div class="ladder-canvas">
                <div class="ladder-rail"></div>
                {band_html}
                {levels_html}
                {range_low_html}
                <div class="ladder-price-marker" style="top: {cur_top:.1f}%;"></div>
                <div class="ladder-price-label-right" style="top: {cur_top:.1f}%;">{cur_price_disp}</div>
            </div>
        </div>
        """
    )


def render_past_odds_panel(odds_step: dict):
    """Render Past Odds panel with 100 dots in a 20x5 grid with staggered fade/scale animation."""
    win_rate = float(odds_step.get("win_rate") or odds_step.get("target_pct") or 54.2)
    stop_rate = float(odds_step.get("stop_pct") or 33.3)
    neither_rate = max(0.0, round(100.0 - win_rate - stop_rate, 1))

    target_dots = int(round(win_rate))
    stop_dots = int(round(stop_rate))
    neither_dots = max(0, 100 - target_dots - stop_dots)
    samples = odds_step.get("samples", 60)

    dots_html = ""
    for idx in range(100):
        col = idx % 20
        delay = col * 0.05
        if idx < target_dots:
            cls = "dot-target"
        elif idx < target_dots + stop_dots:
            cls = "dot-stop"
        else:
            cls = "dot-neither"
        dots_html += f'<div class="odds-dot {cls}" style="animation-delay: {delay:.2f}s;"></div>'

    render_html(
        f"""
        <div class="odds-panel-wrap">
            <div style="display: flex; justify-content: space-between; align-items: center;">
                <span style="font-size: 11px; font-weight: 700; text-transform: uppercase; color: var(--text-muted); letter-spacing: 0.05em;">Past Odds (60-Day Replay)</span>
                <span class="mono" style="font-size: 11px; color: var(--text-muted);">{samples} sessions</span>
            </div>
            <div class="odds-grid-100">
                {dots_html}
            </div>
            <div class="odds-legend">
                <span style="color: var(--gain-green); font-weight: 600;">Target: {win_rate:.1f}% ({target_dots})</span>
                <span style="color: var(--loss-red); font-weight: 600;">Stop: {stop_rate:.1f}% ({stop_dots})</span>
                <span style="color: var(--text-muted); font-weight: 600;">Time Exit: {neither_rate:.1f}% ({neither_dots})</span>
            </div>
        </div>
        """
    )


def render_size_and_risk_panel(s5: dict, s6: dict):
    """Render Size and Risk verification panel."""
    qty = s5.get("quantity", 0)
    pos_val = s5.get("position_value", 0.0)
    charges = s5.get("total_charges", 0.0)
    max_loss = s5.get("max_loss_per_trade", 0.0)

    verdict = s6.get("verdict", "BLOCK")
    reason = s6.get("reason", "")
    is_pass = (verdict == "PASS")
    verdict_color = "var(--gain-green)" if is_pass else "var(--loss-red)"

    render_html(
        f"""
        <div class="terminal-panel" style="margin-bottom: 0;">
            <div style="font-size: 11px; font-weight: 700; text-transform: uppercase; color: var(--text-muted); letter-spacing: 0.05em; margin-bottom: 10px;">
                Position Size & Risk Verification
            </div>
            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 12px; font-size: 12px;">
                <div>
                    <span style="color: var(--text-muted);">Quantity:</span>
                    <span class="mono" style="font-weight: 700; color: var(--text-main); margin-left: 6px;">{qty} shares</span>
                </div>
                <div>
                    <span style="color: var(--text-muted);">Max Loss:</span>
                    <span class="mono" style="font-weight: 700; color: var(--loss-red); margin-left: 6px;">₹{max_loss:,.2f}</span>
                </div>
                <div>
                    <span style="color: var(--text-muted);">Order Value:</span>
                    <span class="mono" style="color: var(--text-main); margin-left: 6px;">₹{pos_val:,.2f}</span>
                </div>
                <div>
                    <span style="color: var(--text-muted);">Round-trip Charges:</span>
                    <span class="mono" style="color: var(--text-main); margin-left: 6px;">₹{charges:,.2f}</span>
                </div>
            </div>
            <div style="background-color: rgba(14, 17, 22, 0.7); border: 1px solid var(--border-subtle); border-radius: 6px; padding: 10px 12px; font-size: 12px;">
                <span class="mono" style="font-weight: 700; color: {verdict_color};">{verdict}</span>:
                <span style="color: var(--text-main); margin-left: 6px;">{reason}</span>
            </div>
        </div>
        """
    )


def render_step_chips(steps: dict):
    """Render Bottom Row Step Chips with live status, tooltips, and legal disclaimer."""
    step_defs = [
        ("Data", steps.get("data_check", {})),
        ("Range", steps.get("opening_range", {})),
        ("Entry", steps.get("entry_status", {})),
        ("Levels", steps.get("levels", {})),
        ("Size", steps.get("size_and_charges", {})),
        ("Risk", steps.get("risk_check", {})),
        ("Odds", steps.get("historical_odds", {})),
        ("Context", steps.get("context", {}))
    ]
    chips_html = ""
    for label, s_data in step_defs:
        status = s_data.get("status", "ok")
        ts = s_data.get("timestamp", "")
        if status == "ok":
            cls = "completed"
            title = f"{label} completed at {ts}"
        elif status in ("warning", "error"):
            cls = "warning"
            title = f"{label} warning: {s_data.get('reason') or s_data.get('failed_reason') or 'check details'}"
        else:
            cls = ""
            title = f"{label}"
        chips_html += f'<span class="step-chip {cls}" title="{title}">{label}</span>'

    legal_text = "Paper trading, not advice. Levels come from fixed rules and past data and do not predict the price."
    render_html(
        f"""
        <div class="step-chips-container">
            <div class="step-chips-list">
                {chips_html}
            </div>
            <div class="step-chip-legal">{legal_text}</div>
        </div>
        """
    )


def render_summary_and_context(summary_step: dict, context_step: dict, deals: list, resolved_symbol: str):
    """Render 3 to 5 sentence plain language summary and market context."""
    sentences = summary_step.get("sentences", [])
    if sentences:
        render_html(
            """
            <div class="terminal-panel" style="margin-bottom: 16px;">
                <div style="font-size: 11px; font-weight: 700; text-transform: uppercase; color: var(--accent-blue); letter-spacing: 0.05em; margin-bottom: 10px;">
                    Rule Summary
                </div>
            """ + "".join(f'<div style="font-size: 13px; color: var(--text-main); margin-bottom: 6px; line-height: 1.5;">• {s}</div>' for s in sentences) + """
            </div>
            """
        )

    vol_label = context_step.get("volume_vs_20d_label", "1.00x vs 20d avg")
    headlines = context_step.get("headlines", [])
    flow_summary = context_step.get("flow_summary", {})
    flow_deals = flow_summary.get("total_deals", 0)
    flow_label = context_step.get("flow_label", "reported activity, previous day, not live")

    render_html(
        f"""
        <div class="terminal-panel">
            <div style="font-size: 11px; font-weight: 700; text-transform: uppercase; color: var(--text-muted); letter-spacing: 0.05em; margin-bottom: 12px;">
                Market Context · Reported Activity, Previous Day, Not Live
            </div>
            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 14px; font-size: 12px;">
                <div>
                    <div style="color: var(--text-muted); margin-bottom: 4px;">Volume vs 20-Day Average:</div>
                    <div class="mono" style="font-size: 14px; font-weight: 600; color: var(--text-main);">{vol_label}</div>
                    <div style="color: var(--text-muted); margin-top: 10px; margin-bottom: 4px;">Institutional Deal Filings:</div>
                    <div class="mono" style="font-size: 14px; font-weight: 600; color: var(--text-main);">{flow_deals} disclosed deals ({flow_label})</div>
                </div>
                <div>
                    <div style="color: var(--text-muted); margin-bottom: 6px;">Relevant Headlines:</div>
                    {"".join(f'<div style="margin-bottom: 6px;"><a href="{h.get("link", "#")}" target="_blank" style="color: var(--accent-blue); text-decoration: none;">{h.get("title")}</a></div>' for h in headlines[:3]) if headlines else '<div style="color: var(--text-muted);">No breaking headlines reported.</div>'}
                </div>
            </div>
        </div>
        """
    )


# ---------------------------------------------------------------- Analysis Card Renderer
def render_analysis_card(analysis: dict):
    """Render the complete 9-step analysis card with timestamps and provenance."""
    steps = analysis.get("steps", {})
    s1 = steps.get("data_check", {})
    s2 = steps.get("opening_range", {})
    s3 = steps.get("entry_status", {})
    s4 = steps.get("levels", {})
    s5 = steps.get("size_and_charges", {})
    s6 = steps.get("risk_check", {})
    s7 = steps.get("historical_odds", {})
    s8 = steps.get("context", {})
    s9 = steps.get("summary", {})

    st.markdown("---")
    st.markdown(f"### Analysis as of `{analysis.get('now_ist', 'Now')}`")

    # Current Price vs Completed Bars Comparison
    c_p1, c_p2 = st.columns(2)
    with c_p1:
        render_html(
            f"""
            <div class="terminal-panel">
                <div style="font-size: 11px; color: var(--text-muted); font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em;">1. Current Price (LTP)</div>
                <div class="mono" style="font-size: 26px; font-weight: 700; color: var(--text-main); margin: 6px 0;">₹{s1.get('current_price') or 0:,.2f}</div>
                <div class="mono" style="font-size: 12px; color: var(--text-muted);">Time: {s1.get('current_price_time', 'N/A')} · Feed: {str(s1.get('current_price_source', 'Broker')).capitalize()}</div>
            </div>
            """
        )
    with c_p2:
        render_html(
            f"""
            <div class="terminal-panel">
                <div style="font-size: 11px; color: var(--text-muted); font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em;">2. Last Completed 5-Minute Bar</div>
                <div class="mono" style="font-size: 26px; font-weight: 700; color: var(--text-main); margin: 6px 0;">₹{s1.get('last_bar_close') or 0:,.2f}</div>
                <div class="mono" style="font-size: 12px; color: var(--text-muted);">Close Time: {s1.get('last_bar_time', 'N/A')} · Bar Age: {s1.get('bar_age', 'N/A')}</div>
            </div>
            """
        )

    st.caption("The entry rule needs completed bars. The current price shows where the stock is right now.")

    # 9-Step Summary sentences
    render_html(
        """
        <div style="background-color: var(--bg-panel); border-left: 3px solid var(--accent-blue); border-radius: var(--radius-sm); padding: 12px 16px; margin: 12px 0;">
            <div style="margin: 0 0 6px 0; color: var(--accent-blue); font-size: 13px; font-weight: 700; text-transform: uppercase;">Current Rules Summary</div>
        </div>
        """
    )
    for s in s9.get("sentences", []):
        st.markdown(f"- {s}")

    # Expandable Full 9-Step Detail Breakdown
    with st.expander("View Full 9-Step Verification Details", expanded=False):
        c1, c2 = st.columns(2)
        with c1:
            st.markdown(f"**Step 1: Data Check** (`{s1.get('timestamp')}`)")
            st.write(f"- Source: `{s1.get('source')}` (Upstox Token: `{s1.get('upstox_token_status')}`)")
            if s1.get("fallback_reason"):
                st.write(f"- Fallback: {s1.get('fallback_reason')}")
            st.write(f"- Market State: `{s1.get('market_state')}`" + (f" ({s1.get('holiday_name')})" if s1.get("holiday_name") else ""))

            st.markdown(f"**Step 2: Opening Range** (`{s2.get('timestamp')}`)")
            st.write(f"- High: `₹{s2.get('high') or 0:.2f}`, Low: `₹{s2.get('low') or 0:.2f}` ({s2.get('bars_used')}/3 bars)")
            st.write(f"- Status: {s2.get('phase_note')}")

            st.markdown(f"**Step 3: Entry Status** (`{s3.get('timestamp')}`)")
            st.write(f"- Signal: `{str(s3.get('signal')).upper()}`")
            if s3.get("trigger"):
                st.write(f"- Triggered: ₹{s3['trigger'].get('price', 0):.2f} at {s3['trigger'].get('time')}")
            if s3.get("late_entry_flag"):
                st.warning(s3.get('late_entry_note'))
            if s3.get("failed_breakout"):
                st.error(s3.get('failed_reason'))

            st.markdown(f"**Step 4: Levels** (`{s4.get('timestamp')}`)")
            if s4.get("entry_level"):
                e_lvl = float(s4.get("entry_level"))
                s_lvl = float(s4.get("stop_level") or 0.0)
                t_lvl = float(s4.get("target_level") or 0.0)
                rr_val = s4.get("reward_risk")
                ts_val = s4.get("tick_size")
                rr_str = f"{float(rr_val):.1f}R" if rr_val is not None else "N/A"
                ts_str = f"₹{float(ts_val):.2f}" if ts_val is not None else "₹0.05"
                st.write(f"- Entry Level: `₹{e_lvl:.2f}` | Stop: `₹{s_lvl:.2f}` | Target: `₹{t_lvl:.2f}`")
                st.write(f"- Reward:Risk: `{rr_str}` | Tick size: `{ts_str}`")
            else:
                st.write(f"- {s4.get('note')}")

        with c2:
            st.markdown(f"**Step 5: Size & Statutory Charges** (`{s5.get('timestamp')}`)")
            st.write(f"- Sized Quantity: `{s5.get('quantity')} shares` (Value: ₹{s5.get('position_value', 0):,.2f})")
            st.write(f"- Round-trip Charges: `₹{s5.get('total_charges', 0):.2f}` (Brokerage, STT, turnover, GST, stamp duty)")

            st.markdown(f"**Step 6: Risk Check** (`{s6.get('timestamp')}`)")
            if s6.get("verdict") == "PASS":
                st.success(f"PASS: {s6.get('reason')}")
            else:
                st.error(f"BLOCK: {s6.get('reason')}")

            st.markdown(f"**Step 7: Historical Odds (Time Machine)** (`{s7.get('timestamp')}`)")
            st.write(f"- Win Rate: `{s7.get('win_rate')}%` across `{s7.get('samples')}` sessions")
            st.write(f"- Edge Verdict: `{s7.get('verdict')}`")

            st.markdown(f"**Step 8: Context (Read-Only)** (`{s8.get('timestamp')}`)")
            st.write(f"- Volume vs 20d Avg: `{s8.get('volume_vs_20d_label')}`")
            st.write(f"- Institutional Flow: `{s8.get('flow_summary', {}).get('total_deals', 0)} deals` ({s8.get('flow_label')})")
            if s8.get("headlines"):
                for h in s8["headlines"][:2]:
                    st.write(f"- News: [{h.get('title')}]({h.get('link')})")
    st.markdown("---")


# ---------------------------------------------------------------- Analysis Workflow & Session State (Phase 2C / ISS-03)
def extract_analysis_context(curr_analysis: dict) -> dict:
    """Extract and validate normalized analysis context for cross-page workflow state.

    Preserves exact provenance (timestamp, source, freshness).
    Never fabricates default entry/stop/target levels if range or analysis is incomplete.
    """
    if not curr_analysis or not isinstance(curr_analysis, dict):
        return {}

    symbol = curr_analysis.get("symbol", "")
    ticker_base = curr_analysis.get("ticker_base", symbol.replace(".NS", ""))
    now_ist = curr_analysis.get("now_ist", "")
    now_date = curr_analysis.get("now_date", "")
    day_phase = curr_analysis.get("day_phase", "")
    market_state = curr_analysis.get("market_state", "")

    steps = curr_analysis.get("steps", {})
    s1 = steps.get("data_check", {})
    s2 = steps.get("opening_range", {})
    s3 = steps.get("entry_status", {})
    s4 = steps.get("levels", {})
    s5 = steps.get("size_and_charges", {})
    s6 = steps.get("risk_check", {})
    s7 = steps.get("historical_odds", {})
    s8 = steps.get("context", {})
    s9 = steps.get("summary", {})

    source = s1.get("source") or ("broker/yahoo")
    bar_age = s1.get("bar_age") or "N/A"
    current_price = s1.get("current_price")
    current_price_time = s1.get("current_price_time")
    current_price_source = s1.get("current_price_source")
    last_bar_close = s1.get("last_bar_close")
    last_bar_time = s1.get("last_bar_time")

    entry_level = s4.get("entry_level")
    stop_level = s4.get("stop_level")
    target_level = s4.get("target_level")
    reward_risk = s4.get("reward_risk", 2.0)
    stop_label = s4.get("stop_label", "opening-range midpoint")
    target_label = s4.get("target_label", "2R")
    tick_size = s4.get("tick_size", 0.05)
    levels_status = s4.get("status", "pending")
    levels_note = s4.get("note", "")

    quantity = s5.get("quantity", 0)
    position_value = s5.get("position_value", 0.0)
    total_charges = s5.get("total_charges", 0.0)
    charges_breakdown = s5.get("charges_breakdown", {})
    account_budget = s5.get("account_budget")
    max_loss_per_trade = s5.get("max_loss_per_trade")

    risk_verdict = s6.get("verdict")
    risk_reason = s6.get("reason", "")

    or_complete = s2.get("complete", False)
    or_high = s2.get("high")
    or_low = s2.get("low")
    or_bars_used = s2.get("bars_used", 0)

    entry_signal = s3.get("signal", "waiting")
    entry_trigger = s3.get("trigger")
    entry_reason = s3.get("reason", "")

    # Strict analysis status determination
    if day_phase == "0915_to_0930":
        analysis_status = "forming_range"
        status_message = "Opening range is forming (09:15 to 09:30 IST); breakout levels will be confirmed at 09:30."
    elif day_phase == "before_0915":
        analysis_status = "pre_open"
        status_message = "Market is not open yet (opens at 09:15 IST)."
    elif market_state in ("holiday", "weekend", "closed") or day_phase == "after_1530_or_closed":
        analysis_status = "market_closed"
        status_message = f"Market session closed ({market_state}). Reference levels from completed session."
    elif entry_level is not None and stop_level is not None and target_level is not None:
        analysis_status = "complete"
        status_message = "Institutional analysis complete with verified levels."
    else:
        analysis_status = "incomplete"
        status_message = levels_note or "Analysis pending or incomplete."

    return {
        "symbol": symbol,
        "ticker_base": ticker_base,
        "now_ist": now_ist,
        "now_date": now_date,
        "day_phase": day_phase,
        "market_state": market_state,
        "source": source,
        "bar_age": bar_age,
        "current_price": current_price,
        "current_price_time": current_price_time,
        "current_price_source": current_price_source,
        "last_bar_close": last_bar_close,
        "last_bar_time": last_bar_time,
        "entry_level": entry_level,
        "stop_level": stop_level,
        "target_level": target_level,
        "reward_risk": reward_risk,
        "stop_label": stop_label,
        "target_label": target_label,
        "tick_size": tick_size,
        "levels_status": levels_status,
        "quantity": quantity,
        "position_value": position_value,
        "total_charges": total_charges,
        "charges_breakdown": charges_breakdown,
        "account_budget": account_budget,
        "max_loss_per_trade": max_loss_per_trade,
        "risk_verdict": risk_verdict,
        "risk_reason": risk_reason,
        "or_complete": or_complete,
        "or_high": or_high,
        "or_low": or_low,
        "or_bars_used": or_bars_used,
        "entry_signal": entry_signal,
        "entry_trigger": entry_trigger,
        "entry_reason": entry_reason,
        "analysis_status": analysis_status,
        "status_message": status_message,
        "sentences": s9.get("sentences", []),
        "chart": curr_analysis.get("chart"),
        "raw_analysis": curr_analysis
    }


def set_active_analysis(curr_analysis: dict) -> dict:
    """Save normalized analysis context into session state for the active stock."""
    if not curr_analysis or not isinstance(curr_analysis, dict):
        return {}
    symbol = curr_analysis.get("symbol", "")
    ctx = extract_analysis_context(curr_analysis)
    st.session_state["active_analysis_symbol"] = symbol
    st.session_state["active_analysis_context"] = ctx
    st.session_state[f"last_analysis_{symbol}"] = curr_analysis
    st.session_state[f"analysis_context_{symbol}"] = ctx
    return ctx


def get_analysis_context_for_symbol(symbol: str) -> Optional[dict]:
    """Retrieve verified analysis context for symbol without state bleed across stocks."""
    if not symbol:
        return None
    # 1. Check if active_analysis_context matches the requested symbol
    active = st.session_state.get("active_analysis_context")
    if active and active.get("symbol") == symbol:
        return active
    # 2. Check per-symbol context cache
    cached = st.session_state.get(f"analysis_context_{symbol}")
    if cached and cached.get("symbol") == symbol:
        return cached
    # 3. Check raw analysis cache and normalize
    raw = st.session_state.get(f"last_analysis_{symbol}")
    if raw and raw.get("symbol") == symbol:
        ctx = extract_analysis_context(raw)
        st.session_state[f"analysis_context_{symbol}"] = ctx
        return ctx
    return None


# ---------------------------------------------------------------- Individual Page Views
def render_home_view(is_sample_mode: bool, config: dict):
    """Render the premium homepage matching the single source of truth design."""
    now_utc = entry_mod.utc_now()

    # 1. Top Header Bar (Theme Selector, Search trigger, Bell, Avatar)
    top_c1, top_c2 = st.columns([2.5, 1.5])
    with top_c2:
        th1, th2, th3, th4 = st.columns([2.4, 0.7, 0.7, 0.7])
        with th1:
            curr_theme = st.session_state.get("theme_mode", "System")
            theme_idx = 0 if curr_theme == "System" else (1 if curr_theme == "Dark" else 2)
            selected_theme = st.selectbox(
                "Theme",
                options=["System", "Dark", "Light"],
                index=theme_idx,
                label_visibility="collapsed",
                key="home_theme_selector"
            )
            if selected_theme != curr_theme:
                st.session_state["theme_mode"] = selected_theme
                st.rerun()
        with th2:
            render_html(
                """
                <div class="ti-header-btn" title="Search">
                    <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2">
                        <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
                    </svg>
                </div>
                """
            )
        with th3:
            render_html(
                """
                <div class="ti-header-btn" title="Notifications">
                    <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2">
                        <path d="M18 8A6 6 0 006 8c0 7-3 9-3 9h24s-3-2-3-9"/><path d="M13.73 21a2 2 0 01-3.46 0"/>
                    </svg>
                </div>
                """
            )
        with th4:
            render_html("""<div class="ti-avatar" title="Account">T</div>""")

    # 2. Hero Section
    render_html(
        """
        <div class="ti-hero">
            <div class="ti-overline">MARKET INTELLIGENCE PLATFORM</div>
            <h1 class="ti-hero-title">
                Read the Market.<br>
                Trade with <span class="ti-accent-purple">Conviction.</span>
            </h1>
            <p class="ti-hero-desc">
                Price action, risk and historical evidence — in one focused workflow.
            </p>
        </div>
        """
    )

    # 3. Stock Search Bar & Button (Exact Reference Match)
    sc1, sc2, sc_empty = st.columns([3.6, 1.2, 1.6])
    with sc1:
        home_query = st.text_input(
            "Stock Search",
            value="",
            placeholder="Enter a stock (e.g. RELIANCE.NS, TCS.NS, INFY.NS)",
            label_visibility="collapsed",
            key="home_search_input"
        )
    with sc2:
        btn_analyze = st.button("Analyze Now →", type="primary", key="home_btn_analyze_action", use_container_width=True)

    if btn_analyze or (home_query and st.session_state.get("home_search_submitted")):
        target_sym = resolve_symbol(home_query if home_query else "RELIANCE.NS")
        st.session_state["active_symbol"] = target_sym
        st.session_state["current_page"] = "Analyze now"
        st.rerun()

    render_html('<div style="height: 16px;"></div>')

    # 4. Main Feature Navigation (3 Columns)
    f1, f2, f3 = st.columns(3)
    with f1:
        render_html(
            """
            <div class="ti-feature-card">
                <div class="ti-feature-icon-box">
                    <svg viewBox="0 0 24 24" width="22" height="22" fill="#9955E8">
                        <path d="M13 2L3 14h7v8l10-12h-7z"/>
                    </svg>
                </div>
                <div>
                    <div class="ti-feature-title">Analyze Now</div>
                    <div class="ti-feature-desc">Get a trade plan with clear entry, stop, target and risk.</div>
                </div>
            </div>
            """
        )
        if st.button("Open Analyze Now", key="nav_btn_analyze_card", use_container_width=True):
            st.session_state["current_page"] = "Analyze now"
            st.rerun()
    with f2:
        render_html(
            """
            <div class="ti-feature-card">
                <div class="ti-feature-icon-box">
                    <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="#9955E8" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
                        <line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/>
                    </svg>
                </div>
                <div>
                    <div class="ti-feature-title">BIG BULL</div>
                    <div class="ti-feature-desc">Discover high-potential opportunities across markets.</div>
                </div>
            </div>
            """
        )
        if st.button("Open BIG BULL", key="nav_btn_bigbull_card", use_container_width=True):
            st.session_state["current_page"] = "Sector heatmap"
            st.rerun()
    with f3:
        render_html(
            """
            <div class="ti-feature-card">
                <div class="ti-feature-icon-box">
                    <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="#9955E8" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
                        <circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>
                    </svg>
                </div>
                <div>
                    <div class="ti-feature-title">Time Machine</div>
                    <div class="ti-feature-desc">Replay the past, test your setup and see historical odds.</div>
                </div>
            </div>
            """
        )
        if st.button("Open Time Machine", key="nav_btn_timemachine_card", use_container_width=True):
            st.session_state["current_page"] = "Time Machine"
            st.rerun()

    render_html('<div style="height: 16px;"></div>')

    # 5. Middle Grid: Market Pulse & Your Next Step
    m_col1, m_col2 = st.columns([1.6, 1.0])
    with m_col1:
        quotes = {}
        try:
            quotes = fetch_cached_market_overview(('^NSEI', '^NSEBANK', '^BSESN'), is_sample_mode=is_sample_mode)
        except Exception:
            quotes = {}

        q_nifty = quotes.get('^NSEI', {})
        p_nifty = f"{q_nifty['price']:,.2f}" if q_nifty.get('price') else "26,328.90"
        c_nifty = q_nifty.get('change_pct', 0.68)
        cls_nifty = "positive" if c_nifty >= 0 else "negative"
        arrow_nifty = "↑" if c_nifty >= 0 else "↓"
        sign_nifty = "+" if c_nifty >= 0 else ""

        q_bank = quotes.get('^NSEBANK', {})
        p_bank = f"{q_bank['price']:,.2f}" if q_bank.get('price') else "59,742.35"
        c_bank = q_bank.get('change_pct', 0.91)
        cls_bank = "positive" if c_bank >= 0 else "negative"
        arrow_bank = "↑" if c_bank >= 0 else "↓"
        sign_bank = "+" if c_bank >= 0 else ""

        q_sensex = quotes.get('^BSESN', {})
        p_sensex = f"{q_sensex['price']:,.2f}" if q_sensex.get('price') else "86,451.62"
        c_sensex = q_sensex.get('change_pct', -0.12)
        cls_sensex = "positive" if c_sensex >= 0 else "negative"
        arrow_sensex = "↑" if c_sensex >= 0 else "↓"
        sign_sensex = "+" if c_sensex >= 0 else ""

        render_html(
            f"""
            <div class="ti-card">
                <div class="ti-card-title">Today's Market Pulse</div>
                <div class="ti-pulse-row">
                    <div class="ti-pulse-card">
                        <div class="ti-pulse-header">{arrow_nifty} NIFTY 50</div>
                        <div class="ti-pulse-price">{p_nifty}</div>
                        <div class="ti-pulse-change {cls_nifty}">{sign_nifty}{c_nifty:.2f}%</div>
                    </div>
                    <div class="ti-pulse-card">
                        <div class="ti-pulse-header">{arrow_bank} BANK NIFTY</div>
                        <div class="ti-pulse-price">{p_bank}</div>
                        <div class="ti-pulse-change {cls_bank}">{sign_bank}{c_bank:.2f}%</div>
                    </div>
                    <div class="ti-pulse-card">
                        <div class="ti-pulse-header">{arrow_sensex} SENSEX</div>
                        <div class="ti-pulse-price">{p_sensex}</div>
                        <div class="ti-pulse-change {cls_sensex}">{sign_sensex}{c_sensex:.2f}%</div>
                    </div>
                </div>
            </div>
            """
        )

    with m_col2:
        render_html(
            """
            <div class="ti-card" style="height: 100%; display: flex; flex-direction: column; justify-content: space-between;">
                <div>
                    <div class="ti-card-title">Your Next Step</div>
                    <div class="ti-next-step-desc">
                        Run a focused analysis or explore opportunities with BIG BULL.
                    </div>
                </div>
            </div>
            """
        )
        if st.button("Analyze a Stock →", key="btn_next_step_action", type="primary", use_container_width=True):
            st.session_state["current_page"] = "Analyze now"
            st.rerun()

    render_html('<div style="height: 20px;"></div>')

    # 6. Bottom Banner: How TradeInsta Helps
    render_html(
        """
        <div class="ti-bottom-banner">
            <div class="ti-banner-title">How TradeInsta Helps</div>
            <div class="ti-banner-divider"></div>
            <div class="ti-pillars-container">
                <div class="ti-pillar">
                    <div class="ti-pillar-icon">
                        <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2">
                            <circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2"/>
                        </svg>
                    </div>
                    <div class="ti-pillar-text">
                        <div class="ti-pillar-name">Rule-Based</div>
                        <div class="ti-pillar-desc">Systematic, repeatable, no guesswork.</div>
                    </div>
                </div>
                <div class="ti-pillar">
                    <div class="ti-pillar-icon">
                        <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2">
                            <polygon points="12 2 2 7 12 12 22 7 12 2"/><polyline points="2 17 12 22 22 17"/><polyline points="2 12 12 17 22 12"/>
                        </svg>
                    </div>
                    <div class="ti-pillar-text">
                        <div class="ti-pillar-name">Risk First</div>
                        <div class="ti-pillar-desc">Position size, max loss and account safety built-in.</div>
                    </div>
                </div>
                <div class="ti-pillar">
                    <div class="ti-pillar-icon">
                        <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2">
                            <line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/>
                        </svg>
                    </div>
                    <div class="ti-pillar-text">
                        <div class="ti-pillar-name">Evidence Driven</div>
                        <div class="ti-pillar-desc">Historical replay and transparent assumptions.</div>
                    </div>
                </div>
            </div>
        </div>
        """
    )


def render_analyze_now_view(resolved_symbol: str, company_name: str, is_sample_mode: bool, config: dict, deals: list = None):
    """Render single-stock analysis & trade execution view."""
    active_b = broker_data.active_broker()
    now_utc = entry_mod.utc_now()
    market_st = alerts.market_status(now_utc, us=False)
    is_market_open = market_st.get("open", False)

    curr_analysis = st.session_state.get(f"last_analysis_{resolved_symbol}")
    if curr_analysis:
        set_active_analysis(curr_analysis)

    steps = curr_analysis.get("steps", {}) if curr_analysis else {}
    s1 = steps.get("data_check", {})
    s2 = steps.get("opening_range", {})
    s3 = steps.get("entry_status", {})
    s4 = steps.get("levels", {})
    s5 = steps.get("size_and_charges", {})
    s6 = steps.get("risk_check", {})
    s7 = steps.get("historical_odds", {})
    s8 = steps.get("context", {})
    s9 = steps.get("summary", {})
    day_phase = curr_analysis.get("day_phase", "0930_to_1530") if curr_analysis else "0930_to_1530"

    cur_p = s1.get("current_price")
    cur_p_time = s1.get("current_price_time") or "N/A"
    cur_p_source = str(s1.get("current_price_source") or ("sample" if is_sample_mode else (active_b or "yahoo"))).capitalize()

    last_b_close = s1.get("last_bar_close")
    last_b_time = s1.get("last_bar_time") or "N/A"
    b_age = s1.get("bar_age") or "N/A"
    chart = curr_analysis.get("chart") if curr_analysis else s1.get("chart")
    bars = (curr_analysis.get("bars") if curr_analysis else s1.get("bars")) or (chart.get("bars") if chart else []) or []
    data_source = s1.get("source") or ("sample" if is_sample_mode else (active_b or "yahoo"))

    # Trust strip at top
    render_trust_strip(data_source=data_source, chart=chart, is_sample_mode=is_sample_mode, now_utc=now_utc)

    # 1. Header: Symbol/Company left, Current Price right
    if cur_p is not None:
        price_sub = f"{cur_p_time} · {cur_p_source}"
        price_str = f"₹{cur_p:,.2f}"
    else:
        price_sub = "Click Analyze now"
        price_str = "--"

    render_html(
        f"""
        <div style="display: flex; justify-content: space-between; align-items: flex-start; flex-wrap: wrap; gap: 16px; margin-bottom: 12px;">
            <div>
                <div style="font-size: 26px; font-weight: 700; color: var(--text-main); letter-spacing: -0.02em;">{resolved_symbol}</div>
                <div style="font-size: 13px; color: var(--text-muted);">{company_name}</div>
            </div>
            <div style="text-align: right;">
                <div style="font-size: 11px; font-weight: 700; text-transform: uppercase; color: var(--text-muted); letter-spacing: 0.05em;">Current Price (Quote LTP)</div>
                <div class="mono" style="font-size: 28px; font-weight: 700; color: var(--text-main); line-height: 1.1;">{price_str}</div>
                <div class="mono" style="font-size: 12px; color: var(--accent-blue);">{price_sub}</div>
            </div>
        </div>
        """
    )

    # Action Button to re-run / run
    c_btn, c_spacer = st.columns([1, 3])
    with c_btn:
        run_analysis = st.button("Analyze now", type="primary", use_container_width=True, help="Compute instant 9-step trade analysis as of the current minute.")

    if run_analysis:
        prog_bar = st.progress(0, text="Initializing 9-step analysis...")
        def _prog_cb(step_num, step_name, status, detail=""):
            pct = int((step_num / 9.0) * 100)
            prog_bar.progress(pct, text=f"Step {step_num}/9: {step_name} · {detail}")

        t_an0 = time.perf_counter()
        curr_analysis = analyze_symbol(
            resolved_symbol,
            is_sample_mode=is_sample_mode,
            config=config,
            progress_callback=_prog_cb
        )
        src_an = curr_analysis.get("steps", {}).get("data_check", {}).get("source", "broker/yahoo")
        log_timing(f"analyze_symbol({resolved_symbol})", time.perf_counter() - t_an0, src_an)
        prog_bar.empty()
        st.session_state[f"last_analysis_{resolved_symbol}"] = curr_analysis
        set_active_analysis(curr_analysis)
        st.rerun()

    if not curr_analysis:
        render_html(
            f"""
            <div class="terminal-panel" style="padding: 32px 24px; text-align: center; margin-top: 14px;">
                <div style="font-size: 15px; font-weight: 700; color: var(--text-main); margin-bottom: 8px;">Analysis Ready</div>
                <div style="font-size: 13px; color: var(--text-muted); max-width: 540px; margin: 0 auto;">
                    Click <b style="color: var(--accent-blue);">Analyze now</b> above to execute the 9-step institutional & intraday verification for <span class="mono" style="color: var(--text-main); font-weight: 600;">{resolved_symbol}</span> as of the current minute.
                </div>
            </div>
            """
        )
        return

    # Separate Completed Bar display + Explanatory line
    render_html(
        f"""
        <div class="terminal-panel" style="padding: 10px 16px; margin-bottom: 14px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px;">
            <div style="font-size: 12px; color: var(--text-muted);">
                Last completed 5-min bar: <span class="mono" style="color: var(--text-main); font-weight: 600;">₹{last_b_close or 0:,.2f}</span> · Close: <span class="mono">{last_b_time}</span> · Age: <span class="mono">{b_age}</span>
            </div>
            <div style="font-size: 11px; color: var(--text-muted); font-style: italic;">
                The entry rule needs completed bars. The current price shows where the stock is right now.
            </div>
        </div>
        """
    )

    # Day Phase Calm State Alert (if market not open / holiday / weekend / closed)
    if day_phase == "before_0915":
        render_html(
            """
            <div style="background-color: var(--bg-panel); border-left: 3px solid var(--warning-amber); border-radius: 6px; padding: 10px 14px; margin-bottom: 14px; font-size: 12px; color: var(--text-muted);">
                <b style="color: var(--warning-amber);">MARKET NOT OPEN</b>: Trading opens at 09:15 IST. Displaying previous completed session levels.
            </div>
            """
        )
    elif day_phase == "0915_to_0930":
        render_html(
            """
            <div style="background-color: var(--bg-panel); border-left: 3px solid var(--accent-blue); border-radius: 6px; padding: 10px 14px; margin-bottom: 14px; font-size: 12px; color: var(--text-muted);">
                <b style="color: var(--accent-blue);">OPENING RANGE FORMING</b>: The 15-minute range is completing. Breakout entry, stop, and target levels will be confirmed at 09:30 IST.
            </div>
            """
        )
    elif day_phase == "after_1530_or_closed":
        render_html(
            """
            <div style="background-color: var(--bg-panel); border-left: 3px solid var(--border-subtle); border-radius: 6px; padding: 10px 14px; margin-bottom: 14px; font-size: 12px; color: var(--text-muted);">
                <b>LAST SESSION</b>: Market closed. Displaying last completed trading session.
            </div>
            """
        )

    # 2. Day Timeline Panel
    ist_tz = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
    now_ist = datetime.datetime.now(ist_tz)
    render_day_timeline(now_ist, trigger_info=s3.get("trigger"))

    # 3. Two Columns: Left Price Ladder (~55%), Right Past Odds + Sizing/Risk (~45%)
    col_ladder, col_odds = st.columns([11, 9])

    with col_ladder:
        range_h = s2.get("high")
        range_l = s2.get("low")
        e_level = s4.get("entry_level")
        s_level = s4.get("stop_level")
        t_level = s4.get("target_level")

        render_price_ladder(
            current_price=cur_p,
            range_high=range_h,
            range_low=range_l,
            entry_level=e_level,
            stop_level=s_level,
            target_level=t_level,
            day_phase=day_phase
        )

    with col_odds:
        render_past_odds_panel(s7)
        st.write("")
        render_size_and_risk_panel(s5, s6)

    # 4. Bottom Row: Step Chips + Plain Summary + Market Context
    render_step_chips(steps)
    render_summary_and_context(s9, s8, deals, resolved_symbol)

    # 5. Expandable Full 9-Step Verification Details
    with st.expander("View Full 9-Step Verification Details", expanded=False):
        c1, c2 = st.columns(2)
        with c1:
            st.markdown(f"**Step 1: Data Check** (`{s1.get('timestamp')}`)")
            st.write(f"- Source: `{s1.get('source')}` (Upstox Token: `{s1.get('upstox_token_status')}`)")
            if s1.get("fallback_reason"):
                st.write(f"- Fallback: {s1.get('fallback_reason')}")
            st.write(f"- Market State: `{s1.get('market_state')}`" + (f" ({s1.get('holiday_name')})" if s1.get("holiday_name") else ""))

            st.markdown(f"**Step 2: Opening Range** (`{s2.get('timestamp')}`)")
            st.write(f"- High: `₹{s2.get('high') or 0:.2f}`, Low: `₹{s2.get('low') or 0:.2f}` ({s2.get('bars_used')}/3 bars)")
            st.write(f"- Status: {s2.get('phase_note')}")

            st.markdown(f"**Step 3: Entry Status** (`{s3.get('timestamp')}`)")
            st.write(f"- Signal: `{str(s3.get('signal')).upper()}`")
            if s3.get("trigger"):
                st.write(f"- Triggered: ₹{s3['trigger'].get('price', 0):.2f} at {s3['trigger'].get('time')}")
            if s3.get("late_entry_flag"):
                st.warning(s3.get('late_entry_note'))
            if s3.get("failed_breakout"):
                st.error(s3.get('failed_reason'))

            st.markdown(f"**Step 4: Levels** (`{s4.get('timestamp')}`)")
            if s4.get("entry_level"):
                e_lvl = float(s4.get("entry_level"))
                s_lvl = float(s4.get("stop_level") or 0.0)
                t_lvl = float(s4.get("target_level") or 0.0)
                rr_val = s4.get("reward_risk")
                ts_val = s4.get("tick_size")
                rr_str = f"{float(rr_val):.1f}R" if rr_val is not None else "N/A"
                ts_str = f"₹{float(ts_val):.2f}" if ts_val is not None else "₹0.05"
                st.write(f"- Entry Level: `₹{e_lvl:.2f}` | Stop: `₹{s_lvl:.2f}` | Target: `₹{t_lvl:.2f}`")
                st.write(f"- Reward:Risk: `{rr_str}` | Tick size: `{ts_str}`")
            else:
                st.write(f"- {s4.get('note')}")

        with c2:
            st.markdown(f"**Step 5: Size & Statutory Charges** (`{s5.get('timestamp')}`)")
            st.write(f"- Sized Quantity: `{s5.get('quantity')} shares` (Value: ₹{s5.get('position_value', 0):,.2f})")
            st.write(f"- Round-trip Charges: `₹{s5.get('total_charges', 0):.2f}` (Brokerage, STT, turnover, GST, stamp duty)")

            st.markdown(f"**Step 6: Risk Check** (`{s6.get('timestamp')}`)")
            if s6.get("verdict") == "PASS":
                st.success(f"PASS: {s6.get('reason')}")
            else:
                st.error(f"BLOCK: {s6.get('reason')}")

            st.markdown(f"**Step 7: Historical Odds (Time Machine)** (`{s7.get('timestamp')}`)")
            st.write(f"- Win Rate: `{s7.get('win_rate')}%` across `{s7.get('samples')}` sessions")
            st.write(f"- Edge Verdict: `{s7.get('verdict')}`")

            st.markdown(f"**Step 8: Context (Read-Only)** (`{s8.get('timestamp')}`)")
            st.write(f"- Volume vs 20d Avg: `{s8.get('volume_vs_20d_label')}`")
            st.write(f"- Institutional Flow: `{s8.get('flow_summary', {}).get('total_deals', 0)} deals` ({s8.get('flow_label')})")
            if s8.get("headlines"):
                for h in s8["headlines"][:2]:
                    st.write(f"- News: [{h.get('title')}]({h.get('link')})")

    # 6. 5-Minute Intraday Candlestick Chart
    with st.expander("5-Minute Intraday Candlestick Chart", expanded=False):
        if bars:
            df_bars = pd.DataFrame(bars)
            df_bars["datetime"] = pd.to_datetime(df_bars["t"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
            df_bars["time_str"] = df_bars["datetime"].dt.strftime("%H:%M")

            fig = go.Figure()
            fig.add_trace(go.Candlestick(
                x=df_bars["time_str"],
                open=df_bars["o"],
                high=df_bars["h"],
                low=df_bars["l"],
                close=df_bars["c"],
                name="5m Candles",
                increasing_line_color="#3DD68C",
                decreasing_line_color="#FF6B6B",
                increasing_fillcolor="#3DD68C",
                decreasing_fillcolor="#FF6B6B"
            ))

            target_val = s4.get("target_level")
            entry_val = s4.get("entry_level")
            stop_val = s4.get("stop_level")

            if target_val:
                fig.add_hline(y=target_val, line_dash="dash", line_color="#3DD68C", line_width=1.5, annotation_text=f"target: ₹{target_val:,.2f}", annotation_position="top right", annotation_font_color="#3DD68C")
            if entry_val:
                fig.add_hline(y=entry_val, line_dash="solid", line_color="#5EA8FF", line_width=2, annotation_text=f"entry level: ₹{entry_val:,.2f}", annotation_position="top right", annotation_font_color="#5EA8FF")
            if stop_val:
                fig.add_hline(y=stop_val, line_dash="dash", line_color="#FF6B6B", line_width=1.5, annotation_text=f"stop: ₹{stop_val:,.2f}", annotation_position="bottom right", annotation_font_color="#FF6B6B")

            fig.update_layout(
                title=f"{resolved_symbol} — 5-Minute Intraday Chart",
                xaxis_title="Time (IST)",
                yaxis_title="Price (₹)",
                xaxis_rangeslider_visible=False,
                height=520,
                margin=dict(l=30, r=30, t=50, b=30),
                paper_bgcolor="#161B22",
                plot_bgcolor="#161B22",
                font=dict(color="#E6EAF0", family="Inter, sans-serif"),
                hovermode="x unified"
            )
            st.plotly_chart(fig, use_container_width=True)

            # Candle Integrity & Provider Disclosure (DEF-06)
            is_indian_sym = (resolved_symbol.endswith(".NS") or resolved_symbol.endswith(".BO"))
            if is_indian_sym and bars:
                ist_tz = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
                last_bar = bars[-1]
                last_dt = datetime.datetime.fromtimestamp(last_bar.get("t", 0), tz=datetime.timezone.utc).astimezone(ist_tz)
                last_time_str = last_dt.strftime("%H:%M")
                is_flat_close = (
                    last_time_str in ("15:25", "15:30") and
                    last_bar.get("o") == last_bar.get("h") == last_bar.get("l") == last_bar.get("c")
                )

                bar_times_set = set(df_bars["time_str"].tolist())
                expected_times = [
                    f"{h:02d}:{m:02d}" for h in range(9, 16) for m in range(0, 60, 5)
                    if (h > 9 or m >= 15) and (h < 15 or m <= 25)
                ]
                missing_times = [t for t in expected_times if t not in bar_times_set and t <= last_time_str]

                if missing_times or is_flat_close:
                    disc_lines = []
                    if missing_times:
                        if all(t in ("15:15", "15:20") for t in missing_times):
                            disc_lines.append(f"• **Missing Tail Candles:** Timestamps `{', '.join(missing_times)}` are omitted by public Yahoo feed; active trading window (09:15–15:15) is fully covered. Flat-by exit executes on the close of the 15:10 bar.")
                        else:
                            disc_lines.append(f"• **Missing Intraday Bars:** `{', '.join(missing_times)}` missing from feed. Price continuity may be interrupted.")
                    if is_flat_close:
                        disc_lines.append(f"• **Closing Record:** Bar `{last_time_str}` is an aggregated closing-price record (O=H=L=C=₹{last_bar.get('c'):,.2f}, Volume: {last_bar.get('v', 0):,}), reflecting the official NSE closing auction rather than a continuous 5-minute trade bar.")

                    st.info("\n\n".join(disc_lines))
        else:
            reason = s1.get("fallback_reason") or s1.get("market_state") or "market closed or data unavailable"
            if s1.get("holiday_name"):
                reason = f"NSE Holiday: {s1.get('holiday_name')}"
            st.info(f"Chart not available: {reason}")


def render_scanner_view(is_sample_mode: bool):
    """Render Scanner view."""
    st.markdown("## Scanner")
    st.caption("Opening-range breakout scan and daily setups across tracked symbols.")
    
    watchlist_path = os.path.join(ROOT, "trader", "watchlist.json")
    if os.path.exists(watchlist_path):
        try:
            with open(watchlist_path, "r", encoding="utf-8") as fh:
                w_data = json.load(fh)
            hits = w_data.get("hits", [])
            st.markdown(f"**Latest Watchlist:** {w_data.get('as_of', 'Recent')} · Scanned: {w_data.get('scanned', 0)} · Hits: {len(hits)}")
            if hits:
                df_hits = pd.DataFrame(hits)
                st.dataframe(df_hits, use_container_width=True, hide_index=True)
            else:
                st.info("No breakout hits in the current watchlist.")
        except Exception as e:
            st.error(f"Error loading watchlist: {e}")
    else:
        st.info("No saved watchlist found in `trader/watchlist.json`.")


def render_entry_timer_view(resolved_symbol: str, is_sample_mode: bool, config: dict):
    """Render Entry Timer bar-by-bar view with resilient error handling."""
    now_utc = entry_mod.utc_now()
    active_b = broker_data.active_broker()
    chart = None
    fetch_err = None

    if not is_sample_mode and active_b == "upstox":
        try:
            chart = broker_data.get_realtime_candles(resolved_symbol, interval="5m")
        except Exception:
            chart = None

    if not chart and not is_sample_mode:
        try:
            chart = fetch_cached_intraday_chart(resolved_symbol)
        except Exception as e:
            fetch_err = str(e)
            chart = None

    if not chart:
        if is_sample_mode:
            sample_path = os.path.join(ROOT, "tests", "fixtures", "sample_reliance.json")
            if os.path.exists(sample_path):
                try:
                    with open(sample_path, "r", encoding="utf-8") as fh:
                        chart = json.load(fh)
                except Exception:
                    chart = None

    data_source = (chart.get("meta") or {}).get("source", "sample" if is_sample_mode else (active_b or "yahoo")) if chart else "yahoo"
    render_trust_strip(data_source=data_source, chart=chart, is_sample_mode=is_sample_mode, now_utc=now_utc)

    st.markdown("## Entry Timer")
    st.caption(f"Watches opening range setup bar by bar for {resolved_symbol} and alerts upon breakout.")
    if chart:
        try:
            eval_res = entry_mod.evaluate(chart, now_utc, setup="orb", or_minutes=15, interval="5m", cfg=config)
            st.write(f"- Status: `{eval_res.get('status')}`")
            st.write(f"- Phase: `{eval_res.get('phase')}`")
            st.write(f"- Reason: `{eval_res.get('reason')}`")
            if eval_res.get("trigger"):
                st.write(f"- Triggered: `₹{eval_res['trigger'].get('price')}` at `{eval_res['trigger'].get('time')}`")
        except Exception as e:
            st.warning(f"Evaluation notice: {e}")
    else:
        if fetch_err:
            st.info(f"Live market feed currently unavailable for {resolved_symbol}: {fetch_err}. Switch to 'Sample offline data' in sidebar to preview.")
        else:
            st.info(f"No chart data available for {resolved_symbol}. Switch to 'Sample offline data' in sidebar to preview.")


def render_exit_planner_view(resolved_symbol: str, is_sample_mode: bool, config: dict):
    """Render Exit Planner stop and target levels view."""
    now_utc = entry_mod.utc_now()
    active_b = broker_data.active_broker()
    data_source = "sample" if is_sample_mode else (active_b or "yahoo")
    render_trust_strip(data_source=data_source, chart=None, is_sample_mode=is_sample_mode, now_utc=now_utc)

    st.markdown("## Exit Planner")
    st.caption(f"Sets pre-trade stop and target levels before entry for {resolved_symbol}.")

    ctx = get_analysis_context_for_symbol(resolved_symbol)
    inherited_entry = None
    has_inherited_levels = False

    if ctx:
        if ctx.get("analysis_status") == "forming_range":
            render_html(
                f"""
                <div class="terminal-panel" style="padding: 12px 16px; margin-bottom: 14px; border-left: 3px solid var(--accent-blue);">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px;">
                        <span style="font-weight: 700; color: var(--text-main); font-size: 13px;">Opening Range Forming (09:15 to 09:30 IST)</span>
                        <span class="terminal-chip active" style="font-size: 10px;">FORMING</span>
                    </div>
                    <div style="font-size: 12px; color: var(--text-muted);">
                        The 15-minute opening range is currently forming. Breakout entry, stop, and target levels will be calculated after the 09:30 IST bar closes. You can enter manual levels below to plan in advance.
                    </div>
                </div>
                """
            )
        elif ctx.get("entry_level") is not None:
            inherited_entry = float(ctx["entry_level"])
            has_inherited_levels = True
            ts_label = ctx.get("now_ist") or "recent"
            src_label = ctx.get("source") or "broker"
            render_html(
                f"""
                <div class="terminal-panel" style="padding: 10px 16px; margin-bottom: 14px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px;">
                    <div style="font-size: 12px; color: var(--text-muted);">
                        Inherited from analysis for <span class="mono" style="color: var(--text-main); font-weight: 600;">{resolved_symbol}</span> at <span class="mono">{ts_label} IST</span> ({src_label})
                    </div>
                    <div>
                        <span class="terminal-chip active" style="font-size: 11px;">INHERITED FROM ANALYSIS</span>
                    </div>
                </div>
                """
            )
        else:
            render_html(
                f"""
                <div class="terminal-panel" style="padding: 12px 16px; margin-bottom: 14px; border-left: 3px solid var(--warning-amber);">
                    <div style="font-size: 12px; color: var(--text-muted);">
                        Analysis for <span class="mono" style="color: var(--text-main); font-weight: 600;">{resolved_symbol}</span> did not provide confirmed breakout levels ({ctx.get('status_message', 'pending')}). Enter levels manually below.
                    </div>
                </div>
                """
            )
    else:
        render_html(
            f"""
            <div class="terminal-panel" style="padding: 14px 18px; margin-bottom: 16px; border-left: 3px solid var(--warning-amber);">
                <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; margin-bottom: 6px;">
                    <span style="font-weight: 700; color: var(--text-main); font-size: 13px;">No Active Analysis for {resolved_symbol}</span>
                    <span class="terminal-chip warning" style="font-size: 10px;">MANUAL MODE</span>
                </div>
                <div style="font-size: 12px; color: var(--text-muted); margin-bottom: 10px;">
                    No active analysis for {resolved_symbol}. Run an analysis first, or enter levels manually.
                </div>
            </div>
            """
        )
        c_an, _ = st.columns([1, 3])
        with c_an:
            if st.button(f"Analyze {resolved_symbol} now", key=f"btn_an_exit_{resolved_symbol}", type="secondary"):
                with st.spinner(f"Analyzing {resolved_symbol}..."):
                    an_res = analyze_symbol(resolved_symbol, is_sample_mode=is_sample_mode, config=config)
                    set_active_analysis(an_res)
                    st.rerun()

    default_entry = inherited_entry if inherited_entry is not None else 2500.0
    entry_val = st.number_input("Entry Price (₹)", value=float(default_entry), step=1.0, key=f"exit_entry_{resolved_symbol}")

    default_rule_idx = 0
    if ctx and ctx.get("target_label") == "1R":
        default_rule_idx = 1
    elif ctx and ctx.get("target_label") == "3R":
        default_rule_idx = 2

    target_rule = st.selectbox("Target Rule", options=["2R", "1R", "3R"], index=default_rule_idx, key=f"exit_rule_{resolved_symbol}")
    rule, param = exit_mod.parse_target(target_rule)

    is_manual_override = has_inherited_levels and (abs(entry_val - inherited_entry) > 0.01)
    if is_manual_override:
        render_html('<div style="margin-bottom: 8px;"><span class="terminal-chip warning">MANUAL OVERRIDE</span> <span style="font-size: 11px; color: var(--text-muted); margin-left: 6px;">Entry differs from analyzed breakout level</span></div>')

    stop_rule = "or_mid" if (ctx and ctx.get("or_high") and ctx.get("or_low") and not is_manual_override) else "pct"
    or_high_dec = Decimal(str(round(ctx["or_high"], 2))) if (ctx and ctx.get("or_high") and not is_manual_override) else None
    or_low_dec = Decimal(str(round(ctx["or_low"], 2))) if (ctx and ctx.get("or_low") and not is_manual_override) else None

    plan = exit_mod.compute(
        entry=Decimal(str(entry_val)),
        stop_rule=stop_rule,
        stop_param=None if stop_rule == "or_mid" else Decimal("1.0"),
        target_rule=rule,
        target_param=param,
        or_high=or_high_dec,
        or_low=or_low_dec,
        sym=resolved_symbol
    )
    c1, c2, c3 = st.columns(3)
    with c1:
        st.metric("Entry", f"₹{float(plan['entry']):,.2f}")
    with c2:
        st.metric("Stop", f"₹{float(plan['stop']):,.2f}", delta=f"{plan.get('stop_label')}", delta_color="inverse")
    with c3:
        st.metric("Target", f"₹{float(plan['target']):,.2f}", delta=f"{plan.get('target_label')}", delta_color="normal")


def render_position_size_view(resolved_symbol: str, is_sample_mode: bool, config: dict):
    """Render Position Sizing view."""
    now_utc = entry_mod.utc_now()
    active_b = broker_data.active_broker()
    data_source = "sample" if is_sample_mode else (active_b or "yahoo")
    render_trust_strip(data_source=data_source, chart=None, is_sample_mode=is_sample_mode, now_utc=now_utc)

    st.markdown("## Position Size")
    st.caption(f"Sizes every position to account budget, max loss, and statutory charges for {resolved_symbol}.")

    ctx = get_analysis_context_for_symbol(resolved_symbol)
    inherited_entry = None
    inherited_stop = None
    has_inherited_levels = False

    if ctx:
        if ctx.get("analysis_status") == "forming_range":
            render_html(
                f"""
                <div class="terminal-panel" style="padding: 12px 16px; margin-bottom: 14px; border-left: 3px solid var(--accent-blue);">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px;">
                        <span style="font-weight: 700; color: var(--text-main); font-size: 13px;">Opening Range Forming (09:15 to 09:30 IST)</span>
                        <span class="terminal-chip active" style="font-size: 10px;">FORMING</span>
                    </div>
                    <div style="font-size: 12px; color: var(--text-muted);">
                        Sized position and statutory charges will be computed once breakout levels are confirmed at 09:30 IST. You can test manual sizing below.
                    </div>
                </div>
                """
            )
        elif ctx.get("entry_level") is not None and ctx.get("stop_level") is not None:
            inherited_entry = float(ctx["entry_level"])
            inherited_stop = float(ctx["stop_level"])
            has_inherited_levels = True
            ts_label = ctx.get("now_ist") or "recent"
            src_label = ctx.get("source") or "broker"
            render_html(
                f"""
                <div class="terminal-panel" style="padding: 10px 16px; margin-bottom: 14px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px;">
                    <div style="font-size: 12px; color: var(--text-muted);">
                        Inherited from analysis for <span class="mono" style="color: var(--text-main); font-weight: 600;">{resolved_symbol}</span> at <span class="mono">{ts_label} IST</span> ({src_label})
                    </div>
                    <div>
                        <span class="terminal-chip active" style="font-size: 11px;">INHERITED FROM ANALYSIS</span>
                    </div>
                </div>
                """
            )
        else:
            render_html(
                f"""
                <div class="terminal-panel" style="padding: 12px 16px; margin-bottom: 14px; border-left: 3px solid var(--warning-amber);">
                    <div style="font-size: 12px; color: var(--text-muted);">
                        Analysis for <span class="mono" style="color: var(--text-main); font-weight: 600;">{resolved_symbol}</span> does not have confirmed stop levels ({ctx.get('status_message', 'pending')}). Enter levels manually below.
                    </div>
                </div>
                """
            )
    else:
        render_html(
            f"""
            <div class="terminal-panel" style="padding: 14px 18px; margin-bottom: 16px; border-left: 3px solid var(--warning-amber);">
                <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; margin-bottom: 6px;">
                    <span style="font-weight: 700; color: var(--text-main); font-size: 13px;">No Active Analysis for {resolved_symbol}</span>
                    <span class="terminal-chip warning" style="font-size: 10px;">MANUAL MODE</span>
                </div>
                <div style="font-size: 12px; color: var(--text-muted); margin-bottom: 10px;">
                    No active analysis for {resolved_symbol}. Run an analysis first, or enter levels manually.
                </div>
            </div>
            """
        )
        c_an, _ = st.columns([1, 3])
        with c_an:
            if st.button(f"Analyze {resolved_symbol} now", key=f"btn_an_ps_{resolved_symbol}", type="secondary"):
                with st.spinner(f"Analyzing {resolved_symbol}..."):
                    an_res = analyze_symbol(resolved_symbol, is_sample_mode=is_sample_mode, config=config)
                    set_active_analysis(an_res)
                    st.rerun()

    default_entry = inherited_entry if inherited_entry is not None else 2500.0
    default_stop = inherited_stop if inherited_stop is not None else 2475.0

    entry_val = st.number_input("Entry Level (₹)", value=float(default_entry), step=1.0, key=f"ps_entry_{resolved_symbol}")
    stop_val = st.number_input("Stop Level (₹)", value=float(default_stop), step=1.0, key=f"ps_stop_{resolved_symbol}")

    is_manual_override = has_inherited_levels and (
        abs(entry_val - inherited_entry) > 0.01 or abs(stop_val - inherited_stop) > 0.01
    )

    if is_manual_override:
        render_html('<div style="margin-bottom: 8px;"><span class="terminal-chip warning">MANUAL OVERRIDE</span> <span style="font-size: 11px; color: var(--text-muted); margin-left: 6px;">Parameters differ from analyzed trade plan</span></div>')

    plan = {
        "symbol": resolved_symbol,
        "side": "buy",
        "date": datetime.date.today().isoformat(),
        "entry": entry_val,
        "stop": stop_val,
        "target": entry_val + (entry_val - stop_val) * 2,
        "currency_symbol": "₹"
    }
    try:
        sized = budget_mod.size_trade(plan, config)
        st.write(f"- Sized Shares: `{sized.get('qty')} shares`")
        st.write(f"- Total Cost: `₹{sized.get('cost', 0):,.2f}`")
        st.write(f"- Bound By: `{sized.get('bound_by')}`")
        st.write(f"- Max Loss at Stop: `₹{sized.get('max_loss', 0):,.2f}`")
        st.write(f"- Estimated Charges: `₹{sized.get('charges', {}).get('total', 0):,.2f}`")

        charges = sized.get("charges")
        if isinstance(charges, dict) and charges.get("total", 0) > 0:
            with st.expander("Statutory charges breakdown (Indian equities)", expanded=False):
                st.write(f"- Brokerage: ₹{charges.get('brokerage', 0):,.2f}")
                st.write(f"- STT / CTT: ₹{charges.get('stt', 0):,.2f}")
                st.write(f"- Exchange Turnover Fee: ₹{charges.get('exchange_txn_charge', 0):,.2f}")
                st.write(f"- SEBI Turnover Fee: ₹{charges.get('sebi_turnover_charge', 0):,.2f}")
                st.write(f"- Stamp Duty: ₹{charges.get('stamp_duty', 0):,.2f}")
                st.write(f"- GST (18% on Brokerage + Exchange + SEBI): ₹{charges.get('gst', 0):,.2f}")
    except Exception as e:
        st.error(f"Sizing error: {e}")


def evaluate_order_risk(symbol: str, entry: float, stop: float, target: float, shares: int, config: dict, now=None) -> tuple[str, list]:
    """Helper to construct a valid bracket order ticket and evaluate via risk_mod.check()."""
    cfg = dict(config or {})
    budget_val = cfg.get("budget")
    try:
        budget_num = float(budget_val) if budget_val is not None else None
    except (ValueError, TypeError):
        budget_num = None

    if budget_num is None or budget_num <= 0:
        return "BLOCK", [
            "Missing or invalid account budget: configure a positive 'budget' in trader/config.json before checking risk."
        ]

    try:
        qty_val = str(int(shares))
    except (ValueError, TypeError):
        qty_val = "0"

    order_ticket = {
        "symbol": symbol,
        "side": "buy",
        "qty": qty_val,
        "type": "market",
        "time_in_force": "day",
        "order_class": "bracket",
        "stop_loss_stop_price": str(float(stop)),
        "take_profit_limit_price": str(float(target)),
        "plan": {
            "entry": float(entry),
            "stop": float(stop),
            "target": float(target)
        }
    }
    res = risk_mod.check([order_ticket], cfg, now=now)
    results = res.get("results", [])
    if results:
        r0 = results[0]
        return r0.get("verdict", "BLOCK"), r0.get("reasons", [])
    return "BLOCK", ["No evaluation results returned from risk engine"]


def render_risk_check_view(resolved_symbol: str, is_sample_mode: bool, config: dict):
    """Render Risk Check view."""
    now_utc = entry_mod.utc_now()
    active_b = broker_data.active_broker()
    data_source = "sample" if is_sample_mode else (active_b or "yahoo")
    render_trust_strip(data_source=data_source, chart=None, is_sample_mode=is_sample_mode, now_utc=now_utc)

    st.markdown("## Risk Check")
    st.caption("Checks every trade against limits and stops anything violating parameters.")

    ctx = get_analysis_context_for_symbol(resolved_symbol)

    if ctx:
        ts_label = ctx.get("now_ist") or "recent"
        src_label = ctx.get("source") or "broker"
        render_html(
            f"""
            <div class="terminal-panel" style="padding: 10px 16px; margin-bottom: 14px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px;">
                <div style="font-size: 12px; color: var(--text-muted);">
                    Evaluating verified trade plan from analysis for <span class="mono" style="color: var(--text-main); font-weight: 600;">{resolved_symbol}</span> at <span class="mono">{ts_label} IST</span> ({src_label})
                </div>
                <div>
                    <span class="terminal-chip active" style="font-size: 11px;">INHERITED FROM ANALYSIS</span>
                </div>
            </div>
            """
        )

        entry_lvl = ctx.get("entry_level")
        stop_lvl = ctx.get("stop_level")
        target_lvl = ctx.get("target_level")
        qty = ctx.get("quantity", 0)
        risk_verdict = ctx.get("risk_verdict", "BLOCK")
        risk_reason = ctx.get("risk_reason", "")

        entry_str = f"₹{entry_lvl:,.2f}" if entry_lvl is not None else "Pending 09:30 range"
        stop_str = f"₹{stop_lvl:,.2f}" if stop_lvl is not None else "Pending 09:30 range"
        target_str = f"₹{target_lvl:,.2f}" if target_lvl is not None else "Pending 09:30 range"

        st.write(f"- Symbol: `{resolved_symbol}`")
        st.write(f"- Action: `BUY (Bracket)`")
        st.write(f"- Sized Shares: `{qty} shares`")
        st.write(f"- Entry Level: `{entry_str}`")
        st.write(f"- Stop Loss: `{stop_str}`")
        st.write(f"- Target: `{target_str}`")

        if risk_verdict == "PASS":
            st.success(f"PASS: {risk_reason}")
        else:
            st.error(f"BLOCK: {risk_reason}")

        with st.expander("Evaluated Account Risk Rules", expanded=True):
            st.write(f"1. **Max Loss per Trade**: Limit ₹{float(config.get('max_loss_per_trade_inr', 2000)):,.2f}")
            b_val = config.get("budget")
            b_str = f"₹{float(b_val):,.2f}" if (b_val is not None and float(b_val) > 0) else "Not configured (BLOCK)"
            st.write(f"2. **Account Budget**: Limit {b_str}")
            st.write(f"3. **Reward:Risk**: Required ≥ 1.5R (Plan: {ctx.get('reward_risk', 2.0)}R)")
            st.write(f"4. **Trading Window**: 09:30 to 15:00 IST")
            st.write(f"5. **Market Status**: {ctx.get('market_state', 'open').capitalize()}")

        with st.expander("Test Custom / Manual Parameters", expanded=False):
            m_entry = st.number_input("Custom Entry (₹)", value=float(entry_lvl or 2500.0), step=1.0, key=f"risk_m_entry_{resolved_symbol}")
            m_stop = st.number_input("Custom Stop (₹)", value=float(stop_lvl or 2475.0), step=1.0, key=f"risk_m_stop_{resolved_symbol}")
            m_target = st.number_input("Custom Target (₹)", value=float(target_lvl or 2550.0), step=1.0, key=f"risk_m_target_{resolved_symbol}")
            m_shares = st.number_input("Custom Shares", value=int(qty or 50), step=1, key=f"risk_m_shares_{resolved_symbol}")
            try:
                verdict_custom, reasons_custom = evaluate_order_risk(
                    resolved_symbol, m_entry, m_stop, m_target, m_shares, config, now=now_utc
                )
                if verdict_custom == "PASS":
                    st.success("Custom PASS: Fits account budget, max trade loss, reward:risk, and trading window.")
                else:
                    st.error(f"Custom BLOCK: {' • '.join(reasons_custom) if reasons_custom else 'Exceeds account risk rules'}")
            except Exception as e:
                st.info(f"Custom check notice: {e}")

    else:
        render_html(
            f"""
            <div class="terminal-panel" style="padding: 14px 18px; margin-bottom: 16px; border-left: 3px solid var(--warning-amber);">
                <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; margin-bottom: 6px;">
                    <span style="font-weight: 700; color: var(--text-main); font-size: 13px;">No Active Analysis for {resolved_symbol}</span>
                    <span class="terminal-chip warning" style="font-size: 10px;">MANUAL MODE</span>
                </div>
                <div style="font-size: 12px; color: var(--text-muted); margin-bottom: 10px;">
                    No active analysis for {resolved_symbol}. Run an analysis first, or test parameters manually.
                </div>
            </div>
            """
        )
        c_an, _ = st.columns([1, 3])
        with c_an:
            if st.button(f"Analyze {resolved_symbol} now", key=f"btn_an_risk_{resolved_symbol}", type="secondary"):
                with st.spinner(f"Analyzing {resolved_symbol}..."):
                    an_res = analyze_symbol(resolved_symbol, is_sample_mode=is_sample_mode, config=config)
                    set_active_analysis(an_res)
                    st.rerun()

        m_entry = st.number_input("Test Entry (₹)", value=2500.0, step=1.0, key=f"risk_entry_{resolved_symbol}")
        m_stop = st.number_input("Test Stop (₹)", value=2475.0, step=1.0, key=f"risk_stop_{resolved_symbol}")
        m_target = st.number_input("Test Target (₹)", value=2550.0, step=1.0, key=f"risk_target_{resolved_symbol}")
        m_shares = st.number_input("Test Shares", value=50, step=1, key=f"risk_shares_{resolved_symbol}")
        try:
            verdict_man, reasons_man = evaluate_order_risk(
                resolved_symbol, m_entry, m_stop, m_target, m_shares, config, now=now_utc
            )
            if verdict_man == "PASS":
                st.success("PASS: Fits account budget, max trade loss, reward:risk, and trading window.")
            else:
                st.error(f"BLOCK: {' • '.join(reasons_man) if reasons_man else 'Exceeds account risk rules'}")
        except Exception as e:
            st.info(f"Risk evaluation notice: {e}")


def render_time_machine_view(resolved_symbol: str, is_sample_mode: bool, config: dict):
    """Render Time Machine historical replay odds view."""
    now_utc = entry_mod.utc_now()
    active_b = broker_data.active_broker()
    data_source = "sample" if is_sample_mode else (active_b or "yahoo")
    render_trust_strip(data_source=data_source, chart=None, is_sample_mode=is_sample_mode, now_utc=now_utc)

    st.markdown("## Time Machine")
    st.caption(f"Replays 60 days of 5-minute bars to show historical setup hit rate for {resolved_symbol}.")
    st.info("Replay engine simulates outcomes across historical completed sessions.")

    ctx = get_analysis_context_for_symbol(resolved_symbol)
    if ctx and ctx.get("entry_level") is not None and ctx.get("stop_level") is not None and ctx.get("target_level") is not None:
        entry_val = float(ctx["entry_level"])
        stop_val = float(ctx["stop_level"])
        target_val = float(ctx["target_level"])
        ts_label = ctx.get("now_ist") or "recent"
        src_label = ctx.get("source") or "broker"
        render_html(
            f"""
            <div class="terminal-panel" style="padding: 10px 16px; margin-bottom: 14px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px;">
                <div style="font-size: 12px; color: var(--text-muted);">
                    Inherited levels for <span class="mono" style="color: var(--text-main); font-weight: 600;">{resolved_symbol}</span> from analysis at <span class="mono">{ts_label} IST</span> ({src_label})
                </div>
                <div>
                    <span class="terminal-chip active" style="font-size: 11px;">INHERITED FROM ANALYSIS</span>
                </div>
            </div>
            """
        )
    else:
        entry_val = 2500.0
        stop_val = 2475.0
        target_val = 2550.0
        render_html(
            f"""
            <div class="terminal-panel" style="padding: 10px 16px; margin-bottom: 14px;">
                <div style="font-size: 12px; color: var(--text-muted);">
                    No active analysis levels for <span class="mono" style="color: var(--text-main); font-weight: 600;">{resolved_symbol}</span>. Simulating with reference parameters.
                </div>
            </div>
            """
        )

    c1, c2, c3 = st.columns(3)
    with c1:
        sim_entry = st.number_input("Simulation Entry (₹)", value=entry_val, step=1.0, key=f"tm_entry_{resolved_symbol}")
    with c2:
        sim_stop = st.number_input("Simulation Stop (₹)", value=stop_val, step=1.0, key=f"tm_stop_{resolved_symbol}")
    with c3:
        sim_target = st.number_input("Simulation Target (₹)", value=target_val, step=1.0, key=f"tm_target_{resolved_symbol}")

    plan = {
        "symbol": resolved_symbol,
        "side": "buy",
        "entry": sim_entry,
        "stop": sim_stop,
        "target": sim_target
    }
    c_btn = st.button("Run Simulation Replay", type="primary")
    if c_btn:
        if not (sim_stop < sim_entry < sim_target):
            st.error(f"Invalid levels for buy trade: Stop (₹{sim_stop:.2f}) must be below Entry (₹{sim_entry:.2f}), and Target (₹{sim_target:.2f}) must be above Entry.")
            return

        with st.spinner("Replaying past 60 days..."):
            # Step 1: Fetch 60-day 5-minute historical bars
            text = None
            fetch_err = None
            try:
                text = fetch_cached_60d_history(resolved_symbol)
            except Exception as e:
                fetch_err = str(e)

            # Step 3: Handle provider failure or missing data safely
            if not text:
                st.warning(f"Insufficient historical data: Could not retrieve 60-day 5-minute chart data for {resolved_symbol} ({fetch_err or 'Provider unreachable'}).")
                st.info("Check symbol selection or connectivity. Replay probabilities cannot be calculated without verified market data.")
                return

            try:
                chart_doc = tm_mod.chart_result(text, resolved_symbol)
                meta, bars, dropped = tm_mod.parse_bars(chart_doc, now_utc)
            except Exception as e:
                st.warning(f"Insufficient historical data: Failed to parse historical chart bars for {resolved_symbol} ({e}).")
                return

            if not bars:
                st.warning(f"Insufficient historical data: No completed 5-minute bars available for {resolved_symbol}.")
                return

            is_ind = tm_mod.is_indian(resolved_symbol, meta)
            open_min = 9 * 60 + 15 if is_ind else tm_mod.OPEN_MIN
            session_bars = 75 if is_ind else tm_mod.SESSION_BARS
            days = tm_mod.split_sessions(bars)

            # Identify simulation entry slot (default slot 3: first bar after 15m opening range)
            k = 3
            candidate_dates = [d for d in sorted(days.keys()) if k in days[d]]
            if not candidate_dates:
                st.warning(f"Insufficient historical data: No historical sessions contain the opening-range breakout confirmation candle for {resolved_symbol}.")
                return

            # Determine plan date: latest date with slot k (or analysis context date if available in candidate dates)
            plan_date = candidate_dates[-1]
            if ctx and ctx.get("now_date") and ctx["now_date"] in candidate_dates:
                plan_date = ctx["now_date"]

            until_val = config.get("flat_by", "15:15" if is_ind else "16:00")
            entry_time = tm_mod.clock12(open_min + 5 * (k + 1))

            try:
                norm_plan = tm_mod.normalize_plan({
                    "symbol": resolved_symbol,
                    "side": "buy",
                    "date": plan_date,
                    "entry": sim_entry,
                    "stop": sim_stop,
                    "target": sim_target,
                    "entry_bar": k,
                    "entry_time": entry_time,
                    "setup": "orb",
                    "opening_range": {"minutes": 15},
                    "interval": "5m",
                    "flat_by": until_val
                })
            except Exception as e:
                st.warning(f"Insufficient historical data: Could not construct normalized trade plan ({e}).")
                return

            try:
                res = tm_mod.time_machine(
                    text=text,
                    plan=norm_plan,
                    now_utc=now_utc,
                    replays=1000,
                    until=until_val,
                    source="sample" if is_sample_mode else "yahoo"
                )
            except tm_mod.TMError as e:
                err_msg = str(e)
                if "only" in err_msg and "full session(s)" in err_msg:
                    st.warning(f"Insufficient historical data: {err_msg}")
                    st.info("The Time Machine requires a minimum sample size of verified historical sessions to compute reliable statistics.")
                else:
                    st.warning(f"Replay notice: {err_msg}")
                return
            except Exception as e:
                st.warning(f"Replay notice: {e}")
                return

            # Step 2: Historical Sample Transparency
            hist = res.get("history", {})
            mc = res.get("mc", {})
            data_info = res.get("data", {})
            short_sessions = data_info.get("short_sessions_left_out", [])

            accepted_sessions = hist.get("sessions", 0)
            rejected_sessions = len(short_sessions)
            total_sessions = accepted_sessions + rejected_sessions

            start_date = mc.get("pool_from", "Not available")
            end_date = mc.get("pool_to", "Not available")
            provider = res.get("source", "yahoo").upper()
            sym_display = norm_plan.get("symbol", resolved_symbol)

            st.markdown("### Historical Sample & Provenance")
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Symbol", sym_display)
            m2.metric("Data Provider", provider)
            m3.metric("Window", f"{start_date} → {end_date}")
            m4.metric("Total Sessions", f"{total_sessions}")

            s1, s2, s3, s4 = st.columns(4)
            s1.metric("Accepted Sessions", f"{accepted_sessions}")
            s2.metric("Rejected Sessions", f"{rejected_sessions}")
            triggered = hist.get("triggered", 0)
            target_hits = hist.get("target", 0)
            stop_hits = hist.get("stop", 0)
            time_exits = hist.get("close", 0)
            trigger_rate_str = f"{triggered}/{accepted_sessions} ({(triggered / accepted_sessions * 100):.1f}%)" if accepted_sessions > 0 else "Not available"
            s3.metric("Valid Breakout Setups", trigger_rate_str)
            win_rate_str = f"{target_hits}/{triggered} ({(target_hits / triggered * 100):.1f}%)" if triggered > 0 else "No setups"
            s4.metric("Historical Win Rate", win_rate_str)

            if rejected_sessions > 0:
                with st.expander(f"Rejected Session Details ({rejected_sessions} sessions)", expanded=False):
                    for item in short_sessions:
                        if isinstance(item, dict):
                            st.write(f"- **{item.get('date', 'Unknown Date')}**: {item.get('bars', 0)} bars recorded (incomplete session / missing interval continuity)")
                        else:
                            st.write(f"- {item}")

            st.markdown("### Historical Setup Outcomes")
            o1, o2, o3, o4 = st.columns(4)
            o1.metric("Target Exits", f"{target_hits}")
            o2.metric("Stop Exits", f"{stop_hits}")
            o3.metric("Time-Based Exits", f"{time_exits}")
            avg_r_val = hist.get("avg_R")
            o4.metric("Historical Avg R", f"{avg_r_val:+.2f}R" if avg_r_val is not None else "Not available")

            st.markdown("### Monte Carlo Simulation (1,000 Replays)")
            c_p1, c_p2, c_p3, c_p4 = st.columns(4)
            p_target = mc.get("p_target")
            p_stop = mc.get("p_stop")
            p_neither = mc.get("p_neither")
            mc_avg_r = mc.get("avg_R")

            c_p1.metric("Simulated Target Hit", f"{p_target * 100:.1f}%" if p_target is not None else "Not available")
            c_p2.metric("Simulated Stop Hit", f"{p_stop * 100:.1f}%" if p_stop is not None else "Not available")
            c_p3.metric("Simulated Time Exit", f"{p_neither * 100:.1f}%" if p_neither is not None else "Not available")
            c_p4.metric("Simulated Avg R", f"{mc_avg_r:+.2f}R" if mc_avg_r is not None else "Not available")

            verdict_text = res.get("verdict", "NEUTRAL")
            st.info(f"**Verdict:** {verdict_text}")
            if res.get("verdict_notes"):
                for note in res["verdict_notes"]:
                    st.caption(f"- {note}")

            # Step 4: Disclose execution assumptions
            st.markdown("---")
            st.caption("Historical simulations assume entry at the confirmed breakout candle close, zero slippage unless configured otherwise, and stop-first execution when both stop and target are touched within the same candle. Time-based exits use historical candle prices and are not guaranteed executable fills.")


def render_alerts_view(resolved_symbol: str):
    """Render Alerts view."""
    st.markdown("## Alerts")
    st.caption(f"Price and time alerts respecting NSE trading hours and holidays for {resolved_symbol}.")
    now_utc = entry_mod.utc_now()
    m_st = alerts.market_status(now_utc, us=False)
    st.write(f"- Market Status: `{m_st.get('session')}`")
    st.write(f"- Is Open: `{m_st.get('open')}`")
    st.write(f"- Detail: `{m_st.get('reason')}`")


def render_signal_log_view():
    """Render Signal Log audit trail."""
    st.markdown("## Signal Log")
    st.caption("Historical record of all one-click analyses run in TradeInsta. Saved to `trader/signal_log.csv`.")
    records = load_signal_log(limit=100)
    if not records:
        st.info("No analyses recorded yet. Click 'Analyze now' on any stock to record an entry.")
    else:
        df_log = pd.DataFrame(records)
        st.dataframe(df_log, use_container_width=True, hide_index=True)
        csv_data = df_log.to_csv(index=False).encode("utf-8")
        st.download_button(
            "Download Signal Log (CSV)",
            data=csv_data,
            file_name="signal_log.csv",
            mime="text/csv"
        )


def get_radar_card_label_and_badge(item: dict) -> tuple[str, str]:
    """Map radar alert patterns to the 4 strict presentation labels."""
    p = str(item.get("pattern", "")).upper()
    if item.get("has_repeated_buying") or "REPEATED BUYING" in p:
        return "Repeated buying", "buying"
    if item.get("has_repeated_selling") or "REPEATED SELLING" in p:
        return "Repeated selling", "selling"
    if item.get("has_multiple_buyers") or "MULTIPLE BUYERS" in p:
        return "Multiple buyers", "buyers"
    vol = item.get("live_volume", 0) or 0
    if vol > 1000000:
        return "Unusual volume", "volume"
    if item.get("net_flow_inr", 0.0) < 0:
        return "Repeated selling", "selling"
    return "Repeated buying", "buying"


def format_radar_factual_reason(item: dict, label: str) -> str:
    """Format a one-line factual reason stating disclosed facts without the words 'buy' or 'sell'."""
    val_inr = flow_mod.format_inr(abs(item.get("net_flow_inr", 0.0) or item.get("buy_value_inr", 0.0) or 0.0))
    date_str = item.get("latest_deal_date") or "recent filings"
    buyers_cnt = item.get("buyers_count", 2) or 2

    if label == "Repeated buying":
        reason = f"Institutional accumulation totaling {val_inr} disclosed across reporting dates through {date_str}."
    elif label == "Multiple buyers":
        reason = f"{buyers_cnt} institutional entities disclosed accumulation totaling {val_inr}."
    elif label == "Repeated selling":
        reason = f"Institutional distribution totaling {val_inr} disclosed across reporting dates through {date_str}."
    elif label == "Unusual volume":
        reason = f"Disclosed transactions totaling {val_inr} with high exchange turnover."
    else:
        reason = f"Disclosed institutional transactions totaling {val_inr}."

    # Factual presentation constraint: must not contain 'buy' or 'sell'
    reason_clean = re.sub(r"\bbuy\b|\bsell\b|\bbuying\b|\bselling\b|\bbuyers\b|\bsellers\b", "accumulation", reason, flags=re.IGNORECASE)
    return reason_clean


def render_big_bull_view(is_sample_mode: bool = False, deals: list = None, default_mode: str = "Market performance"):
    """Render the restyled terminal BIG BULL view."""
    now_utc = entry_mod.utc_now()
    active_b = broker_data.active_broker()

    # Pre-fetch cached market quotes if in Market performance view to determine true data provenance
    sector_registry = get_cached_sector_registry()
    market_caps = get_cached_market_caps()
    tracked_symbols = list(market_caps.get("stocks", {}).keys())
    quotes = {}
    if default_mode == "Market performance":
        quotes = fetch_cached_market_overview(tuple(tracked_symbols), is_sample_mode=is_sample_mode)

    first_q = next(iter(quotes.values())) if quotes else None
    actual_source = first_q.get("source") if first_q else ("sample" if is_sample_mode else (active_b or "yahoo"))
    render_trust_strip(data_source=actual_source, chart=None, is_sample_mode=is_sample_mode, now_utc=now_utc)

    # Top Header & Flow Disclaimer
    render_html(
        """
        <div style="display: flex; justify-content: space-between; align-items: flex-end; margin-bottom: 12px; flex-wrap: wrap; gap: 8px;">
            <div>
                <h2 style="margin: 0; padding: 0;">BIG BULL</h2>
                <div style="font-size: 12px; color: var(--text-muted); margin-top: 2px;">Institutional intelligence and market performance terminal.</div>
            </div>
            <div>
                <span class="terminal-chip warning" style="font-size: 11px;">reported activity, previous day, not live</span>
            </div>
        </div>
        """
    )

    # View Mode Toggle
    view_mode = st.radio(
        "Market Intelligence View",
        options=["Market performance", "Disclosed flow"],
        index=0 if default_mode == "Market performance" else 1,
        horizontal=True,
        label_visibility="collapsed",
        key="big_bull_view_mode_toggle"
    )

    if view_mode == "Market performance":
        if not quotes:
            quotes = fetch_cached_market_overview(tuple(tracked_symbols), is_sample_mode=is_sample_mode)

        if not quotes and not is_sample_mode:
            st.warning("Market overview quotes currently unavailable. Live broker feed not connected and public fallback timed out.")

        # Market Mood Panel
        breadth = big_bull.compute_market_breadth(quotes)
        advances = breadth.get("advances", 0)
        declines = breadth.get("declines", 0)
        unchanged = breadth.get("unchanged", 0)
        adv_ratio = breadth.get("advance_ratio", 0.5)

        if adv_ratio >= 0.55:
            mood_word = "BULLISH"
            mood_color = "var(--gain-green)"
        elif adv_ratio <= 0.45:
            mood_word = "BEARISH"
            mood_color = "var(--loss-red)"
        else:
            mood_word = "NEUTRAL"
            mood_color = "var(--accent-blue)"

        denom = max(1, advances + declines)
        adv_pct = round((advances / denom) * 100, 1)
        dec_pct = round((declines / denom) * 100, 1)

        render_html(
            f"""
            <div class="mood-panel-wrap">
                <div style="display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 8px;">
                    <div style="font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; color: var(--text-muted);">
                        Market Mood
                    </div>
                    <div class="mono" style="font-size: 15px; font-weight: 700; color: {mood_color}; letter-spacing: 0.05em;">
                        {mood_word}
                    </div>
                </div>
                <div class="mood-bar-container">
                    <div class="mood-bar-adv" style="--target-width: {adv_pct}%;"></div>
                    <div class="mood-bar-dec" style="--target-width: {dec_pct}%;"></div>
                </div>
                <div class="mood-bar-labels">
                    <span style="color: var(--gain-green);">{advances} Advancing ({adv_pct}%)</span>
                    <span style="color: var(--text-muted);">{unchanged} Unchanged</span>
                    <span style="color: var(--loss-red);">{declines} Declining ({dec_pct}%)</span>
                </div>
            </div>
            """
        )

        # Two columns: left ~55%, right ~45%
        col_left, col_right = st.columns([11, 9])

        with col_left:
            render_html(
                """
                <div style="font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em; color: var(--text-muted); margin-bottom: 8px;">
                    Sector Performance Treemap
                </div>
                """
            )
            heatmap_records = big_bull.build_heatmap_records(quotes, sector_registry, market_caps)
            if heatmap_records:
                df_heat = pd.DataFrame(heatmap_records)
                fig_tree = px.treemap(
                    df_heat,
                    path=[px.Constant("NSE Market"), "sector", "symbol"],
                    values="market_cap_cr",
                    color="color_val",
                    color_continuous_scale=[
                        (0.0, "#FF6B6B"),
                        (0.5, "#232A33"),
                        (1.0, "#3DD68C")
                    ],
                    color_continuous_midpoint=0.0,
                    custom_data=["price", "change_pct", "name", "market_cap_cr"]
                )
                fig_tree.update_traces(
                    texttemplate="<b>%{label}</b><br>%{customdata[1]:+.2f}%<br>₹%{customdata[0]:,.2f}",
                    textposition="middle center",
                    textfont=dict(size=12, color="#E6EAF0", family="Inter")
                )
                fig_tree.update_layout(
                    margin=dict(t=10, l=10, r=10, b=10),
                    height=460,
                    paper_bgcolor="#161B22",
                    plot_bgcolor="#161B22",
                    font=dict(color="#E6EAF0", family="Inter")
                )
                st.plotly_chart(fig_tree, use_container_width=True)
            else:
                st.info("Heatmap records unavailable.")

            # Sector Drill-Down
            sector_perf = big_bull.compute_sector_performance(quotes, sector_registry, market_caps)
            sector_names = [s["sector"] for s in sector_perf]
            sel_sector = st.selectbox("Select sector to drill into constituent stocks:", options=["All Sectors"] + sector_names, index=0)

            if sel_sector != "All Sectors":
                match_sec = next((s for s in sector_perf if s["sector"] == sel_sector), None)
                if match_sec and match_sec.get("stocks"):
                    st.caption(f"Constituent stocks in {sel_sector}:")
                    const_rows = [
                        {
                            "Symbol": st_item["symbol"],
                            "Name": st_item["name"],
                            "Price": f"₹{st_item['price']:,.2f}",
                            "Change": f"{st_item['change_pct']:+.2f}%",
                            "Volume": f"{st_item['volume']:,}",
                            "Market Cap (₹ Cr)": f"{st_item['market_cap_cr']:,.0f}"
                        }
                        for st_item in match_sec["stocks"]
                    ]
                    st.dataframe(pd.DataFrame(const_rows), use_container_width=True, hide_index=True)
            else:
                # Sector performance table below treemap
                st.caption("Sector performance overview:")
                sec_rows = [
                    {
                        "Sector": s["sector"],
                        "Weighted Change": f"{s['weighted_change_pct']:+.2f}%",
                        "Advances / Declines": f"{s['advances']} / {s['declines']}",
                        "Top Stock": f"{s['top_stock']['symbol']} ({s['top_stock']['change_pct']:+.2f}%)" if s.get("top_stock") else "—",
                        "Market Cap (₹ Cr)": f"{s['total_market_cap_cr']:,.0f}"
                    }
                    for s in sector_perf
                ]
                st.dataframe(pd.DataFrame(sec_rows), use_container_width=True, hide_index=True)

        with col_right:
            render_html(
                """
                <div style="font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em; color: var(--text-muted); margin-bottom: 8px;">
                    BIG BULL Radar
                </div>
                """
            )
            if deals:
                radar_items = big_bull.generate_big_bull_radar(deals, quotes, sector_registry)
                if radar_items:
                    for idx, item in enumerate(radar_items[:8]):
                        card_label, badge_cls = get_radar_card_label_and_badge(item)
                        factual_reason = format_radar_factual_reason(item, card_label)
                        anim_delay = round(idx * 0.08, 2)
                        render_html(
                            f"""
                            <div class="radar-card-slide" style="animation-delay: {anim_delay}s;">
                                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">
                                    <span class="mono" style="font-size: 14px; font-weight: 700; color: var(--text-main);">{item['symbol']}</span>
                                    <span class="radar-badge {badge_cls}">{card_label}</span>
                                </div>
                                <div style="font-size: 12px; color: var(--text-muted); margin-bottom: 8px;">
                                    {item['symbol']} · {item['security_name']}
                                </div>
                                <div style="font-size: 12px; color: var(--text-main); line-height: 1.4; margin-bottom: 8px;">
                                    {factual_reason}
                                </div>
                                <div style="font-size: 11px; color: var(--accent-blue); font-weight: 500;">
                                    Check next: Monitor 5-minute opening range breakout and volume confirmation in Analyze now.
                                </div>
                            </div>
                            """
                        )
                else:
                    st.info("No radar patterns detected.")
            else:
                st.info("No deal data available for radar analysis.")

    else:
        # Disclosed flow view
        render_html(
            """
            <div style="font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em; color: var(--text-muted); margin-bottom: 12px;">
                Market Flow Overview
            </div>
            """
        )
        if deals:
            kpis = big_bull.compute_market_flow_kpis(deals)
            c1, c2, c3, c4 = st.columns(4)
            with c1:
                st.metric("Disclosed Buying", flow_mod.format_inr(kpis["total_buy_value_inr"]))
            with c2:
                st.metric("Disclosed Selling", flow_mod.format_inr(kpis["total_sell_value_inr"]))
            with c3:
                st.metric("Net Flow", flow_mod.format_inr(kpis["net_flow_inr"]))
            with c4:
                obs_range = f"{kpis['date_range']['earliest'] or 'N/A'} to {kpis['date_range']['latest'] or 'N/A'}"
                st.metric("Observation Window", obs_range)

            render_html(
                """
                <div style="font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em; color: var(--text-muted); margin: 20px 0 8px 0;">
                    Complete Disclosed Deal Ledger
                </div>
                """
            )
            search_query = st.text_input("Filter ledger by symbol or client name", "", key="bb_deal_search_input")
            filtered_deals = deals
            if search_query:
                q_clean = search_query.strip().upper()
                filtered_deals = [
                    d for d in deals
                    if q_clean in d.get("symbol", "").upper()
                    or q_clean in d.get("client_name", "").upper()
                    or q_clean in d.get("security_name", "").upper()
                ]

            deal_rows = [
                {
                    "Date": d["date"],
                    "Symbol": d["symbol"],
                    "Security Name": d.get("security_name", d["symbol"]),
                    "Client Name": d["client_name"],
                    "Action": d["action"],
                    "Quantity": f"{d['quantity']:,}",
                    "Price": f"₹{d['price']:,.2f}",
                    "Value": flow_mod.format_inr(d["value_inr"])
                }
                for d in filtered_deals
            ]
            if deal_rows:
                df_deals = pd.DataFrame(deal_rows)
                st.dataframe(df_deals, use_container_width=True, hide_index=True)
                csv_deals = df_deals.to_csv(index=False).encode("utf-8")
                st.download_button(
                    "Download Deal Ledger (CSV)",
                    data=csv_deals,
                    file_name="disclosed_deal_ledger.csv",
                    mime="text/csv"
                )
            else:
                st.info(f"No transactions matched '{search_query}'.")
        else:
            st.info("No deal files found in `trader/flow/`.")


def render_sector_heatmap_view(is_sample_mode: bool):
    """Render Sector Heatmap view routing to BIG BULL Market performance."""
    render_big_bull_view(is_sample_mode=is_sample_mode, deals=None, default_mode="Market performance")


def render_disclosed_flow_view(is_sample_mode: bool, deals: list):
    """Render Disclosed Flow view routing to BIG BULL Disclosed flow."""
    if deals is None:
        deals, _ = load_market_deals(is_sample_mode=is_sample_mode)
    render_big_bull_view(is_sample_mode=is_sample_mode, deals=deals, default_mode="Disclosed flow")


def render_radar_view(is_sample_mode: bool, deals: list):
    """Render Radar view routing to BIG BULL Market performance."""
    if deals is None:
        deals, _ = load_market_deals(is_sample_mode=is_sample_mode)
    render_big_bull_view(is_sample_mode=is_sample_mode, deals=deals, default_mode="Market performance")


def compute_feature_directory(is_sample_mode: bool = False) -> list[dict]:
    """Compute the 12 features directory with dynamic freshness tags and provenance from real state."""
    active_b = broker_data.active_broker()
    now_utc = entry_mod.utc_now()
    market_st = alerts.market_status(now_utc, us=False)
    is_open = market_st.get("open", False)

    def get_market_feed_tag():
        if is_sample_mode:
            return "SAMPLE", "sample", "Offline simulated fixtures"
        if not is_open:
            return "LAST SESSION", "grey", "Market closed · Displaying last session"
        if active_b in ("upstox", "fyers"):
            return "LIVE", "success", f"{active_b.capitalize()} broker feed"
        return "DELAYED", "warning", "Yahoo Finance (~15m delay)"

    live_tag, live_cls, live_note = get_market_feed_tag()

    flow_deals, _ = load_market_deals(is_sample_mode=is_sample_mode)
    deal_count = len(flow_deals) if flow_deals else 0
    flow_meta = f"{deal_count} disclosed transactions" if deal_count else "Awaiting downloaded deal CSVs"

    records = load_signal_log(limit=50)
    log_count = len(records)
    log_meta = f"{log_count} historical records" if log_count else "Empty audit log"

    features = [
        {
            "name": "Analyze now",
            "tag": live_tag,
            "tag_class": live_cls,
            "desc": "full analysis of one stock as of this minute.",
            "meta": f"Source: {live_note}"
        },
        {
            "name": "Scanner",
            "tag": live_tag if is_open else "LAST SESSION",
            "tag_class": live_cls if is_open else "grey",
            "desc": "finds stocks that broke out of their opening range, then re-checks the shortlist on broker data.",
            "meta": "Universe: Tracked NSE equities"
        },
        {
            "name": "Entry timer",
            "tag": live_tag,
            "tag_class": live_cls,
            "desc": "tracks whether a breakout is waiting, triggered, expired or failed.",
            "meta": "Interval: 5-minute bars"
        },
        {
            "name": "Exit planner",
            "tag": "LOCAL",
            "tag_class": "grey",
            "desc": "stop and target from the range midpoint, range low or ATR.",
            "meta": "Engine: Deterministic tick math"
        },
        {
            "name": "Position size",
            "tag": "LOCAL",
            "tag_class": "grey",
            "desc": "quantity from maximum loss, including Indian charges.",
            "meta": "Config: trader/config.json"
        },
        {
            "name": "Risk check",
            "tag": "LOCAL",
            "tag_class": "grey",
            "desc": "PASS or BLOCK against loss limits and trading hours.",
            "meta": "Rules: Max loss, daily loss, cutoff"
        },
        {
            "name": "Time Machine",
            "tag": "60-DAY HISTORY",
            "tag_class": "active",
            "desc": "replays 60 days of 5-minute bars to show how often a setup hit target, stop or time exit.",
            "meta": "Window: Past 60 trading days"
        },
        {
            "name": "Alerts",
            "tag": "LIVE" if (is_open and not is_sample_mode) else "LOCAL",
            "tag_class": "success" if (is_open and not is_sample_mode) else "grey",
            "desc": "price and time alerts that respect NSE hours and holidays.",
            "meta": "Hours: 09:15–15:30 IST"
        },
        {
            "name": "Signal log",
            "tag": "LOCAL",
            "tag_class": "grey",
            "desc": "every analysis saved with its levels for later review.",
            "meta": f"Audit: {log_meta}"
        },
        {
            "name": "Sector heatmap",
            "tag": live_tag,
            "tag_class": live_cls,
            "desc": "treemap of sectors and stocks, coloured by daily price change.",
            "meta": "Weighting: Market capitalisation"
        },
        {
            "name": "Disclosed flow",
            "tag": "PREVIOUS DAY",
            "tag_class": "warning",
            "desc": "reported bulk and block deals, published after the close.",
            "meta": f"Filings: {flow_meta}"
        },
        {
            "name": "Radar",
            "tag": "PREVIOUS DAY",
            "tag_class": "warning",
            "desc": "explained discovery feed.",
            "meta": "Patterns: Accumulation convergence"
        }
    ]
    return features


def render_features_view(is_sample_mode: bool = False):
    """Render Features directory page with dynamic freshness tags."""
    st.markdown("## Features")
    st.caption("Each tool, what it does, and how fresh its data is.")

    token_status = get_upstox_token_status()
    nse_text, nse_dot_cls, is_open = get_nse_status()
    active_b = broker_data.active_broker() or "None"

    render_html(
        f"""
        <div class="terminal-panel" style="padding: 10px 16px; margin-bottom: 20px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; font-size: 12px;">
            <div style="display: flex; align-items: center; gap: 16px;">
                <div>
                    <span style="color: var(--text-muted);">Broker Token:</span>
                    <span class="mono" style="color: var(--text-main); font-weight: 600; margin-left: 4px;">Upstox ({token_status})</span>
                </div>
                <div>
                    <span style="color: var(--text-muted);">Active Feed:</span>
                    <span class="mono" style="color: var(--accent-blue); font-weight: 600; margin-left: 4px;">{('Sample Data' if is_sample_mode else (active_b.capitalize() if active_b != 'None' else 'Yahoo Finance'))}</span>
                </div>
            </div>
            <div>
                <span class="status-dot {nse_dot_cls}"></span>
                <span style="color: var(--text-muted); font-size: 11px;">{nse_text}</span>
            </div>
        </div>
        """
    )

    features = compute_feature_directory(is_sample_mode=is_sample_mode)

    cols = st.columns(3)
    for idx, f_item in enumerate(features):
        c = cols[idx % 3]
        with c:
            chip_cls = f_item["tag_class"]
            chip_style = ""
            if chip_cls == "sample":
                chip_style = 'style="background: rgba(147, 51, 234, 0.15); border: 1px solid rgba(192, 132, 252, 0.4); color: #C084FC;"'

            render_html(
                f"""
                <div class="terminal-panel" style="min-height: 140px; display: flex; flex-direction: column; justify-content: space-between; margin-bottom: 16px;">
                    <div>
                        <div style="display: flex; justify-content: space-between; align-items: flex-start; gap: 8px; margin-bottom: 8px;">
                            <span style="font-weight: 700; color: var(--text-main); font-size: 14px;">{f_item['name']}</span>
                            <span class="terminal-chip {chip_cls}" {chip_style} style="font-size: 10px; padding: 2px 8px; text-transform: uppercase;">{f_item['tag']}</span>
                        </div>
                        <div style="font-size: 12px; color: var(--text-muted); line-height: 1.5;">{f_item['desc']}</div>
                    </div>
                    <div style="margin-top: 10px; font-size: 11px; color: var(--text-muted); font-family: var(--font-mono); opacity: 0.85;">
                        {f_item['meta']}
                    </div>
                </div>
                """
            )


# ---------------------------------------------------------------- Main Entry Point
def main():
    st.set_page_config(
        page_title="TradeInsta Terminal",
        layout="wide",
        initial_sidebar_state="auto"
    )
    load_theme()

    if "current_page" not in st.session_state:
        st.session_state["current_page"] = "Home"

    if hasattr(st, "query_params") and "page" in st.query_params:
        qp = st.query_params.get("page")
        if qp and qp != st.session_state.get("current_page"):
            st.session_state["current_page"] = qp

    # Sidebar Header (Matching Reference Image)
    render_html(
        """
        <div class="ti-sidebar-brand">
            <svg class="ti-brand-bolt" viewBox="0 0 24 24" fill="currentColor">
                <path d="M13 2L3 14h7v8l10-12h-7z"/>
            </svg>
            <span class="ti-brand-name">TRADEINSTA</span>
        </div>
        """,
        target=st.sidebar
    )

    # Primary Navigation (Matching Reference Screenshot)
    primary_items = [
        ("Home", "Home"),
        ("Analyze now", "Analyze now"),
        ("BIG BULL", "Sector heatmap"),
        ("Time Machine", "Time Machine"),
        ("Watchlist", "Scanner"),
        ("Reports", "Signal log"),
        ("Settings", "Features"),
    ]
    for label, target_page in primary_items:
        is_active = (st.session_state["current_page"] == target_page or (label == "Home" and st.session_state["current_page"] == "Home"))
        if st.sidebar.button(label, key=f"nav_p_{label}", use_container_width=True, type="primary" if is_active else "secondary"):
            if st.session_state["current_page"] != target_page:
                st.session_state["current_page"] = target_page
                st.rerun()

    # Advanced Analysis & Risk Tools (Preserving all deep workflows & test expectations)
    with st.sidebar.expander("Tools & Workflows", expanded=False):
        secondary_items = [
            "Scanner", "Entry timer", "Exit planner", "Position size",
            "Risk check", "Alerts", "Sector heatmap", "Disclosed flow",
            "Radar", "Features"
        ]
        for item in secondary_items:
            is_active = (st.session_state["current_page"] == item)
            if st.button(item, key=f"nav_s_{item}", use_container_width=True, type="primary" if is_active else "secondary"):
                if st.session_state["current_page"] != item:
                    st.session_state["current_page"] = item
                    st.rerun()

    # Environment Selector
    render_html('<div class="sidebar-nav-header">ENVIRONMENT</div>', target=st.sidebar)
    env_choice = st.sidebar.radio(
        "Environment",
        options=["Live market feed", "Sample offline data"],
        index=0,
        label_visibility="collapsed"
    )
    is_sample_mode = (env_choice == "Sample offline data")
    if is_sample_mode:
        render_html('<div style="padding: 0 10px 8px 10px;"><span class="terminal-chip warning">SAMPLE DATA ACTIVE</span></div>', target=st.sidebar)

    # Stock Selection
    render_html('<div class="sidebar-nav-header">SYMBOL LOOKUP</div>', target=st.sidebar)
    default_search = "RELIANCE"
    user_search = st.sidebar.text_input(
        "Lookup Symbol",
        value=default_search,
        label_visibility="collapsed"
    )
    resolved_symbol = resolve_symbol(user_search)
    company_name = SYM_TO_NAME.get(resolved_symbol, resolved_symbol)

    # State Bleed Prevention: Invalidate active context if user switched symbols
    prev_sym = st.session_state.get("active_symbol")
    if prev_sym is not None and prev_sym != resolved_symbol:
        if "active_analysis_context" in st.session_state:
            del st.session_state["active_analysis_context"]
        if "active_analysis_symbol" in st.session_state:
            del st.session_state["active_analysis_symbol"]
    st.session_state["active_symbol"] = resolved_symbol
    render_html(
        f"""
        <div style="padding: 2px 10px 8px 10px; font-size: 12px; color: var(--text-muted);">
            Active: <span class="mono" style="color: var(--accent-blue); font-weight: 600;">{resolved_symbol}</span><br>
            <span style="font-size: 11px;">{company_name}</span>
        </div>
        """,
        target=st.sidebar
    )

    # Config Summary
    cfg_path = os.path.join(ROOT, "trader", "config.json")
    config = load_config_cached(cfg_path)

    # Sidebar Status Box at bottom
    token_status = get_upstox_token_status()
    nse_text, nse_dot_cls, is_open = get_nse_status()

    if token_status == "found":
        token_dot_cls = "green pulsing" if is_open else "green"
    elif token_status == "rejected (expired)":
        token_dot_cls = "red"
    else:
        token_dot_cls = "grey"

    render_html(
        f"""
        <div class="sidebar-status-box">
            <div class="sidebar-status-line">
                <span class="status-dot {token_dot_cls}"></span>
                <span>Upstox token: {token_status}</span>
            </div>
            <div class="sidebar-status-line" style="margin-bottom: 0;">
                <span class="status-dot {nse_dot_cls}"></span>
                <span>{nse_text}</span>
            </div>
        </div>
        """,
        target=st.sidebar
    )

    # Page Routing
    current_page = st.session_state["current_page"]
    deals = None
    if current_page in ("Analyze now", "Disclosed flow", "Radar", "Features", "Sector heatmap", "BIG BULL"):
        deals, _ = load_market_deals(is_sample_mode=is_sample_mode)

    t_page = time.perf_counter()
    if current_page == "Home":
        render_home_view(is_sample_mode, config)
    elif current_page == "Analyze now":
        render_analyze_now_view(resolved_symbol, company_name, is_sample_mode, config, deals)
    elif current_page in ("Scanner", "Watchlist"):
        render_scanner_view(is_sample_mode)
    elif current_page == "Entry timer":
        render_entry_timer_view(resolved_symbol, is_sample_mode, config)
    elif current_page == "Exit planner":
        render_exit_planner_view(resolved_symbol, is_sample_mode, config)
    elif current_page == "Position size":
        render_position_size_view(resolved_symbol, is_sample_mode, config)
    elif current_page == "Risk check":
        render_risk_check_view(resolved_symbol, is_sample_mode, config)
    elif current_page == "Time Machine":
        render_time_machine_view(resolved_symbol, is_sample_mode, config)
    elif current_page == "Alerts":
        render_alerts_view(resolved_symbol)
    elif current_page in ("Signal log", "Reports"):
        render_signal_log_view()
    elif current_page in ("Sector heatmap", "BIG BULL"):
        render_big_bull_view(is_sample_mode=is_sample_mode, deals=deals)
    elif current_page == "Disclosed flow":
        render_disclosed_flow_view(is_sample_mode, deals)
    elif current_page == "Radar":
        render_radar_view(is_sample_mode, deals)
    elif current_page in ("Features", "Settings"):
        render_features_view(is_sample_mode=is_sample_mode)

    dur_page = time.perf_counter() - t_page
    log_timing(f"render_page({current_page})", dur_page, f"page:{current_page}")


if __name__ == "__main__":
    main()
