#!/usr/bin/env python3
"""Tests for app.py helper functions, integrations, and badges."""

import unittest
from app import load_universe, resolve_symbol, get_badge_info, SYM_TO_NAME


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
        self.assertEqual(info["label"], "LAST SESSION (market closed)")

    def test_badge_live_broker(self):
        info_upstox = get_badge_info(is_sample=False, is_open=True, data_source="upstox", bar_age="1m")
        self.assertEqual(info_upstox["label"], "LIVE (broker)")

        info_fyers = get_badge_info(is_sample=False, is_open=True, data_source="fyers", bar_age="2m")
        self.assertEqual(info_fyers["label"], "LIVE (broker)")

    def test_badge_delayed_yahoo(self):
        info = get_badge_info(is_sample=False, is_open=True, data_source="yahoo", bar_age="16m")
        self.assertEqual(info["label"], "DELAYED (Yahoo)")


if __name__ == "__main__":
    unittest.main()
