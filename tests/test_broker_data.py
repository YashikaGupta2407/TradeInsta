#!/usr/bin/env python3
import datetime
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch, MagicMock

# Ensure trader directory is on sys.path
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "trader"))
import broker_data


class TestBrokerData(unittest.TestCase):
    def setUp(self):
        self.orig_env = os.environ.copy()
        for k in ("FYERS_APP_ID", "FYERS_ACCESS_TOKEN", "UPSTOX_ACCESS_TOKEN"):
            os.environ.pop(k, None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.orig_env)

    def test_active_broker_detection(self):
        self.assertIsNone(broker_data.active_broker())
        os.environ["FYERS_ACCESS_TOKEN"] = "fake_fyers_token"
        self.assertEqual(broker_data.active_broker(), "fyers")
        os.environ.pop("FYERS_ACCESS_TOKEN")
        os.environ["UPSTOX_ACCESS_TOKEN"] = "fake_upstox_token"
        self.assertEqual(broker_data.active_broker(), "upstox")

    def test_symbol_translation(self):
        self.assertEqual(broker_data._to_fyers_symbol("RELIANCE.NS"), "NSE:RELIANCE-EQ")
        self.assertEqual(broker_data._to_fyers_symbol("TCS"), "NSE:TCS-EQ")
        self.assertEqual(broker_data._to_fyers_symbol("SBIN.BO"), "BSE:SBIN-EQ")
        # Upstox uses official ISIN format for equities: NSE_EQ|<ISIN>
        self.assertEqual(broker_data._to_upstox_instrument("RELIANCE.NS"), "NSE_EQ|INE002A01018")
        self.assertEqual(broker_data._to_upstox_instrument("RELIANCE"), "NSE_EQ|INE002A01018")
        self.assertEqual(broker_data._to_upstox_instrument("INFY"), "NSE_EQ|INE009A01021")
        self.assertEqual(broker_data._to_upstox_instrument("NSE_EQ|INE002A01018"), "NSE_EQ|INE002A01018")
        self.assertIsNone(broker_data._to_upstox_instrument("UNKNOWN_TICKER_12345"))

    def test_format_bar_age(self):
        now_ts = 1729913100
        # 30 seconds ago
        res_recent = broker_data.format_bar_age(now_ts - 30, now_ts, source="fyers")
        self.assertEqual(res_recent, "source: fyers, bar age: 30s")

        # 3 minutes ago
        res_3m = broker_data.format_bar_age(now_ts - 180, now_ts, source="upstox")
        self.assertEqual(res_3m, "source: upstox, bar age: 3m")

        # 16 minutes ago (Yahoo) -> delayed tag
        res_yahoo = broker_data.format_bar_age(now_ts - 960, now_ts, source="yahoo")
        self.assertIn("delayed", res_yahoo)
        self.assertIn("16m", res_yahoo)

    def test_candles_to_series(self):
        chart = {
            "symbol": "RELIANCE.NS",
            "bars": [
                {"t": 100, "o": 10.0, "h": 12.0, "l": 9.5, "c": 11.5, "v": 500},
                {"t": 200, "o": 11.5, "h": 13.0, "l": 11.0, "c": 12.5, "v": 800}
            ],
            "meta": {"source": "fyers"}
        }
        series = broker_data.candles_to_series(chart)
        self.assertEqual(series["t"], [100, 200])
        self.assertEqual(series["o"], [10.0, 11.5])
        self.assertEqual(series["h"], [12.0, 13.0])
        self.assertEqual(series["l"], [9.5, 11.0])
        self.assertEqual(series["c"], [11.5, 12.5])
        self.assertEqual(series["v"], [500, 800])
        self.assertEqual(series["meta"]["source"], "fyers")

    def test_fyers_mock_candles(self):
        os.environ["FYERS_APP_ID"] = "APP123"
        os.environ["FYERS_ACCESS_TOKEN"] = "TOKEN123"
        mock_resp = {
            "s": "ok",
            "candles": [
                [1729913000, 1280.0, 1285.0, 1278.0, 1282.5, 35000],
                [1729913300, 1282.5, 1289.0, 1281.0, 1288.0, 48000]
            ]
        }
        with patch("urllib.request.urlopen") as mock_url:
            mock_cm = MagicMock()
            mock_cm.read.return_value = json.dumps(mock_resp).encode("utf-8")
            mock_url.return_value.__enter__.return_value = mock_cm

            res = broker_data.fetch_fyers_candles("RELIANCE.NS", "5m")
            self.assertIsNotNone(res)
            self.assertEqual(len(res["bars"]), 2)
            self.assertEqual(res["bars"][0]["c"], 1282.5)
            self.assertEqual(res["bars"][1]["c"], 1288.0)
            self.assertEqual(res["meta"]["source"], "fyers")

    def test_upstox_mock_candles(self):
        os.environ["UPSTOX_ACCESS_TOKEN"] = "TOKEN_UPSTOX"
        # Upstox returns newest first
        mock_resp = {
            "status": "success",
            "data": {
                "candles": [
                    ["2026-10-26T15:25:00+05:30", 1282.5, 1289.0, 1281.0, 1288.0, 48000, 0],
                    ["2026-10-26T15:20:00+05:30", 1280.0, 1285.0, 1278.0, 1282.5, 35000, 0]
                ]
            }
        }
        with patch("urllib.request.urlopen") as mock_url:
            mock_cm = MagicMock()
            mock_cm.read.return_value = json.dumps(mock_resp).encode("utf-8")
            mock_url.return_value.__enter__.return_value = mock_cm

            res = broker_data.fetch_upstox_candles("RELIANCE.NS", "5m")
            self.assertIsNotNone(res)
            self.assertEqual(len(res["bars"]), 2)
            # Checked in chronological order
            self.assertLess(res["bars"][0]["t"], res["bars"][1]["t"])
            self.assertEqual(res["meta"]["source"], "upstox")

    def test_fallback_on_empty_credentials(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            res = broker_data.get_realtime_candles("RELIANCE.NS", "5m")
            quotes = broker_data.get_realtime_quotes(["RELIANCE.NS"])
        self.assertIsNone(res)
        self.assertIsNone(quotes)
        out = buf.getvalue()
        self.assertIn("token not set", out)
        self.assertIn("falling back to Yahoo", out)

    def test_fallback_reasons_logged(self):
        # 1. Symbol not found
        os.environ["UPSTOX_ACCESS_TOKEN"] = "TEST_TOKEN_123"
        buf = io.StringIO()
        with redirect_stdout(buf):
            res = broker_data.fetch_upstox_candles("UNKNOWN_XYZ_999", "5m")
        self.assertIsNone(res)
        self.assertIn("symbol not found", buf.getvalue())
        self.assertIn("falling back to Yahoo", buf.getvalue())

        # 2. Token expired (HTTP 401)
        import urllib.error
        err_401 = urllib.error.HTTPError(
            url="https://api.upstox.com/v3/...",
            code=401,
            msg="Unauthorized",
            hdrs={},
            fp=io.BytesIO(b'{"status":"error","errors":[{"message":"Token expired or invalid"}]}')
        )
        buf = io.StringIO()
        with patch("urllib.request.urlopen", side_effect=err_401):
            with redirect_stdout(buf):
                res = broker_data.fetch_upstox_candles("RELIANCE.NS", "5m")
        self.assertIsNone(res)
        self.assertIn("token expired", buf.getvalue())
        self.assertIn("falling back to Yahoo", buf.getvalue())

        # 3. HTTP status code (HTTP 403 Forbidden)
        err_403 = urllib.error.HTTPError(
            url="https://api.upstox.com/v3/...",
            code=403,
            msg="Forbidden",
            hdrs={},
            fp=io.BytesIO(b'{"status":"error","errors":[{"message":"Access denied"}]}')
        )
        buf = io.StringIO()
        with patch("urllib.request.urlopen", side_effect=err_403):
            with redirect_stdout(buf):
                res = broker_data.fetch_upstox_candles("RELIANCE.NS", "5m")
        self.assertIsNone(res)
        self.assertIn("HTTP 403", buf.getvalue())
        self.assertIn("falling back to Yahoo", buf.getvalue())

    def test_never_print_token(self):
        secret_token = "SECRET_SUPER_CONFIDENTIAL_TOKEN_XYZ"
        os.environ["UPSTOX_ACCESS_TOKEN"] = secret_token
        import urllib.error
        err = urllib.error.HTTPError(
            url="https://api.upstox.com/...",
            code=401,
            msg="Unauthorized with " + secret_token,
            hdrs={},
            fp=io.BytesIO(b'{"message":"invalid token ' + secret_token.encode() + b'"}')
        )
        buf = io.StringIO()
        with patch("urllib.request.urlopen", side_effect=err):
            with redirect_stdout(buf):
                broker_data.fetch_upstox_candles("RELIANCE.NS", "5m")
        out = buf.getvalue()
        self.assertNotIn(secret_token, out)
        self.assertIn("falling back to Yahoo", out)


if __name__ == "__main__":
    unittest.main()
