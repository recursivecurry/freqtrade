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
server. Live trading requires an explicit environment selection and a separate
operational validation. The default example must use dry-run and paper mode.
The existing ETF collector uses the production quote server: its credentials may be
used for authorized read-only checks, not assumed to be paper credentials.

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
| `fetch_order`, `fetch_orders`, `fetch_open_orders` | `trading/inquire-daily-ccld` | within 3 months `TTTC0081R` / `VTTC0081R` | business-date range, account, symbol and order ID; cumulative order totals, not individual executions |
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

`005930/KRW` maps to KIS code `005930`, preserving leading zeroes. Market metadata
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
Use the existing KeyValueStore for the account-scoped baseline and Order/Trade
records for subsequent cash movements; store no account number in public IDs.

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
execution IDs. Therefore the initial adapter declares `fetchMyTrades=False` and
raises `NotSupported` for `fetch_my_trades`; it must not fabricate executions or
sum successive snapshots. Freqtrade instead reconciles cumulative `fetch_order`
fields and normalized order fees. This is a deliberate deviation from issue #1's
initial execution-history requirement and must be recorded there before phase 1
is accepted. Execution-level history remains unimplemented until an authoritative
source with stable identity and timestamp is established. It is not a phase-1 claim
that aggregate data fully meets CCXT's personal-trade contract.

The pinned official [private fill-notification example](https://github.com/koreainvestment/open-trading-api/blob/2077773a69a428daf63e148182511f068cc45de3/examples_llm/domestic_stock/ccnl_notice/ccnl_notice.py)
does expose event quantity, price and time over WebSocket (`H0STCNI0` production,
`H0STCNI9` paper), alongside order/cancellation acknowledgements. Only `CNTG_YN=2`
identifies a fill notification. This is a possible separate execution source, but
the sample does not establish a stable execution ID or replay of missed events.
Combining order number, second-resolution time, quantity and price is not proof of
uniqueness: identical fills can occur within the same second. Supporting complete
personal trade history through this route requires a verified identity/deduplication
and reconnect/backfill contract, plus expansion of the initial no-WebSocket scope.
Until the user accepts the cumulative-only contract or that alternative is verified,
the execution-history scope decision remains a phase-1 merge blocker.

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
`freqtrade/exchange/kis_adapter/`; sync and async HTTP transports must not block
one another or refresh tokens concurrently. Start with existing HTTP libraries
and CCXT helpers, not a new service or generic plugin framework.

Local registration must cover exchange validation, listing and resolver/client
creation. Keep existing CCXT paths intact and reject unknown names. Do not patch
`ccxt` globals or installed package files. KIS must set
`always_require_api_keys=True` so dry-run does not remove quote credentials.
Sensitive configuration must use dedicated secret fields rather than logged
`ccxt_config` dictionaries.

## Fixtures and validation gates

`tests/testdata/kis/provenance.json` identifies the origin and environment of each
captured fixture. `ticker_005930.json` is an actual authorized production **quote**
response with no account information or credentials. It establishes quote-field
shape only. It is not a simulated fill, a paper-server response or a validation of
the adapter. This phase did not send orders or query private account history.

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
No paper credentials have been identified yet. Production quote access does not
authorize or prove paper/live order functionality. Real-account trading verification
remains separate from the seven-phase delivery.
