# AutoTrades 2.0

AutoTrades is an intraday trading platform that converts completed market snapshots into structural opportunities, signals, and optionally managed multi-leg trades. The current architecture is event-driven and uses the Auction engine as the authoritative interpreter of local market structure.

The system is designed for deterministic replay, strict typed contracts, visible lifecycle transitions, and continued processing when one symbol or trade fails.

## Architecture

The authoritative signal path is:

```text
Completed candle and snapshot facts
â†’ Auction evidence construction
â†’ persistent directional and balance episodes
â†’ authoritative lifecycle events
â†’ SetupEventRouter
â†’ structural permission matrix
â†’ setup-quality evaluator
â†’ SetupManager selection
â†’ StockAdvisor deployment review
â†’ SignalGenerator persistence
â†’ signals + stock_opportunities
â†’ optional trade generation, execution, monitoring and exit
```

### Responsibility boundaries

**SnapshotGenerator** builds the completed-candle snapshot, objective indicators, market windows, structure, derivatives context, and Auction projection.

**Auction Engine** interprets local price behaviour and maintains causal directional and balance episodes. It emits authoritative lifecycle events but does not write signals or trades.

**SetupEventRouter** maps authoritative events to setup families. Structural permission determines whether the setup is permitted, waiting, or blocked.

**EventDrivenSetupEngine** validates event geometry, entry distance, expected first move, and remaining session time. It produces strict authoritative setup candidates.

**SetupManager** selects among valid candidates deterministically.

**StockAdvisor** applies conservative new-signal deployment checks using current Auction observation context. It does not discover setups and does not alter existing signal or trade lifecycles.

**SignalGenerator** owns signal persistence, progression, replacement, invalidation, and the single-table `stock_opportunities` projection.

**TradeGenerator** converts eligible open signals into the configured trade package.

**TradeExecutor** executes or simulates entry and exit orders.

**TradeMonitor** manages frozen entry risk, adaptive targets, posture, signal-driven exits, sibling-leg exits, and audit reporting.

## Core persisted entities

### Snapshots

Snapshots are stored in `snapshots`. Each row contains the completed-candle snapshot used by the live and replay signal path. Auction continuity is carried through the snapshot memory contract.

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

`stock_opportunities` is the indexed lifecycle projection for deployed opportunities. It keeps normal indexed columns for identity, symbol, side, setup family, timestamps, prices, lifecycle state, and signal links. Candidate interpretations and transition history are stored in JSON fields.

The table is currently treated as intraday operational state and is included in the daily intraday reset.

### User trades and audit

`user_trades` stores individual trade legs. Trade packages are associated through `signal_id`; no separate trade-group table is required.

`auditlog` records strict service and lifecycle diagnostics where configured.

## Runtime services

The primary service entry points are under `scripts/`:

| Script | Purpose |
|---|---|
| `run_stock_rank.py` | Run the production six-minute StockRank service over the active EQ universe |
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

Systemd service templates are stored in the repository root with the `t_*.service` naming convention. `t_prepare_day.service` is a one-shot unit; no automatic timer is installed by this repository.

## Operational programs

Occasional/manual workflows are under `operations/`:

| Program | Responsibility |
|---|---|
| `refresh_broker_instruments.py` | Directly replace the authoritative raw NSE/NFO broker instrument master after complete fetch/structure validation |
| `refresh_derivative_symbols.py` | Upsert application EQ/FUT/CE/PE symbols for the configured front, near and far published expiries; applies by default and supports `--review-only` |
| `filter_stock_universe.py` | Review/apply whitelist, blacklist and minimum-price policy; owns `symbols.enabled` and refreshes the EQ quote price used by that policy |
| `generate_stock_universe.py` | Review/apply long-horizon enabled-to-configured-limit curation; owns only `symbols.active` |

The intended occasional operating cycle is: refresh broker instruments, refresh derivative symbols, review/apply the enabled universe, then review/apply the active universe. Membership operations default to review mode and require `--apply`; authoritative refresh operations apply directly unless their documented review option is used. StockRank is the production intraday attention-ranking service and remains read-only with respect to symbol membership, signals and trades.

## Service window and failure handling

The normal market service window is approximately 09:15â€“15:30 IST on trading days.

Per-record and per-trade exceptions must not terminate an otherwise safe service loop. Service boundaries should:

1. catch the individual failure;
2. log a full traceback and structured symbol/trade context;
3. record failure diagnostics where possible;
4. continue with subsequent records.

Process termination is reserved for startup or preflight failures where continuation would be unsafe or impossible.

## Configuration

Application and database selection come from `config.py` and the standard application configuration. Replay programs use the same configured database and do not maintain a separate database allowlist.

Major configuration modules include:

```text
configs/auction_engine_config.py
configs/signal_config.py
configs/stock_advisor_config.py
configs/execution_config.py
configs/trade_config.py
configs/service_config.py
```

Auction and StockAdvisor configuration contain only settings read by the current runtime. Configuration version or hash values are not used to permit, reject, or restrict processing.

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
â”œâ”€â”€ unit/
â”œâ”€â”€ functionality/
â””â”€â”€ replays/
```

### Unit tests

`tests/unit/` contains automated contract and lifecycle tests. These tests are the default pytest collection target through `pytest.ini`.

Run:

```powershell
python -m pytest -q tests/unit
```

or simply:

```powershell
python -m pytest -q
```

### Functionality programs

`tests/functionality/` contains manually executed programs that exercise one real component, such as one snapshot, derivatives processing, StockRamk, TradeGenerator, TradeExecutor, or TradeMonitor.

Examples:

```powershell
python tests/functionality/test_snapshot_generator.py
python tests/functionality/test_derivatives.py
python tests/functionality/test_trade_generator.py
```

These are not intended to be collected and run together as unit tests. Some require database rows, credentials, market data, or specific manual inputs.

### Replay programs

`tests/replays/` contains chronological and end-to-end historical runners.

| Program | Purpose |
|---|---|
| `replay_snapshots.py` | Generate historical snapshots only |
| `replay_unprocessed.py` | Process existing unprocessed snapshots sequentially; optionally run trades |
| `replay_unprocessed_multi.py` | Process existing unprocessed snapshots with parallel signal workers; optionally run trades |
| `replay_pipeline.py` | Generate snapshots and run the complete end-to-end pipeline |
| `replay_signal_generator.py` | Focused signal and opportunity lifecycle diagnostics from stored snapshots |
| `replay_signal_trade_pipeline.py` | Strict downstream validation through trade creation, execution, monitoring, and exits |

The sequential and multi-worker unprocessed replays are intentionally retained separately for now. They may be compared and merged later after equivalent behaviour is established.

## Replay data-clearing policy

Replay programs use visible source defaults. Destructive cleanup must never be hidden.

Where a replay supports `CLEAR_DATA` or `--clear-data`:

```text
False â†’ preserve the configured database state
True  â†’ clear only the explicitly documented replay output tables
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

The focused signal replay clears `signals` and `stock_opportunities` only when explicitly requested. The strict signal-trade replay clears its documented downstream output scope only when explicitly requested.

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

Sequential:

```powershell
python tests/replays/replay_unprocessed.py
```

Parallel signal workers:

```powershell
python tests/replays/replay_unprocessed_multi.py
```

The trade pipeline remains cadence-based and single-threaded. Only symbol-level signal evaluation is parallel in the multi-worker program.

### Complete end-to-end replay

```powershell
python tests/replays/replay_pipeline.py
```

This program starts before snapshots exist and runs snapshot generation through trade exits.

## Day Prep

Run through its one-shot service or directly on an allowed trading day:

```powershell
python scripts/prepare_day.py
```

The production runner has no command-line override surface. Controlled off-day tests should call `DayPrepService` from the test suite or use the configured run-control whitelist rather than bypassing the production day gate.

Day Prep is a one-shot service/runner workflow. It performs mandatory verified
archives before any current-state table is cleared:

```text
signals       -> signals_history
user_trades   -> user_trades_history
stock_rank    -> stock_rank_history
```

Auditlog history is optional and disabled by default through
`SERVICE_CONFIG.day_prep.archive_auditlog`. Whether archived or not, the
current audit table is cleared during preparation.

The following are intraday working state and are cleared without history:

```text
stock_opportunities
snapshots
candles
derivativeschain
auditlog
```

Current OMS projections (`oms_funds`, `oms_positions`, `oms_orders`) are also
cleared when configured. Their history tables are never cleared.

Day Prep does not read or modify `symbols`. Universe and generation flags
remain owned by the operational universe programs. The service blocks before
any archive or clear when unresolved trades are present, unless that strict
preflight is explicitly disabled in configuration.

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

For a production-like processing check, run one of:

```powershell
python tests/replays/replay_unprocessed.py
python tests/replays/replay_unprocessed_multi.py
```

For complete end-to-end generation:

```powershell
python tests/replays/replay_pipeline.py
```

Confirm:

- no startup or per-record unhandled exceptions;
- signal and opportunity counts match expectations;
- progression uses the same opportunity and signal identity;
- replacement links both old and new opportunities;
- invalidation is structurally independent of trade exit;
- no duplicate trade package is created for one signal;
- each expected package has the configured legs;
- monitor item-error count is zero or every failure is explicitly explained;
- remaining unprocessed snapshot count is correct.

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

The core Auction-to-opportunity refactor and focused validation are complete. Remaining work is broad one-day and multi-day validation, followed by evidence-based tuning of setup quality or Advisor policy. Strategy thresholds, risk rules, and trade-management tuning should remain separate from repository cleanup.

