# KIS candle and investor-flow extension

The extension tracked in [issue #1](https://github.com/recursivecurry/freqtrade/issues/1)
supports `1m`, `5m`, `15m`, `30m`, `1h`, `1d`, `1w` and `1M` candles.
Completed bars retain KST session boundaries and unadjusted prices.
Investor flow is a separate data source; it does not change CCXT's six OHLCV fields.

## Candle boundaries and history

Intraday aggregates start at the verified session opening. A final short bucket
ends at the start of the closing auction; the closing print remains a separate
bar. No bucket spans the ten-minute auction collection interval or an overnight
gap. The new intervals accept short final continuous buckets and exceptional
opening times. The existing five-minute alignment and five-minute closing-print
delay are retained; other intervals expose the closing print after its source
minute is complete. Runtime refresh and signal expiry use these actual endpoints.

Weekly and monthly bars aggregate unadjusted native daily OHLCV. Labels are Monday
midnight KST and the first calendar day of the month, respectively. These labels
may be holidays; backtest executions map to the period's first verified session
opening. A completed period's runtime signal publishes at the next verified
trading opening. Current days and unfinished calendar weeks/months are excluded.
Native daily volume can include activity outside the minute source's regular
session, so it must not be described as a sum of regular-session minute volume.

Intraday history remains limited to 365 calendar days in production and the
current day in paper mode. Daily, weekly and monthly history accepts starting
dates up to 3,660 days back, rounded to the beginning of the requested period.
All covered dates require an explicit verified calendar, including weekends and
holidays. Each daily request is paginated backward, with at most 100 pages.
Insufficient history fails instead of shortening the strategy's warm-up. There
is no claim that every listed security has ten years of broker history.

Download/cache paths preserve the period labels. Backtests visit the actual
session-labelled rows rather than advancing a monthly simulation by fixed
30-day intervals. Continuous-market backtests retain their existing time grid.

KIS informative data uses the exchange's publication clock, including during
historical callback slicing and vectorized indicator merges. The `@informative`
decorator supplies that clock automatically and bypasses its fixed-interval merge
cache for KIS. Manual calls must use
`merge_informative_pair(..., publication_time=self.dp.candle_publication_time)`.
The generic helper rejects tagged KIS frames without this clock. Preserve the
frame's metadata when constructing manual informative data; arbitrary external
frames cannot be identified as KIS data automatically.

## Verified API contracts

| Data | Endpoint under `/uapi/domestic-stock/v1/quotations/` | TR ID | Meaning |
| --- | --- | --- | --- |
| Foreign/institution estimates | `foreign-institution-total` | `FHPTJ04400000` | Ranked, incomplete list; first 30 rows per requested sort in the captured responses |
| Dated investor totals | `investor-trade-by-stock-daily` | `FHPTJ04160001` | Dated foreign, institution and individual net amounts; current date becomes available after the broker's post-close calculation |
| Recent investor totals | `inquire-investor` | `FHKST01010900` | Recent dated rows; current-day data is supplied after market close |
| Daily/weekly/monthly prices | `inquire-daily-itemchartprice` | `FHKST03010100` | `FID_PERIOD_DIV_CODE=D/W/M`; native data availability does not establish runtime integration |

The [official estimate schema](https://apiportal.koreainvestment.com/apiservice-apiservice?/uapi/domestic-stock/v1/quotations/foreign-institution-total)
defines its monetary fields as **million KRW, quantity multiplied by current price**.
The adapter will multiply the broker field by 1,000,000, preserving that valuation
basis and the estimate label. It must not call this an executed buy-minus-sell
cash amount. It must not calculate missing amounts from quantities itself.
The broker's estimates are entered several times during the day; polling more often
does not create new observations at the broker.

The [official dated-flow schema](https://apiportal.koreainvestment.com/apiservice-apiservice?/uapi/domestic-stock/v1/quotations/investor-trade-by-stock-daily)
also defines million KRW. Its current-day calculation starts after 15:40 KST and
publication time varies. Post-close values can still be revised; they are not
labelled immutable final data. The recent-total endpoint's captured gross amounts
also agree with million-KRW scaling, but the dated endpoint is used for the
extension's explicit unit contract.

Estimate rows contain no business date or exact broker update timestamp. Preserve
both as unknown; record the local observation date/time separately. Membership in
the ranked response does not prove data freshness. A missing symbol is unavailable,
not zero. Individual intraday amounts remain unavailable. Do not infer individual
activity by negating foreign plus institution activity, because other investor
categories exist.

## Read-only observations on 2026-10-22

`tests/testdata/kis/flow_contract.json` contains selected public fields captured
after market close. Production returned 30 buy-ranked rows, 30 sell-ranked rows
and 30 dated-flow rows. Three dated rows are retained in the fixture. Estimate
amounts agree with the documented current-price valuation within one million KRW
of source rounding. Dated gross buy prices corroborate the documented unit.
These are response-contract observations, not observations of intraday updates.

Paper credentials returned estimates and recent investor rows in this probe,
although the official estimate schema declares paper unsupported. A separate
dated-flow paper call raised `PermissionDenied`. The extension therefore enables
flow collection only on the production **read-only** connection; paper/dry-run bots
can consume observations already stored by that collector. Production order writes
remain disabled. No broker order or cancellation was sent for these checks.

## Delivery and acceptance

1. Record actual responses, source units, list scope, unknown timestamps and
   environment limitations. Test the serialized read transport and captured units.
2. Extend candle aggregation and runtime/backtest clocks. Check session gaps,
   closing auctions, exceptional schedules, month/year transitions, incomplete
   periods, history bounds and insufficient warm-up data.
3. Collect and persist flow observations with their source, basis, availability
   reason and receive time. Expose point-in-time strategy columns. Revisions must
   remain distinguishable and observations received after a decision time must
   never appear at that earlier time in backtests.

Each stage requires its own PR and GPT-6 Astra medium review, corrections until
approval, then merge. Actual paper-order acceptance remains a separate incomplete
check in issue #1; data development does not resolve its account-binding error.
