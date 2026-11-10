#!/usr/bin/env python3
"""Institutional Flow Monitor for NSE Bulk and Block Deals.

Standard library only. Read-only.
Parses user-downloaded NSE bulk and block deal CSV files from trader/flow/.
Does not scrape nseindia.com or make network requests.
Information only, not a trade recommendation.
"""

import argparse
import csv
import datetime
import glob
import json
import math
import os
import re
import sys

DEFAULT_FLOW_DIR = "trader/flow"
DISCLAIMER = "Information only, not a trade recommendation. Reflects reported institutional/promoter bulk and block filings."


def _clean_str(val):
    if val is None:
        return ""
    return str(val).strip()


def _clean_num(val, default=0.0):
    if val is None:
        return default
    s = str(val).strip().replace(",", "").replace("₹", "").replace("$", "").replace("%", "")
    if not s or s == "-" or s.lower() == "na" or s.lower() == "nan":
        return default
    try:
        return float(s)
    except ValueError:
        return default


def _clean_symbol(sym):
    s = _clean_str(sym).upper()
    if s.endswith(".NS"):
        s = s[:-3]
    elif s.endswith(".BO"):
        s = s[:-3]
    return s


def _infer_deal_type(file_path, row):
    # Check explicit column first
    for k in row:
        kl = k.lower()
        if "deal" in kl and "type" in kl:
            v = str(row[k]).strip().upper()
            if "BLOCK" in v:
                return "BLOCK"
            if "BULK" in v:
                return "BULK"

    # Check filename
    fn = os.path.basename(file_path).lower() if file_path else ""
    if "block" in fn:
        return "BLOCK"
    if "bulk" in fn:
        return "BULK"
    return "BULK"


def _normalize_row_keys(row):
    """Map arbitrary header case/spacing variations to canonical keys."""
    normalized = {}
    for k, v in row.items():
        if not k:
            continue
        clean_k = re.sub(r"[_\s/-]+", " ", k.strip().lower())
        normalized[clean_k] = v
    return normalized


def _get_field(norm_row, candidate_patterns, default=""):
    for cand in candidate_patterns:
        for k, v in norm_row.items():
            if cand in k:
                return v
    return default


def parse_flow_csv(file_path):
    """Parse an NSE bulk or block deal CSV file into structured deal dicts."""
    if not os.path.exists(file_path):
        return []

    deals = []
    deal_type_default = _infer_deal_type(file_path, {})

    with open(file_path, "r", encoding="utf-8-sig", errors="replace") as fh:
        # Detect delimiter (comma or tab)
        sample = fh.read(2048)
        fh.seek(0)
        delimiter = "," if "," in sample else "\t"
        reader = csv.DictReader(fh, delimiter=delimiter)

        for row_idx, raw_row in enumerate(reader):
            if not raw_row or not any(raw_row.values()):
                continue

            norm = _normalize_row_keys(raw_row)

            # Date
            raw_date = _get_field(norm, ["date", "deal date"], "")
            clean_date = _clean_str(raw_date)

            # Symbol
            raw_sym = _get_field(norm, ["symbol", "security symbol", "scrip"], "")
            symbol = _clean_symbol(raw_sym)
            if not symbol:
                continue

            # Security Name
            security_name = _clean_str(_get_field(norm, ["security name", "company", "name"], symbol))

            # Client Name
            client_name = _clean_str(_get_field(norm, ["client name", "client", "acquirer", "seller"], "N/A"))

            # Buy / Sell
            raw_bs = _clean_str(_get_field(norm, ["buy sell", "deal type", "type", "trade type"], "BUY")).upper()
            action = "BUY"
            if "SELL" in raw_bs or "S" == raw_bs or "SALE" in raw_bs:
                action = "SELL"
            elif "BUY" in raw_bs or "B" == raw_bs or "PURCHASE" in raw_bs:
                action = "BUY"

            # Quantity
            raw_qty = _get_field(norm, ["quantity", "qty", "shares"], "0")
            quantity = int(round(_clean_num(raw_qty, 0.0)))

            # Trade Price
            raw_price = _get_field(norm, ["trade price", "wght avg", "price", "rate"], "0.0")
            price = round(_clean_num(raw_price, 0.0), 2)

            # % of Total Shares
            raw_pct = _get_field(norm, ["total shares", "pct of shares", "shares outstanding", "stake"], "")
            pct_shares = _clean_num(raw_pct, None) if raw_pct else None

            # Total Value in INR
            total_value = round(quantity * price, 2)

            deal_type = _infer_deal_type(file_path, raw_row) or deal_type_default

            deals.append({
                "date": clean_date,
                "symbol": symbol,
                "symbol_ns": f"{symbol}.NS",
                "security_name": security_name,
                "client_name": client_name,
                "deal_type": deal_type,
                "action": action,
                "quantity": quantity,
                "price": price,
                "value_inr": total_value,
                "pct_shares": pct_shares,
                "source_file": os.path.basename(file_path)
            })

    return deals


def load_flow_deals(flow_dir=DEFAULT_FLOW_DIR, file_path=None):
    """Load and merge all CSV deal files from a directory or specific file."""
    if file_path:
        return parse_flow_csv(file_path)

    if not os.path.exists(flow_dir):
        return []

    csv_files = sorted(glob.glob(os.path.join(flow_dir, "*.csv")))
    all_deals = []
    seen = set()

    for cf in csv_files:
        file_deals = parse_flow_csv(cf)
        for d in file_deals:
            # Deduplicate exact duplicate rows across files
            key = (d["date"], d["symbol"], d["client_name"], d["action"], d["quantity"], d["price"])
            if key not in seen:
                seen.add(key)
                all_deals.append(d)

    return all_deals


def get_deals_for_symbol(deals, symbol):
    """Get all bulk/block deals for a specific symbol (case-insensitive, handles .NS)."""
    clean = _clean_symbol(symbol)
    matched = [d for d in deals if d["symbol"] == clean]
    # Sort newest first, then largest deal value
    matched.sort(key=lambda x: (x["date"], x["value_inr"]), reverse=True)
    return matched


def get_symbol_flow_summary(deals, symbol):
    """Aggregate flow metrics (buy value, sell value, net value) for a symbol."""
    sym_deals = get_deals_for_symbol(deals, symbol)
    clean = _clean_symbol(symbol)

    buy_qty = sum(d["quantity"] for d in sym_deals if d["action"] == "BUY")
    sell_qty = sum(d["quantity"] for d in sym_deals if d["action"] == "SELL")
    buy_val = sum(d["value_inr"] for d in sym_deals if d["action"] == "BUY")
    sell_val = sum(d["value_inr"] for d in sym_deals if d["action"] == "SELL")

    return {
        "symbol": clean,
        "symbol_ns": f"{clean}.NS",
        "total_deals": len(sym_deals),
        "buy_deals": len([d for d in sym_deals if d["action"] == "BUY"]),
        "sell_deals": len([d for d in sym_deals if d["action"] == "SELL"]),
        "buy_quantity": buy_qty,
        "sell_quantity": sell_qty,
        "buy_value_inr": buy_val,
        "sell_value_inr": sell_val,
        "net_value_inr": round(buy_val - sell_val, 2),
        "net_action": "NET_BUY" if buy_val > sell_val else ("NET_SELL" if sell_val > buy_val else "NEUTRAL"),
        "deals": sym_deals
    }


def get_top_stocks_by_deal_size(deals, top_n=10, deal_type=None):
    """Aggregate and rank stocks by total deal turnover value (Buy + Sell)."""
    filtered = deals
    if deal_type:
        dt_up = deal_type.upper()
        filtered = [d for d in deals if d["deal_type"] == dt_up]

    by_sym = {}
    for d in filtered:
        sym = d["symbol"]
        if sym not in by_sym:
            by_sym[sym] = {
                "symbol": sym,
                "symbol_ns": f"{sym}.NS",
                "security_name": d["security_name"],
                "total_deals": 0,
                "buy_value_inr": 0.0,
                "sell_value_inr": 0.0,
                "total_value_inr": 0.0,
                "net_value_inr": 0.0
            }
        by_sym[sym]["total_deals"] += 1
        val = d["value_inr"]
        by_sym[sym]["total_value_inr"] += val
        if d["action"] == "BUY":
            by_sym[sym]["buy_value_inr"] += val
        else:
            by_sym[sym]["sell_value_inr"] += val

    for sym, rec in by_sym.items():
        rec["net_value_inr"] = round(rec["buy_value_inr"] - rec["sell_value_inr"], 2)
        rec["total_value_inr"] = round(rec["total_value_inr"], 2)
        rec["buy_value_inr"] = round(rec["buy_value_inr"], 2)
        rec["sell_value_inr"] = round(rec["sell_value_inr"], 2)

    ranked = sorted(by_sym.values(), key=lambda x: x["total_value_inr"], reverse=True)
    return ranked[:top_n]


def compute_volume_vs_20d_avg(symbol, series=None, daily_volumes=None):
    """Calculate volume vs 20-day moving average from bar series or list of daily volumes.

    Returns dict with latest_volume, avg_20d_volume, ratio, pct_diff, and display label.
    """
    vols = []
    if daily_volumes:
        vols = [float(v) for v in daily_volumes if v is not None and v > 0]
    elif series and "v" in series:
        vols = [float(v) for v in series["v"] if v is not None and v > 0]

    if len(vols) < 2:
        return {
            "symbol": _clean_symbol(symbol),
            "latest_volume": int(vols[-1]) if vols else None,
            "avg_20d_volume": None,
            "ratio": None,
            "pct_diff": None,
            "label": "Insufficient volume history (<2 bars)"
        }

    latest = vols[-1]
    # Use previous up to 20 daily bars for reference average
    lookback = vols[-21:-1] if len(vols) > 20 else vols[:-1]
    avg_20d = sum(lookback) / float(len(lookback)) if lookback else latest

    ratio = round(latest / avg_20d, 2) if avg_20d > 0 else 1.0
    pct_diff = round((ratio - 1.0) * 100, 1)

    sign = "+" if pct_diff >= 0 else ""
    label = f"{ratio:.2f}x ({sign}{pct_diff:.1f}% vs 20d avg)"

    return {
        "symbol": _clean_symbol(symbol),
        "latest_volume": int(latest),
        "avg_20d_volume": round(avg_20d, 1),
        "ratio": ratio,
        "pct_diff": pct_diff,
        "label": label
    }


def format_inr(value):
    """Format large INR numbers into Crores or Lakhs."""
    if value is None:
        return "₹0.00"
    abs_v = abs(value)
    sign = "-" if value < 0 else ""
    if abs_v >= 10000000:
        return f"{sign}₹{abs_v / 10000000:,.2f} Cr"
    elif abs_v >= 100000:
        return f"{sign}₹{abs_v / 100000:,.2f} L"
    return f"{sign}₹{abs_v:,.2f}"


# ---------------------------------------------------------------- CLI Interface

def cmd_symbol(args):
    deals = load_flow_deals(args.dir, args.file)
    summary = get_symbol_flow_summary(deals, args.symbol)

    if args.json:
        print(json.dumps(summary, indent=2))
        return 0

    print(f"=== Institutional Flow for {summary['symbol_ns']} ===")
    print(f"Total Deals: {summary['total_deals']} (Buys: {summary['buy_deals']}, Sells: {summary['sell_deals']})")
    print(f"Total Buy Value:  {format_inr(summary['buy_value_inr'])} ({summary['buy_quantity']:,} shares)")
    print(f"Total Sell Value: {format_inr(summary['sell_value_inr'])} ({summary['sell_quantity']:,} shares)")
    print(f"Net Flow:         {format_inr(summary['net_value_inr'])} ({summary['net_action']})")
    print()

    if summary["deals"]:
        print("Latest Deals:")
        for d in summary["deals"][:args.limit]:
            pct_str = f" ({d['pct_shares']}% stake)" if d["pct_shares"] is not None else ""
            print(f"  {d['date']} | {d['deal_type']} {d['action']:4s} | {d['quantity']:,} shs @ ₹{d['price']:,.2f} | {format_inr(d['value_inr'])}{pct_str} | {d['client_name']}")
    else:
        print(f"No bulk or block deals found for {summary['symbol_ns']} in {args.dir or args.file or DEFAULT_FLOW_DIR}.")

    print()
    print(DISCLAIMER)
    return 0


def cmd_top(args):
    deals = load_flow_deals(args.dir, args.file)
    ranked = get_top_stocks_by_deal_size(deals, top_n=args.limit, deal_type=args.type)

    if args.json:
        print(json.dumps(ranked, indent=2))
        return 0

    dt_label = f"({args.type.upper()})" if args.type else "(Bulk & Block)"
    print(f"=== Top Stocks by Deal Size {dt_label} ===")
    if not ranked:
        print(f"No deal data found in {args.dir or args.file or DEFAULT_FLOW_DIR}.")
        return 0

    print(f"{'Symbol':<12} {'Deals':<7} {'Total Value':<16} {'Buy Value':<16} {'Sell Value':<16} {'Net Flow':<16}")
    print("-" * 85)
    for r in ranked:
        print(f"{r['symbol']:<12} {r['total_deals']:<7} {format_inr(r['total_value_inr']):<16} {format_inr(r['buy_value_inr']):<16} {format_inr(r['sell_value_inr']):<16} {format_inr(r['net_value_inr']):<16}")

    print()
    print(DISCLAIMER)
    return 0


def cmd_summary(args):
    deals = load_flow_deals(args.dir, args.file)
    unique_symbols = len(set(d["symbol"] for d in deals))
    total_val = sum(d["value_inr"] for d in deals)
    buy_val = sum(d["value_inr"] for d in deals if d["action"] == "BUY")
    sell_val = sum(d["value_inr"] for d in deals if d["action"] == "SELL")

    doc = {
        "total_deals": len(deals),
        "unique_symbols": unique_symbols,
        "total_turnover_inr": total_val,
        "total_buy_inr": buy_val,
        "total_sell_inr": sell_val,
        "disclaimer": DISCLAIMER
    }

    if args.json:
        print(json.dumps(doc, indent=2))
        return 0

    print(f"=== Flow Dataset Summary ===")
    print(f"Total Deals Recorded: {len(deals):,}")
    print(f"Unique Symbols:       {unique_symbols:,}")
    print(f"Total Turnover:       {format_inr(total_val)}")
    print(f"Total Buy Value:      {format_inr(buy_val)}")
    print(f"Total Sell Value:     {format_inr(sell_val)}")
    print()
    print(DISCLAIMER)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Institutional Flow Analysis for NSE Bulk and Block Deals.")
    parser.add_argument("--dir", default=DEFAULT_FLOW_DIR, help=f"Directory with CSV files (default: {DEFAULT_FLOW_DIR})")
    parser.add_argument("--file", help="Specific CSV file to parse")
    parser.add_argument("--json", action="store_true", help="Output JSON")

    sub = parser.add_subparsers(dest="command")

    # symbol
    p_sym = sub.add_parser("symbol", help="Show deals for a specific stock")
    p_sym.add_argument("symbol", help="NSE symbol (e.g. RELIANCE, TCS.NS)")
    p_sym.add_argument("--limit", type=int, default=15, help="Max deals to show (default: 15)")

    # top
    p_top = sub.add_parser("top", help="List top stocks by deal size")
    p_top.add_argument("--limit", type=int, default=10, help="Number of stocks to rank (default: 10)")
    p_top.add_argument("--type", choices=["bulk", "block"], help="Filter by deal type")

    # summary
    sub.add_parser("summary", help="Show overall dataset statistics")

    args = parser.parse_args(argv)

    if args.command == "symbol":
        return cmd_symbol(args)
    elif args.command == "top":
        return cmd_top(args)
    elif args.command == "summary":
        return cmd_summary(args)
    else:
        parser.print_help()
        return 0


if __name__ == "__main__":
    sys.exit(main())
