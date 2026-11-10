---
name: trader-exit
description: >-
  Exit Planner. Sets your stop and your target before you enter, so you know
  when to get out. Reads the entry from trader-entry (or a price you give),
  puts the stop at the opening-range midpoint by default (or the range low,
  your price, a percent, or an ATR multiple), rounds it down to tick size
  (default ₹0.05), and sets the target at 2R (or 1R, or your price). Writes
  trader/plan.json for the Time Machine, the Budget Manager and Price Alerts.
  Long side only. Default flat_by is 15:15 IST.
argument-hint: "[--stop or-mid|or-low|price:X|pct:X|atr:K] [--target 2R|1R|price:X]"
---

# trader-exit

The Exit Planner decides how a trade ends before it starts. The stop is where
the setup has failed. The target is where you take what you came for. Both
are set before the entry.

Day trading loses most people money: having a predetermined stop and target
protects your discipline. Longs only.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan                          # trader/entry.json: midpoint stop, 2R target
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --stop or-mid --target 1R --write
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --stop atr:1.5           # needs session bars
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --symbol TSLA --entry 364.18 --or-high 362.18 --or-low 354.47
python3 "${CLAUDE_SKILL_DIR}/exit.py" show                          # the current trader/plan.json
```

`--now "2026-10-19 10:15"` pins the clock.

## Stop Rules

1. `or-mid`: opening-range midpoint `(high + low) / 2`, rounded down.
2. `or-low`: opening-range low, rounded down.
3. `price:X`: an explicit stop price under the entry.
4. `pct:X`: a percentage under the entry price.
5. `atr:K`: K times the Average True Range of the day's session bars.

## Target Rules

1. `2R`: target set at two times the risk (entry + 2 * risk).
2. `1R`: target set at one times the risk (entry + 1 * risk).
3. `price:X`: an explicit target price above the entry.

## Stops and targets

- **Tick size.** Configurable via `tick_size` in `trader/config.json` (defaults to 0.05).
- **Rounding.** The stop rounds away from the entry down to the tick size. Target rounds half-up to tick size.
- **Midpoint stop.** For TSLA at entry 364.18 with range 362.18 and 354.47, stop is $358.32 and 1R target is $370.04 (risk $5.86, 2R target 375.90).
- **Spread guard.** A stop too tight to survive the spread (under 0.05% of the entry) is rejected.
- **Flat by.** Default 15:15 IST (aligns with Indian broker auto square-off) or 15:55 ET for US stocks.
- **Next steps.** Hand off to /trader-timemachine to replay the trade, then /trader-budget to size it.

A stop is a plan, not a guarantee: a gap or a fast market can fill past it.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No options, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker keys live in environment variables or the Claude Desktop config, never in the chat.
