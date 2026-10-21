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
| daily `fetch_ohlcv` | `quotations/inquire-daily-itemchartprice` | verify before implementation | requested date range and adjustment flag; do not mix adjusted and unadjusted prices |
| `fetch_balance` | `trading/inquire-balance` | `TTTC8434R` / `VTTC8434R` | `CANO`, `ACNT_PRDT_CD`, positions in `output1`, account summary in `output2` |
| cash buying-power check | `trading/inquire-psbl-order` | `TTTC8908R` / `VTTC8908R` | `PDNO`, price/order type; use `nrcvb_buy_amt` and `nrcvb_buy_qty`, never `max_buy_*` credit-inclusive values |
| `create_order` buy / sell | `trading/order-cash` | buy `TTTC0012U` / `VTTC0012U`; sell `TTTC0011U` / `VTTC0011U` | string quantity/price, `EXCG_ID_DVSN_CD=KRX`; acceptance is not a fill |
| `fetch_order`, `fetch_orders`, `fetch_open_orders`, `fetch_my_trades` | `trading/inquire-daily-ccld` | within 3 months `TTTC0081R` / `VTTC0081R` | business-date range, account, symbol and order ID; inspect cumulative quantities and cancellation fields |
| cancellation eligibility | `trading/inquire-psbl-rvsecncl` | verify before implementation | obtain eligible quantity and original order routing identifiers before cancellation |
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

For KRW, define settled cash, cash reserved by open orders and other settlement
amounts separately. Preserve their KIS meanings rather than changing `total` to
match buying power. Check cash-only buying power immediately before buying, and
sellable quantity before selling. The shared collector account is not a dedicated
trading account: no existing holding may be silently adopted or sold by the bot.

Order IDs must remain unique across business dates and account scopes. Keep the
original order number and routing identifier needed for cancellation. Required
order fields include symbol, side, type, quantity, filled, remaining, status,
price, average, cost and fee information; unknown fields use `None` where permitted.
An order accepted by KIS is open until subsequent evidence establishes a terminal
state. Partially filled/cancelled orders retain their filled quantity and costs.

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
