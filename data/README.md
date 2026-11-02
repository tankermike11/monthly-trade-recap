# Mock trade data: each customer's October 2026

Ten made-up customers trading in a simulated market (seed 42). Ticker names are real
so the trades read naturally; none of these price moves happened. In this October 2026,
SPY moved up 0.7% and QQQ moved down 6.8%.

Raw files: `raw/2026-10/<id>/`. Answer keys the tests must reproduce:
`expected/2026-10/<id>.json`. A "trade" is one position from first order to fully
closed, however many orders that took. "Account up/down" is the change in account value
after fees, with open positions at month-end value and deposits or withdrawals taken out.

**p01 Robin, "Steady investor".** Already owns three broad funds and one company's stock. Adds a few shares of two of them early each month and otherwise leaves everything alone. The bond fund pays a small cash dividend at the start of each month.
*October 2026:* 2 trades on 2 days, 4 open at month end, 0 gains, 0 losses, $0 fees, account down $1,826.

**p02 Sam, "Stock day trader".** Buys and sells the same shares within the same day, about three times a day, in a handful of fast-moving stocks. Some trades start before the market opens. Takes a small gain or a small loss quickly and is always out of every stock before the close.
*October 2026:* 70 trades on 22 days, 36 gains, 34 losses, $25 fees, account down $317.

**p03 Casey, "The wheel".** Sells puts on three low-priced stocks with enough cash set aside to buy the shares. When a put ends with the stock below the strike, takes the shares and then sells calls against them. Buys trades back early once half the upfront cash is kept.
*October 2026:* 4 trades on 6 days, 1 open at month end, 3 gains, 0 losses, $24 fees, account up $613.

**p04 Jordan, "Premium seller".** Sells a put below the price and a call above it on big, well-known stocks, usually ending about 6 weeks out. When a stock moves hard one way, slides the other half of the trade closer to the price instead of closing. With about 3 weeks left, moves the whole trade out to the next month. Many trades are still open when the month ends.
*October 2026:* 14 trades on 18 days, 4 open at month end, 13 rolls, 10 gains, 0 losses, $76 fees, account up $8,429.

**p05 Taylor, "Spreads and diagonals".** Trades option spreads where the most that can be lost is known upfront: put spreads and iron condors for cash collected, call spreads and an occasional call diagonal paid for upfront. Takes profits early, usually after a quarter of the possible gain, and rarely holds into the last week.
*October 2026:* 10 trades on 13 days, 5 open at month end, 3 gains, 2 losses, $79 fees, account down $966.

**p06 Alex, "Same-day SPX".** Trades the S&P 500 index itself with small spreads that end the same day, or sometimes the next day. Checks every 15 minutes and closes once most of the possible gain shows up, usually waiting for a fair middle price rather than taking the first one offered; about one trade in three is left alone until it ends and settles in cash. One to three trades a day, one at a time.
*October 2026:* 23 trades on 18 days, 18 gains, 5 losses, $159 fees, account up $116.

**p07 Morgan, "Micro futures".** Trades small stock-index futures (the micro S&P 500 and micro Nasdaq), betting both up and down. Many trades start in the evening session after the stock market has closed. Each trade lasts minutes to a few hours.
*October 2026:* 53 trades on 23 days, 17 gains, 36 losses, $207 fees, account down $3,017.

**p08 Jamie, "Multi-asset".** Uses a bit of everything: buys shares, buys calls on one chip maker, sells puts on the micro S&P 500 future, and buys bitcoin and ether, sometimes on weekends. Adds money to the account mid-month.
*October 2026:* 7 trades on 11 days, 2 open at month end, 2 gains, 3 losses, $181 fees, account down $2,258.

**p09 Riley, "Losing month".** Sells puts on two retail stocks with cash set aside. Sells more of the same put each time the stock drops 3%. In October one of the stocks fell 12% overnight, the puts ended with the stock well below the strike, the shares were bought at the strike, and a few days later they were sold for much less. A red month.
*October 2026:* 4 trades on 6 days, 1 open at month end, 2 gains, 1 loss, $13 fees, account down $1,844.

**p10 Drew, "Messy account".** An account with awkward data. It starts the month already holding shares and options. A call it sold is exercised early, the day before a dividend. One stock splits two-for-one while a put on it is open. One order fills in three pieces and another only partly fills. A roll is done as two separate orders two minutes apart. Half the split shares are sold, and that fill arrives with its fees missing.
*October 2026:* 3 trades on 5 days, 2 open at month end, 1 roll, 0 gains, 2 losses, $9 fees, account down $596.

**November set** (`raw/2026-11/`): all ten continue from their October positions. Clocks
fall back Sunday Nov 1 (p07 trades that evening, p02 before the open Nov 2); markets are
closed Nov 26; Nov 27 closes at 1 pm New York time (p06 trades it); p05's IWM spread
opened Oct 30 closes Nov 3.
