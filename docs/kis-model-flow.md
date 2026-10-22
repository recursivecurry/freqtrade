# KIS model-based intraday investor flow

[Issue #11](https://github.com/recursivecurry/freqtrade/issues/11) adds a separate
model estimate. Broker preliminary reports and post-close reports keep their
existing fields and archive. A model prediction must never fill a missing broker
amount or be presented as an executed cash amount.

## Delivery stages

1. Collect dated member/program inputs with explicit units and receipt windows.
2. Train and evaluate a per-symbol ridge model with chronological splits; save
   portable JSON artifacts with training and availability dates.
3. Archive research predictions and expose a separately labelled point-in-time
   strategy interface; provide observation commands.
4. Validate matched intraday references and gate strategy activation on evidence.

Each stage has its own PR, GPT-6 Astra medium review and approval before merge.
Merging code does not establish intraday accuracy or authorize its production use.

## Input contract

The implementation is independently written against the official KIS schemas.
It does not copy the private reference project's code or require its external
Python wrapper, pickle files, credentials, or absolute paths.

| Endpoint | TR ID | Fields |
| --- | --- | --- |
| `inquire-member-daily` | `FHPST04540000` | `ntby_qty`, `total_shnu_qty`, `total_seln_qty` |
| `program-trade-by-stock-daily` | `FHPPG04650201` | `whol_smtn_ntby_qty` |
| `investor-trade-by-stock-daily` | `FHPTJ04160001` | `frgn_ntby_qty`, `orgn_ntby_qty`, `prsn_ntby_qty` |

Scope is KRX (`J`) only. There is no claim of matching NXT or combined-market
coverage. Member codes are explicit five-digit codes, with at most 20 per symbol.
All quantities are signed whole shares, including valid zero. Institution means
the broker's `orgn_ntby_qty` total; pension is not added again. Individual activity
is its own training target, never the negative sum of other participants.

Rows are matched by business date, independent of response order. Missing targets
or member/program rows exclude a history date; training must check its remaining
sample count. Missing intraday inputs reject the entire snapshot. A continuation
header rejects the response rather than treating a partial page as complete.
History must end before the collection date. No artificial minute history is
reconstructed from daily data. An active verified session is required for live
snapshots; they may not span the close or exceed the collection window (120 seconds
by default, at most 300). These limits concern receipt skew, not exchange latency.

Each source retains its endpoint/TR ID, request start, receipt time and response
hash. Source update time remains unknown when absent from the API. Same values
across polls do not prove stalled data, and a recent receipt does not prove a
recent source update. Source freshness is explicitly `source_update_unknown`.
No automatic request retry replaces a failed input with zero.

## Evaluation and activation policy

Set these requirements before collecting evaluation results. Daily evaluation
uses at least 20 preceding complete dates per fold and at least 10 held-out dates.
Member selection and scaling must be fitted again within each training fold.
Report each participant's MAE, R² and direction agreement, and foreign-minus-
individual signal metrics, alongside zero and preceding-training-mean baselines.
Daily R² is neither an intraday confidence probability nor a confidence interval.

Initial intraday observation requires at least 10 verified trading dates for each
symbol proposed for activation, covering 09:00–10:00, 10:00–14:00 and 14:00–close
KST, with at least 30 valid aligned reference/prediction pairs in each window.
Record actual poll durations, call counts, missing-input rates and receipt skew.
Comparable reference data must describe the same participant, market, cumulative
interval and observation time. A morning prediction compared with end-of-day flow
does not establish morning accuracy. Without such references, intraday accuracy
and activation remain unverified.

For a candidate symbol/window, require positive held-out R², MAE below both
baselines, and direction agreement above 50% for each participant and the signal.
Report sample counts and all failing windows; do not select windows on the same
data and then call the selected result independent validation. Any tuning requires
a new untouched validation period. These initial gates establish data quality,
not profitability. Price/amount estimation and interval deltas are deferred until
their separate contracts and validation exist; v1 provides cumulative share
estimates only.

Until the evidence exists, predictions are research observations, hidden from
strategy numeric inputs. Actual broker update cadence, response coverage, intraday
accuracy and live data-to-display verification remain pending. Unit tests and
synthetic fixtures cannot complete those checks. Existing official flow stays
available when model estimates are unavailable or disabled.

## Daily model artifacts

`flow_model.train_model(history, now)` uses the normalized history bundle and
requires at least 30 complete dates (20 training, 10 evaluation by default).
Candidate members must exist on every included date. Within each fold, rank
members by preceding gross share activity; scale each feature by its preceding
root-mean-square magnitude. Fit a ridge regression with no intercept and no mean
centering, so zero cumulative inputs predict zero activity. The program feature
is an explanatory variable, not a separate participant. Targets remain independent.
The same target names are used by training, prediction and evaluation.

The report includes participant and foreign-minus-individual metrics, chronological
fold dates and selected members. An eligible daily report does not mark intraday
accuracy as verified. Training history must have been received before creation;
its latest date must be before creation's KST date and no more than seven calendar
days old. Predictions also enforce that age limit, model expiry (seven days after
creation), matching market/symbol/unit, complete inputs and a maximum five-minute
input age. Long holidays may therefore leave estimates unavailable.

Artifacts are immutable JSON, created exclusively with `0600` permissions on POSIX. Numeric
arrays, target definitions and availability dates are checked when loaded. A SHA-256
identifier binds coefficients, training metadata and metrics. It detects accidental
modification, not malicious rewriting by a trusted file owner. No pickle executes
when loading a model. Preserve each artifact and its source history to reproduce
predictions; later corrections create a new model identifier.
Schema 2 also binds the candidate universe and fitting parameters into a `recipe_id`,
so a rolling daily model can share a validation protocol without pretending that
one seven-day artifact covers ten trading days. Recreate earlier schema-1 artifacts
from retained history. Windows file privacy requires the user's account ACL; POSIX
mode bits are not an ACL guarantee on Windows.

## Research collection and display

Use the same separate production read-only configuration as the official investor
collector, with an explicit whitelist and verified calendar. No command starts a
bot or permits broker mutations. Keep paths outside the repository's tracked files.
The parent directories must exist; history and model outputs never overwrite files.

```bash
python -m freqtrade.exchange.kis_adapter.model_flow history \
  --config user_data/kis-flow.config.private.json --symbol 005930/KRW \
  --members 00003 00050 --since 2026-08-01 --until 2026-10-21 \
  --output user_data/flow-history.json
python -m freqtrade.exchange.kis_adapter.model_flow train \
  --config user_data/kis-flow.config.private.json \
  --history user_data/flow-history.json --output user_data/flow-model.json
python -m freqtrade.exchange.kis_adapter.model_flow collect \
  --config user_data/kis-flow.config.private.json --model user_data/flow-model.json \
  --database user_data/kis-flow.sqlite --interval 300
python -m freqtrade.exchange.kis_adapter.model_flow inspect \
  --database user_data/kis-flow.sqlite --symbol 005930/KRW
```

Training refuses unknown calendars and active trading sessions. Collection defaults
to one poll; continuous intervals must be at least 60 seconds and add to the time
spent collecting, rather than promising a fixed per-symbol cadence. One process
can load multiple distinct, allowlisted symbol models via repeated `--model`.
A per-database model lock rejects simultaneous model collectors. Every prediction
retains its input snapshot, receipt hashes, model ID, training date, metrics and
elapsed collection time. Failures append null quantities and a redacted reason,
preventing an older success from silently surviving a failed poll.

Research quantities are accessible through the explicit `inspect` command, labelled
`장중 추정 수급 · 모델 추정치`. Strategies call:

```python
dataframe = self.dp.kis_model_flow(dataframe, metadata["pair"], timeframe=self.timeframe)
```

The new `kis_model_*` namespace is independent of official `kis_estimate_*` and
`kis_daily_*` fields. It adds participant `_shares` columns, `status`, `label`,
`unit`, `model_id`, `business_date`, `train_end_date`, `basis`, `intraday_validation`
and `observed_at`. All strategy quantities remain null for `research_only` records.
Uncollected, unknown-clock, failed and stale records carry distinct statuses. The
reader uses the KIS candle publication clock and a maximum five-minute observation
age; a later model/record cannot rewrite an earlier decision. Missing archives are
read-only and return missing values. The existing official reader now also labels
its preliminary and post-close data explicitly in separate label columns.

`inference_at` records inference completion. A collection batch publishes its
records only after collecting the batch; it never backdates publication to each
earlier inference. Input age is checked from the original collection start, so
delayed publication cannot make old inputs fresh. No existing strategy opts in
automatically; numeric columns require the explicit evidence gate below.

## Matched-reference validation and activation

An evidence JSON contains `protocol="kis-flow-intraday-v1"`, a
`protocol_registered_at` millisecond timestamp preceding all evaluated collection,
`symbol`, `recipe_id`, complete `models`, independently verified date-keyed
`sessions`, and `pairs`. Each pair contains an archived `prediction` and a
`reference`. Retain the original independent reference responses outside Git.

Reference fields are `symbol`, `market="KRX"`, `unit="shares"`, `business_date`,
`frequency="intraday"`, `basis="reported_cumulative_net_quantity"`, `source`,
`response_sha256`, `cumulative_from` (verified session opening), `asof`,
`available_at` and `targets` with foreign/institution/individual quantities.
`asof` must match that prediction input snapshot's `available_at`; reference
publication may be later but may not be in the evaluator's future. A post-close
daily total is not an intraday reference. Do not relabel or interpolate daily data
to manufacture this evidence.

The evaluator reloads the original artifacts and reproduces each prediction from
its original snapshot at its inference time. Duplicate input timestamps, later
models, incompatible recipes, units, dates, market scope and references fail.
The report requires ten distinct dates and thirty pairs in **each** opening,
midday and afternoon window. All participant and signal metrics must pass the
predeclared gates; incomplete or poor-quality evidence returns `eligible=false`.
The zero baseline and the preceding-training-mean baseline (scaled by elapsed
session fraction for intraday comparisons) are reported separately. All evaluated
dates must be within sixty calendar days, with the latest within fourteen days.

The protocol date, session provenance, reference source and response hash are
operator-supplied evidence, not cryptographic proof of authenticity. Review those
original records before using the evidence for activation. The evaluator verifies
consistency and reproducibility; it cannot establish that an external supplier's
data or an operator's declared timestamps are truthful. Alignment is explicitly
`receipt_time_with_unknown_source_latency`, since KIS input update times are absent.
Keep that limitation when interpreting measured accuracy.

```bash
python -m freqtrade.exchange.kis_adapter.model_flow validate \
  --evidence user_data/intraday-evidence.json --output user_data/intraday-report.json
python -m freqtrade.exchange.kis_adapter.model_flow collect \
  --config user_data/kis-flow.config.private.json --model user_data/flow-model.json \
  --database user_data/kis-flow.sqlite \
  --validation-evidence user_data/intraday-evidence.json --interval 300
```

The validation command needs no broker credentials and returns nonzero for an
ineligible report. The collector re-evaluates the evidence, rather than trusting
an editable report's success flag. Multiple symbols accept repeated evidence paths.
Only a matching recipe, passing current model daily metrics and an unexpired report
can produce `availability=validated`. Reports expire after the fourteenth calendar
day following their latest reference date. Passing evidence applies only to future
collections; it does not turn earlier research records into validated history.
The strategy reader also enforces report availability before input collection and
expiry at the candle decision. Removing the evidence option restores research-only
collection; official investor fields remain independent.

## Validation status

On 2026-10-23 KST, the production read-only history command collected thirty complete
dates for `005930/KRW` and members `00003`/`00050`, covering 2026-09-07 through
2026-10-22. Four quotation receipts and source hashes are recorded in
`tests/testdata/kis/model_input_verification.json`. The collector exposes GET-only
quotation paths and no broker mutation action. This check validates post-close
input normalization and overlap for that symbol/member set, not intraday coverage,
source update cadence, training quality or the entire candidate universe.

Synthetic offline tests exercise passed/failed evidence, reproducibility, duplicate
observations, future data, expiry, separate labels and as-of numeric gating. They
are not empirical intraday accuracy evidence. No eligible real intraday evidence
is supplied or activated by this change. Live data-to-display, polling/load
measurements and the ten-day matched-reference observation remain pending, so
issue #11 must stay open. The broader Windows CI matrix also has existing KIS
fixture/locale/permission failures; passing the scoped local tests does not certify
that matrix.

## Official references

- [Member daily schema example](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_member_daily/inquire_member_daily.py)
- [Program daily schema example](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/program_trade_by_stock_daily/program_trade_by_stock_daily.py)
- [Investor daily schema example](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/investor_trade_by_stock_daily/investor_trade_by_stock_daily.py)
