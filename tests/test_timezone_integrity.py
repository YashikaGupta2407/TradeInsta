#!/usr/bin/env python3
"""Regression test suite for Timezone & Candle Integrity (ISS-01).

Guarantees:
1. Exact UTC-to-IST conversion without naive datetime local-offset corruption.
2. 5-minute candle timestamp semantics (timestamp = open time; close time = open + 300s).
3. Deterministic session testing:
   - 09:00 IST — Before market opening
   - 09:15 IST — Opening range begins (0 complete bars)
   - 09:25 IST — Opening range is forming (2 complete bars: 09:15, 09:20)
   - 09:30 IST — Opening range completes (3 complete bars: 09:15, 09:20, 09:25)
   - 11:55 IST — Mid-session analysis
   - 15:00 IST — Entry window boundary
   - 15:30 IST — Market close (75th bar completes)
   - 17:29 IST — After-market analysis (last bar is 15:25-15:30, NOT 11:55)
   - Weekend — Market closed
   - Holiday — Market closed (matches trader/holidays.json)
4. Consistent behavior across Upstox (ISO string) and Yahoo (epoch int).
"""

import datetime
import unittest
from unittest.mock import patch

from trader.analyzer import (
    analyze_symbol,
    parse_ist_now,
    IST_TZ,
    UTC_TZ,
    get_skills,
)
from trader.broker_data import format_bar_age
from app import compute_trust_strip_info


def generate_75_session_bars(session_date: datetime.date) -> list:
    """Generate 75 standard 5-minute candles for NSE regular session (09:15 to 15:30 IST)."""
    base_open_dt = datetime.datetime.combine(session_date, datetime.time(9, 15), tzinfo=IST_TZ)
    base_open_ts = int(base_open_dt.timestamp())
    step_sec = 300
    bars = []
    price = 2500.0
    for i in range(75):
        t = base_open_ts + i * step_sec
        bars.append({
            "t": t,
            "o": price + (i * 0.2),
            "h": price + (i * 0.2) + 2.0,
            "l": price + (i * 0.2) - 1.0,
            "c": price + (i * 0.2) + 1.0,
            "v": 50000 + (i * 100)
        })
    return bars


class TestTimezoneCandleIntegrity(unittest.TestCase):
    def setUp(self):
        # Wednesday, 2026-10-21 (standard trading day, non-holiday, non-weekend)
        self.test_date = datetime.date(2026, 10, 21)
        self.bars_75 = generate_75_session_bars(self.test_date)
        self.mock_chart = {
            "symbol": "RELIANCE.NS",
            "meta": {
                "symbol": "RELIANCE.NS",
                "dataGranularity": "5m",
                "source": "yahoo",
                "regularMarketTime": self.bars_75[-1]["t"]
            },
            "bars": self.bars_75
        }

    def _analyze_with_mock_chart(self, now_dt: datetime.datetime):
        """Helper to run analyze_symbol with the 75-bar session chart."""
        alerts_mod, entry_mod, exit_mod, budget_mod, risk_mod, tm_mod, flow_mod, news_mod = get_skills()
        with patch("trader.analyzer.get_broker_data") as mock_bd_getter:
            mock_bd = mock_bd_getter.return_value
            mock_bd.active_broker.return_value = None
            with patch.object(entry_mod, "get_chart", return_value=self.mock_chart):
                return analyze_symbol("RELIANCE.NS", now=now_dt, is_sample_mode=False)

    def test_01_before_market_0900(self):
        """09:00 IST — Before market open: 0 completed bars, range incomplete."""
        now = datetime.datetime.combine(self.test_date, datetime.time(9, 0), tzinfo=IST_TZ)
        res = self._analyze_with_mock_chart(now)

        self.assertEqual(res["day_phase"], "before_0915")
        self.assertEqual(res["market_state"], "pre-open")
        s2 = res["steps"]["opening_range"]
        self.assertFalse(s2["complete"])
        self.assertEqual(s2["bars_used"], 0)
        # Levels should be pending
        self.assertEqual(res["steps"]["levels"]["status"], "pending")
        # Risk check should block
        self.assertEqual(res["steps"]["risk_check"]["verdict"], "BLOCK")

    def test_02_market_opening_0915(self):
        """09:15 IST — First bar starts; 0 completed bars at exact open."""
        now = datetime.datetime.combine(self.test_date, datetime.time(9, 15), tzinfo=IST_TZ)
        res = self._analyze_with_mock_chart(now)

        self.assertEqual(res["day_phase"], "0915_to_0930")
        self.assertEqual(res["market_state"], "open")
        s2 = res["steps"]["opening_range"]
        self.assertFalse(s2["complete"])
        self.assertEqual(s2["bars_used"], 0)
        self.assertEqual(res["steps"]["levels"]["status"], "pending")

    def test_03_opening_range_forming_0925(self):
        """09:25 IST — Two 5m bars completed (09:15-09:20 and 09:20-09:25). Range still forming."""
        now = datetime.datetime.combine(self.test_date, datetime.time(9, 25), tzinfo=IST_TZ)
        res = self._analyze_with_mock_chart(now)

        self.assertEqual(res["day_phase"], "0915_to_0930")
        s2 = res["steps"]["opening_range"]
        self.assertFalse(s2["complete"])
        self.assertEqual(s2["bars_used"], 2)
        # No levels emitted while range is forming
        s4 = res["steps"]["levels"]
        self.assertIsNone(s4["entry_level"])
        self.assertEqual(s4["status"], "pending")

    def test_04_opening_range_completes_0930(self):
        """09:30 IST — Third bar (09:25-09:30) completes. Range is fully formed (3 bars)."""
        now = datetime.datetime.combine(self.test_date, datetime.time(9, 30), tzinfo=IST_TZ)
        res = self._analyze_with_mock_chart(now)

        self.assertEqual(res["day_phase"], "0930_to_1530")
        s2 = res["steps"]["opening_range"]
        self.assertTrue(s2["complete"])
        self.assertEqual(s2["bars_used"], 3)
        self.assertIsNotNone(s2["high"])
        self.assertIsNotNone(s2["low"])
        # Levels should be active
        self.assertEqual(res["steps"]["levels"]["status"], "ok")

    def test_05_mid_session_1155(self):
        """11:55 IST — Bar 11:50-11:55 has completed; bar 11:55-12:00 is incomplete."""
        now = datetime.datetime.combine(self.test_date, datetime.time(11, 55), tzinfo=IST_TZ)
        res = self._analyze_with_mock_chart(now)

        s1 = res["steps"]["data_check"]
        # Exactly 32 5m bars have completed from 09:15 to 11:55
        self.assertEqual(s1["last_bar_time"], "11:55:00 IST")
        self.assertEqual(s1["bar_age"], "just closed")

    def test_06_entry_window_boundary_1500(self):
        """15:00 IST — Entry window boundary (345 min = 69 completed bars)."""
        now = datetime.datetime.combine(self.test_date, datetime.time(15, 0), tzinfo=IST_TZ)
        res = self._analyze_with_mock_chart(now)

        s1 = res["steps"]["data_check"]
        self.assertEqual(s1["last_bar_time"], "15:00:00 IST")
        self.assertEqual(s1["bar_age"], "just closed")

    def test_07_market_close_1530(self):
        """15:30 IST — Market close. All 75 5-minute bars have completed."""
        now = datetime.datetime.combine(self.test_date, datetime.time(15, 30), tzinfo=IST_TZ)
        res = self._analyze_with_mock_chart(now)

        s1 = res["steps"]["data_check"]
        self.assertEqual(s1["last_bar_time"], "15:30:00 IST")
        self.assertEqual(s1["bar_age"], "just closed")

    def test_08_post_market_1729_eliminates_5_5_hour_shift(self):
        """17:29 IST — CRITICAL REGRESSION TEST for ISS-01.

        Before the fix, 17:29 IST resulted in completed bars filtered to 11:55 IST.
        After the fix, the last completed bar must correctly be 15:30 IST,
        with bar age approximately 1h 59m ago.
        """
        now = datetime.datetime.combine(self.test_date, datetime.time(17, 29), tzinfo=IST_TZ)
        res = self._analyze_with_mock_chart(now)

        self.assertEqual(res["day_phase"], "after_1530_or_closed")
        self.assertEqual(res["market_state"], "closed")

        s1 = res["steps"]["data_check"]
        # MUST BE 15:30:00 IST, NOT 11:55:00 IST!
        self.assertEqual(s1["last_bar_time"], "15:30:00 IST")
        self.assertNotEqual(s1["last_bar_time"], "11:55:00 IST")
        self.assertEqual(s1["bar_age"], "1h 59m ago")

    def test_09_weekend(self):
        """Weekend — Saturday 2026-10-24 11:00 IST: market state weekend and risk check blocks."""
        sat_date = datetime.date(2026, 10, 24)
        now = datetime.datetime.combine(sat_date, datetime.time(11, 0), tzinfo=IST_TZ)
        res = self._analyze_with_mock_chart(now)

        self.assertEqual(res["market_state"], "weekend")
        self.assertEqual(res["steps"]["risk_check"]["verdict"], "BLOCK")
        self.assertIn("weekend", res["steps"]["risk_check"]["reason"].lower())

    def test_10_holiday(self):
        """NSE Holiday — 2026-10-02 (Mahatma Gandhi Jayanti from trader/holidays.json)."""
        hol_date = datetime.date(2026, 10, 2)
        now = datetime.datetime.combine(hol_date, datetime.time(11, 0), tzinfo=IST_TZ)
        res = self._analyze_with_mock_chart(now)

        self.assertEqual(res["market_state"], "holiday")
        self.assertEqual(res["steps"]["data_check"]["holiday_name"], "Mahatma Gandhi Jayanti")
        self.assertEqual(res["steps"]["risk_check"]["verdict"], "BLOCK")
        self.assertIn("Mahatma Gandhi Jayanti", res["steps"]["risk_check"]["reason"])

    def test_11_candle_timestamp_exact_closing_boundary(self):
        """Verify that a 5m candle starting at 15:25 IST closes at 15:30 IST.

        At 15:29:59 IST: candle is still forming (end_ts > now_ts).
        At 15:30:00 IST: candle is completed (end_ts <= now_ts).
        """
        bar_1525 = self.bars_75[-1]  # 75th bar starts at 15:25 IST
        step_sec = 300
        end_ts = bar_1525["t"] + step_sec

        # 1 second before completion: 15:29:59 IST
        dt_forming = datetime.datetime.combine(self.test_date, datetime.time(15, 29, 59), tzinfo=IST_TZ)
        ts_forming = int(dt_forming.timestamp())
        self.assertFalse(end_ts <= ts_forming, "Bar should NOT be complete at 15:29:59 IST")

        # Exactly at completion: 15:30:00 IST
        dt_complete = datetime.datetime.combine(self.test_date, datetime.time(15, 30, 0), tzinfo=IST_TZ)
        ts_complete = int(dt_complete.timestamp())
        self.assertTrue(end_ts <= ts_complete, "Bar MUST be complete at 15:30:00 IST")

    def test_12_upstox_and_yahoo_timestamp_consistency(self):
        """Verify that Upstox ISO string and Yahoo epoch integer produce identical Unix timestamps."""
        # Same instant in time: 09:15:00 IST on 2026-10-21
        yahoo_epoch = 1792554300  # Epoch seconds for 2026-10-21 09:15:00 IST
        upstox_iso = "2026-10-21T09:15:00+05:30"

        upstox_dt = datetime.datetime.fromisoformat(upstox_iso)
        upstox_ts = int(upstox_dt.timestamp())

        yahoo_dt = datetime.datetime.fromtimestamp(yahoo_epoch, tz=UTC_TZ).astimezone(IST_TZ)
        yahoo_ts = int(yahoo_dt.timestamp())

        self.assertEqual(upstox_ts, yahoo_ts)
        self.assertEqual(upstox_dt.astimezone(IST_TZ).strftime("%H:%M:%S"), "09:15:00")
        self.assertEqual(yahoo_dt.strftime("%H:%M:%S"), "09:15:00")

    def test_13_broker_format_bar_age_with_naive_utc(self):
        """Verify broker_data.format_bar_age does not shift naive UTC datetime by 5.5 hours."""
        # Reference instant: 17:29 IST (11:59 UTC)
        # Bar from 2 minutes ago: 17:27 IST (11:57 UTC)
        ref_utc_aware = datetime.datetime(2026, 10, 21, 11, 59, 0, tzinfo=UTC_TZ)
        ref_utc_naive = ref_utc_aware.replace(tzinfo=None)
        bar_ts = int(ref_utc_aware.timestamp()) - 120

        age_from_naive = format_bar_age(bar_ts, ref_utc_naive, source="upstox")
        age_from_aware = format_bar_age(bar_ts, ref_utc_aware, source="upstox")

        self.assertEqual(age_from_naive, "source: upstox, bar age: 2m")
        self.assertEqual(age_from_aware, "source: upstox, bar age: 2m")

    def test_14_app_trust_strip_info_at_1729_ist(self):
        """Verify compute_trust_strip_info in app.py displays 15:25 to 15:30, NOT 11:50 to 11:55."""
        # 17:29 IST on trading day
        dt_1729_ist = datetime.datetime.combine(self.test_date, datetime.time(17, 29), tzinfo=IST_TZ)
        dt_1729_utc_naive = dt_1729_ist.astimezone(UTC_TZ).replace(tzinfo=None)

        info = compute_trust_strip_info(
            data_source="yahoo",
            chart=self.mock_chart,
            is_sample_mode=False,
            now_utc=dt_1729_utc_naive
        )

        self.assertEqual(info["bar_range_str"], "15:25 to 15:30")
        self.assertNotEqual(info["bar_range_str"], "11:50 to 11:55")
        self.assertEqual(info["bar_age_str"], "age 119m")
        self.assertEqual(info["source_label"], "LAST SESSION")


if __name__ == "__main__":
    unittest.main()
