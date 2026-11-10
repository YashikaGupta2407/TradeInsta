import json
import os
import re
import unittest

from _load import ROOT

SKILLS_DIR = os.path.join(ROOT, ".agents", "skills") if os.path.isdir(os.path.join(ROOT, ".agents", "skills")) else ROOT
SKILLS = [
    "trader-scanner", "trader-news", "trader-entry", "trader-exit",
    "trader-timemachine", "trader-risk", "trader-budget", "trader-alerts",
    "trader-flow"
]
REMOVED_SKILLS = ["trader-broker", "trader-pelosi", "trader-analyst"]
V1_ONLY = ["trader-copycat", "trader-screener", "trader-tax", "trader-rebalance"]


def frontmatter(path):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    return (m.group(1) if m else ""), text


def every_text_file():
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
        for f in files:
            if f.endswith((".md", ".py", ".json", ".csv", ".txt", ".xml")) or f == "LICENSE":
                yield os.path.join(base, f)


class Repo(unittest.TestCase):
    def test_eight_skill_folders_present(self):
        found = sorted(d for d in os.listdir(SKILLS_DIR)
                       if os.path.isfile(os.path.join(SKILLS_DIR, d, "SKILL.md")))
        self.assertEqual(found, sorted(SKILLS))

    def test_removed_skills_are_absent(self):
        for s in REMOVED_SKILLS:
            self.assertFalse(
                os.path.exists(os.path.join(SKILLS_DIR, s)),
                "%s should be removed per Indian market adaptation" % s
            )

    def test_frontmatter_name_and_description(self):
        for s in SKILLS:
            fm, text = frontmatter(os.path.join(SKILLS_DIR, s, "SKILL.md"))
            self.assertIn("name: %s\n" % s, fm + "\n", s)
            self.assertRegex(fm, r"description: ", s)
            self.assertGreater(len(fm), 100, "%s description is too thin" % s)
            self.assertGreater(len(text.splitlines()), 20, s)

    def test_every_tool_runs_on_python_3_8(self):
        # no 3.9+ only modules or syntax in the tools (3.8 compatibility)
        for s in SKILLS:
            folder = os.path.join(SKILLS_DIR, s)
            for f in os.listdir(folder):
                if not f.endswith(".py"):
                    continue
                with open(os.path.join(folder, f), encoding="utf-8") as fh:
                    src = fh.read()
                for bad in ("import zoneinfo", "from zoneinfo", ".removeprefix(", ".removesuffix(",
                            "match ", "list[", "dict[", "tuple["):
                    if bad == "match ":
                        self.assertNotRegex(src, r"(?m)^\s+match \w+.*:$", "%s/%s" % (s, f))
                    elif bad.endswith("["):
                        self.assertNotRegex(src, r"(?<![\w.])%s" % re.escape(bad), "%s/%s" % (s, f))
                    else:
                        self.assertNotIn(bad, src, "%s/%s" % (s, f))

    def test_indian_trading_limits_and_charges_in_config(self):
        cfg_paths = [
            os.path.join(SKILLS_DIR, "trader-budget", "config.example.json"),
            os.path.join(ROOT, "trader", "config.json")
        ]
        for path in cfg_paths:
            if not os.path.exists(path):
                continue
            with open(path, encoding="utf-8") as fh:
                cfg = json.load(fh)
            self.assertEqual(cfg.get("market"), "NSE")
            self.assertEqual(cfg.get("timezone"), "Asia/Kolkata")
            self.assertEqual(cfg.get("tick_size"), 0.05)
            self.assertIn("max_loss_per_trade_inr", cfg)
            self.assertIn("max_daily_loss_inr", cfg)
            self.assertIn("min_price", cfg)
            self.assertIn("min_volume", cfg)
            self.assertIn("charges", cfg)
            charges = cfg["charges"]
            for fee in ("brokerage_per_order", "brokerage_pct", "stt_pct",
                        "exchange_turnover_fee_pct", "sebi_turnover_fee_pct",
                        "gst_pct", "stamp_duty_pct"):
                self.assertIn(fee, charges, "%s missing from charges in %s" % (fee, path))

    def test_nse_holiday_calendar_coverage(self):
        hol_paths = [
            os.path.join(ROOT, "trader", "holidays.json"),
            os.path.join(SKILLS_DIR, "trader-alerts", "holidays.json")
        ]
        for path in hol_paths:
            self.assertTrue(os.path.exists(path), "%s must exist" % path)
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            hols = data.get("holidays", data)
            years = {d[:4] for d in hols.keys()}
            self.assertIn("2025", years)
            self.assertIn("2026", years)
            self.assertIn("2027", years)

    def test_no_v1_only_skill_is_left_behind(self):
        for path in every_text_file():
            rel = os.path.relpath(path, ROOT)
            if rel.startswith("tests" + os.sep) or rel == "README.md":
                continue
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            for old in V1_ONLY:
                self.assertNotIn(old, text, rel)


if __name__ == "__main__":
    unittest.main()
