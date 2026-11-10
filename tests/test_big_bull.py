#!/usr/bin/env python3
"""Unit tests for BIG BULL institutional intelligence engine."""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
from _load import load

flow = load("trader-flow", "flow")
big_bull = load("trader-flow", "big_bull")


class TestBigBull(unittest.TestCase):
    def setUp(self):
        self.fixture_file = os.path.join(ROOT, "tests", "fixtures", "big_bull", "big_bull_multi_session.csv")
        self.deals = flow.parse_flow_csv(self.fixture_file)

    def test_load_sector_registry(self):
        reg = big_bull.load_sector_registry()
        self.assertIn("Banking & Financial Services", reg["sectors"])
        self.assertIn("HDFCBANK", reg["symbol_map"])
        self.assertEqual(reg["symbol_map"]["HDFCBANK"], "Banking & Financial Services")
        self.assertEqual(reg["symbol_map"]["TCS"], "IT & Software")

    def test_get_stock_sector(self):
        # Exact symbol
        self.assertEqual(big_bull.get_stock_sector("RELIANCE.NS"), "Energy & Power")
        self.assertEqual(big_bull.get_stock_sector("INFY"), "IT & Software")

        # Keyword matching on company name
        self.assertEqual(big_bull.get_stock_sector("XYZBANK", "XYZ Bank Limited"), "Banking & Financial Services")
        self.assertEqual(big_bull.get_stock_sector("XYZPHARMA", "XYZ Life Sciences & Pharma Ltd"), "Pharmaceuticals & Healthcare")

        # Unknown fallback
        self.assertEqual(big_bull.get_stock_sector("UNKNOWNXYZ", "Random Entity"), "Diversified / Others")

    def test_market_flow_kpis(self):
        kpis = big_bull.compute_market_flow_kpis(self.deals)
        self.assertGreater(kpis["total_buy_value_inr"], 0)
        self.assertGreater(kpis["total_sell_value_inr"], 0)
        self.assertEqual(kpis["total_deals"], len(self.deals))
        self.assertGreater(kpis["net_positive_stocks_count"], 0)
        self.assertGreater(kpis["net_negative_stocks_count"], 0)
        self.assertEqual(kpis["date_range"]["earliest"], "06-NOV-2026")
        self.assertEqual(kpis["date_range"]["latest"], "10-NOV-2026")

    def test_sector_flow(self):
        sectors = big_bull.compute_sector_flow(self.deals)
        self.assertGreater(len(sectors), 0)

        # Ranked by net flow descending
        for i in range(len(sectors) - 1):
            self.assertGreaterEqual(sectors[i]["net_flow_inr"], sectors[i + 1]["net_flow_inr"])

        # Check banking sector
        banking = next(s for s in sectors if s["sector"] == "Banking & Financial Services")
        self.assertGreater(banking["buy_value_inr"], 0)
        self.assertEqual(banking["stocks_with_buying"], 1)  # HDFCBANK
        self.assertEqual(banking["status"], "Strong Net Buying")

    def test_stock_accumulation_patterns(self):
        analyzed = big_bull.analyze_stock_accumulation(self.deals)
        sym_map = {s["symbol"]: s for s in analyzed}

        # RELIANCE: Goldman Sachs bought on 06-Nov and 10-Nov -> Repeated Buying + Multiple Buyers
        rel = sym_map["RELIANCE"]
        self.assertTrue(rel["has_repeated_buying"])
        self.assertTrue(rel["has_multiple_buyers"])
        self.assertIn("REPEATED BUYING", rel["patterns"])
        self.assertIn("MULTIPLE BUYERS", rel["patterns"])
        self.assertIn("GOLDMAN SACHS", rel["repeated_buyers"][0])

        # HDFCBANK: Bought by Nomura, ICICI Pru, and Vanguard -> Multiple Buyers (3)
        hdfc = sym_map["HDFCBANK"]
        self.assertEqual(hdfc["distinct_buyers_count"], 3)
        self.assertTrue(hdfc["has_multiple_buyers"])
        self.assertTrue(hdfc["is_strong_net_buying"])

        # INFY: Citigroup sold across 2 dates -> Repeated Selling
        infy = sym_map["INFY"]
        self.assertTrue(infy["has_repeated_selling"])
        self.assertIn("REPEATED SELLING", infy["patterns"])

        # TATAMOTORS: 50% buy and 50% sell -> Mixed Flow
        tm = sym_map["TATAMOTORS"]
        self.assertTrue(tm["is_mixed_flow"])
        self.assertIn("MIXED FLOW", tm["patterns"])

    def test_volume_confirmation(self):
        analyzed = big_bull.analyze_stock_accumulation(self.deals)

        def mock_volume_lookup(sym):
            if sym == "RELIANCE":
                return {
                    "latest_volume": 2000000,
                    "avg_20d_volume": 1200000,
                    "ratio": 1.67,
                    "price": 1265.0,
                    "price_change_pct": 1.2,
                    "label": "1.67x (+66.7% vs 20d avg)"
                }
            return None

        enriched = big_bull.add_volume_confirmation(analyzed, mock_volume_lookup)
        rel = next(s for s in enriched if s["symbol"] == "RELIANCE")
        self.assertTrue(rel["volume_confirmed"])
        self.assertEqual(rel["volume_ratio"], 1.67)

        hdfc = next(s for s in enriched if s["symbol"] == "HDFCBANK")
        self.assertFalse(hdfc["volume_confirmed"])
        self.assertIsNone(hdfc["volume_ratio"])

    def test_watchlist_scoring(self):
        analyzed = big_bull.analyze_stock_accumulation(self.deals)
        sector_flows = big_bull.compute_sector_flow(self.deals)
        watchlist = big_bull.generate_big_bull_watchlist(analyzed, sector_flows=sector_flows, min_score=40)

        self.assertGreater(len(watchlist), 0)
        # Verify scores are bounded between 0 and 100 and descending
        for i in range(len(watchlist) - 1):
            self.assertGreaterEqual(watchlist[i]["score"], watchlist[i + 1]["score"])
            self.assertGreaterEqual(watchlist[i]["score"], 40)
            self.assertLessEqual(watchlist[i]["score"], 100)

        # Top stock has documented reasons
        top = watchlist[0]
        self.assertGreater(len(top["reasons"]), 0)

    def test_market_caps_and_breadth(self):
        mc = big_bull.load_market_caps()
        self.assertIn("RELIANCE", mc["stocks"])
        self.assertIn("TCS", mc["stocks"])

        mock_quotes = {
            "RELIANCE": {"symbol": "RELIANCE", "price": 1260.0, "change_pct": 1.5, "source": "upstox"},
            "TCS": {"symbol": "TCS", "price": 4150.0, "change_pct": -0.8, "source": "upstox"},
            "INFY": {"symbol": "INFY", "price": 1860.0, "change_pct": 0.0, "source": "upstox"},
        }
        breadth = big_bull.compute_market_breadth(mock_quotes)
        self.assertEqual(breadth["advances"], 1)
        self.assertEqual(breadth["declines"], 1)
        self.assertEqual(breadth["unchanged"], 1)
        self.assertEqual(breadth["total"], 3)
        self.assertEqual(breadth["source"], "upstox")

    def test_sector_performance_and_heatmap(self):
        mock_quotes = {
            "RELIANCE": {"symbol": "RELIANCE", "price": 1260.0, "change_pct": 2.0, "volume": 5000000, "source": "upstox"},
            "TCS": {"symbol": "TCS", "price": 4150.0, "change_pct": -1.0, "volume": 1200000, "source": "upstox"},
            "HDFCBANK": {"symbol": "HDFCBANK", "price": 1730.0, "change_pct": 0.8, "volume": 3400000, "source": "upstox"},
        }
        sec_perf = big_bull.compute_sector_performance(mock_quotes)
        self.assertGreater(len(sec_perf), 0)
        # Energy & Power should be top with +2.0%
        self.assertEqual(sec_perf[0]["sector"], "Energy & Power")
        self.assertEqual(sec_perf[0]["weighted_change_pct"], 2.0)

        heatmap = big_bull.build_heatmap_records(mock_quotes)
        self.assertEqual(len(heatmap), 3)
        rel_rec = next(r for r in heatmap if r["symbol"] == "RELIANCE")
        self.assertEqual(rel_rec["sector"], "Energy & Power")
        self.assertEqual(rel_rec["price"], 1260.0)

    def test_big_bull_radar(self):
        mock_quotes = {
            "RELIANCE": {"symbol": "RELIANCE", "price": 1260.0, "change_pct": 1.2, "volume": 5000000, "source": "upstox"},
            "HDFCBANK": {"symbol": "HDFCBANK", "price": 1730.0, "change_pct": 0.5, "volume": 3000000, "source": "upstox"},
        }
        radar = big_bull.generate_big_bull_radar(self.deals, mock_quotes)
        self.assertGreater(len(radar), 0)
        top = radar[0]
        self.assertIn("symbol", top)
        self.assertIn("reasons", top)
        self.assertGreater(len(top["reasons"]), 0)


if __name__ == "__main__":
    unittest.main()
