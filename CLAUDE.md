## Purpose and principles

Build a monthly pipeline that turns each customer's normalized trade data into a personal "Your month" recap: a plain-text version first, then the visual email. Code computes every number and writes every single-fact line; Claude reads the month's trades and writes only what code cannot, the overall picture and what stood out; code checks the words before anything renders.

**Non-negotiable principles**

1. **The model never does math.** All counts, P&L, ratios and dates come from tested Python. The model writes prose with placeholders like `{{adjust_to_close_ratio}}` and never types a number.
2. **Every number traces to fills.** Each fact in the facts sheet keeps the IDs of the fills and trade stories it came from, so any line in a recap can be audited.
3. **The mock data ships with an answer key.** The generator writes the expected facts alongside the trades, and the test suite must reproduce them exactly.
4. **Rewards attach to habits, never to activity or wins.** No copy celebrates trade count, volume or realized gains. Lead with how the customer traded.
5. **The losing month is a first-class case.** Every template and prompt is tested on a red month before a green one.
6. **Trader language, no shorthand.** Use the words traders use (expiration, strike, premium, rolled out), spelled out in full; no acronyms or insider labels (copy rules at the end).
7. **Deterministic and replayable.** Same inputs, same prompt version and same model give the same facts sheet. Every run is archived.


### Copy rules (put these in `CLAUDE.md` and the system prompt)

- Write for a customer who trades: use the words traders use, spelled out in full. Every sentence must read cleanly to someone in their first year of trading options.
- Second person, past tense. Describe what the customer did; never judge it or advise.
- Trader terms are expected: expiration (or expiration date), further out, closer in, strike, premium, sold or collected premium, credit, debit, rolled out, rolled in, rolled up, rolled down, puts, calls, strangle, spread, iron condor, implied volatility.
- No shorthand or insider labels: no "DTE", "IV", "IVR", "IV rank", "delta", "theta", "OTM", "ITM", "P&L", "untested side", "one-lot". Say "expiring about 6 weeks out", "when implied volatility was low compared with its past year", "the side that was not under pressure", "per contract".
- Halves of the month are always "the first half of the month" and "the second half of the month", never a bare "first half".
- Banned words: annotation, invalidation, calibration, thesis, immutable, conviction score, resolution event, "came due", dishonesty, bite, sharp, quiet, expensive, bet (bets, betting).
- Hyphens, colons or semicolons, never em dashes.

| Never | Always |
| --- | --- |
| "Rolled the untested side toward the money 113 times" | "When a stock moved hard, you rolled the side that was not under pressure toward the stock price 113 times" |
| "Median entry 42 DTE" | "You usually opened trades expiring about 6 weeks out" |
| "IVR at entry 25" | "Most of your trades went in when implied volatility was average or low compared with its past year" |
| "Kept 38% of max profit" | "Your winners usually kept about a third of the premium you collected" |
| "Moved one trade to a later end date" | "You rolled one trade out to a later expiration" |
| "7 in the first half, 3 in the second half" | "7 in the first half of the month, 3 in the second half" |
| "Great month! 625 trades!" | "You adjusted 3 times for every time you closed a trade" |

### Guardrails for customer copy

- No praise for activity or wins: never "great job", "crushed it", "record", "streak", "milestone", "most trades ever". Never celebrate a trade count, volume or realized gains. Lead with how the customer traded.
- No advice and no predictions: never "you should", "consider", "next month you will", "recommend", "guaranteed".
- Never rank or compare a customer against other customers (no percentiles, no leaderboards).
- Red month: plain and steady. Name the loss once, put it next to the market move, no consolation clichés.
- No exclamation marks.

## Engineering rules

- Python 3.12-compatible code (the dev venv runs 3.14). Approved dependencies only: pydantic, pytest, polars, jinja2, anthropic, numpy, scipy, pyyaml, openpyxl (demo-site template). Ask before adding anything else.
- `pricing_reference.py` is the validated pricer, copied unchanged from `../pricing_engine_demo/`. Import it; never edit or rewrite it.
- The mock data's answer keys (`data/expected/`) are the source of truth for tests. They come from the generator's own ledger, never from pipeline code.
- Same seed, same output, byte for byte. No wall-clock times, no unordered iteration in anything written to disk.
- No Anthropic API calls before Phase 4. Read the key from `ANTHROPIC_API_KEY`; never hard-code it.
- Build one phase at a time; stop at each phase's acceptance gate for review.
- Out of scope: real customer data, sending email, scheduling.

## Demo site (site/, api/)

- A team-only explainer on Vercel, behind a password (SITE_PASSWORD). Sample or made-up trades only: the upload page says so, and real account data must never be uploaded.
- Uploaded workbooks are processed in memory and never stored or logged (counts only).
- Secrets (ANTHROPIC_API_KEY, SITE_PASSWORD) live in Vercel project settings, never in the repo.
