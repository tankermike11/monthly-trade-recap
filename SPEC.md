# Monthly Trade Recap: Build Spec

Nov 2, 2026 · @Mike

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

### 4.0 Revised 2026-11-04: code writes the facts, Claude writes the picture (`prompts/recap_v4.md`)

With prompts v1 to v3 Claude wrote every line of the recap, and most of what it wrote was a single fact restated in a fixed pattern (64% of its sentences were identical word for word across two runs). From v4 the work is split:

- **Code** (`pipeline/compose.py`) writes every single-fact sentence (getting in, managing, getting out), picks the at-a-glance tiles, and writes the results line (the month's result beside the market move, so a losing month is named once, next to the market, by construction) and what changed.
- **Claude** gets the month's facts, per-trade facts (`trade.t3.opened`, `trade.t3.result`; every number still a placeholder), group counts computed by code (`group.losing.bought_back_before_expiration_day` = "3 of 5"; "all 10", "both", "none of the 4") and every trade grouped by opening day, and writes only `overall`, two or three `observations` (at most one in a low-activity month) and the subject line, in the voice of the curated written report.
- **Validator** (`pipeline/validate_copy.py`), nine checks: lengths and shape, no typed numbers, only real facts, copy rules, no advice, no praise, the month's result left to code, no doubled units or repeated words, and every trade a passage cites is named in it.
- **Accuracy rules in the prompt:** a group word (every, all, both, only, the largest) needs a count or named trades; "usually" facts are medians, never "every"; how a trade ended comes from its `how_it_ended`, never from dates; a range of days covers every trade opened on each day; no times of day. `first_month` is not passed to Claude (it read as "your first month").
- **Claim check** (`pipeline/claimcheck.py`, `prompts/claimcheck_v2.md`, added 2026-11-04): because the validator cannot check what a sentence claims about the trades, and 5,000 to 10,000 recaps a month cannot all be read, a second call reads every sentence Claude wrote beside the facts, counts and full trade table and marks it supported or not. An unsupported sentence fails `claims_supported`; the recap gets one rewrite with the reason, then goes to review. It runs in the single-call writer and as its own batch after each writing round of the monthly run. Measured on `evals/claims.jsonl` (41 recaps from the fact-checked runs, 12 known-bad sentences, 368 known-good; `scripts/eval_claimcheck.py`): Opus 5.5 at medium caught 11 of 12 and held back none of 21 correct recaps, about $0.019 a recap at batch prices; Haiku 5.5 caught 10 of 12 and held 2 of 21; Sonnet 5.5 caught 10 of 12 and held 14 of 21. It also found two errors the manual fact checks missed. Trades carried in from an earlier month no longer carry an opening amount (their recorded cost is not what they cost when opened once added to or adjusted).
- **Result on the October personas** (Opus 5.5, effort medium): 10 of 10 passed, 9 on the first try, $0.92. A full fact check of every claim against the trades found no false statements; wording notes remain (one heading calls a put bought back with 3 days left "well before expiration"). Claims about groups of trades are the one risk the validator cannot check, so the review gallery (Phase 5) should spot-check them.

Sections 4.1 to 4.3 below describe the original design (prompts v1 to v3) and are kept as the record.

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
- **Decided 2026-11-02: Claude Opus 5.5** (`config/model.yaml`). Prompt `recap_v2`, effort medium, October 2026 personas, one blind reviewer:

  | Model | First try (of 10) | Blind review: 1st picks | Points on 6 fully ranked (3/2/1) | Per recap | 30K per month, Batch API |
  | --- | --- | --- | --- | --- | --- |
  | Opus 5.5 | 10 | 5 | 17 | $0.039 | ~$580 |
  | Sonnet 5.5 | 7 | 4 | 10 | $0.016 | ~$235 |
  | Haiku 5.5 | 7 | 1 | 9 | $0.002 | ~$30 |

  Opus is the only model that met the 9-in-10 bar, and the reviewer preferred it. Sonnet was the usual runner-up but close on only 2 of the 5 Opus wins, and 2 of its 4 wins came where the Opus draft had an error the validator now catches (a doubled unit) or a contradiction. Sonnet's misses were mostly the results line running 1 to 2 characters over 160. Haiku led the earlier round on plain wording (`recap_v1`) but fell to last once recaps used trader terms. Revisit with more reviewers, or if cost matters more than the gap in reading quality.
- Make the model name, temperature and max tokens config values, stored with each output.

### Acceptance criteria

- [ ] All 10 personas pass the validator, with at most one retry each.
- [ ] A planted bad output (a raw number, a banned word, an unknown placeholder) fails with a clear reason for each.
- [ ] Persona 9's recap names the loss once and puts it next to the market move.
- [ ] Re-running with the same facts sheet and prompt version produces output that passes the same checks.

## Phase 5: Rendering, review gallery, monthly run

The same validated `RecapCopy` and facts sheet fill every template. Ship plain text first; add the visual email only once the content holds up across all 10 personas.

**Decided 2026-11-04** (after the 4.0 revision):

- **Order:** (1) review gallery with a claims spot-check, (2) monthly batch run and archive, (3) HTML email, (4) phone cards and banner. The plain-text recap already exists, so review and the monthly run come before the visual templates.
- **Sections follow the 4.0 recap:** subject, greeting, overall picture (Claude), at a glance, getting in, managing, getting out (code), what stood out (Claude), how it turned out, still open, what changed, fine print.
- **Phone cards and banner are filled by code from facts** (the value large, its plain phrase beneath); `RecapCopy` no longer has teasers, and cards add no new claims.
- **The gallery lists Claude's claims about groups of trades** (every, all, both, only, the largest, first, last, a range of days, or a code count) beside the trades and facts each one cites, because the validator cannot check them. A customer "needs review" when the copy failed twice, the model declined, or the data has a quality issue (with the issue named).
- **The monthly run uses the Message Batches API.** Server-side refusal fallback is not available in batches, so a declined request goes to review.

### 5.1 Templates

1. **`recap.txt.j2`** (build first): plain-text recap in the style of the original write-up, but in second person. Sections: one-liner, at a glance, getting in, managing, getting out, how it turned out, still open, what changed, fine print.
2. **`recap.html.j2`** (second): the visual email from the October Trader Recap design canvas. Use email-safe HTML (tables, inline styles, no web fonts required to read it, 640 px wide). Charts are built from facts in code: the asset-mix bar, adjusted vs closed bars, the win and loss dot grid, the first half vs second half bar.
3. **Phone cards and banner** (not built yet): the three phone cards and the in-app banner, filled by code from facts (`RecapCopy` no longer has teasers).

Placeholder fill happens only in `render.py`: `{{fact_id}}` → that fact's `display` string. Any unknown placeholder at render time is a hard error.

**Required in every template:** "This isn't your account statement; if anything here differs, your statement is correct." Also: how results were counted, the education-only line, a slot for required disclosures, and an unsubscribe link.

**Light-mode fallback:** the HTML email must stay readable when a mail app forces light or dark mode.

**Built 2026-11-04** (`pipeline/render_email.py`, `templates/recap.html.j2`): the design's dark layout at 640 px, tables and inline styles only, no images, system fonts, tiles stacking below 480 px. It declares itself dark (`color-scheme` metas) so mail apps leave it alone, and stays readable under a forced colour inversion. Differences from the design: the "your month in one line" box holds Claude's overall picture; Claude's observations get their own "What stood out" section; there is no big net-result number, because the month's result is named once, in the results line beside the market move; a card drops the sentence that repeats the value it shows big or as a chart; the "See how Tom handled the same tickers" link is left out until RC-4 is decided. Monthly runs write `recap.html` beside `recap.txt`. Screenshots at 640 and 375 px: `out/email/screens/`.

### 5.2 Review gallery

`python -m pipeline.run --month 2026-10 --gallery` builds `out/2026-10/gallery.html`: one card per customer showing the rendered recap, its flags, the validator result, and a toggle to show the facts sheet beside it. This is how a person reviews a sample before sending.

### 5.3 Monthly run and archive

- One command runs all stages for a month: `python -m pipeline.run --month 2026-11 --batch`.
- **Archive** to `archive/<month>/<customer_id>/`: facts sheet, prompt version, model name, raw model output, validator result, final rendered files, and a hash of each. Never overwrite; a re-run writes a new run ID.
- **Send gate:** nothing is sent automatically in this build. The run ends with a summary: how many recaps passed, how many need review, and why.
- Sending, scheduling and real customer data are out of scope for this build; leave a `send.py` interface stub.

### Acceptance criteria

- [x] All 10 personas render in both templates with no unknown placeholders.
- [x] The HTML email renders correctly in a headless browser at 640 px and 375 px wide (screenshot each).
- [x] The gallery shows all 10, with persona 10 marked "needs review".
- [x] Re-running a month produces a new run ID and leaves the earlier archive untouched.
- [ ] Phone cards and in-app banner (step 4), deferred.

## Build log and where we paused (2026-11-04)

Paused by decision after Phase 5 step 3. Next up, when work resumes: **phone cards and in-app banner** (design PDF pages 2 to 5), filled by code from facts, no new claims.

**Done, in order** (branch `phase-4`, 404 offline tests passing):

| Step | What | Where |
| --- | --- | --- |
| Phase 1 to 3 | Mock data with answer keys, ingest and linker, facts sheet | `mock/`, `pipeline/`, `data/expected/` |
| Phase 4 | Live writer on Opus 5.5 (chosen by a blind review over Sonnet 5.5), trader terms without shorthand, validator with nine checks | `pipeline/write.py`, `pipeline/validate_copy.py`, `prompts/recap_v4.md` |
| 4.0 revision | Code writes every single-fact line; Claude writes only the overall picture and what stood out, with per-trade facts and group counts | `pipeline/compose.py` |
| Demo site | Password-protected Vercel site: try your own upload, how it works, personas (recap, facts, email, workbook), review gallery | `site/`, `api/index.py`, `scripts/build_site.py`; https://monthly-trade-recap.vercel.app |
| Phase 5.1 | Review gallery: ready or needs review with reasons, Claude's claims listed beside the trades they cite | `pipeline/gallery.py` |
| Phase 5.2 | Monthly run through the Batch API: write, check, one rewrite, check, archive with hashes, seeded 5% review sample | `pipeline/monthly.py`, `archive/<month>/run-NNNN` |
| Phase 5.3 | Claim check: a second Opus call verifies every sentence against the trades (caught 11 of 12 known errors, held 0 of 21 clean recaps) | `pipeline/claimcheck.py`, `prompts/claimcheck_v2.md`, `evals/claims.jsonl` |
| Phase 5.4 | HTML email, dark 640 px design, charts from facts | `pipeline/render_email.py`, `templates/recap.html.j2` |

**Live runs:** October `archive/2026-10/run-0001`: 10 of 10 passed (p10 held for data quality, as designed); the site is built from it. November `archive/2026-11/run-0002`: 9 of 10 passed; p04 went to review for a doubled unit, and the claim check caught and fixed a false sentence for p07.

**Cost:** about $0.064 a recap at batch prices ($0.045 writing, $0.019 checking), roughly $640 per 10,000 customers. A site upload costs about $0.11 and takes under a minute; the site function's time limit is 300 seconds.

**Commands to resume:**

- Tests: `.venv/Scripts/python.exe -m pytest -q` (live API tests: `-m live`).
- Monthly run: `python -m pipeline.run --month 2026-11 --batch` (add `--no-wait`, then `--resume run-NNNN`, to submit and come back later; needs `ANTHROPIC_API_KEY`; on this machine also `SSL_CERT_FILE` for the Norton certificate).
- Site: `python scripts/build_site.py --results archive/2026-10/run-0001`, then `vercel deploy --cwd deploy --prod --yes`.

**Still open:** the decisions table below (RC-1, RC-3 to RC-6), and compliance review of the email's fine print and disclosures slot.

## Copy rules, compliance and open decisions

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
| RC-2 | Model for the monthly run | Decided 2026-11-02: Claude Opus 5.5 via the Batch API (see 4.4) | Engineering |
| RC-3 | Review sample size before each send | Human review of every flagged recap plus 5% of the rest for the first 3 months | Compliance |
| RC-4 | Include "see how Tom handled the same tickers" link | Yes for Lossdog; check with CCO for Stox | Product + CCO |
| RC-5 | Skip or shorten recaps for very low activity | Short version under 3 stories; skip at zero trades | Product |
| RC-6 | Month-over-month comparisons | Add from the second month; never compare to other customers | Product |

### Out of scope for this build

Real customer data, sending email, in-app delivery, scheduling, Stox or Lossdog data integrations, and personalization beyond the facts sheet.
