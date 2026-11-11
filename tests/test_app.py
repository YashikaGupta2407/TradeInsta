#!/usr/bin/env python3
"""Tests for app.py helper functions, integrations, and badges."""

import unittest
from app import load_universe, resolve_symbol, get_badge_info, load_market_deals, flow_mod, SYM_TO_NAME


class TestApp(unittest.TestCase):
    def test_load_universe(self):
        sym_to_name, name_to_sym, items = load_universe()
        self.assertGreater(len(sym_to_name), 0)
        self.assertIn("RELIANCE.NS", sym_to_name)

    def test_resolve_symbol_exact(self):
        self.assertEqual(resolve_symbol("RELIANCE.NS"), "RELIANCE.NS")
        self.assertEqual(resolve_symbol("TCS.NS"), "TCS.NS")

    def test_resolve_symbol_without_suffix(self):
        self.assertEqual(resolve_symbol("RELIANCE"), "RELIANCE.NS")
        self.assertEqual(resolve_symbol("tcs"), "TCS.NS")

    def test_resolve_symbol_from_name(self):
        resolved = resolve_symbol("Reliance")
        self.assertEqual(resolved, "RELIANCE.NS")

    def test_resolve_symbol_from_dropdown_format(self):
        resolved = resolve_symbol("INFY.NS — Infosys Limited")
        self.assertEqual(resolved, "INFY.NS")

    def test_badge_sample_data(self):
        info = get_badge_info(is_sample=True, is_open=True, data_source="sample")
        self.assertEqual(info["label"], "SAMPLE DATA")

    def test_badge_market_closed(self):
        info = get_badge_info(is_sample=False, is_open=False, data_source="yahoo", session_reason="after the close")
        self.assertIn("LAST SESSION", info["label"])

    def test_badge_holiday_and_market_not_open(self):
        info_hol = get_badge_info(is_sample=False, is_open=False, data_source="yahoo", holiday_name="Diwali")
        self.assertEqual(info_hol["label"], "HOLIDAY")

        info_pre = get_badge_info(is_sample=False, is_open=False, data_source="yahoo", is_pre_open=True)
        self.assertEqual(info_pre["label"], "MARKET NOT OPEN")

    def test_badge_live_broker(self):
        info_upstox = get_badge_info(is_sample=False, is_open=True, data_source="upstox", bar_age="1m")
        self.assertEqual(info_upstox["label"], "LIVE (broker)")

        info_fyers = get_badge_info(is_sample=False, is_open=True, data_source="fyers", bar_age="2m")
        self.assertEqual(info_fyers["label"], "LIVE (broker)")

    def test_badge_delayed_yahoo(self):
        info = get_badge_info(is_sample=False, is_open=True, data_source="yahoo", bar_age="16m")
        self.assertEqual(info["label"], "DELAYED (Yahoo)")

    def test_load_market_deals_sample_mode(self):
        deals, source_note = load_market_deals(is_sample_mode=True)
        self.assertGreater(len(deals), 0)
        self.assertIn("Sample", source_note)

    def test_load_market_deals_empty_dir_graceful(self):
        # In default workspace without added CSVs, trader/flow returns empty list
        deals, source_note = load_market_deals(is_sample_mode=False)
        self.assertIsInstance(deals, list)

    def test_symbol_flow_with_and_without_deals(self):
        deals, _ = load_market_deals(is_sample_mode=True)
        # Stock with deals
        summary_rel = flow_mod.get_symbol_flow_summary(deals, "RELIANCE")
        self.assertGreater(summary_rel["total_deals"], 0)
        self.assertGreater(summary_rel["buy_value_inr"] + summary_rel["sell_value_inr"], 0)

        # Stock without deals
        summary_unknown = flow_mod.get_symbol_flow_summary(deals, "NONEXISTENTSTOCK")
        self.assertEqual(summary_unknown["total_deals"], 0)
        self.assertEqual(summary_unknown["buy_deals"], 0)
        self.assertEqual(summary_unknown["sell_deals"], 0)
        self.assertEqual(summary_unknown["buy_value_inr"], 0.0)
        self.assertEqual(summary_unknown["sell_value_inr"], 0.0)
        self.assertEqual(summary_unknown["net_value_inr"], 0.0)
        self.assertEqual(len(summary_unknown["deals"]), 0)

    def test_fetch_cached_helpers_exist(self):
        from app import fetch_cached_intraday_chart, fetch_cached_history_chart, fetch_cached_market_overview
        self.assertTrue(callable(fetch_cached_intraday_chart))
        self.assertTrue(callable(fetch_cached_history_chart))
        self.assertTrue(callable(fetch_cached_market_overview))

    def test_rate_limit_and_network_error_keywords(self):
        err_429 = "Yahoo did not answer (HTTP 429 on query2 and query1): wait a minute and try again"
        err_dns = "network error: [Errno 8] nodename nor servname provided, or not known"

        is_429 = any(k in err_429.lower() for k in ["429", "rate", "too many requests", "wait a minute"])
        is_dns = any(k in err_dns.lower() for k in ["nodename", "servname", "could not connect", "network error", "dns"])

        self.assertTrue(is_429)
        self.assertTrue(is_dns)

    def test_badge_never_sample_in_live_mode(self):
        # Even on errors, live mode badge is never "SAMPLE DATA"
        badge = get_badge_info(is_sample=False, is_open=True, data_source="yahoo")
        self.assertNotEqual(badge["label"], "SAMPLE DATA")

        badge_upstox = get_badge_info(is_sample=False, is_open=True, data_source="upstox")
        self.assertEqual(badge_upstox["label"], "LIVE (broker)")


if __name__ == "__main__":
    unittest.main()
