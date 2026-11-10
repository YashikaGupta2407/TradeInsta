---
name: trader-flow
description: >-
  Institutional Flow Monitor. Analyzes NSE bulk and block deal CSV files downloaded
  into trader/flow/ to surface major institutional, mutual fund, and promoter transactions.
  Shows for any symbol the latest bulk/block deals (date, client name, buy/sell action,
  quantity, trade price, value in INR, % shares outstanding), ranks top stocks by deal
  size across the market, and computes volume vs 20-day average. Purely standard library,
  read-only; does not scrape nseindia.com or bypass any site protections. Use when the
  user says "check flow for RELIANCE", "any bulk deals today", "institutional flow",
  "block deals", "who bought or sold", or to inspect big money activity behind scanner setups.
argument-hint: "[symbol] [--dir trader/flow] [--limit 10]"
---

# trader-flow

Institutional Flow Monitor parses user-downloaded NSE bulk and block deal CSV reports from `trader/flow/`.

> **Note**: Information only, never an order or recommendation. Bulk and block deals reflect previous day / completed institutional filings.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this `SKILL.md` lives).

```bash
# Show bulk and block deals for a symbol
python3 "${CLAUDE_SKILL_DIR}/flow.py" symbol RELIANCE.NS

# Rank top stocks by deal volume across all downloaded CSVs
python3 "${CLAUDE_SKILL_DIR}/flow.py" top --limit 10

# Filter specifically by block deals or bulk deals
python3 "${CLAUDE_SKILL_DIR}/flow.py" top --type block
python3 "${CLAUDE_SKILL_DIR}/flow.py" top --type bulk

# Overall dataset summary
python3 "${CLAUDE_SKILL_DIR}/flow.py" summary

# Parse from custom path or output JSON
python3 "${CLAUDE_SKILL_DIR}/flow.py" --dir path/to/csvs symbol TCS.NS --json
```

## What it reads and writes

| File / Folder | Role |
| :--- | :--- |
| `trader/flow/*.csv` | User-downloaded NSE bulk and block deal CSV files |
| `trader/flow/` | Default directory checked for CSV files |

## CSV Headers Supported
Compatible with official NSE bulk and block deal reports:
- `Date` / `Deal Date`
- `Symbol` / `Security Symbol`
- `Security Name` / `Company Name`
- `Client Name` / `Client`
- `Buy / Sell` / `Deal Type` / `Trade Type`
- `Quantity Traded` / `Quantity`
- `Trade Price / Wght. Avg. Price` / `Price`
- `% of Total Shares` / `Shares Outstanding`

