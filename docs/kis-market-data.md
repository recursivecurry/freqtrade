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

The implemented collector was also run after close against production quotes for
two symbols. `tests/testdata/kis/flow_collection_verification.json` records four
stored observations, source hashes and the audited request paths. A repeated run
inserted zero duplicates. The audit permitted only token issuance and GET requests;
broker order/cancel attempts were zero. This does not verify intraday updates.

## Collect and use investor observations

Use a separate private configuration with `exchange.name=kis`,
`exchange.kis.environment=live`, production quote credentials, a static
`pair_whitelist` and verified current-day session hours. The collector creates
no bot, accepts no order action, and requests only public investor quotations.
Do not replace a paper trading bot's credentials with these production keys.

```bash
python -m freqtrade.exchange.kis_adapter.investor_flow \
  --config user_data/kis-flow.config.private.json \
  --database user_data/kis-flow.sqlite
```

The default runs once. `--interval 300` keeps one process and its memory-only token
alive between polls; intervals below 60 seconds are rejected. Stop with Ctrl-C.
For separate one-shot processes, leave at least 60 seconds between token requests.
The parent folder must exist. The SQLite file is created with permissions `0600`;
existing paper-order state is independent. No collector is started automatically.

Each poll reads the buy/sell rankings once for the whole configured symbol list,
then the dated report for each symbol. A symbol absent from both ranking windows
is unavailable. Conflicting overlapping ranking rows are also unavailable. The
latest dated report and its returned history are retained; current-date rows
require a configured session and at least ten minutes after its close. Unknown
close schedules exclude the current-date row. The official calculation's variable
publication delay can still mean that only a prior date is available.

Successful rows and failures are appended atomically. Consecutive identical
observations keep their first observation time; an A-to-B-to-A revision retains
all three versions. Clock rollback and different values at one timestamp fail
instead of rewriting history. Failed polls produce explicit unavailable
observations, so the strategy does not silently keep using the previous success.
The stored payload contains source/unit/basis, receive time, observation date,
source business date when supplied, amounts, participant-specific reasons and
the dated report's returned history. No keys, account IDs or tokens are stored.

Set `exchange.kis.flow_database` to that file in the consuming bot/backtest
configuration. The default is `user_data_dir/kis-flow.sqlite`. A paper or dry-run
bot can read the same archive without authenticating to the production server.
In `populate_indicators`, opt in explicitly:

```python
dataframe = self.dp.kis_investor_flow(
    dataframe, metadata["pair"], timeframe=self.timeframe
)
```

The result adds nullable whole-KRW columns:

- `kis_estimate_foreign_krw`, `kis_estimate_institution_krw` and
  `kis_estimate_individual_krw` (the individual intraday column is always missing).
- `kis_daily_foreign_krw`, `kis_daily_institution_krw`,
  `kis_daily_individual_krw` for the latest returned dated report.
- For each kind, `status`, `basis`, `business_date`, `observed_at` and each
  participant's `reason`, using the same `kis_estimate_` or `kis_daily_` prefix.

Estimate values always carry `business_date_unknown` when available: the broker
did not date them. A recent receive time does not prove the underlying estimate
is fresh. Estimates from a different observation date or more than 180 minutes
before the decision are hidden as `stale_observation`; callers may set a positive
`max_age_minutes` explicitly. Dated reports retain their source business date and
`reported_revisable` status. They are not intraday individual data or immutable
final figures. Numeric zero remains zero; unavailable amounts remain missing.

For every candle, the reader uses its exchange publication time and selects only
observations already received by that time. The archive is read-only in strategies;
missing archives return missing columns with reasons and do not create files.
Historical reports first downloaded today cannot establish what a strategy knew
on an earlier date. Backtests before collection began therefore have no historical
point-in-time flow, even if the broker returns those historical dates today.

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
