# KIS domestic equities adapter

This fork is implementing a local CCXT-compatible adapter for Korea Investment &
Securities (KIS), tracked in [issue #1](https://github.com/recursivecurry/freqtrade/issues/1).
It is not an upstream CCXT integration or an officially supported Freqtrade exchange.
This document defines the implementation contract; it does not establish trading readiness.

## Baseline and scope

- Freqtrade: `2026.10-dev`, starting from `0db79d391f646eafbdf3c0976dd55dc90b296522`.
- CCXT: `4.5.88`, from this branch's `requirements.txt`.
- Python: 3.14 for local verification; the project supports Python 3.12 and newer.
- KIS reference: official examples at commit
  [`2077773a69a428daf63e148182511f068cc45de3`](https://github.com/koreainvestment/open-trading-api/tree/2077773a69a428daf63e148182511f068cc45de3).
- Initial scope: KRX regular-session domestic stocks and ETFs, KRW, whole shares,
  cash-funded long positions, one account per bot and a static symbol list.
- Unsupported: credit, unsettled-credit buying, shorts, derivatives, overseas stocks,
  NXT/SOR, extended sessions, scheduled orders, WebSockets and exchange stop orders.

There are three independent execution modes. Dry-run uses KIS quotes but must never
send order-changing requests. Paper trading sends orders only to the KIS simulation
server. Production order writes remain disabled. The default example must use
dry-run and paper mode.
The existing ETF collector uses the production quote server. Its three configured
profiles also authenticated and returned quotes on the paper server on 2026-10-22.
This does not establish account binding or order permissions.

## API mapping

The TR IDs below are taken from the pinned KIS examples, not inferred from older
examples. Paper availability and response fields must also be verified with the
target account; a matching TR ID alone is not evidence of server compatibility.

| CCXT contract | KIS path under `/uapi/domestic-stock/v1/` | Production / paper TR ID | Important parameters and result |
| --- | --- | --- | --- |
| `fetch_ticker` | `quotations/inquire-price` | `FHKST01010100` / same | `FID_COND_MRKT_DIV_CODE=J`, `FID_INPUT_ISCD`; `output` |
| `fetch_order_book`, `fetch_l2_order_book` | `quotations/inquire-asking-price-exp-ccn` | `FHKST01010200` / same | KRX symbol; price and remaining-volume levels in `output1` |
| intraday `fetch_ohlcv` | `quotations/inquire-time-dailychartprice` | `FHKST03010230` / unverified | business date, time cursor, `output2`; production example documents up to 120 records and up to one year of retained data |
| daily `fetch_ohlcv` | `quotations/inquire-daily-itemchartprice` | `FHKST03010100` / same | requested date range and adjustment flag; do not mix adjusted and unadjusted prices |
| `fetch_balance` | `trading/inquire-balance` | `TTTC8434R` / `VTTC8434R` | `CANO`, `ACNT_PRDT_CD`, positions in `output1`, account summary in `output2` |
| cash buying-power check | `trading/inquire-psbl-order` | `TTTC8908R` / `VTTC8908R` | `PDNO`, price/order type; use `nrcvb_buy_amt` and `nrcvb_buy_qty`, never `max_buy_*` credit-inclusive values |
| `create_order` buy / sell | `trading/order-cash` | buy `TTTC0012U` / `VTTC0012U`; sell `TTTC0011U` / `VTTC0011U` | string quantity/price, `EXCG_ID_DVSN_CD=KRX`; acceptance is not a fill |
| `fetch_order`, `fetch_orders`, `fetch_open_orders` | `trading/inquire-daily-ccld` | within 3 months `TTTC0081R` / `VTTC0081R`; older `CTSC9215R` / `VTSC9215R` | business-date range, account, symbol and order ID; cumulative order totals, not individual executions |
| cancellation eligibility | `trading/inquire-psbl-rvsecncl` | `TTTC0084R` / unverified | obtain eligible quantity and original order routing identifiers before cancellation |
| sellable quantity | `trading/inquire-psbl-sell` | `TTTC8408R` / unverified | `ord_psbl_qty`; the balance endpoint also supplies this field, but its freshness must be checked |
| `cancel_order` | `trading/order-rvsecncl` | `TTTC0013U` / `VTTC0013U` | `KRX_FWDG_ORD_ORGNO`, `ORGN_ODNO`, cancellation code `02`, remaining quantity |
| trading calendar | `quotations/chk-holiday` | `CTCA0903R` / unverified | `BASS_DT`, `opnd_yn`; cache because official guidance recommends approximately one call per day |

Quote and trading requests require authentication. `/oauth2/tokenP` uses
`grant_type=client_credentials`, `appkey` and `appsecret`. Requests carry a Bearer
token, app credentials, `tr_id` and `custtype=P`. Trading POST bodies use uppercase
field names. Hosts are fixed by environment:

- production: `https://openapi.koreainvestment.com:9443`;
- paper: `https://openapivts.koreainvestment.com:29443`.

The adapter must inspect `rt_cd` even for HTTP 200. Response headers and continuation
keys must be retained until all pages have been read. A failed page is an error,
not a successful partial balance or order list. Production examples document
different paper page sizes; do not assume one page is complete.

## Data contract

### Markets and quotes

`005930/KRW` maps to KIS code `005930`, preserving leading zeroes. The current
masters also contain alphanumeric codes such as `0000D0`; retain them verbatim.
Market metadata
uses CCXT's spot representation for engine compatibility and retains the actual
equity/ETF classification in `info`. Master data must identify the asset class,
venue, trading restrictions and quantity/price rules. A six-digit code alone does
not prove that a security is an ordinary stock or an ETF.

The ticker maps `stck_prpr` to `last`, `stck_oprc/stck_hgpr/stck_lwpr` to
`open/high/low`, `acml_vol` to `baseVolume`, `acml_tr_pbmn` to `quoteVolume`, and
`prdy_ctrt` to `percentage`. These are session statistics, not necessarily rolling
24-hour statistics. The quote endpoint does not supply every CCXT ticker field.
Missing bid/ask or exchange timestamps must not be invented: obtain bid/ask from
the order book, and label a local receive time as an observation time.

CCXT `has` and Freqtrade feature flags must describe actual support. No batch ticker,
funding, leverage, public-trade download or streaming capability is declared unless
implemented and tested. Sync and async clients share market metadata using the
CCXT base helpers, including `set_markets_from_exchange`.

### Candles and sessions

OHLCV rows are `[UTC milliseconds, open, high, low, close, volume]` in ascending
order. Implement `since` and `limit` with bounded pagination, duplicate detection
and explicit failure when the requested history cannot be obtained. Supported
initial timeframes are 1m, 5m and 1d. Do not begin trading without sufficient
completed warm-up candles.

Business dates use Asia/Seoul. Before implementing intraday conversion, establish
whether each endpoint labels a bar by its start or end time using observed rows
and the endpoint specification. Five-minute aggregation must use session-aligned
boundaries and complete constituent bars. Do not forward-fill overnight, weekends
or holidays, or treat an intraday gap as a normal closed session. This policy must
reach live cache merging, downloads and backtesting, not just the API parser.

The normal KRX session is 09:00–15:30 Asia/Seoul, including its closing auction.
Confirmed exceptional schedules override it. Missing/stale calendar or schedule
information blocks new orders; it does not stop order reconciliation. A calendar
entry with `opnd_yn=Y` is not by itself evidence of the day's opening/closing times.
Determine closing-auction bar handling and daily-candle anchoring before enabling
the corresponding timeframe. Historical corporate-action consistency and data
coverage must be recorded; neither is guaranteed by an API response being successful.

### Implemented market-data behavior

The daily KIS KOSPI/KOSDAQ masters supply security groups `ST` (stocks) and `EF`
(ETFs). Other groups are excluded. Halted, liquidation and administrative securities,
and securities with a quantity unit other than one, are inactive. This is master
metadata, not proof of current account permission to trade. Archives are downloaded
with TLS verification, redirects disabled and size limits; raw master bytes are
cached by board/KST date. No sample code that disables TLS verification is reused.

Limit-price rounding uses the current
[KRX stock and ETF tick table](https://regulation.krx.co.kr/contents/RGL/03/03010100/RGL03010100T3.jsp).
Stocks use price bands; ETFs use one KRW below 2,000 and five KRW at or above it.
Whole-share precision uses CCXT's existing helpers. The Freqtrade integration also
applies price-band rounding in runtime and backtest order creation.

`fetch_ticker` combines session statistics with a separate order-book request.
Each observation has its own receive time in `info`; neither is presented as an
exchange timestamp or an atomic combined snapshot. The order book validates all
ten levels before applying the requested limit. Unsupported request parameters
fail rather than changing the fixed KRX route.

For candle reads, `options.sessions` is a required date-keyed, independently
verified calendar. An open date contains `open`, `auction`, `close` (`HH:MM`, KST)
and a nonempty `source`; a verified closed date contains `null`. There is no
weekday-to-session fallback. Unknown dates fail before their history is queried.
For example, an operator-verified normal session has `09:00`, `15:20`, `15:30`.
Exceptional sessions must specify their actual boundaries. Supplying this mapping
does not itself verify its source or freshness; operational refresh and order-time
checks belong to phase 4. The holiday endpoint alone cannot supply these hours.

Observed production snapshots on 2026-10-22 show the `09:00` minute changing before
`09:01`, followed by a separate `09:01` minute. Intraday labels are therefore start
times. Four pages for 2026-10-21 contain 380 continuous-session minutes through
`15:19` and one separate `15:30` closing-auction bar. The parser requires every
completed continuous minute; an absent minute is an error, including for illiquid
securities where the API omits zero-trade minutes. It does not synthesize bars.

Five-minute bars contain five complete continuous minutes aligned to the session
opening. The closing auction remains a separate, short bar labelled at the close;
it is available only five minutes after that label. No bars are generated for the
ten-minute closing auction collection interval. Sessions whose boundaries cannot
align to five minutes reject `5m`. One-minute closing bars become available one
minute after their label. Current unfinished bars never reach the caller.

Daily bars use unadjusted prices (`FID_ORG_ADJ_PRC=1`), labelled at midnight KST.
The current KST date is always excluded, even after the regular close. The observed
broker daily volume differs from the sum of regular-session minutes; native daily
volume is retained and must not be described as regular-session-only volume.
Past minute days must match the unadjusted daily opening/closing prices and remain
inside its high/low range before caching. Corporate-action flags or a nonzero
split/adjustment rate reject the day pending reconciliation. This gate detects
inconsistent price bases; it does not implement split/dividend accounting.

History is bounded to the latest 365 calendar days. Minute pagination moves to the
previous minute; daily pagination moves to the previous date. Failed pages,
conflicting duplicates, missing completed bars and calendar contradictions fail
the whole call. `since` selects the first matching bars, while an omitted `since`
selects the latest `limit`; insufficient configured history for the latter fails.
Past verified minute days have a bounded in-memory cache; returned bars are copies.

Production uses the dated intraday endpoint. Paper mode uses the documented
today-only endpoint and rejects past-day intraday requests; no paper historical
TR ID is guessed. Paper authentication and current-day candles were exercised in
the phase-4 dry-run; private account/order availability remains for phase 6. These
public-data checks do not establish private account compatibility.

### Balances and orders

Initial order-capable operation requires a dedicated, cash-only account without
existing positions, open orders or unsettled transactions. Quote-only operation
may use the collector credentials. External cash transfers or manual trades after
initialization block new orders until explicitly reconciled. This restriction
avoids guessing an opening economic cash balance from KIS settlement summaries.

Seed a durable bot cash ledger with the verified opening `dnca_tot_amt`. Establish
the absence of unsettled transactions using broker history and settlement dates;
an empty position list alone is insufficient. Persist the baseline, order intents,
cumulative recognized fills and pending settlements in the existing Freqtrade DB.
Use the existing KeyValueStore for the account-scoped baseline and KIS-owned tables
in the same database for intents, cumulative observations and canonical deltas.
Mirror owned orders into Freqtrade Order/Trade records; store no account number in
public IDs.

Define the normalized KRW wallet as follows (all values are cash, not stock value):

- `total = opening cash - recognized buy costs + recognized net sale proceeds`.
- `R` is cash reserved for remaining buy intents/orders, including unresolved
  submissions and their conservative cost allowance. Market-order reservations
  use the broker's buying-power valuation, not the last traded price.
- `U` is net sale proceeds not yet confirmed as settled. Do not reuse those proceeds
  for buying until settlement is established by the broker and settlement calendar.
- `P` is the lowest non-credit `nrcvb_buy_amt` across the configured symbols,
  queried with the documented cash-only parameters. It is a current safety cap,
  not the ledger's `total`. Check `nrcvb_buy_qty` for the specific order as well.
- `free = max(0, min(total - R - U, P))`; `used = total - free`.

Reject negative totals, unexplained differences and inconsistent snapshots rather
than clamping them into a successful balance. `used` includes reservations,
unsettled sale holds and broker-imposed restrictions; expose the breakdown in
sanitized `info`. Preserve `dnca_tot_amt`, `nxdy_excc_amt` and
`prvs_rcdl_excc_amt` as broker settlement facts, never add them together or treat
them as independently spendable balances. A successful cap check does not erase
a ledger/broker discrepancy. Snapshot ordering must account for already submitted
orders; a changed account during reconciliation requires a fresh snapshot.

Illustrative zero-tax/fee-inclusive arithmetic, not an account tariff:

| Event | `total` | `R` | `U` | `free` when the broker cap is not lower | `used` |
| --- | ---: | ---: | ---: | ---: | ---: |
| Verified clean opening | 1,000,000 | 0 | 0 | 1,000,000 | 0 |
| Reserve a buy including a 100 cost allowance | 1,000,000 | 20,100 | 0 | 979,900 | 20,100 |
| Buy fills for 20,000 plus 100 costs | 979,900 | 0 | 0 | 979,900 | 0 |
| Sell fills for 20,000 less 200 costs | 999,700 | 0 | 19,800 | 979,900 | 19,800 |
| Broker confirms settlement | 999,700 | 0 | 0 | 999,700 | 0 |

Pending purchases are charged once at recognition, not again on their settlement
date. The equity position is then valued separately by Freqtrade, avoiding the
double-counting caused by using unsettled `dnca_tot_amt` as economic cash.
Replayed fills replace the previous cumulative recognition instead of appending
the same debit/credit again. Missing final costs retain a conservative reservation
and block final cash reconciliation until verified or explicitly configured as an
estimate; they must never silently become zero.

For each security, `total = hldg_qty`, `free = ord_psbl_qty`, and
`used = total - free`, after checking whole-share values and `0 <= free <= total`.
Compare positions to the bot's recognized fills. Never adopt an unexplained holding.

Order IDs must remain unique across business dates and account scopes. Keep the
original order number and routing identifier needed for cancellation. Required
order fields include symbol, side, type, quantity, filled, remaining, status,
price, average, cost and fee information; unknown fields use `None` where permitted.
An order accepted by KIS is open until subsequent evidence establishes a terminal
state. Partially filled/cancelled orders retain their filled quantity and costs.

Normalize `ord_qty -> amount`, `tot_ccld_qty -> filled`, `rmn_qty -> remaining`,
`ord_unpr -> price`, `avg_prvs -> average`, `tot_ccld_amt -> cost`, and
`sll_buy_dvsn_cd` (`01` sell, `02` buy) to `side`. Preserve cancelled and rejected
quantities (`cnc_cfrm_qty`, `rjct_qty`) separately: after cancellation, remaining
need not equal amount minus filled. Positive working remainder always means
`open`, even when some shares were cancelled or rejected. For example, amount 10,
filled 3, cancelled 2 and remaining 5 is still open. Terminal states require zero
working remainder and consistent whole-order evidence. A complete fill with no
cancelled/rejected quantity takes precedence as `closed`; otherwise establish
whole-order cancellation, rejection or expiry explicitly. Do not infer a terminal
state from a nonzero cancelled quantity alone. Inconsistent totals are errors.
Order time comes from `ord_dt` plus `ord_tmd` in
Asia/Seoul; it must not be presented as the timestamp of every execution.

The documented daily inquiry returns cumulative order totals without individual
execution IDs. The user accepted observation-interval synthetic fills on 2026-10-22.
For consecutive consistent snapshots `(Q0, C0)` and `(Q1, C1)`, create one record
with `amount=Q1-Q0`, `cost=C1-C0`, and weighted `price=cost/amount` when both deltas
are positive. Use `tot_ccld_amt`, not rounded average price multiplied by quantity.
This record can combine multiple actual executions. It must have
`info.synthetic=True`, observation interval boundaries and
`info.timestamp_kind=observation`; its timestamp is the observation time, never a
claimed exchange execution time. The identifier is a durable internal record ID,
not a broker execution ID.

Persist the observed snapshot and new record atomically in the existing DB, scoped
by account, business date and order identity. Repeated snapshots produce no records.
After restart, continue from the persisted watermark; an observation gap produces
one explicitly marked aggregate over that gap, not invented individual fills.
The first nonzero snapshot without earlier observations produces a recovered
aggregate with an unknown interval start, preserving its incomplete timing history.

`fetch_my_trades` returns the persisted canonical records, not the cumulative
snapshots. `since` filters observation timestamps and `limit` applies after ordering
by observation time and internal sequence. This is a documented Freqtrade-targeted
deviation from exchange-execution-time filtering; callers needing true execution
timestamps must not use it. Freqtrade's order-date filter must retrieve the complete
record set for that order without truncation. Enable `fetchMyTrades` only once this
durable retrieval contract is implemented and tested.

Require sums of canonical record amounts/costs to match the latest order totals.
Negative deltas, a changed amount with missing/inconsistent cost, or changed gross
cost without changed quantity trigger reconciliation, not a fabricated zero-size
or negative fill. Preserve prior records and block publication of an inconsistent
history until resolved. Fee-only corrections update normalized order fees separately
and never create fills; missing or estimated fees are not final execution fees.

The pinned official [private fill-notification example](https://github.com/koreainvestment/open-trading-api/blob/2077773a69a428daf63e148182511f068cc45de3/examples_llm/domestic_stock/ccnl_notice/ccnl_notice.py)
does expose event quantity, price and time over WebSocket (`H0STCNI0` production,
`H0STCNI9` paper), alongside order/cancellation acknowledgements. Only `CNTG_YN=2`
identifies a fill notification. This is a possible separate execution source, but
the sample does not establish a stable execution ID or replay of missed events.
Combining order number, second-resolution time, quantity and price is not proof of
uniqueness: identical fills can occur within the same second. Supporting complete
personal trade history through this route requires a verified identity/deduplication
and reconnect/backfill contract, plus expansion of the initial no-WebSocket scope.
The initial implementation uses the accepted synthetic-delta contract above and
does not depend on this unverified WebSocket recovery route. Exact broker execution
history remains unsupported and must not be advertised as implemented.

KRX session orders must use DAY semantics. Add DAY to Freqtrade's configuration,
types and feature validation instead of claiming GTC support. At session close,
query the broker to determine the final state; do not infer cancellation or erase
the remaining order just because the clock crossed the close. Overnight automatic
re-entry is disabled. ROI and unfilled timeouts initially measure elapsed wall
time, as Freqtrade does, but may only cause a new broker request during a verified
session. Reconcile orders before resuming strategy decisions after a session gap.

For every order-changing request, persist intent before sending. A timeout after
transmission is an unresolved order, not permission to resend. Reconcile using
broker history and durable intent metadata; if matching is ambiguous, block new
orders pending resolution. Cancellation acknowledgement is not final cancellation.
Reuse the existing Order/Trade DB where possible and add only the missing intent
state. The HTTP layer must not automatically retry trading POSTs.

Use account-specific commission and product/date-specific taxes. Keep estimates
and confirmed amounts distinct; replace estimates without double charging. Do not
hard-code a single tax rate for all stocks and ETFs. Missing cost rules block
cost-qualified dry-run/backtest validation. Whole-share rounding and price-band
tick rules must also reach Freqtrade's backtest precision calculations.

Freqtrade currently seeds both `fee_open` and `fee_close` from one side-less
`get_fee` call. Add a narrow cost-estimation hook with side, symbol, quantity,
price and timestamp, retaining the existing calculation for other exchanges.
Use it separately for entry and exit in live/dry-run and in backtesting at the
historical trade timestamp. Buy commission and sell commission plus applicable
taxes must have distinct amounts/rates. Update the exit estimate when its expected
date, price or product rule changes, including positions held across a tax change.
Do not reinterpret all equity costs as a single symmetric global `fee` setting.

Order-level confirmed cost data must replace, not add to, estimates through the
existing fee update path. KIS `prsm_tlex_smtl` is explicitly an estimated total
expense, not proof of final commission/tax. Phase 5 must identify the authoritative
final-cost source or retain estimated status and mark final-cost reconciliation
unverified; it may not advertise confirmed costs from that field. Tests must cover
asymmetric buy/sell costs, historical effective dates and repeated order snapshots.

## Implementation boundaries

`Kis(Exchange)` owns Freqtrade integration and session/price policy. Local
`KisSync(ccxt.Exchange)` and `KisAsync(ccxt.async_support.Exchange)` provide CCXT
contracts. Shared request construction, token state and parsers live in
`freqtrade/exchange/kis_adapter/`. The two facades share a serialized read transport;
the async facade runs blocking I/O and rate waits in a worker thread. Authentication
is serialized with reads so there is no concurrent token refresh. Start with
existing HTTP libraries and CCXT helpers, not a new service or generic plugin framework.

Local registration must cover exchange validation, listing and resolver/client
creation. Keep existing CCXT paths intact and reject unknown names. Do not patch
`ccxt` globals or installed package files. KIS must set
`always_require_api_keys=True` so dry-run does not remove quote credentials.
Sensitive configuration must use dedicated secret fields rather than logged
`ccxt_config` dictionaries.

### Client foundation

`KisSync` and `KisAsync` accept `apiKey`, `secret`, optional `accountId` (eight
ASCII digits), optional `uid` (two-digit account product), and
`options.environment` (`paper` by default, or explicit `live`). Quote-only clients
can omit both account fields. Creating two facades with the same `KisClient`
shares authentication and pacing; mismatched credentials or environments fail.
`Kis` now loads these clients through the normal exchange resolver. `exchange.kis`
passes the environment and sessions; standard exchange secret fields supply keys.

The transport uses Python's HTTPS implementation with certificate validation,
fixed broker hosts, no redirects and no environment proxy inheritance. It accepts
only the allowlisted GET endpoints above. Token issuance and explicitly enabled
paper cash orders/cancellations are the only allowed POSTs.
Tokens are cached in memory, renewed 60 seconds before expiry, and issuance
attempts are at least 60 seconds apart. Separate processes do not share this
budget or cache; use dedicated app keys or coordinate them operationally.

All requests are serialized and spaced at least 1.1 seconds apart in both
environments. This conservative initial policy is not a claim about an account's
quota; broker throttling still propagates as `RateLimitExceeded`. A request has
a 30-second socket timeout. Authentication expiry permits at most one GET replay,
subject to the issuance cooldown. Permission failures, throttling, transport errors
and other business failures propagate without automatic retries. Exceptions omit
broker messages, credentials, account queries and response bodies. Unknown
business errors remain `ExchangeError` until their codes are verified.

The FK100/NK100 page reader retains continuation headers/cursors and fails on a
missing cursor, repeated cursor, failed page or page limit. It never returns a
successful partial list. Private capabilities are enabled only for an explicitly
configured paper trading client; quote-only clients do not advertise them.

Each facade releases its reference on `close()`; the final release discards shared
credentials and the token. HTTP responses are closed after each request. Cancelling
an async await does not terminate an in-flight read in its worker; closing waits
for serialized work to finish. Cancelling an async order await cannot undo a
request already transmitted by the worker; its durable intent must be reconciled.
Network-isolated tests exercise the transport, concurrency, cancellation and
cleanup. No KIS order or paper-server validation is established by these tests.

## Fixtures and validation gates

`tests/testdata/kis/provenance.json` identifies the origin and environment of each
captured fixture. `ticker_005930.json` is an actual authorized production **quote**
response with no account information or credentials. It establishes quote-field
shape only. It is not a simulated fill, a paper-server response or a validation of
the adapter. Phase 1 did not send orders or query private account history.

The phase-3 master subsets, book, daily/minute pages and opening sequence are also
actual public market responses. Their provenance is recorded separately from
synthetic test schedules and faults. `live_read_verification.json` records a direct
adapter run against production quotes, including source hashes and request paths.
It covers authentication, master classification, ticker/top of book, historical
minutes, five-minute aggregation and native daily bars. Broker mutations were zero.
It does not establish paper-server compatibility or Freqtrade trading readiness.

Subsequent synthetic order/error fixtures must be identified as synthetic and
trace their field definitions to a pinned official specification. They must never
be described as observed KIS order responses.

| Gate | Evidence needed before declaring completion |
| --- | --- |
| 1: API contract | pinned baseline, endpoint/field map, explicit unknowns and fixture provenance |
| 2: client foundation | network-isolated sync/async tests, shared token/rate state, sanitized error mapping and cleanup |
| 3: market data | market classification, price rules, pagination, observed bar labels and session-boundary tests |
| 4: Freqtrade dry-run | loader/settings, DAY/session/cost integration, strategy→DB/UI evidence, zero broker mutations |
| 5: order lifecycle | cash/sellable checks, partial fills, cancel races, uncertain intent and restart reconciliation |
| 6: KIS paper server | actual paper authentication, quotes, orders/cancellations/fills and DB reconciliation; skips remain unverified |
| 7: delivery | custom image built from the reviewed source, reproducible commands, targeted regression and limitations |

Unverified endpoint/TR-ID entries in the mapping table are implementation gates,
not placeholders to fill by guessing. Phase 1 records them; the corresponding
phase cannot be declared complete until its mapping and availability are resolved.
Paper quote access has been verified; account reads and order permissions have not.
Real-account trading verification remains separate from the seven-phase delivery.

## Freqtrade dry-run integration

The exchange name is `kis`. Exchange listing identifies it as a local experimental
adapter. `dry_run=false` enables guarded broker orders only when
`exchange.kis.environment=paper`. Production writes are rejected.
`exchange.kis.environment` independently selects `paper` or `live` quotes;
it does not turn a dry-run order into a broker order.

Set `order_time_in_force` to `{"entry": "DAY", "exit": "DAY"}` in the configuration,
because exchange validation runs before strategy loading. Only a static list of
active master securities is allowed. FreqAI, order-flow data, CCXT overrides and
raw broker-response logging are rejected. Both facades retain the same quote keys
and transport; the bot's original configuration still has its secret fields removed.

The session mapping lives in `exchange.kis.sessions`. Trading dates also need an
aware `verified_at` timestamp no more than 24 hours old. The operator must verify
the actual hours, including exceptional schedules, and restart/reload with current
settings. There is no automatic exchange-announcement feed. Missing, closed,
future-dated or stale schedules block new virtual orders. Existing DAY orders can
still expire after their original close; expiry is local dry-run behavior, not a
claim about a broker cancellation. ROI and timeout clocks remain elapsed wall time.

KIS candles retain session gaps and midnight-KST daily labels in live caches,
downloads, offline loads, plots and backtests. Warm-up loads count real preceding
bars rather than elapsed minutes. Imported backtest datasets must match the verified
session labels across their covered interval; an intraday gap stops the run. Daily
backtests execute at the verified opening. Runtime daily signals publish at the next
verified opening, avoiding consumption of the new daily signal before trading starts.
Daily signal freshness and optional entry-signal expiry use that publication time,
so a verified weekend or holiday does not make the previous session's bar stale.
An omitted intervening calendar date or missing business-day candle still blocks it.
Backtest fills, trade closure, callbacks and protections share the execution clock;
intraday detail timestamps keep their actual time.
An intraday paper strategy cannot obtain warm-up bars from a previous paper date.

`exchange.kis.costs` maps each symbol to dated rules with `from`, `through`, `source`,
`tax_treatment`, `buy_commission`, `sell_commission` and `sell_tax`. Rates are decimal
fractions. Exactly one rule must cover the trade date. Supported treatments are
`stock_transaction_tax` and `domestic_equity_etf` (zero transaction tax). NAV/profit
withholding is unsupported. The model conservatively rounds commission and tax
separately upward to whole KRW and retains their estimated status. It is not a claim
about the account's actual billing-rounding convention. Supply verified product,
date and account rates before interpreting results as account-cost validation.
Symmetric global `fee` configuration is rejected. Exit estimates refresh at the
current execution date, including a changed tax rule after an overnight hold.

The real paper-quote run in `tests/testdata/kis/dryrun_verification.json` used an
explicit validation-only strategy and synthetic, labelled cost rules. It verifies
the signal → virtual fill → DB → FreqUI integration, not profitability or actual
account charges. Two virtual shares bought at 224,000 KRW and sold at 223,000 KRW
produced estimated costs of 448 and 1,338 KRW and a net loss of 3,786 KRW. The DB,
authenticated API and captured FreqUI display agreed. The broker audit contained
only token issuance and quote reads: zero order/cancel requests. The independent
existing Kraken dry-run service was not replaced by this validation process.

## Paper order lifecycle implementation

Paper trading requires an initialized, durable SQLite Freqtrade database and the
standard secret fields `exchange.key`, `secret`, `account_id` (eight ASCII digits)
and `uid` (two ASCII digits). Keep account fields as strings in a private JSON
configuration: the generic environment loader converts numeric values to numbers,
which loses leading zeroes. Never put secrets into `ccxt_config` or committed
examples. The database baseline is bound to the environment and credential scope;
changing credentials requires explicit reconciliation and migration.

One serialized client owns the DB through a local file lock. This prevents two
clients writing the same database; it is not an account-wide distributed lock.
Use one dedicated account and app key per bot, with no concurrent manual trades
or separate bot database. Position adjustment and skipping startup order updates
are rejected. Close the bot before backing up the SQLite database, including all
`kis_*` tables and KeyValueStore entries.

Before the first order, repeated complete history and balance reads must agree
on an empty, settled, cash-only account. Subsequent snapshots must match the
persisted cash ledger and owned positions. An external order or unexplained cash
movement creates a durable reconciliation block. Owned order/history queries
continue during that block so fills can still be observed. New mutations require
the account to reconcile and a fresh verified open trading session.

`exchange.kis.settlement_calendar` is a date-keyed mapping whose records contain
`settles` (boolean) and `source` (nonempty verified reference). Supply every calendar
date after an order through its second settlement day, including closed days.
The adapter does not infer weekdays or download a holiday feed. Each order retains
its established settlement date. Sale proceeds remain reserved until that date
and an agreeing broker cash snapshot; missing calendar data blocks submission.

An order intent is committed before the single paper POST. A timeout or malformed
acknowledgement stays unresolved and is never automatically resent. A later
complete inquiry can match one previously unclaimed order by symbol, side, type,
quantity, limit price and the bounded submission-time interval. No match stays
blocked; multiple matches require operator reconciliation. This relies on the
dedicated-account restriction because the broker provides no client idempotency
key. Restart recovery imports owned orders missing from the Freqtrade DB before
new strategy activity. Import completion is acknowledged only after the Freqtrade
accounting commit; an interruption replays unfinished accounting even when the
Order row already exists. Terminal zero-fill entries retain canonical history
without creating an economic Trade. Fully or partially filled orders update
positions and estimated costs from cumulative totals without replaying earlier fills.

Cancellation first queries eligibility and validates the original routing branch
and remaining whole quantity. The implementation uses the documented `TTTC0084R`
on the paper host because the pinned source documents no paper-specific ID; actual
paper availability remains unverified. It does not invent a `V`-prefixed ID or
send a cancellation when eligibility fails. An accepted cancel response leaves the
order open until cumulative history establishes the terminal state. Late fills
remain recognized. Prior-day DAY orders are queried, never declared canceled
merely because the clock passed their close.

The local canonical fill IDs and watermarks survive restart in SQLite. Quantity or
gross-cost regressions, inconsistent identity/state transitions and corrupted
canonical totals block publication of contradictory history. A fee-only correction
does not create a synthetic fill. Partial entry/exit quantities and weighted gross
averages reach Freqtrade accounting even while the order remains open.

Costs remain explicitly estimated. The pinned period-profit inquiry groups fees
and tax by date/product rather than providing an order identity; it is not an
authoritative per-order cost source. Final-cost reconciliation is unverified.
An estimated settlement cash difference blocks trading rather than silently
adjusting the baseline. There is no automatic unblock or intent-deletion command:
stop the bot, preserve the DB, compare broker history and resolve the discrepancy
before any supervised repair. Deleting state or resending an unresolved request
can duplicate orders and is not a recovery procedure.

`tests/exchange/kis_adapter/test_orders.py` and `test_trading.py` use explicitly
synthetic responses with network access blocked. They cover restart and lost
responses, partial fills, cancellation races, atomic watermarks, settlement holds,
external activity and Freqtrade DB import/accounting. These checks do not establish
real paper account compatibility. On 2026-10-22 the collector's three profiles
authenticated and read paper quotes, but all three paper balance probes failed.
A subsequent read-only retry returned `EGW02007`, explicitly stating that the app
key was not a paper-trading key. A separate paper key/account binding is required.
No broker order/cancel request was sent. Phase 6 must verify private endpoint
availability, real paper fills/cancellations and DB reconciliation with a working
paper-account binding before this adapter can be described as validated for paper
trading.
