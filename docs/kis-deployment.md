# KIS development deployment

The adapter's implementation and automated checks are available in this fork.
Actual paper-account orders, cancellation availability, fills and final costs
remain unverified until separate KIS paper credentials are available. See the
[adapter contract and limitations](kis-adapter.md). Production writes are disabled.

## Build the fork

Run these commands from a clean checkout of the reviewed commit. The KIS Compose
file uses the repository Dockerfile and copies this fork into the image. Its
separate project name, data directory and localhost port leave the existing
default Compose service independent.

```bash
export KIS_SOURCE_REVISION="$(git rev-parse HEAD)"
export KIS_IMAGE_TAG="$(git rev-parse --short=12 HEAD)"
docker compose -f docker-compose.kis.yml build kis
docker image inspect "freqtrade-kis:$KIS_IMAGE_TAG" \
  --format '{{.Id}} {{index .Config.Labels "org.opencontainers.image.revision"}}'
```

Keep the image ID, revision and validation output together. A revision label is
metadata; the smoke output also hashes the imported adapter and accounting source
files. Tags alone do not identify immutable image contents. Build dependencies and
FreqUI installation require network access.

## Verify without a broker account

The standalone smoke driver uses captured public quote fixtures and explicitly
synthetic private responses. It needs no keys and runs with networking disabled:

```bash
docker run --rm --network none --entrypoint python \
  --mount "type=bind,source=$PWD/tests,target=/checks,readonly" \
  "freqtrade-kis:$KIS_IMAGE_TAG" /checks/kis_container_smoke.py
```

Five fresh Python processes execute preflight, submission, a partial-fill
reconciliation, remaining-quantity cancellation and restart reconciliation against
one temporary DB. The final assertions require three held shares, seven canceled
shares, one canonical synthetic fill and 969,970 KRW economic cash under synthetic
cost rules. Networking is also rejected by the driver. Its temporary broker/DB
state is removed after execution; it does not run a long-lived service.

This verifies image packaging, actual bot initialization, SQLite persistence and
restart accounting. It does not verify a KIS account, real executions or account
tariffs. `phase6_complete` remains false. Native checks use:

```bash
python tests/kis_container_smoke.py
pytest tests/exchange/kis_adapter tests/freqtradebot tests/persistence \
  tests/optimize/test_backtesting.py tests/exchange/test_kraken.py \
  tests/exchange/test_exchange_utils.py
```

## Configure a separate KIS bot

```bash
mkdir -p user_data/kis
cp config_examples/config_kis.example.json user_data/kis/config.json
cp config_examples/config_kis.private.example.json user_data/kis/config.private.json
chmod 600 user_data/kis/config.private.json
```

Fill the private file's `exchange.api_key`, `secret`, `account_id` and `uid` with
the selected environment's credentials. Account fields must be JSON strings so
leading zeroes survive. Leave account fields empty for quote-only dry-run when
they are not required. Never place credentials in Compose build arguments,
`ccxt_config`, tracked files or shell command arguments.

The examples default to paper quotes, dry-run, a stopped bot and the bundled
no-signal `KisValidationStrategy`. Credentials and calendars are empty, so they
cannot trade as shipped. Fill `exchange.kis.sessions`, `settlement_calendar` and
`costs` with verified schedules and account/product rates using the
[documented formats](kis-adapter.md#paper-order-lifecycle-implementation).
Supply all required history dates and fresh trading-day verification timestamps.
An intraday paper strategy cannot load previous-day paper minutes; collect enough
current-day warm-up data or use a supported daily strategy and verified history.

Start only after configuration is ready:

```bash
docker compose -f docker-compose.kis.yml up -d kis
docker compose -f docker-compose.kis.yml logs --tail 100 kis
docker compose -f docker-compose.kis.yml stop kis
```

The default strategy produces no signals. To exercise a chosen strategy, put it
under `user_data/kis/strategies` and override the service command explicitly:

```bash
docker compose -f docker-compose.kis.yml run --rm kis trade \
  --config /freqtrade/user_data/config.json \
  --config /freqtrade/user_data/config.private.json \
  --strategy YourStrategy
```

That remains dry-run unless an explicit local configuration overrides it. Set
`initial_state=running` when ready to let the chosen strategy process entries.
DAY lifetimes, verified session gates, whole shares and cost rules still apply.

For FreqUI, add an `api_server` section to the private configuration with
`enabled=true`, `listen_ip_address=0.0.0.0`, `listen_port=8080`, a username, a strong
password and a fresh JWT secret. The mapped address is `http://127.0.0.1:8082`.
The examples leave the API disabled. Existing Tailscale routes are unchanged;
any additional route should target this separate local port.

## Prepare actual paper verification

Stop the KIS service before running the one-shot validator. Use separate paper
credentials and the paper overlay, which selects the dedicated paper DB instead
of the virtual dry-run DB:

```bash
docker compose -f docker-compose.kis.yml stop kis
docker compose -f docker-compose.kis.yml run --rm --entrypoint python kis \
  -m freqtrade.exchange.kis_adapter.validate \
  --config /freqtrade/user_data/config.json \
  --config /freqtrade/user_data/config.private.json \
  --config /freqtrade/config_examples/config_kis.paper.example.json \
  --report /freqtrade/user_data/paper-preflight-01.json
```

Preflight sends no broker order/cancel requests. Continue with the adapter
document's [explicit paper commands](kis-adapter.md#paper-validation-commands),
using these same three config arguments and a new report filename each time.
Submission/cancellation require both the action and `--allow-paper-orders`.
Wait at least 60 seconds between fresh processes sharing an app key, and respect
broker cooldown errors. Never repeat an unresolved submission automatically.

Do not enable normal paper strategy execution until actual order/DB/UI validation
has passed and its missing scenarios are recorded. The overlay is explicit opt-in
to paper private-account operation; it never enables production writes.

## Stop, back up and recover

Stop the KIS service and any one-shot validator before maintenance. Use SQLite's
backup API rather than copying only the main file while WAL records may exist:

```bash
docker compose -f docker-compose.kis.yml stop kis
docker compose -f docker-compose.kis.yml run --rm -T --entrypoint python kis - <<'PY'
import os
import sqlite3
from datetime import datetime, UTC
from pathlib import Path

os.umask(0o077)
folder = Path('/freqtrade/user_data/backups')
folder.mkdir(exist_ok=True)
name = datetime.now(UTC).strftime('paper-%Y%m%dT%H%M%S%f.sqlite')
source = sqlite3.connect('file:/freqtrade/user_data/trades-paper.kis.sqlite?mode=ro', uri=True)
target = sqlite3.connect(folder / name)
source.backup(target)
target.close()
source.close()
PY
```

The backup includes Order/Trade records, KeyValueStore, all `kis_*` tables, intents
and fill watermarks. Protect backups as private account data. For dry-run, use the
dry-run database filename instead. Restart recovery must use the same environment,
credential scope and complete database; do not start with an empty DB to bypass an
unresolved order or reconciliation block.

After an interruption, run read-only `reconcile` before resuming strategy activity.
Preserve the database and reports when reconciliation fails. An old backup may
lack orders submitted after its creation; restoring it is not proof that those
orders were never sent. Compare complete broker history before any supervised
repair. There is no supported automatic intent deletion or unblock operation.

`docker compose -f docker-compose.kis.yml down` removes this project's containers
and network; the bind-mounted `user_data/kis` directory remains. Keep it for restart
or rollback, along with the matching image ID and verified configuration.
