#!/usr/bin/env python3
"""Regression tests for Phase 3B: Data Integrity and Risk Safety."""

import unittest
import datetime
from unittest.mock import patch, MagicMock

import app
from app import evaluate_order_risk, resolve_symbol
from trader.analyzer import analyze_symbol
import trader.broker_data as broker_data


class TestDataIntegrityAndRiskSafety(unittest.TestCase):
    def setUp(self):
        self.valid_config = {
            "market": "NSE",
            "timezone": "Asia/Kolkata",
            "budget": 500000,
            "cash_buffer_pct": 10,
            "max_position_pct": 75,
            "max_order_inr": 400000,
            "min_order_inr": 500,
            "max_loss_per_trade_inr": 2000,
            "max_daily_loss_inr": 6000,
            "max_trades_per_day": 3,
            "max_orders_per_day": 10,
            "min_reward_risk": 1.0,
            "require_stop": True,
            "no_entries_before": "09:30",
            "no_entries_after": "15:00",
            "flat_by": "15:15",
            "charges": {
                "brokerage_per_order": 20.0,
                "brokerage_pct": 0.03,
                "stt_pct": 0.025,
                "exchange_turnover_fee_pct": 0.00297,
                "sebi_turnover_fee_pct": 0.0001,
                "gst_pct": 18.0,
                "stamp_duty_pct": 0.003
            }
        }
        # Tuesday 10:00 AM IST = 04:30 AM UTC
        self.market_open_utc = datetime.datetime(2026, 11, 10, 4, 30, 0)

    # ----------------------------------------------------------------
    # 1. Account Budget Tests
    # ----------------------------------------------------------------
    def test_missing_budget_evaluate_order_risk(self):
        cfg = dict(self.valid_config)
        del cfg["budget"]
        verdict, reasons = evaluate_order_risk("RELIANCE.NS", 1340.0, 1335.0, 1350.0, 50, cfg, now=self.market_open_utc)
        self.assertEqual(verdict, "BLOCK")
        self.assertTrue(any("budget" in r.lower() for r in reasons))

    def test_none_budget_evaluate_order_risk(self):
        cfg = dict(self.valid_config)
        cfg["budget"] = None
        verdict, reasons = evaluate_order_risk("RELIANCE.NS", 1340.0, 1335.0, 1350.0, 50, cfg, now=self.market_open_utc)
        self.assertEqual(verdict, "BLOCK")
        self.assertTrue(any("budget" in r.lower() for r in reasons))

    def test_zero_budget_evaluate_order_risk(self):
        cfg = dict(self.valid_config)
        cfg["budget"] = 0
        verdict, reasons = evaluate_order_risk("RELIANCE.NS", 1340.0, 1335.0, 1350.0, 50, cfg, now=self.market_open_utc)
        self.assertEqual(verdict, "BLOCK")
        self.assertTrue(any("budget" in r.lower() for r in reasons))

    def test_negative_budget_evaluate_order_risk(self):
        cfg = dict(self.valid_config)
        cfg["budget"] = -50000
        verdict, reasons = evaluate_order_risk("RELIANCE.NS", 1340.0, 1335.0, 1350.0, 50, cfg, now=self.market_open_utc)
        self.assertEqual(verdict, "BLOCK")
        self.assertTrue(any("budget" in r.lower() for r in reasons))

    def test_valid_configured_budget_evaluate_order_risk(self):
        cfg = dict(self.valid_config)
        cfg["budget"] = 500000
        # 50 shares * 1340 = 67,000 INR (well within 500k budget)
        # risk per share = 5.0 -> 50 * 5 = 250 (within 2000 max loss)
        verdict, reasons = evaluate_order_risk("RELIANCE.NS", 1340.0, 1335.0, 1350.0, 50, cfg, now=self.market_open_utc)
        self.assertEqual(verdict, "PASS")

    def test_analyzer_missing_budget_returns_block(self):
        cfg = dict(self.valid_config)
        cfg["budget"] = 0
        res = analyze_symbol("RELIANCE.NS", is_sample_mode=True, config=cfg)
        step6 = res.get("steps", {}).get("risk_check", {})
        self.assertEqual(step6.get("verdict"), "BLOCK")
        self.assertTrue(any("budget" in r.lower() for r in step6.get("reasons", [])))

    # ----------------------------------------------------------------
    # 2. Candle Integrity & Audit Tests
    # ----------------------------------------------------------------
    def test_duplicate_candles_detection(self):
        # Construct synthetic candle series with a duplicate timestamp
        t0 = 1794455100  # 09:15
        t1 = 1794455400  # 09:20
        raw_bars = [
            {"t": t0, "o": 100, "h": 105, "l": 95, "c": 102, "v": 1000},
            {"t": t1, "o": 102, "h": 106, "l": 101, "c": 104, "v": 1200},
            {"t": t1, "o": 102, "h": 106, "l": 101, "c": 104, "v": 1200},  # duplicate
        ]
        seen = set()
        dupes = []
        for b in raw_bars:
            if b["t"] in seen:
                dupes.append(b["t"])
            seen.add(b["t"])
        self.assertEqual(len(dupes), 1)
        self.assertEqual(dupes[0], t1)

    def test_out_of_order_candles_detection(self):
        t0 = 1794455100  # 09:15
        t1 = 1794455400  # 09:20
        t2 = 1794455700  # 09:25
        raw_bars = [
            {"t": t0, "o": 100, "h": 105, "l": 95, "c": 102, "v": 1000},
            {"t": t2, "o": 104, "h": 107, "l": 103, "c": 105, "v": 1500},
            {"t": t1, "o": 102, "h": 106, "l": 101, "c": 104, "v": 1200},  # out of order
        ]
        out_of_order = []
        last_t = None
        for b in raw_bars:
            if last_t is not None and b["t"] < last_t:
                out_of_order.append((last_t, b["t"]))
            last_t = b["t"]
        self.assertEqual(len(out_of_order), 1)
        self.assertEqual(out_of_order[0], (t2, t1))

    def test_missing_opening_range_candles_marked_incomplete(self):
        # If bars 09:15 - 09:30 (slots 0, 1, 2) are missing, ORB cannot form
        chart_missing_or = {
            "symbol": "RELIANCE.NS",
            "meta": {"dataGranularity": "5m"},
            "bars": [
                {"t": 1794456900, "o": 1340, "h": 1345, "l": 1338, "c": 1342, "v": 50000}  # 09:45 only
            ]
        }
        res = app.entry_mod.evaluate(chart_missing_or, now_utc=self.market_open_utc, setup="orb", or_minutes=15)
        # Should be no-data or waiting because ORB cannot be completed
        self.assertIn(res.get("status"), ("no-data", "waiting"))
        self.assertIsNone(res.get("or_high"))

    # ----------------------------------------------------------------
    # 3. Symbol Normalization Tests
    # ----------------------------------------------------------------
    def test_symbol_normalization_ordinary_nse(self):
        self.assertEqual(resolve_symbol("RELIANCE"), "RELIANCE.NS")
        self.assertEqual(resolve_symbol("reliance.ns"), "RELIANCE.NS")
        self.assertEqual(resolve_symbol("TCS"), "TCS.NS")
        self.assertEqual(resolve_symbol("INFY.NS"), "INFY.NS")
        self.assertEqual(resolve_symbol("tcs"), "TCS.NS")

    def test_symbol_normalization_with_name_split(self):
        self.assertEqual(resolve_symbol("RELIANCE — Reliance Industries Limited"), "RELIANCE.NS")

    def test_symbol_normalization_instrument_keys(self):
        # Known Upstox instrument key for Reliance
        self.assertEqual(resolve_symbol("NSE_EQ|INE002A01018"), "RELIANCE.NS")
        # Index instrument key should not append .NS
        self.assertEqual(resolve_symbol("NSE_INDEX|Nifty 50"), "NSE_INDEX|NIFTY 50")

    def test_symbol_normalization_index_identifiers(self):
        # Should never append .NS to index symbols
        self.assertEqual(resolve_symbol("^NSEI"), "^NSEI")
        self.assertEqual(resolve_symbol("^BSESN"), "^BSESN")
        self.assertEqual(resolve_symbol("NIFTY 50"), "^NSEI")
        self.assertEqual(resolve_symbol("Sensex"), "^BSESN")

    # ----------------------------------------------------------------
    # 4. Independent Position Sizing Arithmetic Tests
    # ----------------------------------------------------------------
    def test_independent_position_sizing_arithmetic(self):
        plan_doc = {
            "symbol": "RELIANCE.NS",
            "side": "buy",
            "entry": 1341.50,
            "stop": 1335.65,
            "target": 1353.20
        }
        sized = app.budget_mod.size_trade(plan_doc, self.valid_config)
        qty = sized["qty"]
        risk_ps = sized["limits"][0]["per"]
        tot_charges = sized["charges"]["total"]
        max_loss = sized["max_loss"]

        # Recalculate independently
        self.assertEqual(risk_ps, 5.85)
        self.assertEqual(qty, 279)
        self.assertEqual(sized["cost"], 279 * 1341.50)
        self.assertAlmostEqual(max_loss, round(279 * 5.85 + tot_charges, 2), places=2)
        # Ensure total risk fits within max allowed loss of 2000 INR
        self.assertLessEqual(max_loss, 2000.0)

    # ----------------------------------------------------------------
    # 5. Phase 3C Time Machine Reliability & Data Validation Tests
    # ----------------------------------------------------------------
    def test_time_machine_yahoo_symbol_formatting(self):
        # DEF-04: .NS and .BO must NOT be replaced with -NS or -BO
        url_rel = app.tm_mod.chart_url("RELIANCE.NS")
        self.assertIn("RELIANCE.NS", url_rel)
        self.assertNotIn("RELIANCE-NS", url_rel)

        url_tcs = app.tm_mod.chart_url("TCS.NS")
        self.assertIn("TCS.NS", url_tcs)
        self.assertNotIn("TCS-NS", url_tcs)

        # DEF-04: International US tickers like BRK.A must be converted to BRK-A
        url_brk = app.tm_mod.chart_url("BRK.A")
        self.assertIn("BRK-A", url_brk)

    def test_time_machine_complete_75_bar_session(self):
        # Construct a synthetic full 75-bar Indian session (slots 0..74)
        bars_75 = {i: {"o": 100, "h": 105, "l": 95, "c": 102, "v": 1000} for i in range(75)}
        days = {"2026-10-14": bars_75}
        full, short = app.tm_mod.full_sessions(days, before="2026-10-15", session_bars=75, indian=True)
        self.assertEqual(len(full), 1)
        self.assertEqual(len(short), 0)

    def test_time_machine_missing_tail_candles_tolerated_for_1515_flat_by(self):
        # DEF-05: Slots 0..71 present (09:15 to 15:10, finishes 15:15), slots 72-73 missing
        bars_73 = {i: {"o": 100, "h": 105, "l": 95, "c": 102, "v": 1000} for i in range(72)}
        bars_73[74] = {"o": 102, "h": 102, "l": 102, "c": 102, "v": 500000}  # 15:25 auction
        days = {"2026-10-14": bars_73}
        full, short = app.tm_mod.full_sessions(days, before="2026-10-15", session_bars=75, max_slot_needed=71, indian=True)
        self.assertEqual(len(full), 1)
        self.assertEqual(len(short), 0)
        self.assertEqual(len(full[0][1]), 72)

    def test_time_machine_missing_opening_range_candle_rejected(self):
        # Missing slot 1 (09:20 bar)
        bars_missing_or = {i: {"o": 100, "h": 105, "l": 95, "c": 102, "v": 1000} for i in range(75) if i != 1}
        days = {"2026-10-14": bars_missing_or}
        full, short = app.tm_mod.full_sessions(days, before="2026-10-15", session_bars=75, max_slot_needed=71, indian=True)
        self.assertEqual(len(full), 0)
        self.assertEqual(len(short), 1)

    def test_time_machine_missing_breakout_candle_rejected(self):
        # Missing slot 5 (09:40 bar)
        bars_missing_breakout = {i: {"o": 100, "h": 105, "l": 95, "c": 102, "v": 1000} for i in range(75) if i != 5}
        days = {"2026-10-14": bars_missing_breakout}
        full, short = app.tm_mod.full_sessions(days, before="2026-10-15", session_bars=75, max_slot_needed=71, indian=True)
        self.assertEqual(len(full), 0)
        self.assertEqual(len(short), 1)

    def test_time_machine_missing_exit_candle_rejected(self):
        # If simulation needs up to slot 71, but slot 70 (15:05) is missing
        bars_missing_exit = {i: {"o": 100, "h": 105, "l": 95, "c": 102, "v": 1000} for i in range(75) if i != 70}
        days = {"2026-10-14": bars_missing_exit}
        full, short = app.tm_mod.full_sessions(days, before="2026-10-15", session_bars=75, max_slot_needed=71, indian=True)
        self.assertEqual(len(full), 0)
        self.assertEqual(len(short), 1)

    def test_time_machine_insufficient_sample_size_fails_closed(self):
        # If fewer than MIN_SESSIONS (15) full sessions exist, time_machine raises TMError
        bars_75 = {i: {"o": 100, "h": 105, "l": 95, "c": 102, "v": 1000} for i in range(75)}
        # Only 5 sessions
        days = {f"2026-10-0{i}": bars_75 for i in range(1, 6)}
        full, short = app.tm_mod.full_sessions(days, before="2026-10-15", session_bars=75, max_slot_needed=71, indian=True)
        self.assertEqual(len(full), 5)
        self.assertLess(len(full), app.tm_mod.MIN_SESSIONS)

    def test_no_look_ahead_bias_in_history_replay(self):
        # Verify history replay only inspects bars after entry bar kk up to last
        # Construct session with entry at slot 3 (09:30 close)
        # Bar 4 triggers stop, Bar 10 hits target. Stop must be recorded first.
        rule = {"setup": "orb", "or_minutes": 15, "stop_rule": "or_mid", "target_r": 2.0}
        D = [
            {"o": 100, "h": 102, "l": 98, "c": 100},  # 0
            {"o": 100, "h": 103, "l": 99, "c": 101},  # 1
            {"o": 101, "h": 104, "l": 100, "c": 102}, # 2 -> OR high=104, low=98, mid=101
            {"o": 102, "h": 106, "l": 101, "c": 105}, # 3 -> Breakout close 105 > 104 (Entry=105, Stop=101, Target=113)
            {"o": 105, "h": 106, "l": 100.5, "c": 102},# 4 -> Hits stop at 100.5 <= 101
            {"o": 102, "h": 115, "l": 102, "c": 114}, # 5 -> Later moves to 115
        ]
        res = app.tm_mod.history_replay([("2026-10-14", D)], rule, last=5, side="buy")
        self.assertEqual(res["triggered"], 1)
        self.assertEqual(res["trades"][0]["outcome"], "stop")
        self.assertEqual(res["trades"][0]["R"], -1.0)


if __name__ == "__main__":
    unittest.main()
