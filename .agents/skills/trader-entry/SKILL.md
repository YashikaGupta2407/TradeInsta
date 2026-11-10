---
name: trader-entry
description: >-
  Entry Timer. Watches an NSE setup bar by bar and tells you the moment to get in.
  Reads free Yahoo 1-minute or 5-minute bars, builds the opening range (9:15 to 9:30 AM IST
  by default) and calls the entry on the first completed bar that CLOSES above the
  range high, or above a level you give it, with an optional volume check. Says waiting
  and how far, triggered with the time and the price, expired, or not open yet, and
  writes trader/entry.json for /trader-exit. Longs only. It never places an order.
argument-hint: "[TICKER] [--setup orb|level] [--interval 5m|1m]"
---

# trader-entry

The Entry Timer turns a setup into one moment: the bar that confirms it. It
reads 1-minute or 5-minute bars and waits for each one to finish. A spike in
progress does not trigger: only a bar that closes above the level or range
high counts. Free Yahoo data can lag, so it tracks the timestamp of Yahoo's
last trade and says so when Yahoo is behind.

Day trading loses most people money. This tool does not make trades win; it
holds you to your rules so you do not jump in early on emotion. Longs only.
Nothing was placed.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/entry.py" check RELIANCE.NS                  # 15-minute opening range (9:15-9:30)
python3 "${CLAUDE_SKILL_DIR}/entry.py" check RELIANCE.NS --write          # also save trader/entry.json
python3 "${CLAUDE_SKILL_DIR}/entry.py" check RELIANCE.NS --setup level --level 1215.00
python3 "${CLAUDE_SKILL_DIR}/entry.py" watch RELIANCE.NS --until 15:00    # stops on trigger or cutoff
python3 "${CLAUDE_SKILL_DIR}/entry.py" watchlist                          # hits in trader/watchlist.json
```

## Running in the background with /loop

To track a breakout bar by bar while you do other work, run `watch` in the background:

```bash
# Watch TSLA in the background until trigger or cutoff
python3 "${CLAUDE_SKILL_DIR}/entry.py" watch TSLA --until 15:30
```

Use `/loop` to periodically inspect the status of your watchlist.

## The Rule for NSE & US

- **Opening range (orb):**
  - NSE: 9:15 to 9:30 AM IST (the 9:15, 9:20 and 9:25 five-minute bars).
  - US: 9:30 to 9:45 AM ET (the 9:30, 9:35 and 9:40 five-minute bars).
- **Trigger:** First completed bar after the range that CLOSES above the opening range high.
- **Entry Window:** Default no new entries before 9:30 IST (9:45 ET) and no new entries after 15:00 IST (15:30 ET).
- **Next step:** Set the stop and target with /trader-exit.

## Example: the reel

At 10:15 AM on Monday 2026-10-19, the TSLA 10:10 bar (bar 8) closed at $364.18, above the range high of $362.18:

```
TRIGGERED 10:15 AM at $364.18: the 10:10 bar (bar 8) closed above the range high $362.18 (+0.55%).
Nothing was placed.
Next: set the stop and target BEFORE you buy: /trader-exit (exit.py plan --from trader/entry.json).
```

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No options, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker keys live in environment variables or the Claude Desktop config, never in the chat.
