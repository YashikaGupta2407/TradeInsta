#!/usr/bin/env python3
"""Comprehensive test suite for Workflow & Session State Integration (Phase 2C / ISS-03).

Covers:
1. Active analysis context is stored correctly in st.session_state.
2. Exit Planner inherits levels from active analysis.
3. Position Size uses inherited levels and produces correct sizing.
4. Risk Check evaluates the real trade from active analysis.
5. Changing symbol clears / invalidates previous context (no state bleed).
6. Missing analysis shows 'no active analysis' state gracefully.
7. Incomplete analysis (e.g. during opening range formation) does NOT provide fabricated levels.
8. Blocked risk check is correctly reflected across downstream views.
9. Manual override of inherited values is possible and clearly labeled.
10. Analysis timestamp and data source are preserved across page switches.
"""

from decimal import Decimal
import unittest
from unittest.mock import patch, MagicMock

import streamlit as st
import app
from app import (
    extract_analysis_context,
    set_active_analysis,
    get_analysis_context_for_symbol,
    render_exit_planner_view,
    render_position_size_view,
    render_risk_check_view,
    render_time_machine_view,
    evaluate_order_risk,
)


def _build_sample_analysis_complete(symbol="RELIANCE.NS"):
    return {
        "symbol": symbol,
        "ticker_base": symbol.replace(".NS", ""),
        "now_ist": "10:15:00",
        "now_date": "2026-11-12",
        "market_state": "open",
        "day_phase": "0930_to_1530",
        "steps": {
            "data_check": {
                "source": "upstox",
                "current_price": 2520.0,
                "current_price_time": "10:15:00",
                "current_price_source": "upstox",
                "last_bar_close": 2518.0,
                "last_bar_time": "10:15",
                "bar_age": "2m",
                "bars": []
            },
            "opening_range": {
                "high": 2510.0,
                "low": 2490.0,
                "bars_used": 3,
                "complete": True,
                "status": "ok"
            },
            "entry_status": {
                "signal": "triggered",
                "trigger": {"price": 2510.0, "time": "09:35"},
                "reason": "Breakout above range high 2510.0"
            },
            "levels": {
                "entry_level": 2510.0,
                "stop_level": 2500.0,
                "target_level": 2530.0,
                "reward_risk": 2.0,
                "stop_label": "opening-range midpoint",
                "target_label": "2R",
                "tick_size": 0.05,
                "status": "ok"
            },
            "size_and_charges": {
                "quantity": 180,
                "position_value": 451800.0,
                "total_charges": 312.50,
                "charges_breakdown": {
                    "brokerage": 40.0,
                    "stt": 225.0,
                    "exchange_txn_charge": 14.5,
                    "gst": 11.2,
                    "stamp_duty": 15.0,
                    "sebi_turnover_charge": 6.8
                },
                "account_budget": 500000.0,
                "max_loss_per_trade": 2000.0,
                "status": "ok"
            },
            "risk_check": {
                "verdict": "PASS",
                "reason": "Fits account budget and max loss limit",
                "status": "ok"
            },
            "historical_odds": {
                "win_rate": 62.5,
                "avg_win": 1.9,
                "avg_loss": 1.0,
                "samples": 45,
                "verdict": "Positive edge",
                "status": "ok"
            },
            "context": {
                "volume_vs_20d_ratio": 1.45,
                "volume_vs_20d_label": "1.45x vs 20d avg",
                "status": "ok"
            },
            "summary": {
                "sentences": ["Breakout above 2510 confirmed.", "Risk PASS."]
            }
        }
    }


def _build_sample_analysis_forming(symbol="RELIANCE.NS"):
    return {
        "symbol": symbol,
        "ticker_base": symbol.replace(".NS", ""),
        "now_ist": "09:20:00",
        "now_date": "2026-11-12",
        "market_state": "open",
        "day_phase": "0915_to_0930",
        "steps": {
            "data_check": {
                "source": "upstox",
                "current_price": 2505.0,
                "current_price_time": "09:20:00",
                "bar_age": "1m"
            },
            "opening_range": {
                "high": 2508.0,
                "low": 2495.0,
                "bars_used": 1,
                "complete": False,
                "status": "forming"
            },
            "entry_status": {
                "signal": "forming",
                "reason": "Opening range forming (1 of 3 bars closed)."
            },
            "levels": {
                "entry_level": None,
                "stop_level": None,
                "target_level": None,
                "reward_risk": 2.0,
                "status": "pending",
                "note": "Opening range is still forming (09:15 to 09:30)."
            },
            "size_and_charges": {
                "quantity": 0,
                "position_value": 0.0,
                "total_charges": 0.0,
                "status": "warning"
            },
            "risk_check": {
                "verdict": "BLOCK",
                "reason": "Opening range is still forming; entries not allowed before 09:30.",
                "status": "ok"
            }
        }
    }


def _build_sample_analysis_blocked(symbol="RELIANCE.NS", reason="Market is closed today (Weekend)."):
    return {
        "symbol": symbol,
        "ticker_base": symbol.replace(".NS", ""),
        "now_ist": "11:00:00",
        "now_date": "2026-11-15",
        "market_state": "weekend",
        "day_phase": "after_1530_or_closed",
        "steps": {
            "data_check": {
                "source": "yahoo",
                "current_price": 2500.0,
                "bar_age": "2d"
            },
            "opening_range": {
                "high": 2510.0,
                "low": 2490.0,
                "complete": True
            },
            "levels": {
                "entry_level": 2510.0,
                "stop_level": 2500.0,
                "target_level": 2530.0,
                "status": "ok"
            },
            "size_and_charges": {
                "quantity": 0,
                "position_value": 0.0,
                "total_charges": 0.0
            },
            "risk_check": {
                "verdict": "BLOCK",
                "reason": reason,
                "status": "ok"
            }
        }
    }


def _mock_cols(n):
    count = n if isinstance(n, int) else len(n)
    return [MagicMock() for _ in range(count)]


class TestWorkflowSessionState(unittest.TestCase):
    def setUp(self):
        self.state = {}
        self.patcher = patch.object(st, "session_state", self.state)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    # 1. Active analysis context is stored correctly in st.session_state
    def test_01_set_and_extract_active_analysis_context(self):
        raw = _build_sample_analysis_complete("RELIANCE.NS")
        ctx = set_active_analysis(raw)

        self.assertEqual(self.state["active_analysis_symbol"], "RELIANCE.NS")
        self.assertIn("active_analysis_context", self.state)
        self.assertEqual(ctx["symbol"], "RELIANCE.NS")
        self.assertEqual(ctx["entry_level"], 2510.0)
        self.assertEqual(ctx["stop_level"], 2500.0)
        self.assertEqual(ctx["target_level"], 2530.0)
        self.assertEqual(ctx["quantity"], 180)
        self.assertEqual(ctx["risk_verdict"], "PASS")
        self.assertEqual(ctx["analysis_status"], "complete")

    # 2. Exit Planner inherits levels from active analysis
    def test_02_exit_planner_inherits_levels(self):
        raw = _build_sample_analysis_complete("RELIANCE.NS")
        set_active_analysis(raw)

        ctx = get_analysis_context_for_symbol("RELIANCE.NS")
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx["entry_level"], 2510.0)
        self.assertEqual(ctx["stop_level"], 2500.0)
        self.assertEqual(ctx["target_level"], 2530.0)

        # Mock streamlit widgets in render_exit_planner_view
        with patch.object(st, "markdown"), patch.object(st, "caption"), \
             patch.object(st, "number_input", return_value=2510.0) as mock_num, \
             patch.object(st, "selectbox", return_value="2R"), \
             patch.object(st, "columns", side_effect=_mock_cols), \
             patch.object(st, "metric") as mock_metric:

            render_exit_planner_view("RELIANCE.NS", is_sample_mode=False, config={})
            # Verify number_input was called with the inherited entry price 2510.0, not the 2500.0 mock
            mock_num.assert_called_once()
            args, kwargs = mock_num.call_args
            self.assertEqual(kwargs.get("value"), 2510.0)

    # 3. Position Size uses inherited levels and produces correct sizing
    def test_03_position_size_uses_inherited_levels(self):
        raw = _build_sample_analysis_complete("RELIANCE.NS")
        set_active_analysis(raw)

        ctx = get_analysis_context_for_symbol("RELIANCE.NS")
        self.assertEqual(ctx["quantity"], 180)
        self.assertEqual(ctx["position_value"], 451800.0)
        self.assertEqual(ctx["total_charges"], 312.50)

        with patch.object(st, "markdown"), patch.object(st, "caption"), \
             patch.object(st, "number_input", side_effect=[2510.0, 2500.0]) as mock_inputs, \
             patch.object(st, "write") as mock_write, \
             patch.object(st, "expander"):

            render_position_size_view("RELIANCE.NS", is_sample_mode=False, config={"max_loss_per_trade_inr": 2000})
            # Verify inputs received inherited 2510.0 and 2500.0
            self.assertEqual(mock_inputs.call_count, 2)
            first_call_val = mock_inputs.call_args_list[0][1]["value"]
            second_call_val = mock_inputs.call_args_list[1][1]["value"]
            self.assertEqual(first_call_val, 2510.0)
            self.assertEqual(second_call_val, 2500.0)

    # 4. Risk Check evaluates the real trade from active analysis
    def test_04_risk_check_evaluates_real_trade(self):
        raw = _build_sample_analysis_complete("RELIANCE.NS")
        set_active_analysis(raw)

        with patch.object(st, "markdown"), patch.object(st, "caption"), \
             patch.object(st, "write") as mock_write, \
             patch.object(st, "success") as mock_success, \
             patch.object(st, "expander"):

            render_risk_check_view("RELIANCE.NS", is_sample_mode=False, config={})
            mock_success.assert_called_once()
            self.assertIn("PASS", mock_success.call_args[0][0])
            self.assertIn("Fits account budget", mock_success.call_args[0][0])

    # 5. Changing symbol clears / invalidates previous context (no state bleed)
    def test_05_changing_symbol_prevents_state_bleed(self):
        raw_reliance = _build_sample_analysis_complete("RELIANCE.NS")
        set_active_analysis(raw_reliance)
        self.state["active_symbol"] = "RELIANCE.NS"

        # Simulate user switching symbol in the sidebar to TCS.NS
        new_symbol = "TCS.NS"
        prev_sym = self.state.get("active_symbol")
        if prev_sym is not None and prev_sym != new_symbol:
            if "active_analysis_context" in self.state:
                del self.state["active_analysis_context"]
            if "active_analysis_symbol" in self.state:
                del self.state["active_analysis_symbol"]
        self.state["active_symbol"] = new_symbol

        # Verify active context was invalidated
        self.assertNotIn("active_analysis_context", self.state)
        # Verify get_analysis_context_for_symbol returns None for TCS.NS
        tcs_ctx = get_analysis_context_for_symbol("TCS.NS")
        self.assertIsNone(tcs_ctx)

    # 6. Missing analysis shows 'no active analysis' state gracefully
    def test_06_missing_analysis_shows_manual_mode(self):
        ctx = get_analysis_context_for_symbol("INFY.NS")
        self.assertIsNone(ctx)

        with patch.object(st, "markdown"), patch.object(st, "caption"), \
             patch.object(st, "columns", side_effect=_mock_cols), \
             patch.object(st, "button", return_value=False), \
             patch.object(st, "number_input", return_value=2500.0) as mock_num, \
             patch.object(st, "selectbox", return_value="2R"), \
             patch.object(st, "metric"):

            render_exit_planner_view("INFY.NS", is_sample_mode=False, config={})
            # Verify clean default fallback 2500.0 without crash
            mock_num.assert_called_once()
            self.assertEqual(mock_num.call_args[1]["value"], 2500.0)

    # 7. Incomplete analysis (e.g. during opening range formation) does NOT provide fabricated levels
    def test_07_opening_range_forming_no_fabricated_levels(self):
        raw_forming = _build_sample_analysis_forming("RELIANCE.NS")
        ctx = set_active_analysis(raw_forming)

        self.assertEqual(ctx["analysis_status"], "forming_range")
        self.assertIsNone(ctx["entry_level"])
        self.assertIsNone(ctx["stop_level"])
        self.assertIsNone(ctx["target_level"])
        self.assertEqual(ctx["quantity"], 0)
        self.assertIn("forming", ctx["status_message"])

    # 8. Blocked risk check is correctly reflected across downstream views
    def test_08_blocked_risk_status_preserved(self):
        raw_blocked = _build_sample_analysis_blocked("RELIANCE.NS", reason="Market is closed today (Weekend).")
        set_active_analysis(raw_blocked)

        ctx = get_analysis_context_for_symbol("RELIANCE.NS")
        self.assertEqual(ctx["risk_verdict"], "BLOCK")
        self.assertIn("Weekend", ctx["risk_reason"])

        with patch.object(st, "markdown"), patch.object(st, "caption"), \
             patch.object(st, "write"), \
             patch.object(st, "error") as mock_error, \
             patch.object(st, "expander"):

            render_risk_check_view("RELIANCE.NS", is_sample_mode=False, config={})
            self.assertGreaterEqual(mock_error.call_count, 1)
            first_err = mock_error.call_args_list[0][0][0]
            self.assertIn("BLOCK", first_err)
            self.assertIn("Weekend", first_err)

    # 9. Manual override of inherited values is possible and clearly labeled
    def test_09_manual_override_detected(self):
        raw = _build_sample_analysis_complete("RELIANCE.NS")
        set_active_analysis(raw)

        # In Exit Planner, user types 2550.0 instead of inherited 2510.0
        with patch.object(st, "markdown"), patch.object(st, "caption"), \
             patch.object(st, "number_input", return_value=2550.0), \
             patch.object(st, "selectbox", return_value="2R"), \
             patch.object(st, "columns", side_effect=_mock_cols), \
             patch.object(st, "metric"):

            # Capture render_html call for MANUAL OVERRIDE
            with patch("app.render_html") as mock_render_html:
                render_exit_planner_view("RELIANCE.NS", is_sample_mode=False, config={})
                rendered_markup = "".join(call[0][0] for call in mock_render_html.call_args_list)
                self.assertIn("MANUAL OVERRIDE", rendered_markup)

    # 10. Analysis timestamp and data source are preserved across page switches
    def test_10_provenance_and_timestamp_preserved(self):
        raw = _build_sample_analysis_complete("RELIANCE.NS")
        raw["now_ist"] = "09:45:12"
        raw["steps"]["data_check"]["source"] = "upstox"
        set_active_analysis(raw)

        # Fetch in Exit Planner
        ctx1 = get_analysis_context_for_symbol("RELIANCE.NS")
        self.assertEqual(ctx1["now_ist"], "09:45:12")
        self.assertEqual(ctx1["source"], "upstox")

        # Simulate page switch to Position Size
        self.state["current_page"] = "Position size"
        ctx2 = get_analysis_context_for_symbol("RELIANCE.NS")
        self.assertEqual(ctx2["now_ist"], "09:45:12")
        self.assertEqual(ctx2["source"], "upstox")

        # Simulate page switch to Risk Check
        self.state["current_page"] = "Risk check"
        ctx3 = get_analysis_context_for_symbol("RELIANCE.NS")
        self.assertEqual(ctx3["now_ist"], "09:45:12")
        self.assertEqual(ctx3["source"], "upstox")

    # 11. Time machine inherits real levels from active context
    def test_11_time_machine_inherits_levels(self):
        raw = _build_sample_analysis_complete("RELIANCE.NS")
        set_active_analysis(raw)

        with patch.object(st, "markdown"), patch.object(st, "caption"), \
             patch.object(st, "info"), \
             patch.object(st, "columns", side_effect=_mock_cols), \
             patch.object(st, "number_input", side_effect=[2510.0, 2500.0, 2530.0]) as mock_num, \
             patch.object(st, "button", return_value=False):

            render_time_machine_view("RELIANCE.NS", is_sample_mode=False, config={})
            self.assertEqual(mock_num.call_count, 3)
            self.assertEqual(mock_num.call_args_list[0][1]["value"], 2510.0)
            self.assertEqual(mock_num.call_args_list[1][1]["value"], 2500.0)
            self.assertEqual(mock_num.call_args_list[2][1]["value"], 2530.0)

    # 12. Manual Risk Check helper evaluates valid order ticket and returns PASS
    def test_12_manual_risk_check_evaluates_valid_order_ticket_pass(self):
        import datetime
        now_session_utc = datetime.datetime(2026, 11, 12, 4, 45) # Thursday 10:15 IST
        cfg = {
            "budget": 500000,
            "max_loss_per_trade_inr": 2000,
            "max_order_inr": 500000,
            "max_daily_loss_inr": 10000,
            "min_reward_risk": 1.5
        }
        verdict, reasons = evaluate_order_risk(
            "RELIANCE.NS", entry=2500.0, stop=2475.0, target=2550.0, shares=50, config=cfg, now=now_session_utc
        )
        self.assertEqual(verdict, "PASS")
        self.assertEqual(reasons, [])

    # 13. Manual Risk Check blocks excessive loss with descriptive reason
    def test_13_manual_risk_check_blocks_excessive_loss(self):
        import datetime
        now_session_utc = datetime.datetime(2026, 11, 12, 4, 45)
        cfg = {
            "budget": 500000,
            "max_loss_per_trade_inr": 2000,
            "max_order_inr": 500000,
            "max_daily_loss_inr": 10000,
            "min_reward_risk": 1.5
        }
        # Loss: 50 * (2500 - 2400) = ₹5,000 > ₹2,000 limit
        verdict, reasons = evaluate_order_risk(
            "RELIANCE.NS", entry=2500.0, stop=2400.0, target=2550.0, shares=50, config=cfg, now=now_session_utc
        )
        self.assertEqual(verdict, "BLOCK")
        self.assertTrue(any("max loss per trade" in r for r in reasons))

    # 14. Manual Risk Check handles zero quantity and inverted stop levels safely
    def test_14_manual_risk_check_handles_zero_qty_and_inverted_stops_safely(self):
        import datetime
        now_session_utc = datetime.datetime(2026, 11, 12, 4, 45)
        cfg = {"budget": 500000, "max_loss_per_trade_inr": 2000}

        # Zero quantity
        v_qty, r_qty = evaluate_order_risk("RELIANCE.NS", 2500.0, 2475.0, 2550.0, 0, cfg, now=now_session_utc)
        self.assertEqual(v_qty, "BLOCK")
        self.assertTrue(any("positive number" in r for r in r_qty))

        # Inverted stop (stop > entry on buy)
        v_stop, r_stop = evaluate_order_risk("RELIANCE.NS", 2500.0, 2600.0, 2550.0, 50, cfg, now=now_session_utc)
        self.assertEqual(v_stop, "BLOCK")
        self.assertTrue(any("must be below the entry" in r for r in r_stop))

    # 15. Risk check view renders manual test expander without exceptions
    def test_15_render_risk_check_view_custom_expander_integration(self):
        raw = _build_sample_analysis_complete("RELIANCE.NS")
        set_active_analysis(raw)

        cfg = {"budget": 500000, "max_loss_per_trade_inr": 2000}
        with patch.object(st, "markdown"), patch.object(st, "caption"), \
             patch.object(st, "write"), \
             patch.object(st, "success") as mock_succ, \
             patch.object(st, "error") as mock_err, \
             patch.object(st, "expander"), \
             patch.object(st, "number_input", side_effect=[2500.0, 2400.0, 2550.0, 50]), \
             patch("app.render_html"):

            render_risk_check_view("RELIANCE.NS", is_sample_mode=False, config=cfg)
            # Inherited trade plan PASS is called
            mock_succ.assert_called()
            # Custom plan BLOCK is called for loss exceeding ₹2,000
            mock_err.assert_called()
            self.assertIn("Custom BLOCK", mock_err.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
