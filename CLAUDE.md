## Purpose and principles

Build a monthly pipeline that turns each customer's normalized trade data into a personal "Your month" recap: a plain-text version first, then the visual email. Code computes every number; Claude only chooses which facts matter and writes the words; code checks the words before anything renders.

**Non-negotiable principles**

1. **The model never does math.** All counts, P&L, ratios and dates come from tested Python. The model writes prose with placeholders like `{{adjust_to_close_ratio}}` and never types a number.
2. **Every number traces to fills.** Each fact in the facts sheet keeps the IDs of the fills and trade stories it came from, so any line in a recap can be audited.
3. **The mock data ships with an answer key.** The generator writes the expected facts alongside the trades, and the test suite must reproduce them exactly.
4. **Rewards attach to habits, never to activity or wins.** No copy celebrates trade count, volume or realized gains. Lead with how the customer traded.
5. **The losing month is a first-class case.** Every template and prompt is tested on a red month before a green one.
6. **Plain language.** Every string must survive being read aloud to someone who has never used a brokerage app (copy rules at the end).
7. **Deterministic and replayable.** Same inputs, same prompt version and same model give the same facts sheet. Every run is archived.


### Copy rules (put these in `CLAUDE.md` and the system prompt)

- Every string must survive being read aloud to someone who has never used a brokerage app.
- Second person, past tense, plain words. Describe what the customer did; never judge it or advise.
- No jargon in customer copy: no "DTE", "IVR", "delta", "untested side", "debit diagonal". Say "ended 5 to 6 weeks out", "when option prices were low", "the other half of the trade".
- Banned words: annotation, invalidation, calibration, thesis, immutable, conviction score, resolution event, "came due", dishonesty, bite, sharp, quiet, expensive.
- Hyphens, colons or semicolons, never em dashes.

| Never | Always |
| --- | --- |
| "Rolled the untested side toward the money 113 times" | "When a stock moved hard one way, you slid the other half of the trade closer to the price" |
| "Median entry 42 DTE" | "You usually picked end dates about 6 weeks away" |
| "IVR at entry 25" | "Most of your trades went in when option prices were average or low" |
| "Kept 38% of max profit" | "Your winners usually kept about a third of the cash you collected upfront" |
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
