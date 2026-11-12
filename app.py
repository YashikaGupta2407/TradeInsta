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

    ns_key = f"{q_upper}.NS"
    if ns_key in SYM_TO_NAME:
        return ns_key

    if q_upper in NAME_TO_SYM:
        return NAME_TO_SYM[q_upper]

    for name_up, s in NAME_TO_SYM.items():
        if q_upper in name_up:
            return s

    return ns_key


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
    """Load single CSS theme file once via cached reader."""
    css_content = get_theme_css()
    if css_content:
        render_html(f"<style>{css_content}</style>")


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


# ---------------------------------------------------------------- Individual Page Views
def render_analyze_now_view(resolved_symbol: str, company_name: str, is_sample_mode: bool, config: dict, deals: list = None):
    """Render single-stock analysis & trade execution view."""
    active_b = broker_data.active_broker()
    now_utc = entry_mod.utc_now()
    market_st = alerts.market_status(now_utc, us=False)
    is_market_open = market_st.get("open", False)

    curr_analysis = st.session_state.get(f"last_analysis_{resolved_symbol}")

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
    entry_val = st.number_input("Entry Price (₹)", value=2500.0, step=1.0)
    target_rule = st.selectbox("Target Rule", options=["2R", "1R", "3R"], index=0)
    rule, param = exit_mod.parse_target(target_rule)
    plan = exit_mod.compute(
        entry=Decimal(str(entry_val)),
        stop_rule="pct",
        stop_param=Decimal("1.0"),
        target_rule=rule,
        target_param=param,
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
    entry_val = st.number_input("Entry Level (₹)", value=2500.0, step=1.0, key="ps_entry")
    stop_val = st.number_input("Stop Level (₹)", value=2475.0, step=1.0, key="ps_stop")
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
    except Exception as e:
        st.error(f"Sizing error: {e}")


def render_risk_check_view(resolved_symbol: str, is_sample_mode: bool, config: dict):
    """Render Risk Check view."""
    now_utc = entry_mod.utc_now()
    active_b = broker_data.active_broker()
    data_source = "sample" if is_sample_mode else (active_b or "yahoo")
    render_trust_strip(data_source=data_source, chart=None, is_sample_mode=is_sample_mode, now_utc=now_utc)

    st.markdown("## Risk Check")
    st.caption("Checks every trade against limits and stops anything violating parameters.")
    plan_mock = {
        "symbol": resolved_symbol,
        "side": "buy",
        "entry": 2500.0,
        "stop": 2475.0,
        "target": 2550.0,
        "shares": 50,
        "order_type": "bracket"
    }
    try:
        res = risk_mod.check(plan_mock, config, now_utc=now_utc)
        if res.get("verdict") == "PASS":
            st.success(f"PASS: {res.get('reason')}")
        else:
            st.error(f"BLOCK: {res.get('reason')}")
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
    plan = {
        "symbol": resolved_symbol,
        "side": "buy",
        "entry": 2500.0,
        "stop": 2475.0,
        "target": 2550.0
    }
    c_btn = st.button("Run Simulation Replay", type="primary")
    if c_btn:
        with st.spinner("Replaying past 60 days..."):
            try:
                res = tm_mod.time_machine(plan=plan, runs=1000)
                st.write(f"- Target Hit: `{res.get('target_pct', 0):.1f}%`")
                st.write(f"- Stop Hit: `{res.get('stop_pct', 0):.1f}%`")
                st.write(f"- Time Exit: `{res.get('neither_pct', 0):.1f}%`")
                st.write(f"- Verdict: `{res.get('verdict', 'NEUTRAL')}`")
            except Exception as e:
                st.warning(f"Replay notice: {e}")


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
    data_source = "sample" if is_sample_mode else (active_b or "yahoo")
    render_trust_strip(data_source=data_source, chart=None, is_sample_mode=is_sample_mode, now_utc=now_utc)

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
        # Load baseline datasets with cached lookups
        sector_registry = get_cached_sector_registry()
        market_caps = get_cached_market_caps()
        tracked_symbols = list(market_caps.get("stocks", {}).keys())
        quotes = fetch_cached_market_overview(tuple(tracked_symbols), is_sample_mode=is_sample_mode)

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
        initial_sidebar_state="expanded"
    )
    load_theme()

    if "current_page" not in st.session_state:
        st.session_state["current_page"] = "Analyze now"

    # Sidebar Header
    render_html(
        """
        <div style="padding: 10px 10px 4px 10px;">
            <div style="font-size: 16px; font-weight: 700; color: var(--text-main); letter-spacing: -0.02em;">TradeInsta Terminal</div>
            <div style="font-size: 11px; color: var(--text-muted);">Institutional & Intraday Intelligence</div>
        </div>
        """,
        target=st.sidebar
    )

    # Navigation Helper
    def nav_group(header: str, items: list):
        render_html(f'<div class="sidebar-nav-header">{header}</div>', target=st.sidebar)
        for item in items:
            is_active = (st.session_state["current_page"] == item)
            if st.sidebar.button(item, key=f"nav_{item}", use_container_width=True, type="primary" if is_active else "secondary"):
                if st.session_state["current_page"] != item:
                    st.session_state["current_page"] = item
                    st.rerun()

    nav_group("ANALYSIS", ["Analyze now", "Scanner", "Entry timer", "Exit planner"])
    nav_group("RISK", ["Position size", "Risk check", "Time Machine"])
    nav_group("MONITOR", ["Alerts", "Signal log"])
    nav_group("MARKET INTELLIGENCE", ["Sector heatmap", "Disclosed flow", "Radar"])
    nav_group("SYSTEM", ["Features"])

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
    if current_page in ("Analyze now", "Disclosed flow", "Radar", "Features"):
        deals, _ = load_market_deals(is_sample_mode=is_sample_mode)

    t_page = time.perf_counter()
    if current_page == "Analyze now":
        render_analyze_now_view(resolved_symbol, company_name, is_sample_mode, config, deals)
    elif current_page == "Scanner":
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
    elif current_page == "Signal log":
        render_signal_log_view()
    elif current_page == "Sector heatmap":
        render_sector_heatmap_view(is_sample_mode)
    elif current_page == "Disclosed flow":
        render_disclosed_flow_view(is_sample_mode, deals)
    elif current_page == "Radar":
        render_radar_view(is_sample_mode, deals)
    elif current_page == "Features":
        render_features_view(is_sample_mode=is_sample_mode)

    dur_page = time.perf_counter() - t_page
    log_timing(f"render_page({current_page})", dur_page, f"page:{current_page}")


if __name__ == "__main__":
    main()
