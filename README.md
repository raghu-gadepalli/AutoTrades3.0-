# AutoTrades 2.0

AutoTrades is an intraday trading platform that converts completed market snapshots into structural opportunities, signals, and optionally managed multi-leg trades. The current architecture is event-driven and uses the Auction engine as the authoritative interpreter of local market structure.

The system is designed for deterministic replay, strict typed contracts, visible lifecycle transitions, and continued processing when one symbol or trade fails.

## Architecture

The authoritative signal path is:

```text
Completed candle and snapshot facts
→ Auction evidence construction
→ persistent directional and balance episodes
→ authoritative lifecycle events
→ SetupEventRouter
→ structural permission matrix
→ setup-quality evaluator
→ SetupManager selection
→ StockAdvisor deployment review
→ SignalGenerator persistence
→ signals + stock_opportunities
→ optional trade generation, execution, monitoring and exit
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
| `run_stock_rank.py` | Six-minute StockRank service over a common completed active-universe snapshot cadence; persists ranks and logs summaries without CSV output |
| `gen_derivatives.py` | Generate derivatives-chain context |
| `gen_snapshots.py` | Generate completed-candle snapshots |
| `gen_signals.py` | Process unprocessed snapshots through SignalGenerator |
| `gen_trades.py` | Create trade packages from eligible signals |
| `exec_trades.py` | Execute pending entries and exits |
| `mon_trades.py` | Monitor open positions and prepare exits |
| `event_handler.py` | Coordinate scheduled service execution |
| `run_broker_reconcile.py` | Reconcile broker and database state |
| `run_trade_backfill.py` | Backfill trade execution details where required |
| `init_intraday_reset.py` | Archive configured data and clear intraday operational tables |

Systemd service templates are stored in the repository root with the `t_*.service` naming convention.

## Operational programs

Occasional/manual workflows are under `operations/`:

| Program | Responsibility |
|---|---|
| `filter_stock_universe.py` | Review/apply whitelist and blacklist policy; owns only `symbols.enabled` |
| `generate_stock_universe.py` | Review/apply long-horizon 150-to-100 curation; owns only `symbols.active` |
| `refresh_broker_instruments.py` | Refresh raw NSE/NFO broker instruments |
| `refresh_derivative_symbols.py` | Truncate and rebuild EQ plus configured current/near/far FUT/OPT symbols from broker instruments |

Universe operations default to review mode and require `--apply` for membership writes. `refresh_derivative_symbols.py` applies by default: it builds and validates the complete plan first, truncates `symbols`, recreates EQ rows with generation flags enabled but `enabled=False` and `active=False`, and recreates current/near/far derivatives as enabled. Run `filter_stock_universe.py` and `generate_stock_universe.py` immediately afterwards to restore EQ policy and active membership. The retired first-candle StockScan selector and its separate service module have been removed. StockRank runs every six minutes over the active universe, owns only `stock_rank` rows, and remains diagnostic until StockAdvisor integration.

## StockRank

StockRank measures current cross-sectional attention-worthiness across the curated active universe. Snapshots remain on a three-minute cadence; StockRank persists a common-cadence ranking every six minutes after a configured completion lag. Each row records movement quality, range/stall penalties, movement classification, absolute score, cross-sectional rank and an attention tier (`PRIORITY`, `SECONDARY` or `SUPPRESSED`).

StockRank does not alter `enabled`, `active`, signals, opportunities or trades. The production runner writes database rows and concise logs only. Detailed CSV diagnostics belong to `tests/functionality/test_stock_rank.py`; historical consolidated analysis belongs to `tests/replays/replay_stock_rank.py`.

If `stock_rank` was created before the attention-tier field was added, run `database/sql/20260801_add_stock_rank_attention_tier.sql` once before starting the service.

## Service window and failure handling

The normal market service window is approximately 09:15–15:30 IST on trading days.

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

or simply:

```powershell
python -m pytest -q
```

### Functionality programs

`tests/functionality/` contains manually executed programs that exercise one real component, such as one snapshot, derivatives processing, StockRank, TradeGenerator, TradeExecutor, or TradeMonitor.

Examples:

```powershell
python tests/functionality/test_snapshot_generator.py
python tests/functionality/test_derivatives.py
python tests/functionality/test_stock_rank.py
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
| `replay_stock_rank.py` | Causal multi-cadence StockRank replay with consolidated cadence, row and symbol reports |

The sequential and multi-worker unprocessed replays are intentionally retained separately for now. They may be compared and merged later after equivalent behaviour is established.

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

## Intraday reset

Run:

```powershell
python scripts/init_intraday_reset.py
```

The reset scope is explicitly configured in `SERVICE_CONFIG.init_reset.intraday_tables`. It currently includes:

```text
user_trades
stock_opportunities
signals
snapshots
candles
derivativeschain
oms_funds_history
oms_positions_history
oms_orders_history
auditlog
```

Signals and user trades may be archived according to reset configuration before truncation. Opportunity archival can be added later if required; the current table is cleared as intraday operational state.

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
