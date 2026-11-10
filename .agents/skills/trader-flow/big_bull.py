#!/usr/bin/env python3
"""BIG BULL — Institutional Buying Intelligence & Sector Flow Analysis Engine.

Standard library only. Read-only.
Extends trader-flow to surface institutional accumulation patterns, sector-wide capital
flow, multi-session repeated buying, volume confirmation, and a transparent watchlist scoring model.
Information only, not a trade recommendation.
"""

import datetime
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
DEFAULT_SECTORS_FILE = os.path.join(ROOT, "trader", "sectors.json")

DISCLAIMER = (
    "DISCLAIMER: BIG BULL institutional intelligence is provided strictly for market research "
    "and analysis. Disclosed bulk/block deals represent completed, retrospective transactions. "
    "Not an investment recommendation, trade signal, or solicitation."
)


def load_sector_registry(sectors_path=None):
    """Load symbol-to-sector mapping and keyword heuristic rules from trader/sectors.json."""
    path = sectors_path or DEFAULT_SECTORS_FILE
    if not os.path.exists(path):
        # Fallback to local skills relative path
        alt_path = os.path.join(os.path.dirname(__file__), "..", "..", "..", "trader", "sectors.json")
        if os.path.exists(alt_path):
            path = alt_path

    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return data
        except Exception:
            pass

    return {
        "sectors": [
            "Banking & Financial Services", "IT & Software", "Pharmaceuticals & Healthcare",
            "Automobile", "Energy & Power", "FMCG", "Metals & Mining", "Infrastructure",
            "Realty", "Chemicals", "Telecom", "Consumer Durables", "Diversified / Others"
        ],
        "symbol_map": {},
        "keyword_rules": []
    }


def get_stock_sector(symbol, company_name="", registry=None):
    """Classify an NSE stock into its designated sector using symbol map or name heuristics."""
    reg = registry or load_sector_registry()
    sym = str(symbol or "").upper().strip()
    if sym.endswith(".NS"):
        sym = sym[:-3]
    elif sym.endswith(".BO"):
        sym = sym[:-3]

    symbol_map = reg.get("symbol_map", {})
    if sym in symbol_map:
        return symbol_map[sym]

    # Keyword heuristics on company name
    name_upper = str(company_name or "").upper()
    for rule in reg.get("keyword_rules", []):
        sec = rule.get("sector")
        keywords = rule.get("keywords", [])
        for kw in keywords:
            if re.search(r"\b" + re.escape(kw) + r"\b", name_upper) or kw in name_upper:
                return sec

    return "Diversified / Others"


def compute_market_flow_kpis(deals):
    """Calculate aggregate market-wide institutional buying vs. selling metrics."""
    if not deals:
        return {
            "total_buy_value_inr": 0.0,
            "total_sell_value_inr": 0.0,
            "net_flow_inr": 0.0,
            "total_turnover_inr": 0.0,
            "total_buy_deals": 0,
            "total_sell_deals": 0,
            "total_deals": 0,
            "net_positive_stocks_count": 0,
            "net_negative_stocks_count": 0,
            "neutral_stocks_count": 0,
            "total_stocks_count": 0,
            "date_range": {"earliest": None, "latest": None}
        }

    buy_val = sum(d["value_inr"] for d in deals if d["action"] == "BUY")
    sell_val = sum(d["value_inr"] for d in deals if d["action"] == "SELL")
    net_val = round(buy_val - sell_val, 2)
    turnover = round(buy_val + sell_val, 2)

    buy_deals = len([d for d in deals if d["action"] == "BUY"])
    sell_deals = len([d for d in deals if d["action"] == "SELL"])

    # Stock net flows
    stock_net = {}
    for d in deals:
        sym = d["symbol"]
        val = d["value_inr"]
        stock_net.setdefault(sym, 0.0)
        stock_net[sym] += val if d["action"] == "BUY" else -val

    net_pos = sum(1 for v in stock_net.values() if v > 0)
    net_neg = sum(1 for v in stock_net.values() if v < 0)
    neutral = sum(1 for v in stock_net.values() if v == 0)

    dates = [d["date"] for d in deals if d.get("date")]
    earliest = min(dates) if dates else None
    latest = max(dates) if dates else None

    return {
        "total_buy_value_inr": round(buy_val, 2),
        "total_sell_value_inr": round(sell_val, 2),
        "net_flow_inr": net_val,
        "total_turnover_inr": turnover,
        "total_buy_deals": buy_deals,
        "total_sell_deals": sell_deals,
        "total_deals": len(deals),
        "net_positive_stocks_count": net_pos,
        "net_negative_stocks_count": net_neg,
        "neutral_stocks_count": neutral,
        "total_stocks_count": len(stock_net),
        "date_range": {"earliest": earliest, "latest": latest}
    }


def compute_sector_flow(deals, sector_registry=None):
    """Aggregate disclosed institutional buying, selling, and net capital flow sector-wise."""
    reg = sector_registry or load_sector_registry()
    sectors = reg.get("sectors", [])

    by_sector = {
        s: {
            "sector": s,
            "buy_value_inr": 0.0,
            "sell_value_inr": 0.0,
            "net_flow_inr": 0.0,
            "turnover_inr": 0.0,
            "total_deals": 0,
            "buy_deals": 0,
            "sell_deals": 0,
            "stocks_with_buying": set(),
            "stocks_with_selling": set(),
            "all_stocks": set(),
            "stock_net_map": {}
        }
        for s in sectors
    }

    for d in deals:
        sec = get_stock_sector(d["symbol"], d.get("security_name", ""), reg)
        if sec not in by_sector:
            sec = "Diversified / Others"

        item = by_sector[sec]
        val = d["value_inr"]
        sym = d["symbol"]
        item["total_deals"] += 1
        item["turnover_inr"] += val
        item["all_stocks"].add(sym)
        item["stock_net_map"].setdefault(sym, 0.0)

        if d["action"] == "BUY":
            item["buy_value_inr"] += val
            item["buy_deals"] += 1
            item["stocks_with_buying"].add(sym)
            item["stock_net_map"][sym] += val
        else:
            item["sell_value_inr"] += val
            item["sell_deals"] += 1
            item["stocks_with_selling"].add(sym)
            item["stock_net_map"][sym] -= val

    result = []
    for s, item in by_sector.items():
        if item["total_deals"] == 0:
            continue

        buy_v = round(item["buy_value_inr"], 2)
        sell_v = round(item["sell_value_inr"], 2)
        net_v = round(buy_v - sell_v, 2)
        turnover = round(item["turnover_inr"], 2)
        buy_share = (buy_v / turnover) if turnover > 0 else 0.5

        # Classify status
        if net_v > 0 and buy_share >= 0.65:
            status = "Strong Net Buying"
        elif net_v > 0:
            status = "Positive Buying"
        elif net_v < 0 and (1.0 - buy_share) >= 0.65:
            status = "Strong Net Selling"
        elif net_v < 0:
            status = "Net Selling"
        else:
            status = "Balanced Flow"

        # Top stocks by net buying in this sector
        ranked_stocks = sorted(
            item["stock_net_map"].items(),
            key=lambda x: x[1],
            reverse=True
        )
        top_stocks = [{"symbol": sym, "net_inr": round(val, 2)} for sym, val in ranked_stocks if val != 0][:5]

        result.append({
            "sector": s,
            "buy_value_inr": buy_v,
            "sell_value_inr": sell_v,
            "net_flow_inr": net_v,
            "turnover_inr": turnover,
            "buy_share_pct": round(buy_share * 100, 1),
            "total_deals": item["total_deals"],
            "buy_deals": item["buy_deals"],
            "sell_deals": item["sell_deals"],
            "stocks_with_buying": len(item["stocks_with_buying"]),
            "stocks_with_selling": len(item["stocks_with_selling"]),
            "total_stocks": len(item["all_stocks"]),
            "top_stocks": top_stocks,
            "status": status
        })

    # Sort sectors by Net Buying Value (highest net buyers first)
    result.sort(key=lambda x: x["net_flow_inr"], reverse=True)
    return result


def analyze_stock_accumulation(deals, sector_registry=None):
    """Identify stocks being accumulated and detect smart money patterns."""
    reg = sector_registry or load_sector_registry()
    by_stock = {}

    for d in deals:
        sym = d["symbol"]
        if sym not in by_stock:
            sec = get_stock_sector(sym, d.get("security_name", ""), reg)
            by_stock[sym] = {
                "symbol": sym,
                "symbol_ns": f"{sym}.NS",
                "security_name": d.get("security_name", sym),
                "sector": sec,
                "buy_value_inr": 0.0,
                "sell_value_inr": 0.0,
                "buy_quantity": 0,
                "sell_quantity": 0,
                "buy_deals": 0,
                "sell_deals": 0,
                "total_deals": 0,
                "buyer_clients": {},   # client -> list of dates
                "seller_clients": {},  # client -> list of dates
                "dates": set(),
                "deal_records": []
            }

        rec = by_stock[sym]
        rec["total_deals"] += 1
        rec["dates"].add(d["date"])
        rec["deal_records"].append(d)
        val = d["value_inr"]
        qty = d["quantity"]
        client = d.get("client_name", "Unknown")

        if d["action"] == "BUY":
            rec["buy_value_inr"] += val
            rec["buy_quantity"] += qty
            rec["buy_deals"] += 1
            rec["buyer_clients"].setdefault(client, []).append(d["date"])
        else:
            rec["sell_value_inr"] += val
            rec["sell_quantity"] += qty
            rec["sell_deals"] += 1
            rec["seller_clients"].setdefault(client, []).append(d["date"])

    analyzed = []
    for sym, rec in by_stock.items():
        buy_v = round(rec["buy_value_inr"], 2)
        sell_v = round(rec["sell_value_inr"], 2)
        net_v = round(buy_v - sell_v, 2)
        turnover = round(buy_v + sell_v, 2)

        distinct_buyers = len(rec["buyer_clients"])
        distinct_sellers = len(rec["seller_clients"])

        # Detect repeated buying across distinct dates
        repeated_buyers = [
            client for client, dates in rec["buyer_clients"].items()
            if len(set(dates)) >= 2
        ]
        has_repeated_buying = len(repeated_buyers) > 0

        # Detect repeated selling across distinct dates
        repeated_sellers = [
            client for client, dates in rec["seller_clients"].items()
            if len(set(dates)) >= 2
        ]
        has_repeated_selling = len(repeated_sellers) > 0

        has_multiple_buyers = distinct_buyers >= 2
        buy_ratio = (buy_v / turnover) if turnover > 0 else 0.0
        is_strong_net_buying = net_v > 0 and buy_ratio >= 0.75
        is_mixed_flow = buy_v > 0 and sell_v > 0 and (buy_v / turnover >= 0.35) and (sell_v / turnover >= 0.35)

        # Pattern tags
        patterns = []
        if has_repeated_buying:
            patterns.append("REPEATED BUYING")
        if has_multiple_buyers:
            patterns.append("MULTIPLE BUYERS")
        if is_strong_net_buying:
            patterns.append("STRONG NET BUYING")
        if has_repeated_selling:
            patterns.append("REPEATED SELLING")
        if is_mixed_flow:
            patterns.append("MIXED FLOW")

        # Primary trend label
        if has_repeated_buying and has_multiple_buyers:
            trend = "Institutional Accumulation"
        elif is_strong_net_buying:
            trend = "Strong Net Buying"
        elif has_multiple_buyers:
            trend = "Multiple Buyers"
        elif has_repeated_buying:
            trend = "Repeated Buyer"
        elif is_mixed_flow:
            trend = "Mixed / Transfer Flow"
        elif net_v > 0:
            trend = "Net Positive"
        elif has_repeated_selling:
            trend = "Repeated Selling"
        elif net_v < 0:
            trend = "Net Selling"
        else:
            trend = "Neutral"

        sorted_dates = sorted(rec["dates"])
        latest_date = sorted_dates[-1] if sorted_dates else ""
        earliest_date = sorted_dates[0] if sorted_dates else ""

        analyzed.append({
            "symbol": sym,
            "symbol_ns": rec["symbol_ns"],
            "security_name": rec["security_name"],
            "sector": rec["sector"],
            "buy_value_inr": buy_v,
            "sell_value_inr": sell_v,
            "net_flow_inr": net_v,
            "turnover_inr": turnover,
            "buy_quantity": rec["buy_quantity"],
            "sell_quantity": rec["sell_quantity"],
            "buy_deals": rec["buy_deals"],
            "sell_deals": rec["sell_deals"],
            "total_deals": rec["total_deals"],
            "distinct_buyers_count": distinct_buyers,
            "distinct_sellers_count": distinct_sellers,
            "buyer_clients": list(rec["buyer_clients"].keys()),
            "seller_clients": list(rec["seller_clients"].keys()),
            "repeated_buyers": repeated_buyers,
            "repeated_sellers": repeated_sellers,
            "has_repeated_buying": has_repeated_buying,
            "has_multiple_buyers": has_multiple_buyers,
            "has_repeated_selling": has_repeated_selling,
            "is_strong_net_buying": is_strong_net_buying,
            "is_mixed_flow": is_mixed_flow,
            "patterns": patterns,
            "trend": trend,
            "latest_deal_date": latest_date,
            "earliest_deal_date": earliest_date,
            "deal_records": sorted(rec["deal_records"], key=lambda x: (x["date"], x["value_inr"]), reverse=True)
        })

    # Sort stocks by Net Buying Value
    analyzed.sort(key=lambda x: x["net_flow_inr"], reverse=True)
    return analyzed


def add_volume_confirmation(stock_records, volume_lookup_fn=None):
    """Enrich accumulated stock records with exchange volume baseline and price confirmation."""
    for item in stock_records:
        sym = item["symbol"]
        vol_info = volume_lookup_fn(sym) if volume_lookup_fn else None

        if not vol_info:
            item["latest_volume"] = None
            item["avg_20d_volume"] = None
            item["volume_ratio"] = None
            item["volume_confirmed"] = False
            item["price"] = None
            item["price_change_pct"] = None
            item["volume_label"] = "No exchange volume data"
            continue

        ratio = vol_info.get("ratio")
        item["latest_volume"] = vol_info.get("latest_volume")
        item["avg_20d_volume"] = vol_info.get("avg_20d_volume")
        item["volume_ratio"] = ratio
        item["volume_confirmed"] = (ratio is not None and ratio >= 1.2)
        item["price"] = vol_info.get("price")
        item["price_change_pct"] = vol_info.get("price_change_pct")
        item["volume_label"] = vol_info.get("label", "N/A")

    return stock_records


def generate_big_bull_watchlist(accumulated_stocks, sector_flows=None, min_score=35):
    """Score accumulated stocks using a transparent 0-to-100 institutional conviction model.

    Scoring Criteria:
    - Net Buying Magnitude: Up to 25 pts (+15 pts if net positive, +10 pts bonus if >= ₹50 Cr)
    - Institutional Breadth: Up to 20 pts (+15 pts for >=2 distinct buyers, +5 pts bonus if >=3)
    - Pattern Persistence: +20 pts for repeated buying across distinct session dates
    - Sector Tailwinds: +15 pts if the stock's sector is net positive institutional buying
    - Volume Confirmation: +10 pts if session volume >= 1.2x of 20-day average
    - Price Support: +10 pts if price change >= 0% or positive
    """
    positive_sectors = set()
    if sector_flows:
        for sf in sector_flows:
            if sf.get("net_flow_inr", 0) > 0:
                positive_sectors.add(sf["sector"])

    watchlist = []

    for s in accumulated_stocks:
        score = 0
        reasons = []

        # 1. Net Buying Magnitude (Max 25)
        net_val = s["net_flow_inr"]
        if net_val > 0:
            score += 15
            if net_val >= 500000000:  # ₹50 Cr
                score += 10
                reasons.append(f"Major Net Buy (+₹{net_val / 10000000:,.1f} Cr)")
            else:
                reasons.append(f"Positive Net Flow (+₹{net_val / 10000000:,.1f} Cr)")
        elif net_val < 0:
            # Penalize net sellers
            score -= 20

        # 2. Institutional Breadth (Max 20)
        buyers = s["distinct_buyers_count"]
        if buyers >= 3:
            score += 20
            reasons.append(f"{buyers} Distinct Institutional Buyers")
        elif buyers >= 2:
            score += 15
            reasons.append(f"Multiple Buyers ({buyers})")

        # 3. Pattern Persistence (Max 20)
        if s["has_repeated_buying"]:
            score += 20
            rep_names = ", ".join(s["repeated_buyers"][:2])
            reasons.append(f"Repeated Accumulation ({rep_names})")

        # 4. Sector Tailwinds (Max 15)
        sec = s["sector"]
        if sec in positive_sectors:
            score += 15
            reasons.append(f"Sector Inflow ({sec})")

        # 5. Volume Confirmation (Max 10)
        if s.get("volume_confirmed"):
            score += 10
            reasons.append(f"Volume Surge ({s.get('volume_ratio', 1.0):.1f}x 20d avg)")

        # 6. Price Support (Max 10)
        chg = s.get("price_change_pct")
        if chg is not None and chg >= 0:
            score += 10
            reasons.append(f"Positive Price Momentum ({chg:+.1f}%)")

        score = max(0, min(100, score))

        if score >= min_score and net_val > 0:
            watchlist.append({
                "symbol": s["symbol"],
                "symbol_ns": s["symbol_ns"],
                "security_name": s["security_name"],
                "sector": s["sector"],
                "score": score,
                "reasons": reasons,
                "net_flow_inr": net_val,
                "buy_value_inr": s["buy_value_inr"],
                "sell_value_inr": s["sell_value_inr"],
                "buyers_count": buyers,
                "trend": s["trend"],
                "patterns": s["patterns"],
                "latest_deal_date": s["latest_deal_date"],
                "deal_records": s["deal_records"]
            })

    watchlist.sort(key=lambda x: (x["score"], x["net_flow_inr"]), reverse=True)
    return watchlist

