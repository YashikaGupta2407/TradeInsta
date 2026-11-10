#!/usr/bin/env python3
"""Tests for app.py helper functions and integrations."""

import unittest
from app import load_universe, resolve_symbol, SYM_TO_NAME


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
        # Name resolution for known stocks
        resolved = resolve_symbol("Reliance")
        self.assertEqual(resolved, "RELIANCE.NS")

    def test_resolve_symbol_from_dropdown_format(self):
        resolved = resolve_symbol("INFY.NS — Infosys Limited")
        self.assertEqual(resolved, "INFY.NS")


if __name__ == "__main__":
    unittest.main()

