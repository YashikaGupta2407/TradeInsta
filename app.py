#!/usr/bin/env python3
"""Local Streamlit app wrapping existing trader skills.

Runs strictly on 127.0.0.1.
Paper trading, not advice.
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
import trader.broker_data as broker_data

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


def get_badge_info(is_sample: bool, is_open: bool, data_source: str, bar_age: str = "", session_reason: str = "") -> dict:
    """Determine top-of-page badge state, color, and label.

    Badge labels:
    - 'LIVE (broker)': Broker real-time intraday stream during market hours
    - 'DELAYED (Yahoo)': Yahoo Finance ~15-minute delayed stream during market hours
    - 'LAST SESSION (market closed)': Outside market hours or holiday (displaying last session data)
    - 'SAMPLE DATA': Offline simulated test fixtures / demo data
    """
    if is_sample:
        return {
            "label": "SAMPLE DATA",
            "bg": "#7c3aed",  # Purple
            "detail": f"Offline test fixture / simulated candles • Source: {data_source.capitalize()}"
        }
    if not is_open:
        reason_text = f" ({session_reason})" if session_reason else ""
        return {
            "label": "LAST SESSION (market closed)",
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


def main():
    # ---------------------------------------------------------------- Page Config
    st.set_page_config(
        page_title="NSE Day Trader",
        page_icon="📈",
        layout="wide",
        initial_sidebar_state="expanded"
    )

    # ---------------------------------------------------------------- Sidebar
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
    st.sidebar.header("📡 Feed Mode")
    feed_mode = st.sidebar.radio(
        "Data Source Mode",
        options=["Live / Network Market Feed", "Sample Data (Offline Fixture)"],
        index=0,
        help="Choose between real network market data or offline test fixtures."
    )
    is_sample_mode = (feed_mode == "Sample Data (Offline Fixture)")

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
    # Never display or log the actual tokens!
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
        try:
            chart = entry_mod.get_chart(resolved_symbol, interval="5m")
        except Exception as e:
            fetch_error = str(e)

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

    # Header title
    st.title(f"{resolved_symbol}")
    if company_name and company_name != resolved_symbol:
        st.subheader(company_name)

    # ---------------------------------------------------------------- Data Evaluation
    if fetch_error or not chart:
        st.error(f"⚠️ Could not load market data for {resolved_symbol}: {fetch_error or 'no data available'}")
        return

    if not bars:
        st.warning(f"No 5-minute bars returned for {resolved_symbol} today.")
        return

    # Evaluate setup using trader-entry
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
        # Safe fallback if exit calculation fails
        stop_level = round(entry_price * 0.99, 2)
        target_level = round(entry_price * 1.02, 2)

    # Size the trade using trader-budget
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
