# Monthly Trade Recap: Build Spec

Nov 2, 2026 · @Mike

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

**How to use this with Claude Code**

- Save this doc as `SPEC.md` at the repo root, and put the copy rules plus the principles above in `CLAUDE.md`.
- Build phase by phase. Each phase ends with its acceptance criteria passing; do not start the next phase until they do.
- Python 3.12, `pydantic` for schemas, `pytest` for tests, `duckdb` or `polars` for the analysis layer, `anthropic` SDK for the model call, `jinja2` for templates.
- Reuse the existing `pricing_reference.py` (BSM and binomial) for mock option prices rather than writing a new pricer.

## Architecture and repo layout

Five stages run in order for each customer and month; only stage 4 calls the model.

```
recap/
  SPEC.md                 # this document
  CLAUDE.md               # principles + copy rules
  schemas/                # pydantic models: raw, trades, facts, recap output
  mock/
    generator.py          # seeded mock data generator
    personas/*.yaml       # one config per persona
    market/               # simulated or historical price paths
  data/
    raw/<month>/<customer_id>/   # fills.parquet, orders, positions, events, account.json
    expected/<month>/<customer_id>.json   # answer keys (mock only)
  pipeline/
    ingest.py             # load + validate raw data
    classify.py           # strategy classifier (Phase 2)
    link.py               # trade linker (Phase 2)
    metrics.py            # metrics + facts sheet (Phase 3)
    write.py              # model call (Phase 4)
    validate_copy.py      # copy validator (Phase 4)
    render.py             # templates (Phase 5)
    run.py                # CLI: python -m pipeline.run --month 2026-10 [--customer ID] [--batch]
  prompts/recap_v1.md     # versioned system prompt
  templates/
    recap.txt.j2          # plain-text recap (built first)
    recap.html.j2         # visual email (after content holds up)
  out/<month>/            # facts, model output, rendered recaps, review gallery
  archive/<month>/        # immutable record of each run
  tests/
```

**Data contracts between stages** (each one is a pydantic model, stored as JSON or Parquet):

| Stage | Input | Output |
| --- | --- | --- |
| 1. Ingest | Raw files per customer | Validated `RawMonth` |
| 2. Classify and link | `RawMonth` | `TradeStory[]` |
| 3. Metrics | `TradeStory[]` + `RawMonth` | `FactsSheet` |
| 4. Write | `FactsSheet` + prompt | `RecapCopy` (validated) |
| 5. Render | `RecapCopy` + `FactsSheet` | `recap.txt`, `recap.html` |

Each stage can run alone from its stored input, so a prompt change re-runs only stages 4 and 5.

## Phase 1: Schemas and mock data

Define the raw schemas, then build a seeded generator that produces realistic October 2026 data for 10 personas, each with an answer key of expected facts.

### 1.1 Raw schemas (one set per customer per month)

**`Account`**: customer\_id, first\_name, time\_zone, enabled products (options level, futures, crypto, event contracts), month start and end account value, deposits and withdrawals with dates.

**`Fill`** (one row per leg):

| Field | Notes |
| --- | --- |
| exec\_id, order\_id | order\_id groups the legs of one multi-leg order |
| ts\_utc, session | session = pre\_market, regular, after\_hours, overnight |
| asset\_class | stock\_etf, equity\_option, index\_option, future, future\_option, crypto, event\_contract |
| symbol, underlying | OCC symbol for options; root + contract month for futures |
| side, open\_close | buy or sell; open or close |
| qty, price, multiplier | multiplier 100 for equity options, per contract spec for futures |
| strike, expiration, put\_call | options only |
| commission, fees | separate fields; missing must be explicit, never zero by default |

**`Order`**: order\_id, ts\_utc, legs (exec\_ids), order type, net price, credit or debit.

**`Position`**: snapshots at month start and month end, with quantity and cost basis per instrument.

**`Event`**: expiration (worthless or cash-settled), assignment, exercise, split, dividend, with the affected position and resulting stock fill if any.

**`MarketContext`**: per fill, the underlying price, implied volatility and IV rank at entry, days to expiration. Per month: a few index levels (SPY, QQQ, /ES) at start and end.

### 1.2 Mock generator

- `python -m mock.generator --month 2026-10 --seed 42` writes `data/raw/2026-10/<id>/` and `data/expected/2026-10/<id>.json`.
- **Prices:** a simulated daily price path per underlying (geometric Brownian motion with a few planted gaps), seeded. Intraday fills sit inside that day's high and low.
- **Option prices:** priced with `pricing_reference.py` from the path, a per-symbol vol level and the day's date. Only strikes and expirations that would actually be listed (monthly third Fridays, weeklies for liquid names, standard strike spacing).
- **Behavior:** each persona is a YAML file of rules the generator follows, for example: entry days to expiration, strike selection by delta, roll trigger, profit-taking target, size, trading hours.
- **Fees:** a simple fee schedule ($0.65 per option contract to open, free to close on index options, $1.25 per futures contract) so net P&L is testable.
- **Answer key:** while generating, the generator records the ground truth: each trade story with its fills, its strategy label, rolls, outcome, and the headline facts the metrics layer must reproduce.

### 1.3 The 10 personas

| # | Persona | Behavior | What it tests |
| --- | --- | --- | --- |
| 1 | Steady investor | Buys 3 ETFs and 1 stock, holds, gets a dividend | Low-activity short version |
| 2 | Stock day trader | 60+ same-day round trips, some pre-market | Session times, fees eating results |
| 3 | The wheel | Sells puts, gets assigned, sells covered calls | Assignment, stock and option in one story |
| 4 | Premium seller | 45-day strangles, rolls the untested side, many rolls | Trade linker, roll types, open positions at month end |
| 5 | Spreads and diagonals | Defined-risk spreads, quick 20-30% profit taking | Multi-leg classification, debit trades |
| 6 | Same-day SPX | 0-1 day index spreads, many tiny holds | Index options, cash settlement, fee schedule |
| 7 | Micro futures | /MES and /MNQ, overnight sessions | Futures P&L math, sessions |
| 8 | Multi-asset | Stocks, equity options, futures options, some crypto | Mixed-asset summaries |
| 9 | Losing month | Short puts through a planted 12% drop, assigned, sells stock at a loss | Red-month tone, biggest-loss copy |
| 10 | Messy account | Carried-in positions, partial fills, early assignment, a 2-for-1 split, one fill with fees missing | Data quality flags and fallbacks |

**Calendar edge cases** (generate a November 2026 set too): daylight saving ends Nov 1; market holiday Nov 26; half day Nov 27; a trade opened Oct 30 and closed Nov 3.

### Acceptance criteria

- [ ] Same seed produces byte-identical output.
- [ ] Every option fill price is within the simulated bid-ask of the theoretical price; every stock fill is within the day's range.
- [ ] Every persona's YAML is under 60 lines and readable by a non-engineer.
- [ ] A one-page `data/README.md` describes each persona's month in plain English.

## Phase 2: Strategy classifier and trade linker

Turn raw fills into trade stories: one story per position from first open to final close (or still open), with every roll inside it. This is the hardest phase and the one every later number depends on.

### 2.1 Strategy classifier (`classify.py`)

Rule-based, never AI. Input is one order's legs plus any existing position in the same underlying. Output is one label from a fixed list, plus `confidence: exact | inferred`.

| Label | Rule (simplified) |
| --- | --- |
| stock\_long, stock\_short | Shares only |
| long\_call, long\_put, short\_call, short\_put | One option leg |
| covered\_call | Short call against at least 100 shares per contract held |
| cash\_secured\_put | Short put with cash at least strike x 100 (if cash data exists, else short\_put) |
| vertical (credit or debit) | Two legs, same expiration and type, different strikes, opposite sides |
| strangle, straddle (long or short) | One call and one put, same expiration, same side |
| iron\_condor, iron\_butterfly | Four legs: a put vertical and a call vertical, same expiration |
| butterfly, broken\_wing\_butterfly | 1-2-1 ratio, same type and expiration; unequal wings = broken wing |
| calendar, diagonal | Same type, different expirations (same strike = calendar) |
| future\_long, future\_short | Outright futures |
| crypto\_long, crypto\_short | Crypto spot |
| custom | Anything else; keep the legs, never force a label |

The classifier also labels each **order's effect**: open, close, roll, adjust (adds or removes a leg), or partial close.

### 2.2 Trade linker (`link.py`)

- A **trade story** = all orders on one underlying that keep a position alive, from the first opening order until the net position is flat.
- A **roll** = an order (or two orders within 5 minutes) that closes legs and opens legs on the same underlying while the story stays open. Make the window a config value.
- **Roll types** (each counted separately): out in time, up (call or put strike raised), down, untested side toward the money, tested side away from the money, widened, narrowed. Record credit or debit for each roll.
- **Assignment and exercise** stay in the same story: a short put assigned into stock, then covered calls on that stock, is one wheel story.
- **Carried-in positions** (opened before the month) start a story flagged `carried_in: true`, using the month-start snapshot for cost basis.
- **Partial closes** keep the story open with reduced size.
- **Splits** adjust strikes, quantity and multiplier on open stories without creating new orders.
- Each story records: underlying, strategy at open and at month end, every order in sequence, opening credit or debit, realized P&L after fees, days held, days left at close, status (closed, open, expired, assigned).

### Acceptance criteria

- [ ] For all 10 personas, every trade story's fills, label, rolls and outcome match the answer key exactly.
- [ ] Unit tests cover each label in the table and each roll type with a hand-built order.
- [ ] Net quantity across all stories reconciles to the month-end position snapshot, for every customer.
- [ ] Realized P&L across all closed stories, plus open P&L, reconciles to the account value change net of deposits and withdrawals (within $1).
- [ ] Persona 10's problems produce named data-quality flags, never a crash.

## Phase 3: Metrics and the facts sheet

Compute every number in code and package it as a facts sheet: 30 to 60 small, labeled facts per customer that the model can choose from but never change.

### 3.1 Metrics by section

| Section | Metrics |
| --- | --- |
| At a glance | Active trading days; trade stories opened, closed, still open; mix by asset class; share of activity outside regular hours |
| Getting in | Typical days to expiration at entry, and how it changed first half vs second half; typical IV rank at entry; typical credit or debit per contract; scaling in (same strike traded 3+ times) |
| Managing | Adjustments vs closes; roll counts by type; share of rolls for a credit; most-adjusted stories |
| Getting out | Typical days held; typical days left at close; share of upfront cash kept on winners; fastest winners; losers closed early |
| Results | Wins and losses; net realized P&L after fees; total fees; biggest win and biggest loss (as a multiple of upfront cash where that applies) |
| Still open | Open stories with their underlying, adjustment count, and any pushed into next month |
| What changed | First half vs second half, and this month vs last month when history exists |
| Context | Index moves for the month (SPY, QQQ) so a red month can be read against the market |

Use medians for "typical", never means. Define "first half" as trading days, not calendar days.

### 3.2 Facts sheet format

```json
{
  "customer_id": "p04",
  "month": "2026-10",
  "first_name": "Jordan",
  "flags": ["green_month", "has_open_positions"],
  "facts": [
    {
      "id": "adjust_to_close_ratio",
      "section": "managing",
      "value": 3.23,
      "display": "3 to 1",
      "plain": "times you adjusted a trade for every time you closed one",
      "weight": 0.8,
      "sources": ["story:p04-0012", "story:p04-0019"]
    }
  ]
}
```

- `display` is formatted in code ("$1,240", "3 to 1", "1 in 4"). The model only ever inserts `display` values.
- `weight` (0 to 1) is a code-side guess at how notable the fact is, for example a big change from last month or an unusual ratio. The model may still choose others.
- `plain` is a fixed, pre-approved phrase per fact type, written by a person, not the model.
- `sources` keeps the audit trail back to stories and fills.

### 3.3 Flags

`green_month`, `red_month`, `flat_month`, `low_activity` (fewer than 3 stories: use the short template), `first_month`, `has_open_positions`, `data_quality_issue` (route to review, never send automatically), `no_options`, `no_futures`.

### 3.4 Guardrail facts that are never produced

No fact may reward or rank activity: no "most trades ever", no streaks, no volume milestones, no percentile against other customers. Code must not emit these fact types at all.

### Acceptance criteria

- [ ] Each persona's headline facts match its answer key.
- [ ] Every fact has at least one source, and every source resolves to real fills.
- [ ] Persona 9 is flagged `red_month`; persona 1 is flagged `low_activity`; persona 10 is flagged `data_quality_issue`.
- [ ] Facts sheet build for one customer runs in under 1 second.

## Phase 4: Model call, structured output, validator

One Claude call per customer turns the facts sheet into `RecapCopy` JSON; a validator in code accepts it, retries once, or sends it to review.

### 4.1 The prompt (`prompts/recap_v1.md`)

- **System prompt (cached):** the role ("write a monthly recap of the customer's own trading, in second person"), the copy rules, the guardrails (no advice, no predictions, no praise for activity or wins), the output schema, and 3 worked examples: one green month, one red month, one low-activity month.
- **User turn:** the facts sheet JSON only.
- **Tone rules by flag:** `red_month` = plain and steady, name the loss once, put it next to the market context, no consolation clichés; `low_activity` = short template, 3 tiles, no sections.
- Version every prompt file; store the version with each output.

### 4.2 Output schema (`RecapCopy`)

```json
{
  "one_liner": "You get paid upfront, then you keep moving the trade instead of closing it.",
  "glance_tiles": ["active_days", "stories_closed", "adjust_to_close_ratio", "outside_hours_share"],
  "sections": {
    "getting_in":  ["You usually collected {{median_open_credit}} upfront on a single contract."],
    "managing":    ["About {{credit_roll_share}} of your adjustments put more cash in your pocket."],
    "getting_out": ["Most trades closed with {{median_days_left_at_close}} still left on the clock."]
  },
  "what_changed": "After {{shift_date}}, more of your trades ended within days instead of weeks.",
  "teasers": [
    {"fact": "adjustments_count", "line": "times you adjusted a trade instead of closing it."}
  ],
  "subject_line": "Your October, trade by trade"
}
```

Use the API's structured output (JSON schema or a forced tool call) so the shape is guaranteed; the validator checks the content.

### 4.3 Validator (`validate_copy.py`)

Each rule is a named check that returns pass or fail with a reason:

1. Schema valid; every list within its length limit (max 3 lines per section, max 160 characters per line, subject line max 50).
2. No digits anywhere outside `{{placeholders}}` (allow a small whitelist such as month names).
3. Every placeholder and every fact ID exists in that customer's facts sheet.
4. No banned words, no em dashes, no exclamation marks.
5. No advice or prediction phrases: "you should", "consider", "next month you will", "recommend", "guaranteed", and similar (list kept in config).
6. No activity praise: "great job", "crushed it", "record", "streak", and similar.
7. Red-month check: if `red_month`, the copy must reference the results fact once and the market context fact once.

Fail once → retry with the failure reasons appended. Fail twice → `out/<month>/review/<customer_id>.json` and never sent.

### 4.4 Running it

- **Dev:** synchronous calls, one customer at a time, `--customer p09`.
- **Monthly run:** the Message Batches API, one request per customer, with `custom_id` = customer\_id. Poll until done, then validate and render.
- **Model choice:** run all 10 personas through Haiku and Sonnet; pick the cheaper one that passes validation on the first try at least 9 times in 10 and reads better in blind review.
- Make the model name, temperature and max tokens config values, stored with each output.

### Acceptance criteria

- [ ] All 10 personas pass the validator, with at most one retry each.
- [ ] A planted bad output (a raw number, a banned word, an unknown placeholder) fails with a clear reason for each.
- [ ] Persona 9's recap names the loss once and puts it next to the market move.
- [ ] Re-running with the same facts sheet and prompt version produces output that passes the same checks.

## Phase 5: Rendering, review gallery, monthly run

The same validated `RecapCopy` and facts sheet fill every template. Ship plain text first; add the visual email only once the content holds up across all 10 personas.

### 5.1 Templates

1. **`recap.txt.j2`** (build first): plain-text recap in the style of the original write-up, but in second person. Sections: one-liner, at a glance, getting in, managing, getting out, how it turned out, still open, what changed, fine print.
2. **`recap.html.j2`** (second): the visual email from the October Trader Recap design canvas. Use email-safe HTML (tables, inline styles, no web fonts required to read it, 640 px wide). Charts are built from facts in code: the asset-mix bar, adjusted vs closed bars, the win and loss dot grid, the first half vs second half bar.
3. **Teasers and banner** (third): the three phone cards and the in-app banner, each filled from `teasers` and the facts they name.

Placeholder fill happens only in `render.py`: `{{fact_id}}` → that fact's `display` string. Any unknown placeholder at render time is a hard error.

**Required in every template:** "This isn't your account statement; if anything here differs, your statement is correct." Also: how results were counted, the education-only line, a slot for required disclosures, and an unsubscribe link.

**Light-mode fallback:** the HTML email must stay readable when a mail app forces light or dark mode.

### 5.2 Review gallery

`python -m pipeline.run --month 2026-10 --gallery` builds `out/2026-10/gallery.html`: one card per customer showing the rendered recap, its flags, the validator result, and a toggle to show the facts sheet beside it. This is how a person reviews a sample before sending.

### 5.3 Monthly run and archive

- One command runs all stages for a month: `python -m pipeline.run --month 2026-11 --batch`.
- **Archive** to `archive/<month>/<customer_id>/`: facts sheet, prompt version, model name, raw model output, validator result, final rendered files, and a hash of each. Never overwrite; a re-run writes a new run ID.
- **Send gate:** nothing is sent automatically in this build. The run ends with a summary: how many recaps passed, how many need review, and why.
- Sending, scheduling and real customer data are out of scope for this build; leave a `send.py` interface stub.

### Acceptance criteria

- [ ] All 10 personas render in both templates with no unknown placeholders.
- [ ] The HTML email renders correctly in a headless browser at 640 px and 375 px wide (screenshot each).
- [ ] The gallery shows all 10, with persona 10 marked "needs review".
- [ ] Re-running a month produces a new run ID and leaves the earlier archive untouched.

## Copy rules, compliance and open decisions

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

### Compliance notes (flag for CCO review; do not design around)

- These recaps are likely retail communications under FINRA Rule 2210, so they need principal review of the template and prompt, and retention of what was sent. The archive in 5.3 is built for this.
- Options content needs Registered Options Principal sign-off.
- Numbers must reconcile to books and records; the statement line goes in every recap.
- No predictions, no recommendations, no comparisons that rank customers.
- No activity rewards (Phase 3 guardrails).

### Open decisions

| # | Decision | Recommendation | Owner |
| --- | --- | --- | --- |
| RC-1 | Lossdog pilot: is linked-account data complete enough to show results? | If fees or fills are partial, drop the results section and lead with habits | Product |
| RC-2 | Model for the monthly run | Decide from the Phase 4 Haiku vs Sonnet test | Engineering |
| RC-3 | Review sample size before each send | Human review of every flagged recap plus 5% of the rest for the first 3 months | Compliance |
| RC-4 | Include "see how Tom handled the same tickers" link | Yes for Lossdog; check with CCO for Stox | Product + CCO |
| RC-5 | Skip or shorten recaps for very low activity | Short version under 3 stories; skip at zero trades | Product |
| RC-6 | Month-over-month comparisons | Add from the second month; never compare to other customers | Product |

### Out of scope for this build

Real customer data, sending email, in-app delivery, scheduling, Stox or Lossdog data integrations, and personalization beyond the facts sheet.
