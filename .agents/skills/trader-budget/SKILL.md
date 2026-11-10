---
name: trader-budget
description: >-
  Budget Manager. Sizes every position to your budget, your max loss per
  trade, and Indian statutory transaction costs (brokerage, STT, exchange fee,
  SEBI turnover fee, GST, stamp duty). Reads the trade plan from trader-exit
  (entry, stop, target), finds whole shares fitting inside four limits (max loss
  including round-trip charges, position cap, max order, cash after buffer),
  and writes one proposed bracket order ticket to trader/orders.json. All
  charges, limits, and tick sizes are fully editable in trader/config.json.
argument-hint: "[budget in rupees]"
---

# trader-budget

The Budget Manager turns a plan ("buy RELIANCE.NS at 1201.80, stop 1180.00") into an
exact order ("buy 80 shares"), inside rules you set in `trader/config.json`.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/size.py"                        # trader/plan.json + trader/config.json
python3 "${CLAUDE_SKILL_DIR}/size.py" --write                # write trader/orders.json
python3 "${CLAUDE_SKILL_DIR}/size.py" --limit                # a limit order at the plan's entry
python3 "${CLAUDE_SKILL_DIR}/size.py" --limit 1200.00 --write
```

`--now "2026-10-19 10:15"` pins the clock (IST).

## Configurable Charges in config.json

All transaction costs are fully editable under `"charges"` in `trader/config.json`:
- **brokerage_per_order**: flat cap (default ₹20)
- **brokerage_pct**: percentage cap (default 0.03%)
- **stt_pct**: Securities Transaction Tax on sell side (default 0.025%)
- **exchange_turnover_fee_pct**: NSE turnover fee (default 0.00297%)
- **sebi_turnover_fee_pct**: SEBI turnover fee (default 0.0001% = ₹10/crore)
- **gst_pct**: GST on brokerage and fees (default 18.0%)
- **stamp_duty_pct**: Stamp duty on buy side (default 0.003%)
- **tick_size**: default 0.05

## Sizing Formula

$$\text{Net Max Loss} = [\text{Shares} \times (\text{Entry} - \text{Stop})] + \text{Round-Trip Charges}$$
The algorithm sizes the largest whole shares such that $\text{Net Max Loss} \le \text{Max Loss Per Trade}$.
