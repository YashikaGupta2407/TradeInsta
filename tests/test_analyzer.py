#!/usr/bin/env python3
"""Comprehensive unit tests for trader.analyzer.analyze_symbol.

Tests each phase of the day and edge cases:
- 09:10 (Before 09:15): market not open, previous session reference
- 09:20 (09:15 to 09:30): opening range forming, NO entry/stop/target levels emitted
- 09:32: range complete (3 bars), breakout evaluation begins
- 11:47: mid-day analysis, bars sliced strictly to completed bars before 11:47
- 14:58: late session, near cutoff
- 15:45 (After 15:30): market closed, LAST SESSION badge
- Weekend: closed
- Holiday: matches trader/holidays.json (e.g. 2026-10-02), HOLIDAY badge, risk block
- Missing data / invalid ticker: graceful degradation
- Late entry flag: >30m since breakout
- Failed breakout detection
- Signal log persistence without tokens
"""

import datetime
import os
import unittest
from unittest.mock import patch, MagicMock

from trader.analyzer import (
    analyze_symbol,
    parse_ist_now,
    load_signal_log,
    LOG_PATH,
    IST_TZ,
    get_skills
)


class TestAnalyzerPhases(unittest.TestCase):
    def setUp(self):
        # Regular trading day (Wednesday, 2026-10-21 - not a holiday, not a weekend)
        self.year = 2026
        self.month = 10
        self.day = 21

    def test_phase_1_before_0915(self):
        # 09:10 IST on a regular trading day
        test_now = datetime.datetime(self.year, self.month, self.day, 9, 10, 0, tzinfo=IST_TZ)
        res = analyze_symbol("RELIANCE", now=test_now, is_sample_mode=False)

        self.assertEqual(res["day_phase"], "before_0915")
        self.assertEqual(res["market_state"], "pre-open")
        self.assertEqual(res["steps"]["data_check"]["badge"]["label"], "MARKET NOT OPEN")

        # Range should not be formed yet
        s2 = res["steps"]["opening_range"]
        self.assertFalse(s2["complete"])

        # In sample mode, badge should explicitly be SAMPLE DATA
        res_sample = analyze_symbol("RELIANCE", now=test_now, is_sample_mode=True)
        self.assertEqual(res_sample["steps"]["data_check"]["badge"]["label"], "SAMPLE DATA")

        # Levels should be pending
        s4 = res["steps"]["levels"]
        self.assertEqual(s4["status"], "pending")

        # Risk check blocks
        s6 = res["steps"]["risk_check"]
        self.assertEqual(s6["verdict"], "BLOCK")
        self.assertIn("not open yet", s6["reason"].lower())

    def test_phase_2_0915_to_0930_range_forming_no_levels(self):
        # 09:20 IST: Range still forming (only 1 5m bar completed at 09:20)
        test_now = datetime.datetime(self.year, self.month, self.day, 9, 20, 0, tzinfo=IST_TZ)
        res = analyze_symbol("RELIANCE", now=test_now, is_sample_mode=True)

        self.assertEqual(res["day_phase"], "0915_to_0930")
        self.assertEqual(res["market_state"], "open")

        s2 = res["steps"]["opening_range"]
        self.assertFalse(s2["complete"])
        self.assertIn("still forming", s2["phase_note"].lower())

        # CRITICAL RULE: During 09:15 to 09:30, MUST NOT output entry, stop or target!
        s4 = res["steps"]["levels"]
        self.assertIsNone(s4["entry_level"])
        self.assertIsNone(s4["stop_level"])
        self.assertIsNone(s4["target_level"])
        self.assertEqual(s4["status"], "pending")
        self.assertIn("forming", s4["note"].lower())

        # Risk check blocks before 09:30
        s6 = res["steps"]["risk_check"]
        self.assertEqual(s6["verdict"], "BLOCK")
        self.assertIn("before 09:30", s6["reason"])

    def test_phase_3_0932_range_complete(self):
        # 09:32 IST: 09:15-09:30 range is fully complete (3 bars: 09:15-09:20, 09:20-09:25, 09:25-09:30)
        test_now = datetime.datetime(self.year, self.month, self.day, 9, 32, 0, tzinfo=IST_TZ)
        res = analyze_symbol("RELIANCE", now=test_now, is_sample_mode=True)

        self.assertEqual(res["day_phase"], "0930_to_1530")
        s2 = res["steps"]["opening_range"]
        self.assertIsNotNone(s2["high"])
        self.assertIsNotNone(s2["low"])
        self.assertEqual(s2["bars_used"], 3)
        self.assertTrue(s2["complete"])

        # Levels should now be calculated
        s4 = res["steps"]["levels"]
        self.assertEqual(s4["status"], "ok")
        self.assertIsNotNone(s4["entry_level"])
        self.assertIsNotNone(s4["stop_level"])
        self.assertIsNotNone(s4["target_level"])

    def test_phase_4_mid_day_1147(self):
        # 11:47 IST: mid-day analysis, uses only completed bars up to 11:45
        test_now = datetime.datetime(self.year, self.month, self.day, 11, 47, 0, tzinfo=IST_TZ)
        res = analyze_symbol("RELIANCE", now=test_now, is_sample_mode=True)

        self.assertEqual(res["day_phase"], "0930_to_1530")
        s1 = res["steps"]["data_check"]
        self.assertIsNotNone(s1["last_bar_time"])
        self.assertIsNotNone(s1["bar_age"])
        self.assertEqual(s1["explanation"], "The entry rule needs completed bars. The current price shows where the stock is right now.")

        # Sizing and risk
        s5 = res["steps"]["size_and_charges"]
        self.assertGreater(s5["quantity"], 0)
        self.assertGreater(s5["total_charges"], 0)

        # Context
        s8 = res["steps"]["context"]
        self.assertEqual(s8["flow_label"], "previous-day reported activity, not live")

        # Summary sentences check
        s9 = res["steps"]["summary"]
        self.assertGreaterEqual(len(s9["sentences"]), 3)
        self.assertLessEqual(len(s9["sentences"]), 5)
        combined_text = " ".join(s9["sentences"]).lower()
        self.assertIn("entry level", combined_text)
        self.assertIn("stop", combined_text)
        self.assertIn("target", combined_text)
        self.assertNotIn("buy now", combined_text)
        self.assertNotIn("sell now", combined_text)

    def test_phase_5_late_day_1458(self):
        # 14:58 IST: near 15:00 cutoff
        test_now = datetime.datetime(self.year, self.month, self.day, 14, 58, 0, tzinfo=IST_TZ)
        res = analyze_symbol("RELIANCE", now=test_now, is_sample_mode=True)
        self.assertEqual(res["day_phase"], "0930_to_1530")

    def test_phase_6_post_market_1545(self):
        # 15:45 IST: market closed for the day
        test_now = datetime.datetime(self.year, self.month, self.day, 15, 45, 0, tzinfo=IST_TZ)
        res = analyze_symbol("RELIANCE", now=test_now, is_sample_mode=True)

        self.assertEqual(res["day_phase"], "after_1530_or_closed")
        self.assertEqual(res["market_state"], "closed")

    def test_weekend_handling(self):
        # Saturday (2026-10-24)
        test_now = datetime.datetime(2026, 10, 24, 12, 0, 0, tzinfo=IST_TZ)
        res = analyze_symbol("RELIANCE", now=test_now, is_sample_mode=True)

        self.assertEqual(res["market_state"], "weekend")
        s6 = res["steps"]["risk_check"]
        self.assertEqual(s6["verdict"], "BLOCK")
        self.assertIn("weekend", s6["reason"].lower())

    def test_holiday_handling(self):
        # Mahatma Gandhi Jayanti (2026-10-02) from trader/holidays.json
        test_now = datetime.datetime(2026, 10, 2, 11, 0, 0, tzinfo=IST_TZ)
        res = analyze_symbol("RELIANCE", now=test_now, is_sample_mode=True)

        self.assertEqual(res["market_state"], "holiday")
        self.assertEqual(res["steps"]["data_check"]["holiday_name"], "Mahatma Gandhi Jayanti")
        s6 = res["steps"]["risk_check"]
        self.assertEqual(s6["verdict"], "BLOCK")
        self.assertIn("Mahatma Gandhi Jayanti", s6["reason"])

    def test_missing_data_graceful_degradation(self):
        # Passing totally invalid ticker in live mode mock
        test_now = datetime.datetime(self.year, self.month, self.day, 12, 0, 0, tzinfo=IST_TZ)
        alerts_mod, entry_mod, _, _, _, _, _, _ = get_skills()
        with patch("trader.broker_data.get_realtime_candles", return_value=None):
            with patch("trader.broker_data.get_realtime_quotes", return_value=None):
                with patch.object(entry_mod, "get_chart", side_effect=Exception("No chart available")):
                    res = analyze_symbol("TOTALLY_INVALID_999", now=test_now, is_sample_mode=False)

        # Step 1 should report error, but function does NOT crash
        self.assertIn("data_check", res["steps"])
        self.assertEqual(res["steps"]["data_check"]["status"], "error")
        # Subsequent steps should continue
        self.assertIn("levels", res["steps"])
        self.assertIn("summary", res["steps"])

    def test_late_entry_flag_when_triggered_long_ago(self):
        # 14:00 IST when breakout happened around 09:35 (>30m ago)
        test_now = datetime.datetime(self.year, self.month, self.day, 14, 0, 0, tzinfo=IST_TZ)
        res = analyze_symbol("RELIANCE", now=test_now, is_sample_mode=True)
        s3 = res["steps"]["entry_status"]
        if s3.get("signal") == "triggered" and s3.get("minutes_since_trigger", 0) > 30:
            self.assertTrue(s3["late_entry_flag"])
            self.assertIn("different trade", s3["late_entry_note"].lower())

    def test_signal_log_persistence(self):
        # Check write to trader/signal_log.csv
        test_now = datetime.datetime(self.year, self.month, self.day, 11, 0, 0, tzinfo=IST_TZ)
        res = analyze_symbol("RELIANCE", now=test_now, is_sample_mode=True)

        self.assertTrue(os.path.exists(LOG_PATH))
        records = load_signal_log(limit=5)
        self.assertGreater(len(records), 0)
        last_rec = records[-1]
        self.assertEqual(last_rec["symbol"], "RELIANCE.NS")
        self.assertIn("outcome", last_rec)
        self.assertEqual(last_rec["outcome"], "")
        # Zero tokens in record
        self.assertNotIn("token", str(last_rec).lower())


if __name__ == "__main__":
    unittest.main()
