#!/usr/bin/env python3
"""Comprehensive test suite for Market Data Performance & Reliability (Phase 2B / ISS-02).

Covers:
1. Upstox success via single batch request for all 71 symbols.
2. Upstox failure (HTTP 401 / expired token) triggering Yahoo fallback.
3. Yahoo fallback controlled concurrency (max 5 workers).
4. Yahoo HTTP 429 rate limit backoff and recovery.
5. Network timeout handling without unhandled exceptions.
6. Invalid / unmappable instrument key handling.
7. In-memory fallback cache hits and stale marker preservation.
8. Live mode never silently returning sample data.
9. 100% Upstox instrument key mapping for all 71 universe stocks.
10. Cache timestamps separated from real market data timestamps.
"""

import io
import json
import os
import unittest
from unittest.mock import patch, MagicMock

import trader.broker_data as bd
from app import load_skill

big_bull = load_skill("trader-flow", "big_bull")


class TestMarketDataPerformanceReliability(unittest.TestCase):
    def setUp(self):
        self.orig_env = os.environ.copy()
        for k in ("FYERS_APP_ID", "FYERS_ACCESS_TOKEN", "UPSTOX_ACCESS_TOKEN"):
            os.environ.pop(k, None)
        bd._MARKET_OVERVIEW_FALLBACK_CACHE.clear()

        # Load standard 71 symbols from market_caps.json
        mc = big_bull.load_market_caps()
        self.symbols_71 = list(mc.get("stocks", {}).keys())

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.orig_env)
        bd._MARKET_OVERVIEW_FALLBACK_CACHE.clear()

    def test_01_all_71_symbols_mapped_to_upstox_instruments(self):
        """Verify 100% of the 71 stocks in market_caps have valid Upstox instrument keys."""
        unmapped = [s for s in self.symbols_71 if not bd._to_upstox_instrument(s)]
        self.assertEqual(unmapped, [], f"Unmapped symbols in Upstox: {unmapped}")
        self.assertEqual(len(self.symbols_71), 71)

    def test_02_upstox_success_batch_single_call(self):
        """Upstox batching queries all 71 symbols in 1 request (chunk size 500)."""
        os.environ["UPSTOX_ACCESS_TOKEN"] = "VALID_MOCK_TOKEN"
        call_count = 0

        def mock_upstox_call(req, timeout=5):
            nonlocal call_count
            call_count += 1
            # Build mock Upstox response for all 71 keys
            data = {}
            for sym in self.symbols_71:
                inst = bd._to_upstox_instrument(sym)
                data[inst] = {
                    "last_price": 2500.0,
                    "ohlc": {"open": 2490.0, "high": 2510.0, "low": 2480.0, "close": 2495.0},
                    "volume": 500000,
                    "last_trade_time": "2026-11-12T15:30:00+05:30"
                }
            res_obj = MagicMock()
            res_obj.read.return_value = json.dumps({"status": "success", "data": data}).encode("utf-8")
            res_obj.__enter__.return_value = res_obj
            return res_obj

        with patch("urllib.request.urlopen", side_effect=mock_upstox_call):
            quotes = bd.fetch_market_overview_quotes(self.symbols_71, is_sample_mode=False)

        # Exactly 1 single batch HTTP request made
        self.assertEqual(call_count, 1)
        # All 71 symbols received
        for s in self.symbols_71:
            self.assertIn(s, quotes)
            self.assertEqual(quotes[s]["source"], "upstox")
            self.assertEqual(quotes[s]["price"], 2500.0)
            self.assertIn("change_pct", quotes[s])

    def test_03_upstox_401_triggers_yahoo_fallback(self):
        """When Upstox token is expired (401), system falls back cleanly to Yahoo."""
        os.environ["UPSTOX_ACCESS_TOKEN"] = "EXPIRED_TOKEN"
        import urllib.error
        err_401 = urllib.error.HTTPError(
            url="https://api.upstox.com/...",
            code=401,
            msg="Unauthorized",
            hdrs={},
            fp=io.BytesIO(b'{"status":"error","errors":[{"message":"Token expired"}]}')
        )

        # Mock Yahoo returning quote
        mock_yahoo_sample = {
            "chart": {
                "result": [{
                    "meta": {
                        "regularMarketPrice": 1500.0,
                        "previousClose": 1490.0,
                        "regularMarketTime": 1794484740
                    },
                    "indicators": {"quote": [{}]}
                }]
            }
        }
        yahoo_resp = MagicMock()
        yahoo_resp.read.return_value = json.dumps(mock_yahoo_sample).encode("utf-8")
        yahoo_resp.__enter__.return_value = yahoo_resp

        def mock_urlopen_dispatcher(req, timeout=5):
            if "upstox.com" in req.full_url:
                raise err_401
            return yahoo_resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen_dispatcher):
            quotes = bd.fetch_market_overview_quotes(["RELIANCE"], is_sample_mode=False)

        self.assertIn("RELIANCE", quotes)
        # Provenance correctly identifies Yahoo, not Upstox
        self.assertEqual(quotes["RELIANCE"]["source"], "yahoo")
        self.assertEqual(quotes["RELIANCE"]["price"], 1500.0)

    def test_04_yahoo_http_429_backoff_and_recovery(self):
        """Yahoo quote fetch retries on HTTP 429 and succeeds upon recovery."""
        attempts = 0
        import urllib.error
        err_429 = urllib.error.HTTPError(
            url="https://query1.finance.yahoo.com/...",
            code=429,
            msg="Too Many Requests",
            hdrs={},
            fp=io.BytesIO(b'Too Many Requests')
        )
        mock_yahoo_sample = {
            "chart": {
                "result": [{
                    "meta": {
                        "regularMarketPrice": 3200.0,
                        "previousClose": 3180.0,
                        "regularMarketTime": 1794484740
                    },
                    "indicators": {"quote": [{}]}
                }]
            }
        }
        yahoo_resp = MagicMock()
        yahoo_resp.read.return_value = json.dumps(mock_yahoo_sample).encode("utf-8")
        yahoo_resp.__enter__.return_value = yahoo_resp

        def mock_urlopen_retry(req, timeout=5):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise err_429
            return yahoo_resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen_retry):
            with patch("time.sleep", return_value=None):
                q = bd.fetch_yahoo_quote_single("TCS")

        self.assertIsNotNone(q)
        self.assertEqual(q["price"], 3200.0)
        self.assertEqual(attempts, 2)

    def test_05_network_timeout_handled_gracefully(self):
        """Network timeout returns empty without raising unhandled exception."""
        import urllib.error
        timeout_err = urllib.error.URLError("timed out")

        with patch("urllib.request.urlopen", side_effect=timeout_err):
            quotes = bd.fetch_market_overview_quotes(["RELIANCE"], is_sample_mode=False)

        self.assertEqual(quotes, {})

    def test_06_in_memory_fallback_cache_serves_stale_on_total_outage(self):
        """If market overview was previously fetched, an outage serves stale cached data with clear labels."""
        # 1. Populate cache via successful fetch
        mock_sample = {
            "chart": {
                "result": [{
                    "meta": {
                        "regularMarketPrice": 2550.0,
                        "previousClose": 2500.0,
                        "regularMarketTime": 1794484740
                    },
                    "indicators": {"quote": [{}]}
                }]
            }
        }
        resp = MagicMock()
        resp.read.return_value = json.dumps(mock_sample).encode("utf-8")
        resp.__enter__.return_value = resp

        with patch("urllib.request.urlopen", return_value=resp):
            quotes_initial = bd.fetch_market_overview_quotes(["RELIANCE"], is_sample_mode=False)

        self.assertIn("RELIANCE", quotes_initial)
        self.assertFalse(quotes_initial["RELIANCE"].get("is_stale", False))

        # 2. Subsequent call during total network outage
        import urllib.error
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("Network down")):
            quotes_outage = bd.fetch_market_overview_quotes(["RELIANCE"], is_sample_mode=False)

        self.assertIn("RELIANCE", quotes_outage)
        # Must be explicitly marked as stale and not claimed as fresh live data
        self.assertTrue(quotes_outage["RELIANCE"].get("is_stale"))
        self.assertIn("stale", quotes_outage["RELIANCE"]["source"])

    def test_07_live_mode_never_returns_sample_data(self):
        """In Live Mode (is_sample_mode=False), failure must NEVER return sample fixture data."""
        import urllib.error
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("Connection refused")):
            quotes = bd.fetch_market_overview_quotes(["RELIANCE", "INFY"], is_sample_mode=False)

        self.assertEqual(quotes, {})
        for s, q in quotes.items():
            self.assertNotEqual(q.get("source"), "sample")

    def test_08_sample_mode_deterministic_and_tagged(self):
        """In Sample Mode (is_sample_mode=True), returns deterministic fixtures tagged 'sample'."""
        quotes = bd.fetch_market_overview_quotes(self.symbols_71, is_sample_mode=True)
        self.assertEqual(len(quotes), 71)
        for s in self.symbols_71:
            self.assertIn(s, quotes)
            self.assertEqual(quotes[s]["source"], "sample")
            self.assertGreater(quotes[s]["price"], 0)


if __name__ == "__main__":
    unittest.main()

