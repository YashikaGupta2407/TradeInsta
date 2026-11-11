#!/usr/bin/env python3
"""Local Streamlit app wrapping existing trader skills and BIG BULL Institutional Intelligence.

Runs strictly on 127.0.0.1.
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
import trader.broker_data as broker_data
from trader.broker_data import fetch_market_overview_quotes
from trader.analyzer import analyze_symbol, load_signal_log


# ---------------------------------------------------------------- Universe Lookup
@st.cache_data
def load_universe():
    """Load symbol-to-name mapping from trader/universe.txt."""
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
    return sym_to_name, name_to_sym, items


SYM_TO_NAME, NAME_TO_SYM, UNIVERSE_ITEMS = load_universe()


# ---------------------------------------------------------------- Cached Market Data Fetchers
@st.cache_data(ttl=60)
def fetch_cached_intraday_chart(symbol: str, interval: str = "5m"):
    """Fetch same-day intraday chart with 60-second TTL to avoid Yahoo rate-limiting on page clicks."""
    return entry_mod.get_chart(symbol, interval=interval)


@st.cache_data(ttl=3600)
def fetch_cached_history_chart(symbol: str, interval: str = "5m", day: str = None):
    """Fetch historical day chart with longer 3600s TTL cache."""
    return entry_mod.get_chart(symbol, interval=interval, day=day)


@st.cache_data(ttl=60)
def fetch_cached_market_overview(symbols: tuple, is_sample_mode: bool = False):
    """Fetch market overview quotes with 60-second TTL to avoid Yahoo rate-limiting on page clicks."""
    return fetch_market_overview_quotes(list(symbols), is_sample_mode=is_sample_mode)



def resolve_symbol(query: str) -> str:
    """Resolve user input (company name or NSE ticker) to a clean .NS ticker."""
    if not query:
        return "RELIANCE.NS"
    q = query.strip()
    # Check if format is 'SYMBOL — Name'
    if " — " in q:
        q = q.split(" — ")[0].strip()

    q_upper = q.upper()
    if q_upper.endswith(".NS") or q_upper.endswith(".BO"):
        return q_upper

    # Check exact symbol in universe
    ns_key = f"{q_upper}.NS"
    if ns_key in SYM_TO_NAME:
        return ns_key

    # Check exact company name
    if q_upper in NAME_TO_SYM:
        return NAME_TO_SYM[q_upper]

    # Partial company name match
    for name_up, s in NAME_TO_SYM.items():
        if q_upper in name_up:
            return s

    # Fallback to symbol with .NS suffix
    return ns_key


def get_badge_info(is_sample: bool, is_open: bool, data_source: str, bar_age: str = "", session_reason: str = "", holiday_name: str = None, is_pre_open: bool = False) -> dict:
    """Determine top-of-page badge state, color, and label.

    Badge labels:
    - 'LIVE (broker)': Broker real-time intraday stream during market hours
    - 'DELAYED (Yahoo)': Yahoo Finance ~15-minute delayed stream during market hours
    - 'LAST SESSION': Outside market hours or weekend (displaying last session data)
    - 'HOLIDAY': NSE Trading Holiday
    - 'MARKET NOT OPEN': Pre-market window before 09:15 IST
    - 'SAMPLE DATA': Offline simulated test fixtures / demo data
    """
    if is_sample:
        return {
            "label": "SAMPLE DATA",
            "bg": "#7c3aed",  # Purple
            "detail": f"Offline test fixture / simulated candles • Source: {data_source.capitalize()}"
        }
    if holiday_name:
        return {
            "label": "HOLIDAY",
            "bg": "#b45309",  # Amber/Brown
            "detail": f"NSE Holiday: {holiday_name} • Market Closed • Source: {data_source.capitalize()}"
        }
    if is_pre_open:
        return {
            "label": "MARKET NOT OPEN",
            "bg": "#d97706",  # Dark Amber
            "detail": f"Pre-market window (opens at 09:15 IST) • Displaying last session • Source: {data_source.capitalize()}"
        }
    if not is_open:
        reason_text = f" ({session_reason})" if session_reason else ""
        return {
            "label": "LAST SESSION",
            "bg": "#475569",  # Slate Grey
            "detail": f"Market closed{reason_text} • Displaying last session • Source: {data_source.capitalize()} • Age: {bar_age}"
        }
    if data_source in ("upstox", "fyers"):
        return {
            "label": "LIVE (broker)",
            "bg": "#16a34a",  # Bold Green
            "detail": f"Broker real-time feed ({data_source.capitalize()}) • Bar age: {bar_age}"
        }
    return {
        "label": "DELAYED (Yahoo)",
        "bg": "#dc2626",  # Red
        "detail": f"Yahoo Finance ~15m delay • Bar age: {bar_age}"
    }


# ---------------------------------------------------------------- Deal Loading Helper
def load_market_deals(is_sample_mode: bool = False):
    """Load bulk/block deals from trader/flow/ or sample fixtures if in sample mode.

    Returns:
        tuple: (deals: list, source_note: str)
    """
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

    return deals, source_note


# ---------------------------------------------------------------- BIG BULL Dashboard View
def render_big_bull_view(is_sample_mode: bool = False, deals=None, data_source_note=None):
    """Render BIG BULL — Institutional Buying Intelligence & Sector Flow Analysis Terminal."""
    if deals is None:
        deals, data_source_note = load_market_deals(is_sample_mode=is_sample_mode)

    # Load Sector Registry & Market Caps
    sector_registry = big_bull.load_sector_registry()
    market_caps = big_bull.load_market_caps()
    tracked_symbols = list(market_caps.get("stocks", {}).keys())

    # Fetch Upstox Live / Fallback Quotes with 60s cache
    quotes = {}
    fetch_overview_err = None
    try:
        quotes = fetch_cached_market_overview(tuple(tracked_symbols), is_sample_mode=is_sample_mode)
    except Exception as e:
        fetch_overview_err = str(e)

    # Session state cache for resilient fallback on rate-limits
    if "last_known_overview_quotes" not in st.session_state:
        st.session_state["last_known_overview_quotes"] = {}
    if "last_known_overview_ts" not in st.session_state:
        st.session_state["last_known_overview_ts"] = ""

    if quotes:
        st.session_state["last_known_overview_quotes"] = quotes
        st.session_state["last_known_overview_ts"] = datetime.datetime.now().strftime("%H:%M:%S IST")
    elif not quotes and not is_sample_mode:
        cached_quotes = st.session_state.get("last_known_overview_quotes")
        cached_ts = st.session_state.get("last_known_overview_ts")
        err_str = (fetch_overview_err or "").lower()

        if any(term in err_str for term in ["429", "rate", "too many requests", "wait a minute"]):
            if cached_quotes and cached_ts:
                st.warning(f"⚠️ Yahoo is rate-limiting; retry in a few minutes. Showing last cached data from {cached_ts}.")
                quotes = cached_quotes
            else:
                st.warning("⚠️ Yahoo is rate-limiting; retry in a few minutes.")
                return
        elif any(term in err_str for term in ["nodename", "servname", "could not connect", "network", "timed out", "dns"]):
            if cached_quotes and cached_ts:
                st.warning(f"⚠️ Network/DNS error while contacting market feed. Showing last cached data from {cached_ts}.")
                quotes = cached_quotes
            else:
                st.warning("⚠️ Network/DNS error while contacting market feed. Please check your internet connection or retry shortly.")
                return
        else:
            if cached_quotes and cached_ts:
                st.info(f"Showing last cached market overview data from {cached_ts}.")
                quotes = cached_quotes
            else:
                st.warning(
                    "⚠️ No live market quotes could be retrieved from Upstox or Yahoo Finance. "
                    "Please verify that `UPSTOX_ACCESS_TOKEN` is set in your environment, or switch to 'Sample Data (Offline Fixture)' in the sidebar."
                )
                return

    breadth = big_bull.compute_market_breadth(quotes)
    sec_perf = big_bull.compute_sector_performance(quotes, sector_registry, market_caps)

    # Data Source Provenance Badge
    active_b = broker_data.active_broker()
    if is_sample_mode:
        feed_badge = "🟣 SAMPLE DATA (Offline Fixture)"
        feed_color = "#a855f7"
        feed_desc = "Offline sample market quotes and test deals."
    elif breadth.get("source") == "upstox" or active_b == "upstox":
        feed_badge = "🟢 UPSTOX LIVE (Broker Feed V2/V3)"
        feed_color = "#10b981"
        feed_desc = "Live market quotes & ticks via authorized Upstox API."
    elif breadth.get("source") == "fyers" or active_b == "fyers":
        feed_badge = "🟢 FYERS LIVE (Broker Feed)"
        feed_color = "#10b981"
        feed_desc = "Live market quotes via authorized Fyers API."
    else:
        feed_badge = "🔴 YAHOO FINANCE (Delayed Reference Feed)"
        feed_color = "#ef4444"
        feed_desc = "Delayed market data (~15m lag) fallback."

    # Custom CSS for dark institutional styling
    st.markdown(
        """
        <style>
        .bb-header {
            background: linear-gradient(90deg, #0f172a 0%, #1e293b 100%);
            border: 1px solid #334155;
            border-radius: 8px;
            padding: 18px 22px;
            margin-bottom: 16px;
        }
        .bb-title {
            color: #f8fafc;
            font-size: 24px;
            font-weight: 700;
            letter-spacing: -0.5px;
            margin: 0 0 6px 0;
        }
        .bb-subtitle {
            color: #94a3b8;
            font-size: 13px;
            margin: 0 0 12px 0;
        }
        .bb-pill-container {
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            margin-top: 8px;
        }
        .bb-pill {
            background-color: #1e293b;
            border: 1px solid #475569;
            color: #38bdf8;
            padding: 4px 12px;
            border-radius: 16px;
            font-size: 12px;
            font-weight: 600;
        }
        .bb-card {
            background-color: #0f172a;
            border: 1px solid #1e293b;
            border-radius: 8px;
            padding: 14px 16px;
            margin-bottom: 12px;
        }
        .bb-badge {
            display: inline-block;
            padding: 3px 10px;
            border-radius: 12px;
            font-size: 11px;
            font-weight: 700;
            letter-spacing: 0.3px;
        }
        </style>
        """,
        unsafe_allow_html=True
    )

    # 1. Header & Primary Questions
    st.markdown(
        f"""
        <div class="bb-header">
            <div style="display: flex; justify-content: space-between; align-items: flex-start; flex-wrap: wrap; gap: 10px;">
                <div>
                    <div class="bb-title">🐂 BIG BULL — Follow the Smart Money</div>
                    <div class="bb-subtitle">Upstox Live Market Intelligence • Sector Capital Flow • Institutional Smart Money Radar</div>
                </div>
                <div style="background-color: #020617; border: 1px solid #334155; border-radius: 6px; padding: 6px 12px; font-size: 12px;">
                    <span style="color: {feed_color}; font-weight: 700;">{feed_badge}</span><br>
                    <span style="color: #64748b; font-size: 11px;">{feed_desc}</span>
                </div>
            </div>
            <div class="bb-pill-container">
                <span class="bb-pill">🌐 MARKET OVERVIEW</span>
                <span class="bb-pill">🗺️ SECTOR HEATMAP</span>
                <span class="bb-pill">📡 BIG BULL RADAR</span>
                <span class="bb-pill">🔒 TOKEN PROTECTED</span>
            </div>
        </div>
        """,
        unsafe_allow_html=True
    )

    # Compliance Notice
    st.markdown(
        """
        <div style="background-color: #1e293b; border-left: 4px solid #38bdf8; padding: 8px 14px; border-radius: 4px; margin-bottom: 16px;">
            <span style="font-weight: 600; color: #38bdf8; font-size: 12px;">Notice:</span>
            <span style="color: #cbd5e1; font-size: 12px;"> Information and quantitative research only, not investment advice. Live prices powered by Upstox/broker API; Disclosed deals reflect official NSE regulatory filings (retrospective reported transactions, not live open-market order books).</span>
        </div>
        """,
        unsafe_allow_html=True
    )

    # 2. Main Experience Tabs
    tab_overview, tab_heatmap, tab_radar = st.tabs([
        "🌐 A. Market Overview",
        "🗺️ B. Market Heatmap",
        "📡 C. Big Bull Radar"
    ])

    # =========================================================================
    # TAB A: MARKET OVERVIEW
    # =========================================================================
    with tab_overview:
        st.markdown("### 📊 Live Market Breadth & Sector Performance")
        st.caption("Live stock price movement and sector-wise advances/declines across leading NSE equities.")

        # Breadth Telemetry Metrics
        m_col1, m_col2, m_col3, m_col4 = st.columns(4)
        with m_col1:
            st.metric(
                label="Tracked Universe",
                value=f"{breadth['total']} Stocks",
                delta=f"{len(sec_perf)} Sectors",
                delta_color="off"
            )
        with m_col2:
            st.metric(
                label="Advancing Stocks",
                value=f"{breadth['advances']}",
                delta=f"{round(breadth['advances'] / max(1, breadth['total']) * 100, 1)}% of market",
                delta_color="normal"
            )
        with m_col3:
            st.metric(
                label="Declining Stocks",
                value=f"{breadth['declines']}",
                delta=f"-{round(breadth['declines'] / max(1, breadth['total']) * 100, 1)}% of market",
                delta_color="inverse"
            )
        with m_col4:
            st.metric(
                label="Advance / Decline Ratio",
                value=f"{breadth['advance_ratio']:.2f}",
                delta=f"{breadth['unchanged']} Unchanged",
                delta_color="off"
            )

        # Visual Advance/Decline Progress Bar
        total_movers = max(1, breadth['advances'] + breadth['declines'])
        adv_pct = int(round((breadth['advances'] / total_movers) * 100))
        dec_pct = 100 - adv_pct
        st.markdown(
            f"""
            <div style="margin: 12px 0 24px 0;">
                <div style="display: flex; justify-content: space-between; font-size: 12px; margin-bottom: 4px; font-weight: 600;">
                    <span style="color: #10b981;">Advancing: {breadth['advances']} ({adv_pct}%)</span>
                    <span style="color: #ef4444;">Declining: {breadth['declines']} ({dec_pct}%)</span>
                </div>
                <div style="display: flex; height: 10px; border-radius: 5px; overflow: hidden; background-color: #334155;">
                    <div style="width: {adv_pct}%; background-color: #10b981;"></div>
                    <div style="width: {dec_pct}%; background-color: #ef4444;"></div>
                </div>
            </div>
            """,
            unsafe_allow_html=True
        )

        # Top Gainers & Losers Columns
        g_col, l_col = st.columns(2)
        with g_col:
            st.markdown("#### 🚀 Top Market Gainers")
            gainers_rows = [
                {
                    "Symbol": g["symbol"],
                    "Live Price (₹)": f"₹{g['price']:,.2f}",
                    "Change %": f"+{g['change_pct']:.2f}%",
                    "Net Move": f"+₹{g.get('net_change', 0):,.2f}",
                    "Volume": f"{g.get('volume', 0):,}" if g.get("volume") else "—",
                    "Source": g.get("source", "broker")
                }
                for g in breadth["top_gainers"]
            ]
            st.dataframe(pd.DataFrame(gainers_rows), use_container_width=True, hide_index=True)

        with l_col:
            st.markdown("#### 🔻 Top Market Losers")
            losers_rows = [
                {
                    "Symbol": l["symbol"],
                    "Live Price (₹)": f"₹{l['price']:,.2f}",
                    "Change %": f"{l['change_pct']:+.2f}%",
                    "Net Move": f"₹{l.get('net_change', 0):,.2f}",
                    "Volume": f"{l.get('volume', 0):,}" if l.get("volume") else "—",
                    "Source": l.get("source", "broker")
                }
                for l in breadth["top_losers"]
            ]
            st.dataframe(pd.DataFrame(losers_rows), use_container_width=True, hide_index=True)

        st.divider()

        # Sector Performance Leaderboard
        st.markdown("#### 🏭 Sector Performance Leaderboard (Ranked)")
        st.caption("Sectors ranked by live market-cap-weighted performance with advance/decline breadth.")

        sec_table_rows = []
        for s in sec_perf:
            top_stk = s["top_stock"]
            top_str = f"{top_stk['symbol']} ({top_stk['change_pct']:+.2f}%)" if top_stk else "—"
            sec_table_rows.append({
                "Sector": s["sector"],
                "Performance": f"{s['weighted_change_pct']:+.2f}%",
                "Simple Avg": f"{s['avg_change_pct']:+.2f}%",
                "Breadth": f"{s['advances']} Adv / {s['declines']} Dec",
                "Top Performer": top_str,
                "Market Cap (Cr)": f"₹{s['total_market_cap_cr']:,.0f} Cr",
                "Trend": s["trend"]
            })
        st.dataframe(pd.DataFrame(sec_table_rows), use_container_width=True, hide_index=True)

        st.divider()

        # Interactive Sector Exploration
        st.markdown("#### 🔍 Interactive Sector Exploration")
        st.caption("Select any sector to drill into its constituent stocks.")

        sector_names = [s["sector"] for s in sec_perf]
        selected_sec_name = st.selectbox("Select Sector to Explore", options=sector_names, index=0)
        matched_sec = next((s for s in sec_perf if s["sector"] == selected_sec_name), None)

        if matched_sec and matched_sec["stocks"]:
            sec_stock_rows = [
                {
                    "Symbol": st_item["symbol"],
                    "Company Name": st_item["name"],
                    "Live Price": f"₹{st_item['price']:,.2f}",
                    "Prev Close": f"₹{st_item['prev_close']:,.2f}",
                    "Change %": f"{st_item['change_pct']:+.2f}%",
                    "Net Move": f"{st_item['net_change']:+.2f}",
                    "Market Cap (Cr)": f"₹{st_item['market_cap_cr']:,.0f} Cr",
                    "Volume": f"{st_item['volume']:,}" if st_item.get("volume") else "—"
                }
                for st_item in matched_sec["stocks"]
            ]
            st.dataframe(pd.DataFrame(sec_stock_rows), use_container_width=True, hide_index=True)

    # =========================================================================
    # TAB B: MARKET HEATMAP
    # =========================================================================
    with tab_heatmap:
        st.markdown("### 🗺️ Interactive Market Heatmap")
        st.caption("Rectangular stock tiles grouped by Sector. Tile size indicates Market Capitalisation; color indicates Live Price Movement (Green = Gain, Red = Loss).")

        heatmap_records = big_bull.build_heatmap_records(quotes, sector_registry, market_caps)

        if heatmap_records:
            df_heat = pd.DataFrame(heatmap_records)
            fig_tree = px.treemap(
                df_heat,
                path=[px.Constant("NSE Market"), "sector", "symbol"],
                values="market_cap_cr",
                color="color_val",
                color_continuous_scale=[
                    (0.0, "#dc2626"),   # Deep Red for <= -3%
                    (0.35, "#ef4444"),  # Red
                    (0.5, "#334155"),   # Slate for 0%
                    (0.65, "#22c55e"),  # Green
                    (1.0, "#15803d")    # Dark Green for >= +3%
                ],
                color_continuous_midpoint=0.0,
                custom_data=["price", "change_pct", "name", "market_cap_cr"]
            )

            fig_tree.update_traces(
                texttemplate="<b>%{label}</b><br>%{customdata[1]:+.2f}%<br>₹%{customdata[0]:,.2f}",
                textposition="middle center",
                hovertemplate="<b>%{label}</b> (%{customdata[2]})<br>Price: ₹%{customdata[0]:,.2f}<br>Change: %{customdata[1]:+.2f}%<br>Market Cap: ₹%{customdata[3]:,.0f} Cr<extra></extra>",
                textfont=dict(size=14, color="#ffffff")
            )

            fig_tree.update_layout(
                margin=dict(t=30, l=10, r=10, b=10),
                height=650,
                paper_bgcolor="#0b0f19",
                plot_bgcolor="#0b0f19",
                font=dict(color="#f8fafc")
            )

            st.plotly_chart(fig_tree, use_container_width=True)
            st.caption("💡 Click on any Sector box in the treemap to zoom into its constituent stocks. Click the top bar to zoom back out.")
        else:
            st.info("Heatmap data unavailable. Ensure market cap mappings are configured.")

        st.divider()

        # Stock Details Inspector
        st.markdown("#### 🔬 Stock Details Inspector")
        st.caption("Select any stock to inspect its full quote parameters and historical deal filings.")

        all_mapped_syms = sorted(list(set(r["symbol"] for r in heatmap_records)))
        selected_insp_sym = st.selectbox("Choose Stock to Inspect", options=all_mapped_syms, index=0)

        insp_q = quotes.get(selected_insp_sym, {})
        cap_ref = market_caps.get("stocks", {}).get(selected_insp_sym, {})

        ic1, ic2, ic3, ic4 = st.columns(4)
        with ic1:
            st.metric(
                label="Live LTP",
                value=f"₹{insp_q.get('price', 0.0):,.2f}",
                delta=f"{insp_q.get('change_pct', 0.0):+.2f}%",
                delta_color="normal" if insp_q.get("change_pct", 0.0) >= 0 else "inverse"
            )
        with ic2:
            st.metric(
                label="Previous Close",
                value=f"₹{insp_q.get('prev_close', 0.0):,.2f}",
                delta=f"Open: ₹{insp_q.get('open', 0.0):,.2f}" if insp_q.get("open") else None,
                delta_color="off"
            )
        with ic3:
            st.metric(
                label="Day Range",
                value=f"₹{insp_q.get('high', 0.0):,.2f} H",
                delta=f"₹{insp_q.get('low', 0.0):,.2f} L",
                delta_color="off"
            )
        with ic4:
            st.metric(
                label="Volume & Source",
                value=f"{insp_q.get('volume', 0):,} shs" if insp_q.get("volume") else "Active",
                delta=insp_q.get("source", "broker").upper(),
                delta_color="off"
            )

        # Check for disclosed deals on this stock
        sym_deals = flow_mod.get_deals_for_symbol(deals, selected_insp_sym) if deals else []
        if sym_deals:
            st.markdown(f"**Disclosed Institutional Filings for {selected_insp_sym} ({len(sym_deals)} Deals):**")
            d_table = [
                {
                    "Date": d["date"],
                    "Type": d["deal_type"],
                    "Action": d["action"],
                    "Quantity": f"{d['quantity']:,}",
                    "Price (₹)": f"₹{d['price']:,.2f}",
                    "Total Value": flow_mod.format_inr(d["value_inr"]),
                    "Client Name": d["client_name"]
                }
                for d in sym_deals
            ]
            st.dataframe(pd.DataFrame(d_table), use_container_width=True, hide_index=True)
        else:
            st.caption(f"No bulk/block deal disclosures on record for {selected_insp_sym} in `trader/flow/`.")

    # =========================================================================
    # TAB C: BIG BULL RADAR
    # =========================================================================
    with tab_radar:
        st.markdown("### 📡 Big Bull Smart Money Radar")
        st.caption("Surfaces stocks exhibiting repeated institutional accumulation, multiple disclosed buyers, and supporting Upstox live price/volume action.")

        if not deals:
            st.info(
                "No bulk or block deal CSV files found in `trader/flow/`. "
                "Download official NSE bulk/block deal CSV files into `trader/flow/` or enable 'Sample Data (Offline Fixture)' in the sidebar to view smart-money radar alerts."
            )
            return

        # Overall Disclosed Flow Telemetry
        kpis_raw = big_bull.compute_market_flow_kpis(deals)
        d_range = kpis_raw["date_range"]
        date_str = f"{d_range['earliest']} to {d_range['latest']}" if d_range["earliest"] else "Single Session"

        rk1, rk2, rk3, rk4 = st.columns(4)
        with rk1:
            st.metric(
                label="Disclosed Buying",
                value=flow_mod.format_inr(kpis_raw["total_buy_value_inr"]),
                delta=f"{kpis_raw['total_buy_deals']} Buy Deals",
                delta_color="normal"
            )
        with rk2:
            st.metric(
                label="Disclosed Selling",
                value=flow_mod.format_inr(kpis_raw["total_sell_value_inr"]),
                delta=f"{kpis_raw['total_sell_deals']} Sell Deals",
                delta_color="inverse"
            )
        with rk3:
            st.metric(
                label="Net Smart Money Flow",
                value=flow_mod.format_inr(kpis_raw["net_flow_inr"]),
                delta="NET CAPITAL INFLOW" if kpis_raw["net_flow_inr"] >= 0 else "NET CAPITAL OUTFLOW",
                delta_color="normal" if kpis_raw["net_flow_inr"] >= 0 else "inverse"
            )
        with rk4:
            st.metric(
                label="Observation Window",
                value=f"{kpis_raw['total_deals']} Total Deals",
                delta=date_str,
                delta_color="off"
            )

        st.divider()

        # Generate Explainable Radar Alerts
        radar_alerts = big_bull.generate_big_bull_radar(deals, quotes, sector_registry)

        st.markdown("#### 🎯 Smart Money Accumulation & Alert Signals")
        st.caption("Institutional buying disclosures corroborated with live Upstox price reaction and volume surge.")

        if radar_alerts:
            for item in radar_alerts[:8]:
                conviction_badge = "🔥 HIGH CONVICTION" if item["is_high_conviction"] else "👀 MONITORING"
                conviction_bg = "#15803d" if item["is_high_conviction"] else "#334155"
                net_str = flow_mod.format_inr(item["net_flow_inr"])
                net_color = "#10b981" if item["net_flow_inr"] >= 0 else "#ef4444"

                price_str = f"₹{item['live_price']:,.2f}" if item["live_price"] else "—"
                chg_str = f"{item['live_change_pct']:+.2f}%" if item["live_change_pct"] is not None else "—"

                with st.container():
                    st.markdown(
                        f"""
                        <div style="background-color: #0b0f19; border: 1px solid #1e293b; border-radius: 8px; padding: 14px 18px; margin-bottom: 12px;">
                            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">
                                <div>
                                    <span style="font-size: 18px; font-weight: 700; color: #f8fafc;">{item['symbol']}</span>
                                    <span style="font-size: 13px; color: #94a3b8; margin-left: 8px;">{item['security_name']} • {item['sector']}</span>
                                </div>
                                <div>
                                    <span style="background-color: {conviction_bg}; color: #ffffff; padding: 3px 8px; border-radius: 10px; font-size: 11px; font-weight: 700;">{conviction_badge}</span>
                                    <span style="background-color: #1e293b; color: #38bdf8; padding: 3px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; margin-left: 6px;">{item['pattern']}</span>
                                </div>
                            </div>
                            <div style="display: flex; gap: 24px; font-size: 13px; margin: 8px 0; color: #cbd5e1;">
                                <div><b>Disclosed Net Flow:</b> <span style="color: {net_color}; font-weight: 700;">{net_str}</span></div>
                                <div><b>Disclosed Buying:</b> <span style="color: #10b981;">{flow_mod.format_inr(item['buy_value_inr'])}</span></div>
                                <div><b>Live Upstox LTP:</b> <span style="color: #38bdf8; font-weight: 600;">{price_str} ({chg_str})</span></div>
                                <div><b>Latest Deal Date:</b> <span>{item['latest_deal_date']}</span></div>
                            </div>
                        </div>
                        """,
                        unsafe_allow_html=True
                    )

                    with st.expander(f"🔍 Why is {item['symbol']} highlighted? (Click to view explainable rationale & underlying deals)", expanded=False):
                        st.markdown("**Evidence & Convergence Factors:**")
                        for r in item["reasons"]:
                            st.markdown(f"- {r}")

                        if item.get("deals"):
                            st.markdown("**Underlying Reported Transactions:**")
                            d_rows = [
                                {
                                    "Date": d["date"],
                                    "Type": d["deal_type"],
                                    "Action": d["action"],
                                    "Quantity": f"{d['quantity']:,}",
                                    "Price": f"₹{d['price']:,.2f}",
                                    "Value": flow_mod.format_inr(d["value_inr"]),
                                    "Client Name": d["client_name"]
                                }
                                for d in item["deals"]
                            ]
                            st.dataframe(pd.DataFrame(d_rows), use_container_width=True, hide_index=True)
        else:
            st.info("No stocks currently meet radar accumulation criteria.")

        st.divider()

        # Market-wide raw deal ledger audit
        with st.expander("📑 Complete Disclosed Deal Ledger", expanded=False):
            search_deal = st.text_input("Search Ledger by Symbol or Client Name", value="")
            disp = deals
            if search_deal:
                s_up = search_deal.upper()
                disp = [d for d in disp if s_up in d["symbol"].upper() or s_up in d.get("client_name", "").upper()]

            raw_tbl = [
                {
                    "Date": d["date"],
                    "Symbol": d["symbol"],
                    "Company": d.get("security_name", ""),
                    "Sector": big_bull.get_stock_sector(d["symbol"], d.get("security_name", ""), sector_registry),
                    "Type": d["deal_type"],
                    "Action": d["action"],
                    "Quantity": f"{d['quantity']:,}",
                    "Price (₹)": f"₹{d['price']:,.2f}",
                    "Total Value": flow_mod.format_inr(d["value_inr"]),
                    "Client Name": d["client_name"]
                }
                for d in disp
            ]
            st.dataframe(pd.DataFrame(raw_tbl), use_container_width=True, hide_index=True)


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
    st.markdown(f"### ⚡ Analysis as of `{analysis.get('now_ist', 'Now')}`")

    # Current Price vs Completed Bars Comparison
    c_p1, c_p2 = st.columns(2)
    with c_p1:
        st.markdown(
            f"""
            <div style="background-color: #f1f5f9; border: 1px solid #cbd5e1; border-radius: 8px; padding: 14px; margin-bottom: 8px;">
                <div style="font-size: 13px; color: #64748b; font-weight: 600; text-transform: uppercase;">1. Current Price (LTP)</div>
                <div style="font-size: 26px; font-weight: 700; color: #0f172a; margin: 4px 0;">₹{s1.get('current_price') or 0:,.2f}</div>
                <div style="font-size: 12px; color: #475569;">Time: {s1.get('current_price_time', 'N/A')} • Feed: {str(s1.get('current_price_source', 'Broker')).capitalize()}</div>
            </div>
            """,
            unsafe_allow_html=True
        )
    with c_p2:
        st.markdown(
            f"""
            <div style="background-color: #f1f5f9; border: 1px solid #cbd5e1; border-radius: 8px; padding: 14px; margin-bottom: 8px;">
                <div style="font-size: 13px; color: #64748b; font-weight: 600; text-transform: uppercase;">2. Last Completed 5-Minute Bar</div>
                <div style="font-size: 26px; font-weight: 700; color: #0f172a; margin: 4px 0;">₹{s1.get('last_bar_close') or 0:,.2f}</div>
                <div style="font-size: 12px; color: #475569;">Close Time: {s1.get('last_bar_time', 'N/A')} • Bar Age: {s1.get('bar_age', 'N/A')}</div>
            </div>
            """,
            unsafe_allow_html=True
        )

    st.caption("ℹ️ *The entry rule needs completed bars. The current price shows where the stock is right now.*")

    # 9-Step Summary sentences
    st.markdown(
        f"""
        <div style="background-color: #eff6ff; border-left: 4px solid #3b82f6; border-radius: 6px; padding: 14px 18px; margin: 12px 0;">
            <h4 style="margin: 0 0 8px 0; color: #1e3a8a; font-size: 16px;">📋 Current Rules Summary</h4>
        </div>
        """,
        unsafe_allow_html=True
    )
    for s in s9.get("sentences", []):
        st.markdown(f"- {s}")

    # Expandable Full 9-Step Detail Breakdown
    with st.expander("🔍 View Full 9-Step Verification Details", expanded=False):
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
                st.warning(f"⚠️ {s3.get('late_entry_note')}")
            if s3.get("failed_breakout"):
                st.error(f"❌ {s3.get('failed_reason')}")

            st.markdown(f"**Step 4: Levels** (`{s4.get('timestamp')}`)")
            if s4.get("entry_level"):
                st.write(f"- Entry Level: `₹{s4.get('entry_level'):.2f}` | Stop: `₹{s4.get('stop_level'):.2f}` | Target: `₹{s4.get('target_level'):.2f}`")
                st.write(f"- Reward:Risk: `{s4.get('reward_risk'):.1f}R` | Tick size: `₹{s4.get('tick_size'):.2f}`")
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


# ---------------------------------------------------------------- Main Entry Point
def main():
    st.set_page_config(
        page_title="TradeInsta Terminal",
        page_icon="📈",
        layout="wide",
        initial_sidebar_state="expanded"
    )

    # Sidebar Navigation
    st.sidebar.title("TradeInsta Terminal")
    nav_view = st.sidebar.radio(
        "Navigation",
        options=["📊 Trade Setup & Execution", "🐂 BIG BULL — Follow the Smart Money"],
        index=0,
        help="Switch between single-stock trade execution and institutional smart money flow intelligence."
    )

    st.sidebar.divider()
    st.sidebar.header("📡 Feed Mode")
    feed_mode = st.sidebar.radio(
        "Data Source Mode",
        options=["Live / Network Market Feed", "Sample Data (Offline Fixture)"],
        index=0,
        help="Choose between real network market data or offline test fixtures."
    )
    is_sample_mode = (feed_mode == "Sample Data (Offline Fixture)")
    deals, deal_source_note = load_market_deals(is_sample_mode=is_sample_mode)

    if nav_view == "🐂 BIG BULL — Follow the Smart Money":
        render_big_bull_view(is_sample_mode=is_sample_mode, deals=deals, data_source_note=deal_source_note)
        return

    # ---------------------------------------------------------------- Single Stock Trade Setup View
    st.sidebar.divider()
    st.sidebar.header("🔍 Stock Selection")
    default_search = "RELIANCE"
    user_search = st.sidebar.text_input(
        "Company Name or NSE Symbol",
        value=default_search,
        help="Type any company name (e.g. Infosys, Tata Motors) or NSE symbol (e.g. RELIANCE, INFY.NS)"
    )

    resolved_symbol = resolve_symbol(user_search)
    company_name = SYM_TO_NAME.get(resolved_symbol, resolved_symbol)

    st.sidebar.markdown(f"**Selected:** `{resolved_symbol}`")
    if company_name != resolved_symbol:
        st.sidebar.caption(f"{company_name}")

    st.sidebar.divider()
    st.sidebar.header("⚙️ Configuration")
    cfg_path = os.path.join(ROOT, "trader", "config.json")
    config = budget_mod.load_config(cfg_path)

    account_budget = float(config.get("budget", 500000))
    max_loss_cfg = float(config.get("max_loss_per_trade_inr", 2000))

    st.sidebar.markdown(f"**Account Size:** ₹{account_budget:,.2f}")
    st.sidebar.markdown(f"**Max Loss / Trade:** ₹{max_loss_cfg:,.2f}")
    st.sidebar.markdown(f"**Max Position Cap:** {config.get('max_position_pct', 75)}%")
    st.sidebar.markdown(f"**Cash Buffer:** {config.get('cash_buffer_pct', 10)}%")
    st.sidebar.caption(f"Loaded from `{os.path.relpath(cfg_path, ROOT)}`")

    st.sidebar.divider()
    st.sidebar.subheader("🔒 Broker Connection")
    active_b = broker_data.active_broker()
    if active_b == "upstox":
        st.sidebar.success("Upstox: Connected via Environment (Token Protected)")
    elif active_b == "fyers":
        st.sidebar.success("Fyers: Connected via Environment (Token Protected)")
    else:
        st.sidebar.info("Broker: None active in environment (Falling back to Yahoo)")

    # ---------------------------------------------------------------- Core Data & Engine
    now_utc = entry_mod.utc_now()
    market_st = alerts.market_status(now_utc, us=False)
    is_market_open = market_st.get("open", False)

    chart = None
    fetch_error = None

    if is_sample_mode:
        sample_path = os.path.join(ROOT, "tests", "fixtures", "sample_reliance.json")
        if not os.path.exists(sample_path):
            sample_path = os.path.join(ROOT, "tests", "fixtures", "trader-entry", "TSLA-2026-10-19-5m.json")
        try:
            with open(sample_path, "r", encoding="utf-8") as fh:
                chart = json.load(fh)
            chart.setdefault("meta", {})["source"] = "sample"
        except Exception as e:
            fetch_error = f"Could not load sample fixture: {e}"
    else:
        # 1. Prefer Upstox for candles when token is set
        if active_b == "upstox":
            try:
                chart = broker_data.get_realtime_candles(resolved_symbol, interval="5m")
            except Exception:
                chart = None

        # 2. Fall back to cached Yahoo only if broker is unavailable or returned None
        if not chart:
            try:
                chart = fetch_cached_intraday_chart(resolved_symbol, interval="5m")
            except Exception as e:
                fetch_error = str(e)

    # Session state cache for resilient fallback on 429 or network errors
    if "last_known_charts" not in st.session_state:
        st.session_state["last_known_charts"] = {}
    if "last_known_timestamps" not in st.session_state:
        st.session_state["last_known_timestamps"] = {}

    rate_limit_notice = None
    network_notice = None

    if chart and not fetch_error:
        # Cache successful live/sample chart
        st.session_state["last_known_charts"][resolved_symbol] = chart
        st.session_state["last_known_timestamps"][resolved_symbol] = datetime.datetime.now().strftime("%H:%M:%S IST")
    elif fetch_error:
        err_lower = fetch_error.lower()
        cached_chart = st.session_state["last_known_charts"].get(resolved_symbol)
        cached_ts = st.session_state["last_known_timestamps"].get(resolved_symbol, "")

        if any(term in err_lower for term in ["429", "rate", "too many requests", "wait a minute"]):
            if cached_chart and cached_ts:
                rate_limit_notice = f"⚠️ Yahoo is rate-limiting; retry in a few minutes. Showing last cached data from {cached_ts}."
                chart = cached_chart
                fetch_error = None
            else:
                rate_limit_notice = "⚠️ Yahoo is rate-limiting; retry in a few minutes."
        elif any(term in err_lower for term in ["nodename", "servname", "could not connect", "network error", "dns", "timed out", "temporary failure"]):
            if cached_chart and cached_ts:
                network_notice = f"⚠️ Network/DNS error while contacting Yahoo Finance. Showing last cached data from {cached_ts}."
                chart = cached_chart
                fetch_error = None
            else:
                network_notice = "⚠️ Network/DNS error while contacting Yahoo Finance. Please check your internet connection or retry shortly."

    # Determine data source & bar age
    bars = (chart.get("bars") if chart else []) or []
    data_source = (chart.get("meta") or {}).get("source", "yahoo") if chart else "yahoo"

    latest_t = bars[-1]["t"] if bars else None
    bar_age_str = ""
    if latest_t:
        bar_age_full = broker_data.format_bar_age(latest_t, now_utc=now_utc, source=data_source)
        if "bar age: " in bar_age_full:
            bar_age_str = bar_age_full.split("bar age: ")[1]
        else:
            bar_age_str = bar_age_full
    else:
        bar_age_str = "no bars"

    # ---------------------------------------------------------------- Clear Top Badge
    badge = get_badge_info(
        is_sample=is_sample_mode,
        is_open=is_market_open,
        data_source=data_source,
        bar_age=bar_age_str,
        session_reason=market_st.get("reason", "")
    )

    st.markdown(
        f"""
        <div style="background-color: {badge['bg']}; color: white; padding: 12px 20px; border-radius: 8px; font-weight: 700; font-size: 16px; margin-bottom: 16px; display: flex; align-items: center; justify-content: space-between; box-shadow: 0 2px 4px rgba(0,0,0,0.12);">
            <div style="display: flex; align-items: center;">
                <span style="height: 12px; width: 12px; background-color: white; border-radius: 50%; display: inline-block; margin-right: 12px;"></span>
                <span style="letter-spacing: 0.5px; font-size: 17px;">{badge['label']}</span>
            </div>
            <div style="font-size: 13px; font-weight: 400; opacity: 0.95;">
                {badge['detail']}
            </div>
        </div>
        """,
        unsafe_allow_html=True
    )

    if rate_limit_notice:
        st.warning(rate_limit_notice)
    elif network_notice:
        st.warning(network_notice)

    # Note: Paper trading, not advice
    st.markdown(
        """
        <div style="background-color: #f8f9fa; border-left: 5px solid #007bff; padding: 10px 16px; border-radius: 4px; margin-bottom: 15px;">
            <span style="font-weight: 600; color: #1a73e8;">Note:</span>
            <span style="color: #495057;"> Paper trading, not advice. All figures, stop levels, and risk calculations are simulated.</span>
        </div>
        """,
        unsafe_allow_html=True
    )

    # ---------------------------------------------------------------- Setup & Analysis Tabs
    tab_setup, tab_log = st.tabs(["⚡ Trade Setup & Live Analysis", "📜 Signal Log"])

    with tab_setup:
        # Header title and One-Click 'Analyze now' Action
        c_info, c_btn = st.columns([3, 1])
        with c_info:
            st.title(f"{resolved_symbol}")
            if company_name and company_name != resolved_symbol:
                st.subheader(company_name)
        with c_btn:
            st.write("")
            run_analysis = st.button("⚡ Analyze now", type="primary", use_container_width=True, help="Compute instant 9-step trade analysis as of the current minute.")

        if run_analysis:
            prog_bar = st.progress(0, text="Initializing 9-step analysis...")
            def _prog_cb(step_num, step_name, status, detail=""):
                pct = int((step_num / 9.0) * 100)
                prog_bar.progress(pct, text=f"Step {step_num}/9: {step_name} • {detail}")

            res_analysis = analyze_symbol(
                resolved_symbol,
                is_sample_mode=is_sample_mode,
                config=config,
                progress_callback=_prog_cb
            )
            prog_bar.empty()
            st.session_state[f"last_analysis_{resolved_symbol}"] = res_analysis

        curr_analysis = st.session_state.get(f"last_analysis_{resolved_symbol}")
        if curr_analysis:
            render_analysis_card(curr_analysis)

        # ---------------------------------------------------------------- Data Evaluation
        if fetch_error or not chart:
            if not rate_limit_notice and not network_notice:
                st.error(f"⚠️ Could not load market data for {resolved_symbol}: {fetch_error or 'no data available'}")
            return

    if not bars:
        st.warning(f"No 5-minute bars returned for {resolved_symbol} today.")
        return

    eval_res = entry_mod.evaluate(chart, now_utc, setup="orb", or_minutes=15, interval="5m", cfg=config)
    or_info = eval_res.get("opening_range")
    or_high = or_info.get("high") if or_info else None
    or_low = or_info.get("low") if or_info else None

    signal_status = eval_res.get("status", "waiting").upper()
    signal_phase = eval_res.get("phase", "")
    signal_reason = eval_res.get("reason", "")
    trigger_info = eval_res.get("trigger")

    # Entry level
    entry_price = None
    if trigger_info and trigger_info.get("price"):
        entry_price = float(trigger_info["price"])
    elif or_high is not None:
        entry_price = float(or_high)
    elif eval_res.get("level") is not None:
        entry_price = float(eval_res["level"])
    else:
        entry_price = float(bars[-1]["c"])

    # Calculate stop & target using trader-exit
    stop_level = None
    target_level = None
    reward_risk = 2.0
    stop_label = "opening-range midpoint"
    target_label = "2R"

    try:
        rule, param = exit_mod.parse_target("2R")
        exit_plan = exit_mod.compute(
            entry=Decimal(str(round(entry_price, 2))),
            stop_rule="or_mid" if (or_high and or_low) else "pct",
            stop_param=None if (or_high and or_low) else Decimal("1.0"),
            target_rule=rule,
            target_param=param,
            or_high=Decimal(str(round(or_high, 2))) if or_high else None,
            or_low=Decimal(str(round(or_low, 2))) if or_low else None,
            sym=resolved_symbol
        )
        stop_level = float(exit_plan["stop"])
        target_level = float(exit_plan["target"])
        reward_risk = float(exit_plan.get("reward_risk", 2.0))
        stop_label = exit_plan.get("stop_label", stop_label)
        target_label = exit_plan.get("target_label", target_label)
    except Exception as e:
        stop_level = round(entry_price * 0.99, 2)
        target_level = round(entry_price * 1.02, 2)

    plan_doc = {
        "symbol": resolved_symbol,
        "side": "buy",
        "date": eval_res.get("date") or datetime.date.today().isoformat(),
        "entry": entry_price,
        "stop": stop_level,
        "target": target_level,
        "stop_label": stop_label,
        "target_label": target_label,
        "currency_symbol": "₹"
    }

    sized = None
    size_error = None
    try:
        sized = budget_mod.size_trade(plan_doc, config)
    except Exception as e:
        size_error = str(e)

    # ---------------------------------------------------------------- Metric Cards
    st.markdown("### 📊 Trade Setup & Telemetry")
    m1, m2, m3, m4, m5 = st.columns(5)

    with m1:
        or_h_str = f"₹{or_high:,.2f}" if or_high else "Forming..."
        or_l_str = f"₹{or_low:,.2f}" if or_low else "Forming..."
        st.metric(
            label="Opening Range (15m)",
            value=f"{or_h_str}",
            delta=f"Low: {or_l_str}",
            delta_color="off"
        )

    with m2:
        status_color = "normal"
        if signal_status == "TRIGGERED":
            status_color = "inverse"
        st.metric(
            label="Signal Status",
            value=signal_status,
            delta=f"Phase: {signal_phase}" if signal_phase else None
        )

    with m3:
        st.metric(
            label="entry level",
            value=f"₹{entry_price:,.2f}",
            delta=f"Last: ₹{bars[-1]['c']:,.2f}" if bars else None,
            delta_color="off"
        )

    with m4:
        stop_diff = (stop_level - entry_price) if stop_level else 0
        st.metric(
            label="stop",
            value=f"₹{stop_level:,.2f}" if stop_level else "N/A",
            delta=f"{stop_diff:,.2f} ({stop_label})",
            delta_color="inverse"
        )

    with m5:
        target_diff = (target_level - entry_price) if target_level else 0
        st.metric(
            label="target",
            value=f"₹{target_level:,.2f}" if target_level else "N/A",
            delta=f"+{target_diff:,.2f} ({target_label})",
            delta_color="normal"
        )

    if signal_reason:
        st.info(f"**Signal Reason:** {signal_reason}")

    # ---------------------------------------------------------------- Sizing & Charges
    st.markdown("### 💰 Position Sizing & Statutory Charges")
    s_col1, s_col2 = st.columns([1, 1])

    if sized:
        qty = sized.get("qty", 0)
        cost = sized.get("cost", 0.0)
        bound_by = sized.get("bound_by", "max loss")
        charges = sized.get("charges", {})
        total_charges = charges.get("total", 0.0)
        net_max_loss = sized.get("max_loss", 0.0)

        with s_col1:
            st.markdown(
                f"""
                <div style="background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 16px;">
                    <h4 style="margin-top: 0; color: #1e293b;">Proposed Position</h4>
                    <table style="width: 100%; border-collapse: collapse;">
                        <tr><td style="padding: 6px 0; color: #64748b;">Quantity:</td><td style="text-align: right; font-weight: 600; font-size: 18px; color: #0f172a;">{qty} shares</td></tr>
                        <tr><td style="padding: 6px 0; color: #64748b;">Total Order Value:</td><td style="text-align: right; font-weight: 600; color: #0f172a;">₹{cost:,.2f}</td></tr>
                        <tr><td style="padding: 6px 0; color: #64748b;">Sizing Bound By:</td><td style="text-align: right; font-weight: 600; color: #2563eb;">{bound_by}</td></tr>
                        <tr><td style="padding: 6px 0; color: #64748b;">Net Max Loss (at Stop):</td><td style="text-align: right; font-weight: 600; color: #dc2626;">₹{net_max_loss:,.2f}</td></tr>
                        <tr><td style="padding: 6px 0; color: #64748b;">Account Max Loss Limit:</td><td style="text-align: right; color: #64748b;">₹{max_loss_cfg:,.2f}</td></tr>
                    </table>
                </div>
                """,
                unsafe_allow_html=True
            )

        with s_col2:
            st.markdown(
                f"""
                <div style="background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 16px;">
                    <h4 style="margin-top: 0; color: #1e293b;">Indian Statutory Charges Breakdown</h4>
                    <table style="width: 100%; border-collapse: collapse;">
                        <tr><td style="padding: 4px 0; color: #64748b;">Brokerage (Buy + Sell):</td><td style="text-align: right; font-weight: 500;">₹{charges.get('total_brokerage', 0.0):,.2f}</td></tr>
                        <tr><td style="padding: 4px 0; color: #64748b;">STT (Securities Transaction Tax):</td><td style="text-align: right; font-weight: 500;">₹{charges.get('stt', 0.0):,.2f}</td></tr>
                        <tr><td style="padding: 4px 0; color: #64748b;">Exchange Turnover Fee:</td><td style="text-align: right; font-weight: 500;">₹{charges.get('exchange_fee', 0.0):,.2f}</td></tr>
                        <tr><td style="padding: 4px 0; color: #64748b;">SEBI Turnover Fee:</td><td style="text-align: right; font-weight: 500;">₹{charges.get('sebi_fee', 0.0):,.2f}</td></tr>
                        <tr><td style="padding: 4px 0; color: #64748b;">GST (18% on Brokerage + Exch):</td><td style="text-align: right; font-weight: 500;">₹{charges.get('gst', 0.0):,.2f}</td></tr>
                        <tr><td style="padding: 4px 0; color: #64748b;">Stamp Duty:</td><td style="text-align: right; font-weight: 500;">₹{charges.get('stamp_duty', 0.0):,.2f}</td></tr>
                        <tr style="border-top: 1px solid #cbd5e1;"><td style="padding: 6px 0; font-weight: 600; color: #0f172a;">Total Estimated Charges:</td><td style="text-align: right; font-weight: 700; color: #0f172a;">₹{total_charges:,.2f}</td></tr>
                    </table>
                </div>
                """,
                unsafe_allow_html=True
            )
    else:
        st.warning(f"Could not calculate position size: {size_error or 'invalid trade parameters'}")

    # ---------------------------------------------------------------- 5-Minute Chart
    st.markdown("### 📈 5-Minute Intraday Chart")

    df_bars = pd.DataFrame(bars)
    df_bars["datetime"] = pd.to_datetime(df_bars["t"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
    df_bars["time_str"] = df_bars["datetime"].dt.strftime("%H:%M")

    fig = go.Figure()

    # Candlestick chart
    fig.add_trace(go.Candlestick(
        x=df_bars["time_str"],
        open=df_bars["o"],
        high=df_bars["h"],
        low=df_bars["l"],
        close=df_bars["c"],
        name="5m Candles",
        increasing_line_color="#10b981",
        decreasing_line_color="#ef4444",
        increasing_fillcolor="#10b981",
        decreasing_fillcolor="#ef4444"
    ))

    # Draw Target line
    if target_level:
        fig.add_hline(
            y=target_level,
            line_dash="dash",
            line_color="#10b981",
            line_width=1.5,
            annotation_text=f"target: ₹{target_level:,.2f}",
            annotation_position="top right",
            annotation_font_color="#10b981"
        )

    # Draw Entry Level line
    if entry_price:
        fig.add_hline(
            y=entry_price,
            line_dash="solid",
            line_color="#2563eb",
            line_width=2,
            annotation_text=f"entry level: ₹{entry_price:,.2f}",
            annotation_position="top right",
            annotation_font_color="#2563eb"
        )

    # Draw Stop line
    if stop_level:
        fig.add_hline(
            y=stop_level,
            line_dash="dash",
            line_color="#ef4444",
            line_width=1.5,
            annotation_text=f"stop: ₹{stop_level:,.2f}",
            annotation_position="bottom right",
            annotation_font_color="#ef4444"
        )

    # Draw Opening Range High / Low lines
    if or_high and or_low:
        fig.add_hline(
            y=or_high,
            line_dash="dot",
            line_color="#64748b",
            line_width=1,
            annotation_text=f"OR High: ₹{or_high:,.2f}",
            annotation_position="top left",
            annotation_font_color="#64748b"
        )
        fig.add_hline(
            y=or_low,
            line_dash="dot",
            line_color="#64748b",
            line_width=1,
            annotation_text=f"OR Low: ₹{or_low:,.2f}",
            annotation_position="bottom left",
            annotation_font_color="#64748b"
        )

    fig.update_layout(
        title=f"{resolved_symbol} — 5-Minute Intraday Chart with Trade Levels",
        xaxis_title="Time (IST)",
        yaxis_title="Price (₹)",
        xaxis_rangeslider_visible=False,
        height=550,
        margin=dict(l=40, r=40, t=50, b=40),
        template="plotly_white",
        hovermode="x unified"
    )

    st.plotly_chart(fig, use_container_width=True)

    # ---------------------------------------------------------------- Single Stock Flow Section
    st.markdown("### 🌊 Flow (previous day, not live)")
    st.caption("Information only, not a trade recommendation. Reflects reported institutional and promoter bulk/block deals.")

    if not deals:
        st.info("No bulk/block deal data available. Please add the downloaded NSE CSV files into `trader/flow/` or switch to 'Sample Data (Offline Fixture)' in the sidebar.")
    else:
        sym_flow = flow_mod.get_symbol_flow_summary(deals, resolved_symbol)
        vol_ratio = flow_mod.compute_volume_vs_20d_avg(resolved_symbol, daily_volumes=[b["v"] for b in bars] if bars else None)

        f_col1, f_col2, f_col3, f_col4, f_col5 = st.columns(5)
        with f_col1:
            st.metric(
                label="Bulk/Block Deals",
                value=f"{sym_flow['total_deals']}",
                delta=f"{sym_flow['buy_deals']} Buy / {sym_flow['sell_deals']} Sell",
                delta_color="off"
            )
        with f_col2:
            st.metric(
                label="Total Buy Flow",
                value=flow_mod.format_inr(sym_flow["buy_value_inr"]),
                delta=f"{sym_flow['buy_quantity']:,} shs" if sym_flow['buy_quantity'] else None,
                delta_color="normal"
            )
        with f_col3:
            st.metric(
                label="Total Sell Flow",
                value=flow_mod.format_inr(sym_flow["sell_value_inr"]),
                delta=f"{sym_flow['sell_quantity']:,} shs" if sym_flow['sell_quantity'] else None,
                delta_color="inverse"
            )
        with f_col4:
            net_delta_color = "normal" if sym_flow["net_value_inr"] >= 0 else "inverse"
            st.metric(
                label="Net Flow",
                value=flow_mod.format_inr(sym_flow["net_value_inr"]),
                delta=sym_flow["net_action"],
                delta_color=net_delta_color
            )
        with f_col5:
            st.metric(
                label="Volume vs 20d Avg",
                value=f"{vol_ratio['ratio']:.2f}x" if vol_ratio.get("ratio") else "N/A",
                delta=f"{vol_ratio.get('pct_diff', 0):+.1f}%" if vol_ratio.get("pct_diff") is not None else vol_ratio.get("label", ""),
                delta_color="off"
            )

        if sym_flow["deals"]:
            deal_rows = []
            for d in sym_flow["deals"]:
                pct_val = f"{d['pct_shares']:.2f}%" if d.get("pct_shares") is not None else "-"
                deal_rows.append({
                    "Date": d["date"],
                    "Type": d["deal_type"],
                    "Action": d["action"],
                    "Quantity": f"{d['quantity']:,}",
                    "Price (₹)": f"₹{d['price']:,.2f}",
                    "Total Value": flow_mod.format_inr(d["value_inr"]),
                    "% Stake": pct_val,
                    "Client Name": d["client_name"]
                })
            st.dataframe(pd.DataFrame(deal_rows), use_container_width=True, hide_index=True)
        else:
            st.info(f"No bulk or block deals recorded for {resolved_symbol} in current deal disclosures.")

        with st.expander("🏆 Top Stocks by Deal Size Across Market (from downloaded flow files)", expanded=False):
            top_stocks = flow_mod.get_top_stocks_by_deal_size(deals, top_n=10)
            if top_stocks:
                top_rows = []
                for t in top_stocks:
                    top_rows.append({
                        "Symbol": t["symbol"],
                        "Security Name": t["security_name"],
                        "Deals": t["total_deals"],
                        "Total Turnover": flow_mod.format_inr(t["total_value_inr"]),
                        "Buy Flow": flow_mod.format_inr(t["buy_value_inr"]),
                        "Sell Flow": flow_mod.format_inr(t["sell_value_inr"]),
                        "Net Flow": flow_mod.format_inr(t["net_value_inr"])
                    })
                st.dataframe(pd.DataFrame(top_rows), use_container_width=True, hide_index=True)
            else:
                st.write("No deal files currently loaded in `trader/flow/`. Place downloaded CSV files there to see market-wide deal rankings.")

    with tab_log:
        st.markdown("### 📜 Signal Log")
        st.caption("Historical record of all one-click analyses run in TradeInsta. Saved to `trader/signal_log.csv`.")
        records = load_signal_log(limit=100)
        if not records:
            st.info("No analyses recorded yet. Click 'Analyze now' on any stock to record an entry.")
        else:
            df_log = pd.DataFrame(records)
            st.dataframe(df_log, use_container_width=True, hide_index=True)
            csv_data = df_log.to_csv(index=False).encode("utf-8")
            st.download_button(
                "📥 Download Signal Log (CSV)",
                data=csv_data,
                file_name="signal_log.csv",
                mime="text/csv"
            )

    # ---------------------------------------------------------------- Footer Note
    st.markdown(
        """
        <div style="text-align: center; color: #94a3b8; font-size: 13px; margin-top: 30px; padding: 12px; border-top: 1px solid #e2e8f0;">
            Paper trading, not advice. Strictly running locally on 127.0.0.1. Credentials are never written, logged, or displayed.
        </div>
        """,
        unsafe_allow_html=True
    )


if __name__ == "__main__":
    main()
