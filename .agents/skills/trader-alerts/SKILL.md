---
name: trader-alerts
description: >-
  Price Alerts. Rings when a price, a stop or the opening bell hits on the NSE.
  Turns the trade plan into three alerts in one go (the stop, the target and
  "flat by close" at your flat-by time), plus above / below / stop / target
  levels, percent moves from the previous close, any time of day, and the NSE
  opening bell (9:15 AM IST), each ringing once so you are not spammed. Knows
  NSE trading hours (9:15–15:30 IST), reads the editable NSE holiday list
  (covering 2025, 2026, 2027), fetches delayed quotes for .NS symbols, and writes
  trader/prices.json for sizing and the risk check. Use when the user says "alert
  me when", "tell me if RELIANCE hits", "watch my stop", "watch my trade", "remind
  me to close by 15:15", "is the market open", "when does the market open", "get
  me a price", or right after /trader-exit writes a plan.
argument-hint: "[ticker] [price]"
---

# trader-alerts

Price Alerts watches the numbers so you do not have to stare at a chart. An
alert is information, never an order. When a stop alert rings, manage your
position accordingly.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/alerts.py" clock                          # NSE open or closed, next open
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --from-plan                # stop, target, flat by close from trader/plan.json
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --opening-bell             # 9:15 AM IST bell
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol RELIANCE.NS --stop 1180.00
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol RELIANCE.NS --target 1260.00
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --at 15:15 --label "flat by close"
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol TCS.NS --pct 2.5
python3 "${CLAUDE_SKILL_DIR}/alerts.py" check                          # exit 0 = something rang
python3 "${CLAUDE_SKILL_DIR}/alerts.py" quote RELIANCE.NS --write      # -> trader/prices.json
python3 "${CLAUDE_SKILL_DIR}/alerts.py" list
python3 "${CLAUDE_SKILL_DIR}/alerts.py" reset reliance-plan-stop
```

`--now "2026-10-19 10:15"` pins the clock (IST); `--offline DIR` reads saved
quote files instead of the network.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/plan.json` | the trade from trader-exit, for `add --from-plan` |
| `trader/alerts.json` | the rules (`alerts.example.json` in this folder) |
| `trader/holidays.json` | the editable NSE holiday list (2025–2027) |
| `trader/state/alerts.json` | which rules rang and when, so each rings once |
| `trader/prices.json` | the latest quotes, merged in, for trader-budget and trader-risk |

## The NSE clock

Regular session 9:15 AM to 3:30 PM IST (Asia/Kolkata), closed on weekends and
on NSE holidays defined in `trader/holidays.json`. Muhurat trading evening sessions
are also supported.
