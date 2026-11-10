#!/usr/bin/env python3
"""Unit tests for trader-flow skill."""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
from _load import load

flow = load("trader-flow", "flow")


class TestTraderFlow(unittest.TestCase):
    def setUp(self):
        self.fixture_dir = os.path.join(ROOT, "tests", "fixtures", "trader-flow")
        self.bulk_csv = os.path.join(self.fixture_dir, "bulk_deals_sample.csv")
        self.block_csv = os.path.join(self.fixture_dir, "block_deals_sample.csv")

    def test_parse_bulk_deals_csv(self):
        deals = flow.parse_flow_csv(self.bulk_csv)
        self.assertEqual(len(deals), 5)
        d0 = deals[0]
        self.assertEqual(d0["symbol"], "RELIANCE")
        self.assertEqual(d0["symbol_ns"], "RELIANCE.NS")
        self.assertEqual(d0["deal_type"], "BULK")
        self.assertEqual(d0["action"], "BUY")
        self.assertEqual(d0["quantity"], 1500000)
        self.assertEqual(d0["price"], 1260.50)
        self.assertAlmostEqual(d0["value_inr"], 1500000 * 1260.50, places=2)
        self.assertEqual(d0["pct_shares"], 0.55)

    def test_parse_block_deals_csv(self):
        deals = flow.parse_flow_csv(self.block_csv)
        self.assertEqual(len(deals), 4)
        for d in deals:
            self.assertEqual(d["deal_type"], "BLOCK")
        hdfc_deals = [d for d in deals if d["symbol"] == "HDFCBANK"]
        self.assertEqual(len(hdfc_deals), 2)
        self.assertIn("NOMURA", hdfc_deals[0]["client_name"])

    def test_load_all_deals_from_dir(self):
        deals = flow.load_flow_deals(flow_dir=self.fixture_dir)
        # 5 bulk deals + 4 block deals = 9 total
        self.assertEqual(len(deals), 9)

    def test_get_deals_for_symbol(self):
        deals = flow.load_flow_deals(flow_dir=self.fixture_dir)
        # Query with .NS suffix
        rel_deals_ns = flow.get_deals_for_symbol(deals, "RELIANCE.NS")
        self.assertEqual(len(rel_deals_ns), 4)

        # Query without .NS suffix
        rel_deals_raw = flow.get_deals_for_symbol(deals, "reliance")
        self.assertEqual(len(rel_deals_raw), 4)

        # Query symbol not in deals
        unknown_deals = flow.get_deals_for_symbol(deals, "UNKNOWNSTOCK")
        self.assertEqual(len(unknown_deals), 0)

    def test_get_symbol_flow_summary(self):
        deals = flow.load_flow_deals(flow_dir=self.fixture_dir)
        summary = flow.get_symbol_flow_summary(deals, "RELIANCE")
        self.assertEqual(summary["symbol"], "RELIANCE")
        self.assertEqual(summary["total_deals"], 4)
        self.assertEqual(summary["buy_deals"], 2)
        self.assertEqual(summary["sell_deals"], 2)
        self.assertGreater(summary["buy_value_inr"], 0)
        self.assertGreater(summary["sell_value_inr"], 0)
        self.assertEqual(summary["net_action"], "NET_BUY")

    def test_get_top_stocks_by_deal_size(self):
        deals = flow.load_flow_deals(flow_dir=self.fixture_dir)
        top = flow.get_top_stocks_by_deal_size(deals, top_n=3)
        self.assertEqual(len(top), 3)
        # RELIANCE has the largest combined deals
        self.assertEqual(top[0]["symbol"], "RELIANCE")
        self.assertGreater(top[0]["total_value_inr"], top[1]["total_value_inr"])

    def test_top_stocks_filter_by_type(self):
        deals = flow.load_flow_deals(flow_dir=self.fixture_dir)
        top_block = flow.get_top_stocks_by_deal_size(deals, top_n=5, deal_type="block")
        symbols_in_block = [x["symbol"] for x in top_block]
        self.assertIn("HDFCBANK", symbols_in_block)
        self.assertNotIn("TATAMOTORS", symbols_in_block)

    def test_compute_volume_vs_20d_avg(self):
        # 20 bars of 100,000 and 1 bar of 200,000 -> 2.0x ratio
        vols = [100000] * 20 + [200000]
        res = flow.compute_volume_vs_20d_avg("RELIANCE", daily_volumes=vols)
        self.assertEqual(res["ratio"], 2.0)
        self.assertEqual(res["pct_diff"], 100.0)
        self.assertIn("2.00x", res["label"])
        self.assertIn("+100.0%", res["label"])

    def test_compute_volume_insufficient_data(self):
        res = flow.compute_volume_vs_20d_avg("RELIANCE", daily_volumes=[100000])
        self.assertIsNone(res["ratio"])
        self.assertIn("Insufficient", res["label"])

    def test_number_cleaner(self):
        self.assertEqual(flow._clean_num("1,500,000"), 1500000.0)
        self.assertEqual(flow._clean_num("₹1,260.50"), 1260.50)
        self.assertEqual(flow._clean_num("0.55%"), 0.55)
        self.assertEqual(flow._clean_num("-"), 0.0)
        self.assertEqual(flow._clean_num(None), 0.0)


if __name__ == "__main__":
    unittest.main()
