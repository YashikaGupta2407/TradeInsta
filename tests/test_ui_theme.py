#!/usr/bin/env python3
"""Tests for Phase 1 UI Theme, App Shell, and Navigation."""

import datetime
import os
import re
import unittest
from unittest.mock import patch

from app import get_upstox_token_status, get_nse_status, compute_trust_strip_info


class TestUITheme(unittest.TestCase):
    def setUp(self):
        self.root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.theme_path = os.path.join(self.root, "ui", "theme.css")
        self.app_path = os.path.join(self.root, "app.py")

    def test_theme_css_exists_and_contains_design_tokens(self):
        self.assertTrue(os.path.exists(self.theme_path), "ui/theme.css must exist")
        with open(self.theme_path, "r", encoding="utf-8") as f:
            css = f.read()

        # Design tokens
        self.assertIn("#0E1116", css, "Background #0E1116 must be defined")
        self.assertIn("#161B22", css, "Panel background #161B22 must be defined")
        self.assertIn("#12161C", css, "Sidebar background #12161C must be defined")
        self.assertIn("#232A33", css, "Border subtle #232A33 must be defined")
        self.assertIn("#5EA8FF", css, "Accent blue #5EA8FF must be defined")
        self.assertIn("#3DD68C", css, "Gain green #3DD68C must be defined")
        self.assertIn("#FF6B6B", css, "Loss red #FF6B6B must be defined")
        self.assertIn("#F2B84B", css, "Warning amber #F2B84B must be defined")

        # Fonts
        self.assertIn("Inter", css, "Inter font must be referenced")
        self.assertIn("IBM Plex Mono", css, "IBM Plex Mono font must be referenced")
        self.assertIn("tabular-nums", css, "tabular-nums must be enforced for tabular mono alignment")

        # Radiuses
        self.assertIn("12px", css, "Panel radius 12px must be present")
        self.assertIn("999px", css, "Chip radius 999px must be present")

        # Pulsing animation and accessibility
        self.assertIn("pulse-ring", css, "Pulsing keyframe animation must be defined")
        self.assertIn("@media (prefers-reduced-motion: reduce)", css, "Accessibility reduced-motion query must be defined")

    def test_no_emojis_in_app_code(self):
        with open(self.app_path, "r", encoding="utf-8") as f:
            app_code = f.read()

        emoji_pattern = re.compile(r"[\U00010000-\U0010ffff]", flags=re.UNICODE)
        matches = set(emoji_pattern.findall(app_code))
        self.assertEqual(len(matches), 0, f"No emojis should be present in app.py. Found: {matches}")

    def test_token_status_detection(self):
        # When token is not set
        with patch.dict(os.environ, {"UPSTOX_ACCESS_TOKEN": ""}, clear=True):
            status = get_upstox_token_status()
            self.assertEqual(status, "not set")

        # When token is set
        with patch.dict(os.environ, {"UPSTOX_ACCESS_TOKEN": "mock_token_abc"}, clear=True):
            status = get_upstox_token_status()
            self.assertEqual(status, "found")

    def test_nse_status_detection(self):
        text, dot_cls, is_open = get_nse_status()
        self.assertIsInstance(text, str)
        self.assertIsInstance(dot_cls, str)
        self.assertIsInstance(is_open, bool)
        self.assertTrue(text.startswith("NSE open") or text.startswith("NSE closed"))

    def test_headless_app_navigation(self):
        from streamlit.testing.v1 import AppTest

        at = AppTest.from_file(self.app_path)
        at.run(timeout=10)
        self.assertEqual(len(at.exception), 0, f"App execution failed with: {[e.value for e in at.exception]}")

        # Check that navigation buttons exist
        button_labels = [b.label for b in at.sidebar.button]
        self.assertIn("Analyze now", button_labels)
        self.assertIn("Scanner", button_labels)
        self.assertIn("Position size", button_labels)
        self.assertIn("Features", button_labels)

    def test_trust_strip_live_upstox_pulsing_and_bar_age(self):
        ist_tz = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
        # Wednesday 11:47 IST during active market hours
        t_live = datetime.datetime(2026, 10, 21, 11, 47, tzinfo=ist_tz)
        b_start = int(datetime.datetime(2026, 10, 21, 11, 40, tzinfo=ist_tz).timestamp())
        chart = {"bars": [{"t": b_start, "o": 100, "h": 105, "l": 99, "c": 102}]}

        info = compute_trust_strip_info("upstox", chart=chart, is_sample_mode=False, now_utc=t_live)
        self.assertEqual(info["source_label"], "LIVE · Upstox")
        self.assertEqual(info["dot_cls"], "green pulsing")
        self.assertTrue(info["is_live"])
        self.assertIn("11:40 to 11:45", info["bar_info_str"])
        self.assertIn("age 2m", info["bar_info_str"])
        self.assertFalse(info["is_sample_mode"])

    def test_trust_strip_delayed_yahoo_no_pulse(self):
        ist_tz = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
        t_live = datetime.datetime(2026, 10, 21, 11, 47, tzinfo=ist_tz)
        chart = {"bars": [{"t": int(t_live.timestamp()) - 600, "o": 100, "h": 105, "l": 99, "c": 102}]}

        info = compute_trust_strip_info("yahoo", chart=chart, is_sample_mode=False, now_utc=t_live)
        self.assertEqual(info["source_label"], "DELAYED · Yahoo")
        self.assertEqual(info["dot_cls"], "amber")
        self.assertFalse(info["is_live"])
        self.assertFalse(info["is_sample_mode"])

    def test_trust_strip_market_closed_and_holiday(self):
        ist_tz = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
        chart = {"bars": [{"t": 1761000000, "o": 100, "h": 105, "l": 99, "c": 102}]}

        # Closed (after 15:30)
        t_closed = datetime.datetime(2026, 10, 21, 16, 0, tzinfo=ist_tz)
        info_closed = compute_trust_strip_info("yahoo", chart=chart, is_sample_mode=False, now_utc=t_closed)
        self.assertEqual(info_closed["source_label"], "LAST SESSION")
        self.assertEqual(info_closed["dot_cls"], "grey")
        self.assertFalse(info_closed["is_live"])

        # Holiday (2026-10-02 Mahatma Gandhi Jayanti)
        t_hol = datetime.datetime(2026, 10, 2, 11, 0, tzinfo=ist_tz)
        info_hol = compute_trust_strip_info("yahoo", chart=chart, is_sample_mode=False, now_utc=t_hol)
        self.assertTrue(info_hol["source_label"].startswith("HOLIDAY"))
        self.assertEqual(info_hol["dot_cls"], "grey")
        self.assertFalse(info_hol["is_live"])

        # Pre-open (09:10 IST)
        t_pre = datetime.datetime(2026, 10, 21, 9, 10, tzinfo=ist_tz)
        info_pre = compute_trust_strip_info("yahoo", chart=chart, is_sample_mode=False, now_utc=t_pre)
        self.assertEqual(info_pre["source_label"], "MARKET NOT OPEN")
        self.assertEqual(info_pre["dot_cls"], "grey")
        self.assertFalse(info_pre["is_live"])

    def test_trust_strip_sample_mode_badge(self):
        ist_tz = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
        t_live = datetime.datetime(2026, 10, 21, 11, 47, tzinfo=ist_tz)
        chart = {"bars": [{"t": int(t_live.timestamp()) - 300, "o": 100, "h": 105, "l": 99, "c": 102}]}

        info = compute_trust_strip_info("sample", chart=chart, is_sample_mode=True, now_utc=t_live)
        self.assertEqual(info["dot_cls"], "grey")
        self.assertTrue(info["is_sample_mode"])

    def test_price_ladder_rules(self):
        from unittest.mock import MagicMock
        import streamlit as st

        captured_html = []
        capture_fn = lambda html, **kw: captured_html.append(html)
        with patch.object(st, "html", side_effect=capture_fn), patch.object(st, "markdown", side_effect=capture_fn):
            from app import render_price_ladder

            # Test normal full analysis ladder
            render_price_ladder(
                current_price=2520.0,
                range_high=2500.0,
                range_low=2450.0,
                entry_level=2500.0,
                stop_level=2475.0,
                target_level=2550.0,
                day_phase="0930_to_1530"
            )
            full_html = "".join(captured_html)
            self.assertIn("TARGET", full_html)
            self.assertIn("ENTRY LEVEL", full_html)
            self.assertIn("STOP", full_html)
            self.assertIn("RANGE LOW", full_html)
            self.assertIn("2,520.00", full_html)

            # Rule: Do not use the words buy or sell on the ladder
            self.assertNotIn("buy", full_html.lower())
            self.assertNotIn("sell", full_html.lower())

            # Test 09:15 to 09:30 forming window (entry, stop, target suppressed)
            forming_html = []
            forming_capture_fn = lambda html, **kw: forming_html.append(html)
            with patch.object(st, "html", side_effect=forming_capture_fn), patch.object(st, "markdown", side_effect=forming_capture_fn):
                render_price_ladder(
                    current_price=2480.0,
                    range_high=2490.0,
                    range_low=2460.0,
                    entry_level=None,
                    stop_level=None,
                    target_level=None,
                    day_phase="0915_to_0930"
                )
            forming_text = "".join(forming_html)
            self.assertIn("OPENING RANGE FORMING", forming_text)
            self.assertNotIn("ENTRY LEVEL", forming_text)
            self.assertNotIn("TARGET", forming_text)

    def test_past_odds_100_dots(self):
        import streamlit as st
        captured_html = []
        capture_fn = lambda html, **kw: captured_html.append(html)
        with patch.object(st, "html", side_effect=capture_fn), patch.object(st, "markdown", side_effect=capture_fn):
            from app import render_past_odds_panel
            render_past_odds_panel({"win_rate": 60.0, "stop_pct": 30.0, "samples": 50})

        html = "".join(captured_html)
        dot_count = html.count("odds-dot")
        self.assertEqual(dot_count, 100, "Past odds panel must render exactly 100 dots")
        self.assertIn("dot-target", html)
        self.assertIn("dot-stop", html)
        self.assertIn("dot-neither", html)

    def test_step_chips_and_disclaimer(self):
        import streamlit as st
        captured_html = []
        capture_fn = lambda html, **kw: captured_html.append(html)
        with patch.object(st, "html", side_effect=capture_fn), patch.object(st, "markdown", side_effect=capture_fn):
            from app import render_step_chips
            mock_steps = {
                "data_check": {"status": "ok", "timestamp": "11:47:00"},
                "opening_range": {"status": "ok", "timestamp": "11:47:00"},
                "entry_status": {"status": "ok", "timestamp": "11:47:00"},
                "levels": {"status": "ok", "timestamp": "11:47:00"},
                "size_and_charges": {"status": "ok", "timestamp": "11:47:00"},
                "risk_check": {"status": "ok", "timestamp": "11:47:00"},
                "historical_odds": {"status": "ok", "timestamp": "11:47:00"},
                "context": {"status": "ok", "timestamp": "11:47:00"}
            }
            render_step_chips(mock_steps)

        html = "".join(captured_html)
        for label in ["Data", "Range", "Entry", "Levels", "Size", "Risk", "Odds", "Context"]:
            self.assertIn(label, html)
        self.assertIn("Paper trading, not advice. Levels come from fixed rules and past data and do not predict the price.", html)

    def test_clean_html_helper_no_indented_lines_and_no_empty_lines(self):
        from app import clean_html, render_html
        import streamlit as st

        sample_inputs = [
            """
            <div class="timeline-panel-wrap">
                <div class="timeline-header">Day Timeline</div>

                <div class="timeline-track">
                    <div class="timeline-fill" style="width: 50%;"></div>
                </div>

            </div>
            """,
            """
                <div class="ladder-canvas">
                    <div class="ladder-line target" style="top: 20%;"></div>

                    <div class="ladder-label-left">TARGET</div>
                </div>
            """,
            """
            <span class="step-chip">Data</span>
            """
        ]

        for raw_html in sample_inputs:
            cleaned = clean_html(raw_html)
            self.assertTrue(len(cleaned) > 0)
            lines = cleaned.splitlines()
            for line in lines:
                # Must not start with 4 or more spaces
                self.assertFalse(line.startswith("    "), f"Line starts with 4+ spaces: {line!r}")
                # Must not start with tabs
                self.assertFalse(line.startswith("\t"), f"Line starts with tab: {line!r}")
                # Must not be empty
                self.assertTrue(len(line.strip()) > 0, "Found empty line in cleaned output")

        # Test render_html dispatches cleaned string
        captured = []
        with patch.object(st, "html", side_effect=lambda h, **kw: captured.append(h)):
            render_html("   <div>Test Content</div>   \n\n   <span>Sub</span>   ")
        self.assertEqual(len(captured), 1)
        self.assertNotIn("    ", captured[0])
        self.assertNotIn("\n\n", captured[0])

    def test_features_directory_12_cards_and_dynamic_tags(self):
        from app import compute_feature_directory

        # Test live mode directory
        feats = compute_feature_directory(is_sample_mode=False)
        self.assertEqual(len(feats), 12, "Must contain exactly 12 tools")

        f_map = {f["name"]: f for f in feats}
        expected_names = [
            "Analyze now", "Scanner", "Entry timer", "Exit planner",
            "Position size", "Risk check", "Time Machine", "Alerts",
            "Signal log", "Sector heatmap", "Disclosed flow", "Radar"
        ]
        for name in expected_names:
            self.assertIn(name, f_map)

        # Descriptions
        self.assertEqual(f_map["Analyze now"]["desc"], "full analysis of one stock as of this minute.")
        self.assertEqual(f_map["Scanner"]["desc"], "finds stocks that broke out of their opening range, then re-checks the shortlist on broker data.")
        self.assertEqual(f_map["Entry timer"]["desc"], "tracks whether a breakout is waiting, triggered, expired or failed.")
        self.assertEqual(f_map["Exit planner"]["desc"], "stop and target from the range midpoint, range low or ATR.")
        self.assertEqual(f_map["Position size"]["desc"], "quantity from maximum loss, including Indian charges.")
        self.assertEqual(f_map["Risk check"]["desc"], "PASS or BLOCK against loss limits and trading hours.")
        self.assertEqual(f_map["Time Machine"]["desc"], "replays 60 days of 5-minute bars to show how often a setup hit target, stop or time exit.")
        self.assertEqual(f_map["Alerts"]["desc"], "price and time alerts that respect NSE hours and holidays.")
        self.assertEqual(f_map["Signal log"]["desc"], "every analysis saved with its levels for later review.")
        self.assertEqual(f_map["Sector heatmap"]["desc"], "treemap of sectors and stocks, coloured by daily price change.")
        self.assertEqual(f_map["Disclosed flow"]["desc"], "reported bulk and block deals, published after the close.")
        self.assertEqual(f_map["Radar"]["desc"], "explained discovery feed.")

        # Fixed category freshness tags
        self.assertEqual(f_map["Time Machine"]["tag"], "60-DAY HISTORY")
        self.assertEqual(f_map["Disclosed flow"]["tag"], "PREVIOUS DAY")
        self.assertEqual(f_map["Radar"]["tag"], "PREVIOUS DAY")
        self.assertEqual(f_map["Exit planner"]["tag"], "LOCAL")
        self.assertEqual(f_map["Position size"]["tag"], "LOCAL")
        self.assertEqual(f_map["Risk check"]["tag"], "LOCAL")
        self.assertEqual(f_map["Signal log"]["tag"], "LOCAL")

        # Test sample mode: live tools switch to SAMPLE tag
        sample_feats = compute_feature_directory(is_sample_mode=True)
        sample_map = {f["name"]: f for f in sample_feats}
        self.assertEqual(sample_map["Analyze now"]["tag"], "SAMPLE")
        self.assertEqual(sample_map["Entry timer"]["tag"], "SAMPLE")

    def test_features_page_navigation(self):
        from streamlit.testing.v1 import AppTest

        at = AppTest.from_file(self.app_path)
        at.run(timeout=10)
        self.assertEqual(len(at.exception), 0)

        # Find Features button and click
        feat_buttons = [b for b in at.sidebar.button if b.label == "Features"]
        self.assertEqual(len(feat_buttons), 1)
        feat_buttons[0].click().run(timeout=10)
        self.assertEqual(len(at.exception), 0)

    def test_radar_card_label_mapping(self):
        from app import get_radar_card_label_and_badge

        # 1. Repeated buying
        lbl, badge = get_radar_card_label_and_badge({"has_repeated_buying": True, "pattern": "REPEATED BUYING"})
        self.assertEqual(lbl, "Repeated buying")
        self.assertEqual(badge, "buying")

        # 2. Repeated selling
        lbl, badge = get_radar_card_label_and_badge({"has_repeated_selling": True, "pattern": "REPEATED SELLING"})
        self.assertEqual(lbl, "Repeated selling")
        self.assertEqual(badge, "selling")

        # 3. Multiple buyers
        lbl, badge = get_radar_card_label_and_badge({"has_multiple_buyers": True, "pattern": "MULTIPLE BUYERS"})
        self.assertEqual(lbl, "Multiple buyers")
        self.assertEqual(badge, "buyers")

        # 4. Unusual volume
        lbl, badge = get_radar_card_label_and_badge({"live_volume": 2500000})
        self.assertEqual(lbl, "Unusual volume")
        self.assertEqual(badge, "volume")

    def test_radar_factual_reason_omits_buy_and_sell(self):
        from app import format_radar_factual_reason

        mock_items = [
            {"net_flow_inr": 750000000, "buy_value_inr": 750000000, "latest_deal_date": "2026-10-21", "buyers_count": 3},
            {"net_flow_inr": -400000000, "sell_value_inr": 400000000, "latest_deal_date": "2026-10-21", "buyers_count": 1},
            {"net_flow_inr": 120000000, "buy_value_inr": 120000000, "latest_deal_date": "2026-10-20", "buyers_count": 2},
            {"net_flow_inr": 50000000, "live_volume": 3000000, "latest_deal_date": "2026-10-21"}
        ]

        for item in mock_items:
            for label in ["Repeated buying", "Multiple buyers", "Repeated selling", "Unusual volume"]:
                reason = format_radar_factual_reason(item, label)
                # Strict check: never contain 'buy' or 'sell' as words
                self.assertNotIn("buy", reason.lower())
                self.assertNotIn("sell", reason.lower())
                self.assertTrue(len(reason) > 10)

    def test_big_bull_navigation_headless(self):
        from streamlit.testing.v1 import AppTest

        at = AppTest.from_file(self.app_path)
        at.run(timeout=10)
        self.assertEqual(len(at.exception), 0)

        # 1. Sector Heatmap navigation
        sh_btn = [b for b in at.sidebar.button if b.label == "Sector heatmap"]
        self.assertEqual(len(sh_btn), 1)
        sh_btn[0].click().run(timeout=10)
        self.assertEqual(len(at.exception), 0)

        # 2. Disclosed Flow navigation
        df_btn = [b for b in at.sidebar.button if b.label == "Disclosed flow"]
        self.assertEqual(len(df_btn), 1)
        df_btn[0].click().run(timeout=10)
        self.assertEqual(len(at.exception), 0)

        # 3. Radar navigation
        radar_btn = [b for b in at.sidebar.button if b.label == "Radar"]
        self.assertEqual(len(radar_btn), 1)
        radar_btn[0].click().run(timeout=10)
        self.assertEqual(len(at.exception), 0)

    def test_render_analyze_now_view_with_and_without_bars(self):
        import streamlit as st
        from app import render_analyze_now_view

        cfg = {"tick_size": 0.05, "max_loss_per_trade_inr": 2000.0}

        # Case 1: Normal case with bars
        sample_bars = [
            {"t": 1761019200, "o": 2500.0, "h": 2510.0, "l": 2495.0, "c": 2505.0, "v": 1000},
            {"t": 1761019500, "o": 2505.0, "h": 2520.0, "l": 2502.0, "c": 2518.0, "v": 1500}
        ]
        normal_analysis = {
            "symbol": "RELIANCE.NS",
            "now_ist": "11:45:00 IST",
            "day_phase": "0930_to_1530",
            "bars": sample_bars,
            "chart": {"bars": sample_bars, "meta": {"source": "sample"}},
            "steps": {
                "data_check": {"source": "sample", "current_price": 2518.0, "bars": sample_bars},
                "opening_range": {"high": 2510.0, "low": 2495.0},
                "entry_status": {"signal": "triggered", "trigger": {"price": 2512.0, "time": "09:35"}},
                "levels": {"entry_level": 2510.0, "stop_level": 2500.0, "target_level": 2530.0},
                "size_and_charges": {"quantity": 100, "position_value": 251000.0, "total_charges": 150.0, "max_loss_per_trade": 1000.0},
                "risk_check": {"verdict": "PASS", "reason": "Fits limits"},
                "historical_odds": {"win_rate": 62.0, "samples": 60, "verdict": "Edge"},
                "context": {"volume_vs_20d_label": "1.2x", "headlines": [], "flow_summary": {}},
                "summary": {"sentences": ["Setup confirmed."]}
            }
        }

        with patch.dict(st.session_state, {"last_analysis_RELIANCE.NS": normal_analysis}):
            # Must run without raising NameError or any exception
            render_analyze_now_view("RELIANCE.NS", "Reliance Industries", is_sample_mode=True, config=cfg)

        # Case 2: No-bars case (market closed, weekend, or data error)
        no_bars_analysis = {
            "symbol": "RELIANCE.NS",
            "now_ist": "08:30:00 IST",
            "day_phase": "before_0915",
            "bars": [],
            "chart": {"bars": [], "meta": {"source": "yahoo"}},
            "steps": {
                "data_check": {"source": "yahoo", "market_state": "pre-open", "fallback_reason": "Pre-market window", "bars": []},
                "opening_range": {},
                "entry_status": {"signal": "waiting"},
                "levels": {},
                "size_and_charges": {},
                "risk_check": {"verdict": "BLOCK", "reason": "Market not open"},
                "historical_odds": {},
                "context": {},
                "summary": {"sentences": ["Market opens at 09:15 IST."]}
            }
        }

        with patch.dict(st.session_state, {"last_analysis_RELIANCE.NS": no_bars_analysis}):
            # Must run cleanly without raising NameError
            render_analyze_now_view("RELIANCE.NS", "Reliance Industries", is_sample_mode=False, config=cfg)

        # Case 3: Cold load case (no analysis yet)
        with patch.dict(st.session_state, {}, clear=True):
            render_analyze_now_view("RELIANCE.NS", "Reliance Industries", is_sample_mode=False, config=cfg)


if __name__ == "__main__":
    unittest.main()

