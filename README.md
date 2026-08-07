# AutoTrades 3.0

AutoTrades is an intraday trading platform that converts completed market snapshots into structural opportunities, signals, and optionally managed multi-leg trades. The current architecture is event-driven, with Auction as the authority for objective local market structure.

The system is designed for causal replay, strict typed contracts, visible lifecycle transitions, selective deployment, and continued processing when one symbol or trade fails.

## Architecture

The authoritative processing path is:

```text
Completed candle and snapshot facts
→ Auction evidence construction
→ directional and balance state
→ authoritative Auction events
→ setup-event routing
→ structural permission
→ setup-specific technical proof
→ StockAdvisor deployment review
→ SignalGenerator persistence and lifecycle
→ signals + stock_opportunities
→ optional trade generation, execution, monitoring, and exit
```

### Responsibility boundaries

**SnapshotGenerator** builds the completed-candle snapshot, objective indicators, market windows, structure, derivatives context, and Auction projection.

**Auction Engine** interprets local price behaviour and maintains causal directional and balance state. It emits authoritative events but does not prove setups, write signals, or manage trades.

**SetupEventRouter** maps authoritative events to setup families. Structural permission determines whether a setup family is permitted, waiting, or blocked.

**Setup evaluators** own family-specific technical proof, entry/reference geometry, and confirmation. They do not persist opportunities or signals.

**StockAdvisor** evaluates whether a technically valid candidate is worth deploying now. It applies location, barrier, day-path, repeated-episode, range-churn, freshness, and diagnostic context checks. It does not discover setups, change Auction state, own signal lifecycle, or manage trades.

**SignalGenerator** owns opportunity and signal persistence, progression, replacement, invalidation, and cutoff behaviour.

**TradeGenerator** converts eligible open signals into configured trade packages.

**TradeExecutor** executes or simulates pending entries and exits.

**TradeMonitor** owns position management, frozen entry risk, adaptive targets/stops, signal-driven exits, sibling-leg exits, and audit reporting.

**Account Governor** owns account permission. Its contract is integrated, while functional enforcement remains a roadmap item.

**Market Regime** owns broad market context. Its contract is integrated, while functional classification remains a roadmap item.

## Core persisted entities

### Snapshots

Snapshots are stored in `snapshots`. Each row contains the completed-candle state used by the live or replay signal path. Historical replay decisions must use snapshot/observation time and must not read the current computer clock for trading decisions.

### Signals

Signals are stored in `signals`. SignalGenerator is the only owner of signal creation and lifecycle changes.

Important lifecycle operations include:

```text
CREATE
UPDATE / PROGRESS
REPLACE
INVALIDATE / CLOSE
HOLD
```

### Stock opportunities

`stock_opportunities` is the persisted lifecycle projection for setup opportunities. It stores identity, symbol, side, setup family, timestamps, reference geometry, lifecycle state, signal links, candidate interpretations, and transition history.

The table is treated as intraday operational state and is included in the normal daily reset.

### User trades and audit

`user_trades` stores individual trade legs. Trade packages are associated through `signal_id`; no separate trade-group table is required.

`auditlog` records structured service, account-governor, and lifecycle diagnostics where configured.

## Runtime services

Primary service entry points are under `scripts/`:

| Script | Purpose |
|---|---|
| `gen_derivatives.py` | Generate derivatives-chain context |
| `gen_snapshots.py` | Generate completed-candle snapshots |
| `gen_signals.py` | Process unprocessed snapshots through SignalGenerator |
| `gen_trades.py` | Create trade packages from eligible signals |
| `exec_trades.py` | Execute pending entries and exits |
| `mon_trades.py` | Monitor open positions and prepare exits |
| `event_handler.py` | Coordinate scheduled service execution |
| `run_broker_reconcile.py` | Reconcile broker and database state |
| `run_trade_backfill.py` | Backfill trade execution details where required |
| `prepare_day.py` | Archive durable intraday rows, clear current operational state, and prepare users |

Systemd service templates are stored in the repository root with the `t_*.service` naming convention. `prepare_day.py` is a one-shot program intended to be started directly by the weekday cron; this repository does not define a `t_prepare_day.service` unit.

## Operational programs

Occasional/manual workflows are under `operations/`:

| Program | Responsibility |
|---|---|
| `refresh_broker_instruments.py` | Replace the authoritative raw NSE/NFO broker instrument master after complete fetch and structure validation |
| `refresh_derivative_symbols.py` | Upsert application EQ/FUT/CE/PE symbols for configured expiries; applies by default and supports `--review-only` |
| `filter_stock_universe.py` | Review/apply whitelist, blacklist, and minimum-price policy; owns `symbols.enabled` |
| `generate_stock_universe.py` | Review/apply long-horizon enabled-to-configured-limit curation; owns `symbols.active` |

The intended occasional operating cycle is:

```text
refresh broker instruments
→ refresh derivative symbols
→ review/apply enabled universe
→ review/apply active universe
```

## Service window and failure handling

The normal market service window is approximately 09:15–15:30 IST on trading days.

Per-record, per-symbol, and per-trade exceptions must not terminate an otherwise safe service loop. Service boundaries should:

1. catch the individual failure;
2. log a traceback and structured symbol/trade context;
3. persist failure diagnostics where possible;
4. continue with subsequent records.

Process termination is reserved for startup or unsafe preflight failures where continuation would be unsafe or impossible.

## Configuration

Application and database selection come from `config.py` and the standard application configuration. Replay programs use the same configured database and do not maintain a separate hidden database allowlist.

Major configuration modules include:

```text
configs/auction_engine_config.py
configs/signal_config.py
configs/stock_advisor_config.py
configs/execution_config.py
configs/trade_config.py
configs/service_config.py
```

Auction and StockAdvisor configuration contain only settings consumed by the current runtime. Configuration version or hash values are not used to permit, reject, or restrict processing.

## Installation

The deployed server currently uses Python 3.10. A typical environment setup is:

```bash
python3 -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

On Windows PowerShell:

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Database and application settings must be reviewed before running any service or replay.

## Database migration

Create the opportunity table using:

```text
database/sql/20260729_create_stock_opportunities.sql
```

Apply schema changes only to the intended configured database.

## Test and replay organization

The repository separates automated tests, manual functionality checks, and chronological replays.

```text
tests/
├── unit/
├── functionality/
└── replays/
```

### Unit tests

`tests/unit/` contains automated contract and lifecycle tests. These tests are the default pytest collection target through `pytest.ini`.

Run:

```powershell
python -m pytest -q tests/unit
```

or:

```powershell
python -m pytest -q
```

### Functionality programs

`tests/functionality/` contains manually executed programs that exercise one real component, such as one snapshot, derivatives processing, TradeGenerator, TradeExecutor, or TradeMonitor.

Examples:

```powershell
python tests/functionality/test_snapshot_generator.py
python tests/functionality/test_derivatives.py
python tests/functionality/test_trade_generator.py
```

These programs are not intended to be collected and run together as unit tests. Some require database rows, credentials, market data, or specific manual inputs.

### Replay programs

`tests/replays/` contains chronological and end-to-end historical runners.

| Program | Purpose |
|---|---|
| `replay_snapshots.py` | Generate historical snapshots only |
| `replay_unprocessed.py` | Process all existing unprocessed snapshots chronologically with parallel symbol-level signal workers; optionally run trades once per cadence |
| `replay_pipeline.py` | Generate snapshots and run the complete end-to-end pipeline |
| `replay_signal_generator.py` | Focused signal and opportunity lifecycle diagnostics from stored snapshots |
| `replay_signal_trade_pipeline.py` | Strict downstream validation through trade creation, execution, monitoring, and exits |
| `replay_setup_evaluation.py` | Focused Auction-to-setup evaluation diagnostics |

`replay_unprocessed.py` is the single production-like unprocessed-snapshot runner. Signal evaluation may run concurrently across symbols at the same snapshot time. The downstream trade pipeline remains cadence-based and single-threaded.

## Replay data-clearing policy

Replay programs use visible source defaults. Destructive cleanup must never be hidden.

Where a replay supports `CLEAR_DATA` or `--clear-data`:

```text
False → preserve the configured database state
True  → clear only the explicitly documented replay output tables
```

The default is `False`.

`replay_pipeline.py` clears these tables when `CLEAR_DATA=True`:

```text
auditlog
user_trades
stock_opportunities
signals
snapshots
```

Candles and derivatives data are preserved.

The focused signal replay clears `signals` and `stock_opportunities` only when explicitly requested. The strict signal-trade replay clears only its documented downstream output scope when explicitly requested.

`replay_unprocessed.py` never generates snapshots and never clears tables. It reads all rows where `Snapshot.processed = False`; therefore, database preparation must ensure that only the intended replay rows are unprocessed.

## Recommended replay workflow

### Existing snapshots: signals and opportunities only

```powershell
python tests/replays/replay_signal_generator.py
```

Review the generated summary, lifecycle, evaluation, signal, and opportunity reports.

### Existing snapshots: strict signal and trade pipeline

```powershell
python tests/replays/replay_signal_trade_pipeline.py
```

Review signal, opportunity, trade, audit, monitor-error, validation, and summary reports.

### Production-like chronological processing

```powershell
python tests/replays/replay_unprocessed.py
```

The runner processes one snapshot cadence at a time. Signal evaluation is parallel only across symbols within that cadence. Trade generation, entry execution, monitoring, and exit execution each run once per cadence in live-service order.

### Complete end-to-end replay

```powershell
python tests/replays/replay_pipeline.py
```

This runner starts before snapshots exist and processes snapshot generation through trade exits.

## Day Prep

Run directly on an allowed trading day:

```powershell
python scripts/prepare_day.py
```

The production runner has no command-line override surface. Controlled off-day tests should call `DayPrepService` from the test suite or use the configured run-control whitelist rather than bypassing the production day gate.

Day Prep performs mandatory verified archives before current-state tables are cleared:

```text
signals       → signals_history
user_trades   → user_trades_history
```

Auditlog history is optional and disabled by default through `SERVICE_CONFIG.day_prep.archive_auditlog`. Whether archived or not, the current audit table is cleared during preparation.

The following are intraday working state and are cleared without history:

```text
stock_opportunities
snapshots
candles
derivativeschain
auditlog
```

Current OMS projections (`oms_funds`, `oms_positions`, `oms_orders`) are also cleared when configured. Their history tables are never cleared.

Day Prep does not read or modify `symbols`. Universe and generation flags remain owned by the operational universe programs. The service blocks before any archive or clear when unresolved trades are present, unless that strict preflight is explicitly disabled in configuration.

## Validation checklist

### Compile

```powershell
python -m compileall -q configs database enums models routes schemas scripts services tests utils
```

### Automated tests

```powershell
python -m pytest -q tests/unit
```

### Collection check

```powershell
python -m pytest --collect-only -q
```

Only `tests/unit/` should be collected by default.

### Replay smoke checks

After manually preparing the configured replay database, run:

```powershell
python tests/replays/replay_signal_generator.py
python tests/replays/replay_signal_trade_pipeline.py
```

For a production-like processing check:

```powershell
python tests/replays/replay_unprocessed.py
```

For complete end-to-end generation:

```powershell
python tests/replays/replay_pipeline.py
```

Confirm:

- no startup or unhandled per-record exceptions;
- signal and opportunity counts match expectations;
- progression retains the same opportunity and signal identity;
- approved opposite-side transitions replace and link both opportunities;
- deferred, blocked, or rejected opposite-side transitions invalidate without false replacement;
- invalidation remains structurally independent of trade exit;
- no duplicate trade package is created for one signal;
- opposite trade families are exited before a new opposite family is entered;
- each expected package has the configured legs;
- monitor item-error count is zero or every failure is explicitly explained;
- remaining unprocessed snapshot count is correct;
- replay decisions use historical observation time only.

## Repository hygiene

Generated files should not be committed:

```text
__pycache__/
*.pyc
*.pyo
.pytest_cache/
replay logs
reports generated by local runs
```

Before freezing a release, remove generated residue, run the validation checklist, commit, and create a Git tag.

## Current project phase

The simplified Auction directional core, current setup-evaluation path, SignalGenerator replacement lifecycle, and removal of setup reward/risk have been validated on focused historical replays. The current work is baseline validation across the intended universe, followed by supervised live stability review.

The three workstreams remain separate:

1. Daily Review and Live Stability.
2. Maturity Roadmap Implementation.
3. Backtest Research and Tuning.

Backtest tuning must not be promoted directly to production. Promotion requires causal replay, positive controls, holdout evidence, and supervised validation.
