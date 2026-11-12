#!/usr/bin/env python3
"""Comprehensive test suite for Phase 4: TradeInsta Premium Homepage Redesign.

Verifies:
1. render_home_view executes cleanly without errors.
2. Hero section contains exact typography and copy from the reference image.
3. Search input and Analyze button trigger proper symbol resolution and navigation.
4. Market Pulse displays NIFTY 50, BANK NIFTY, and SENSEX metrics with real or valid fallback.
5. Three core feature navigation cards (Analyze Now, BIG BULL, Time Machine) exist and route properly.
6. "Your Next Step" card triggers focused analysis.
7. "How TradeInsta Helps" banner renders all 3 pillars (Rule-Based, Risk First, Evidence Driven).
8. Theme switcher supports Light, Dark, and System modes.
9. Theme CSS contains all required design tokens, font families, and responsive rules.
10. No emojis exist in app.py.
"""

import os
import re
import unittest
from unittest.mock import patch, MagicMock

import streamlit as st
import app
from app import render_home_view, load_theme, resolve_symbol


class TestHomepageRedesign(unittest.TestCase):
    def setUp(self):
        st.session_state.clear()
        self.root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.theme_path = os.path.join(self.root, "ui", "theme.css")
        self.app_path = os.path.join(self.root, "app.py")
        self.mock_config = {
            "market": "NSE",
            "timezone": "Asia/Kolkata",
            "flat_by": "15:15"
        }

    # -------------------------------------------------------------------------
    # 1. render_home_view Execution
    # -------------------------------------------------------------------------
    def test_01_render_home_view_executes_cleanly(self):
        """Verify render_home_view renders without exceptions."""
        with patch.object(st, "button", return_value=False), \
             patch.object(st, "text_input", return_value=""), \
             patch.object(st, "selectbox", return_value="System"), \
             patch("app.render_html") as mock_html:

            render_home_view(is_sample_mode=True, config=self.mock_config)
            self.assertTrue(mock_html.called)

    # -------------------------------------------------------------------------
    # 2. Hero Section Copy and Typography Verification
    # -------------------------------------------------------------------------
    def test_02_hero_section_copy_exact_match(self):
        """Verify hero section copy matches the single source of truth."""
        rendered_html_chunks = []

        def capture_html(html_str, *args, **kwargs):
            rendered_html_chunks.append(str(html_str))

        with patch.object(st, "button", return_value=False), \
             patch.object(st, "text_input", return_value=""), \
             patch.object(st, "selectbox", return_value="System"), \
             patch("app.render_html", side_effect=capture_html):

            render_home_view(is_sample_mode=True, config=self.mock_config)

            combined_html = " ".join(rendered_html_chunks)
            self.assertIn("MARKET INTELLIGENCE PLATFORM", combined_html)
            self.assertIn("Read the Market.", combined_html)
            self.assertIn("Trade with", combined_html)
            self.assertIn("Conviction.", combined_html)
            self.assertIn("ti-accent-purple", combined_html)
            self.assertIn("Price action, risk and historical evidence — in one focused workflow.", combined_html)

    # -------------------------------------------------------------------------
    # 3. Market Pulse Content (NIFTY, BANK NIFTY, SENSEX)
    # -------------------------------------------------------------------------
    def test_03_market_pulse_indices_rendered(self):
        """Verify Today's Market Pulse renders NIFTY 50, BANK NIFTY, and SENSEX."""
        rendered_html_chunks = []

        def capture_html(html_str, *args, **kwargs):
            rendered_html_chunks.append(str(html_str))

        with patch.object(st, "button", return_value=False), \
             patch.object(st, "text_input", return_value=""), \
             patch.object(st, "selectbox", return_value="System"), \
             patch("app.render_html", side_effect=capture_html):

            render_home_view(is_sample_mode=True, config=self.mock_config)

            combined_html = " ".join(rendered_html_chunks)
            self.assertIn("Today's Market Pulse", combined_html)
            self.assertIn("NIFTY 50", combined_html)
            self.assertIn("BANK NIFTY", combined_html)
            self.assertIn("SENSEX", combined_html)

    # -------------------------------------------------------------------------
    # 4. Search and Analyze Trigger
    # -------------------------------------------------------------------------
    def test_04_search_button_triggers_analyze_navigation(self):
        """Verify submitting search switches current_page to Analyze now."""
        with patch.object(st, "button", side_effect=[True, False, False, False, False]), \
             patch.object(st, "text_input", return_value="TCS.NS"), \
             patch.object(st, "selectbox", return_value="System"), \
             patch("app.render_html"), \
             patch.object(st, "rerun") as mock_rerun:

            render_home_view(is_sample_mode=True, config=self.mock_config)

            self.assertEqual(st.session_state.get("active_symbol"), "TCS.NS")
            self.assertEqual(st.session_state.get("current_page"), "Analyze now")
            mock_rerun.assert_called_once()

    # -------------------------------------------------------------------------
    # 5. Core Feature Navigation Cards
    # -------------------------------------------------------------------------
    def test_05_feature_navigation_cards(self):
        """Verify Analyze Now, BIG BULL, and Time Machine cards are present."""
        rendered_html_chunks = []

        def capture_html(html_str, *args, **kwargs):
            rendered_html_chunks.append(str(html_str))

        with patch.object(st, "button", return_value=False), \
             patch.object(st, "text_input", return_value=""), \
             patch.object(st, "selectbox", return_value="System"), \
             patch("app.render_html", side_effect=capture_html):

            render_home_view(is_sample_mode=True, config=self.mock_config)

            combined_html = " ".join(rendered_html_chunks)
            self.assertIn("Analyze Now", combined_html)
            self.assertIn("Get a trade plan with clear entry, stop, target and risk.", combined_html)
            self.assertIn("BIG BULL", combined_html)
            self.assertIn("Discover high-potential opportunities across markets.", combined_html)
            self.assertIn("Time Machine", combined_html)
            self.assertIn("Replay the past, test your setup and see historical odds.", combined_html)

    # -------------------------------------------------------------------------
    # 6. Your Next Step Card Action
    # -------------------------------------------------------------------------
    def test_06_next_step_action_navigation(self):
        """Verify Your Next Step button switches page to Analyze now."""
        with patch.object(st, "button", side_effect=[False, False, False, False, True]), \
             patch.object(st, "text_input", return_value=""), \
             patch.object(st, "selectbox", return_value="System"), \
             patch("app.render_html"), \
             patch.object(st, "rerun") as mock_rerun:

            render_home_view(is_sample_mode=True, config=self.mock_config)

            self.assertEqual(st.session_state.get("current_page"), "Analyze now")
            mock_rerun.assert_called_once()

    # -------------------------------------------------------------------------
    # 7. How TradeInsta Helps Pillars
    # -------------------------------------------------------------------------
    def test_07_how_tradeinsta_helps_pillars(self):
        """Verify the 3 pillars are rendered with exact copy."""
        rendered_html_chunks = []

        def capture_html(html_str, *args, **kwargs):
            rendered_html_chunks.append(str(html_str))

        with patch.object(st, "button", return_value=False), \
             patch.object(st, "text_input", return_value=""), \
             patch.object(st, "selectbox", return_value="System"), \
             patch("app.render_html", side_effect=capture_html):

            render_home_view(is_sample_mode=True, config=self.mock_config)

            combined_html = " ".join(rendered_html_chunks)
            self.assertIn("How TradeInsta Helps", combined_html)
            self.assertIn("Rule-Based", combined_html)
            self.assertIn("Systematic, repeatable, no guesswork.", combined_html)
            self.assertIn("Risk First", combined_html)
            self.assertIn("Position size, max loss and account safety built-in.", combined_html)
            self.assertIn("Evidence Driven", combined_html)
            self.assertIn("Historical replay and transparent assumptions.", combined_html)

    # -------------------------------------------------------------------------
    # 8. Theme Switcher Engine
    # -------------------------------------------------------------------------
    def test_08_theme_switcher_modes(self):
        """Verify Light, Dark, and System modes inject appropriate script."""
        rendered_html_chunks = []

        def capture_html(html_str, *args, **kwargs):
            rendered_html_chunks.append(str(html_str))

        # Test Dark mode
        st.session_state["theme_mode"] = "Dark"
        with patch("app.render_html", side_effect=capture_html):
            load_theme()
            dark_script = " ".join(rendered_html_chunks)
            self.assertIn("doc.setAttribute('data-theme', 'dark')", dark_script)

        rendered_html_chunks.clear()

        # Test Light mode
        st.session_state["theme_mode"] = "Light"
        with patch("app.render_html", side_effect=capture_html):
            load_theme()
            light_script = " ".join(rendered_html_chunks)
            self.assertIn("doc.setAttribute('data-theme', 'light')", light_script)

        rendered_html_chunks.clear()

        # Test System mode
        st.session_state["theme_mode"] = "System"
        with patch("app.render_html", side_effect=capture_html):
            load_theme()
            sys_script = " ".join(rendered_html_chunks)
            self.assertIn("prefers-color-scheme: dark", sys_script)

    # -------------------------------------------------------------------------
    # 9. Design Tokens & CSS Classes
    # -------------------------------------------------------------------------
    def test_09_css_design_tokens_and_responsive_rules(self):
        """Verify theme.css contains all required design tokens and classes."""
        self.assertTrue(os.path.exists(self.theme_path))
        with open(self.theme_path, "r", encoding="utf-8") as f:
            css = f.read()

        # Core colors from Section 2
        self.assertIn("#050505", css)
        self.assertIn("#080808", css)
        self.assertIn("#101013", css)
        self.assertIn("#17171B", css)
        self.assertIn("#303036", css)
        self.assertIn("#F5F5F7", css)
        self.assertIn("#9A9AA3", css)
        self.assertIn("#9955E8", css)
        self.assertIn("#AD78F4", css)
        self.assertIn("#36C98A", css)
        self.assertIn("#EF626B", css)

        # Light theme colors
        self.assertIn("#FAFAFB", css)
        self.assertIn("#E4E4E9", css)
        self.assertIn("#7D42D4", css)

        # Fonts
        self.assertIn("Space Grotesk", css)
        self.assertIn("Inter", css)
        self.assertIn("IBM Plex Mono", css)

        # Classes
        self.assertIn(".ti-hero", css)
        self.assertIn(".ti-accent-purple", css)
        self.assertIn(".ti-feature-card", css)
        self.assertIn(".ti-pulse-card", css)
        self.assertIn(".ti-bottom-banner", css)

        # Responsive queries
        self.assertIn("@media (max-width: 1024px)", css)
        self.assertIn("@media (max-width: 768px)", css)
        self.assertIn("@media (max-width: 480px)", css)

    # -------------------------------------------------------------------------
    # 10. No Emojis in Codebase
    # -------------------------------------------------------------------------
    def test_10_no_emojis_in_app_code(self):
        """Verify strict absence of Unicode emojis in app.py."""
        with open(self.app_path, "r", encoding="utf-8") as f:
            app_code = f.read()

        emoji_pattern = re.compile(r"[\U00010000-\U0010ffff]", flags=re.UNICODE)
        matches = set(emoji_pattern.findall(app_code))
        self.assertEqual(len(matches), 0, f"Found emojis in app.py: {matches}")


if __name__ == "__main__":
    unittest.main()

