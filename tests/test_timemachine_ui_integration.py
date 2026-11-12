#!/usr/bin/env python3
"""Comprehensive test suite for Phase 3C: Time Machine Streamlit UI Integration.

Verifies:
1. Correct time_machine() invocation signature (no TypeError).
2. Correct result parsing and historical provenance display.
3. Correct accepted and rejected session counts.
4. Correct outcome counts (target, stop, time exits).
5. Insufficient-data handling on provider failure.
6. Insufficient-data handling on session shortage (< MIN_SESSIONS).
7. Execution assumptions disclosure note exact wording.
8. Invalid price levels validation (stop < entry < target).
9. Symbol switching without state bleed.
10. No fabricated probabilities on zero triggers.
"""

import gzip
import unittest
from unittest.mock import patch, MagicMock
import datetime

import streamlit as st
import app
from app import render_time_machine_view, set_active_analysis


def _mock_columns(n):
    cols = []
    for _ in range(n):
        col = MagicMock()
        col.metric = st.metric
        cols.append(col)
    return cols


class TestTimeMachineUIIntegration(unittest.TestCase):
    def setUp(self):
        st.session_state.clear()
        self.tsla_fixture_path = "tests/fixtures/trader-timemachine/TSLA-60d5m.json.gz"
        with gzip.open(self.tsla_fixture_path, "rt", encoding="utf-8") as fh:
            self.tsla_chart_text = fh.read()

        self.mock_config = {
            "flat_by": "15:15",
            "timezone": "Asia/Kolkata",
            "market": "NSE"
        }

    # -------------------------------------------------------------------------
    # 1. Correct Invocation & No TypeError
    # -------------------------------------------------------------------------
    def test_01_time_machine_invocation_signature_and_no_typeerror(self):
        """Verify time_machine is invoked with valid arguments and does not raise TypeError."""
        with patch("app.fetch_cached_60d_history", return_value=self.tsla_chart_text), \
             patch.object(st, "button", return_value=True), \
             patch.object(st, "spinner"), \
             patch.object(st, "markdown"), \
             patch.object(st, "caption"), \
             patch.object(st, "metric"), \
             patch.object(st, "info"), \
             patch.object(st, "warning") as mock_warn, \
             patch.object(st, "error") as mock_err, \
             patch.object(st, "expander"), \
             patch.object(st, "write"), \
             patch.object(st, "columns", side_effect=_mock_columns):

            # Call render_time_machine_view for TSLA
            render_time_machine_view("TSLA", is_sample_mode=False, config=self.mock_config)

            # No errors or warnings should be reported
            mock_err.assert_not_called()
            # If warning was called, assert it wasn't a TypeError
            for call in mock_warn.call_args_list:
                self.assertNotIn("TypeError", str(call))
                self.assertNotIn("missing 1 required positional argument", str(call))

    # -------------------------------------------------------------------------
    # 2. Result Parsing & Metric Display
    # -------------------------------------------------------------------------
    def test_02_result_parsing_and_metrics_display(self):
        """Verify that historical and Monte Carlo metrics are extracted and displayed correctly."""
        mock_res = {
            "created_by": "trader-timemachine",
            "source": "yahoo",
            "data": {
                "symbol": "TSLA",
                "interval": "5m",
                "range": "60d",
                "short_sessions_left_out": [{"date": "2026-08-15", "bars": 30}]
            },
            "plan": {
                "symbol": "TSLA",
                "side": "buy",
                "entry": 364.18,
                "stop": 358.32,
                "target": 370.04
            },
            "history": {
                "sessions": 59,
                "triggered": 33,
                "target": 13,
                "stop": 15,
                "close": 5,
                "avg_R": 0.12
            },
            "mc": {
                "replays": 1000,
                "pool_sessions": 59,
                "pool_from": "2026-07-27",
                "pool_to": "2026-10-16",
                "p_target": 0.372,
                "p_stop": 0.280,
                "p_neither": 0.348,
                "avg_R": 0.15
            },
            "verdict": "Leans toward the target: target first 37.2 % vs stop first 28.0 %."
        }

        metric_records = {}

        def record_metric(label, value, *args, **kwargs):
            metric_records[label] = str(value)

        with patch("app.fetch_cached_60d_history", return_value=self.tsla_chart_text), \
             patch("app.tm_mod.time_machine", return_value=mock_res), \
             patch.object(st, "button", return_value=True), \
             patch.object(st, "spinner"), \
             patch.object(st, "markdown"), \
             patch.object(st, "caption"), \
             patch.object(st, "info"), \
             patch.object(st, "metric", side_effect=record_metric), \
             patch.object(st, "expander"), \
             patch.object(st, "columns", side_effect=_mock_columns):

            render_time_machine_view("TSLA", is_sample_mode=False, config=self.mock_config)

            # Check provenance metrics
            self.assertEqual(metric_records.get("Symbol"), "TSLA")
            self.assertEqual(metric_records.get("Data Provider"), "YAHOO")
            self.assertEqual(metric_records.get("Window"), "2026-07-27 → 2026-10-16")
            self.assertEqual(metric_records.get("Total Sessions"), "60")  # 59 accepted + 1 rejected
            self.assertEqual(metric_records.get("Accepted Sessions"), "59")
            self.assertEqual(metric_records.get("Rejected Sessions"), "1")

            # Check trade outcome metrics
            self.assertEqual(metric_records.get("Valid Breakout Setups"), "33/59 (55.9%)")
            self.assertEqual(metric_records.get("Historical Win Rate"), "13/33 (39.4%)")
            self.assertEqual(metric_records.get("Target Exits"), "13")
            self.assertEqual(metric_records.get("Stop Exits"), "15")
            self.assertEqual(metric_records.get("Time-Based Exits"), "5")
            self.assertEqual(metric_records.get("Historical Avg R"), "+0.12R")

            # Check Monte Carlo probabilities
            self.assertEqual(metric_records.get("Simulated Target Hit"), "37.2%")
            self.assertEqual(metric_records.get("Simulated Stop Hit"), "28.0%")
            self.assertEqual(metric_records.get("Simulated Time Exit"), "34.8%")
            self.assertEqual(metric_records.get("Simulated Avg R"), "+0.15R")

    # -------------------------------------------------------------------------
    # 3. Provider Failure Handling
    # -------------------------------------------------------------------------
    def test_03_insufficient_data_provider_failure(self):
        """When chart fetch fails, display 'Insufficient historical data' and hide probabilities."""
        with patch("app.fetch_cached_60d_history", side_effect=Exception("HTTP 404: Not Found")), \
             patch.object(st, "button", return_value=True), \
             patch.object(st, "spinner"), \
             patch.object(st, "warning") as mock_warn, \
             patch.object(st, "metric") as mock_metric:

            render_time_machine_view("UNKNOWN.NS", is_sample_mode=False, config=self.mock_config)

            mock_warn.assert_called()
            warn_msg = str(mock_warn.call_args[0][0])
            self.assertIn("Insufficient historical data", warn_msg)
            self.assertIn("HTTP 404", warn_msg)
            # Probabilities must NOT be rendered
            mock_metric.assert_not_called()

    # -------------------------------------------------------------------------
    # 4. Few Historical Sessions (< MIN_SESSIONS)
    # -------------------------------------------------------------------------
    def test_04_insufficient_data_few_sessions(self):
        """When fewer than minimum valid sessions exist, report insufficient data."""
        with patch("app.fetch_cached_60d_history", return_value=self.tsla_chart_text), \
             patch("app.tm_mod.time_machine", side_effect=app.tm_mod.TMError("only 5 full session(s) of RELIANCE.NS before 2026-11-12 in the data; the time machine needs at least 20")), \
             patch.object(st, "button", return_value=True), \
             patch.object(st, "spinner"), \
             patch.object(st, "warning") as mock_warn, \
             patch.object(st, "metric") as mock_metric:

            render_time_machine_view("RELIANCE.NS", is_sample_mode=False, config=self.mock_config)

            mock_warn.assert_called()
            warn_msg = str(mock_warn.call_args[0][0])
            self.assertIn("Insufficient historical data", warn_msg)
            self.assertIn("only 5 full session(s)", warn_msg)
            mock_metric.assert_not_called()

    # -------------------------------------------------------------------------
    # 5. Invalid Levels Guard (Buy stop < entry < target)
    # -------------------------------------------------------------------------
    def test_05_invalid_levels_guard(self):
        """Invalid buy levels (stop >= entry) trigger st.error without making provider calls."""
        with patch.object(st, "button", return_value=True), \
             patch.object(st, "number_input", side_effect=[2500.0, 2550.0, 2600.0]), \
             patch("app.fetch_cached_60d_history") as mock_fetch, \
             patch.object(st, "error") as mock_err:

            render_time_machine_view("RELIANCE.NS", is_sample_mode=False, config=self.mock_config)

            mock_err.assert_called_once()
            self.assertIn("Invalid levels for buy trade", str(mock_err.call_args[0][0]))
            mock_fetch.assert_not_called()

    # -------------------------------------------------------------------------
    # 6. Execution Assumptions Disclosure Note
    # -------------------------------------------------------------------------
    def test_06_execution_assumptions_methodology_note(self):
        """Disclose the exact approved simulation methodology note."""
        expected_note = (
            "Historical simulations assume entry at the confirmed breakout candle close, "
            "zero slippage unless configured otherwise, and stop-first execution when both "
            "stop and target are touched within the same candle. Time-based exits use historical "
            "candle prices and are not guaranteed executable fills."
        )

        caption_calls = []

        def record_caption(text):
            caption_calls.append(str(text))

        with patch("app.fetch_cached_60d_history", return_value=self.tsla_chart_text), \
             patch.object(st, "button", return_value=True), \
             patch.object(st, "spinner"), \
             patch.object(st, "caption", side_effect=record_caption), \
             patch.object(st, "markdown"), \
             patch.object(st, "info"), \
             patch.object(st, "metric"), \
             patch.object(st, "expander"), \
             patch.object(st, "columns", side_effect=_mock_columns):

            render_time_machine_view("TSLA", is_sample_mode=False, config=self.mock_config)

            self.assertTrue(
                any(expected_note in call for call in caption_calls),
                f"Expected methodology note not found in caption calls: {caption_calls}"
            )

    # -------------------------------------------------------------------------
    # 7. Symbol Switching & Analysis Context
    # -------------------------------------------------------------------------
    def test_07_symbol_switching_and_analysis_context(self):
        """Active analysis context is inherited only for the matching symbol."""
        # Set context for RELIANCE.NS
        curr_analysis = {
            "symbol": "RELIANCE.NS",
            "now_ist": "10:15:00",
            "now_date": "2026-11-12",
            "steps": {
                "data_check": {"source": "upstox"},
                "levels": {
                    "entry_level": 1341.50,
                    "stop_level": 1337.50,
                    "target_level": 1349.50
                }
            }
        }
        set_active_analysis(curr_analysis)

        with patch("app.render_html") as mock_html, \
             patch.object(st, "number_input") as mock_input, \
             patch.object(st, "button", return_value=False):

            # Render for RELIANCE.NS -> should inherit levels
            render_time_machine_view("RELIANCE.NS", is_sample_mode=False, config=self.mock_config)
            html_call_reliance = str(mock_html.call_args[0][0])
            self.assertIn("INHERITED FROM ANALYSIS", html_call_reliance)
            self.assertIn("1341.5", str(mock_input.call_args_list))

            mock_html.reset_mock()
            mock_input.reset_mock()

            # Switch symbol to INFY.NS -> should NOT inherit RELIANCE levels
            render_time_machine_view("INFY.NS", is_sample_mode=False, config=self.mock_config)
            html_call_infy = str(mock_html.call_args[0][0])
            self.assertIn("Simulating with reference parameters", html_call_infy)
            self.assertNotIn("INHERITED FROM ANALYSIS", html_call_infy)

    # -------------------------------------------------------------------------
    # 8. No Fabricated Probabilities on Zero Triggers
    # -------------------------------------------------------------------------
    def test_08_no_fabricated_probabilities_on_zero_triggers(self):
        """When 0 breakout setups triggered historically, display 'No setups' rather than zero division."""
        mock_zero_res = {
            "source": "yahoo",
            "data": {"symbol": "CHOPPY.NS", "short_sessions_left_out": []},
            "plan": {"symbol": "CHOPPY.NS", "entry": 100.0, "stop": 95.0, "target": 110.0},
            "history": {
                "sessions": 50,
                "triggered": 0,
                "target": 0,
                "stop": 0,
                "close": 0,
                "avg_R": None
            },
            "mc": {
                "replays": 1000,
                "pool_sessions": 50,
                "pool_from": "2026-08-01",
                "pool_to": "2026-10-15",
                "p_target": 0.10,
                "p_stop": 0.20,
                "p_neither": 0.70,
                "avg_R": -0.05
            },
            "verdict": "No historical breakout setups occurred."
        }

        metric_records = {}

        def record_metric(label, value, *args, **kwargs):
            metric_records[label] = str(value)

        with patch("app.fetch_cached_60d_history", return_value=self.tsla_chart_text), \
             patch("app.tm_mod.time_machine", return_value=mock_zero_res), \
             patch.object(st, "button", return_value=True), \
             patch.object(st, "spinner"), \
             patch.object(st, "metric", side_effect=record_metric), \
             patch.object(st, "columns", side_effect=_mock_columns):

            render_time_machine_view("CHOPPY.NS", is_sample_mode=False, config=self.mock_config)

            self.assertEqual(metric_records.get("Valid Breakout Setups"), "0/50 (0.0%)")
            self.assertEqual(metric_records.get("Historical Win Rate"), "No setups")
            self.assertEqual(metric_records.get("Historical Avg R"), "Not available")


if __name__ == "__main__":
    unittest.main()
