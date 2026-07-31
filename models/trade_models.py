# models.py

from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import (
    DECIMAL,
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Float,
    Text,
    UniqueConstraint,
    JSON,
    func,
    text,
    Computed,
)

# -----------------------------------
# SQLAlchemy Base
# -----------------------------------

class Base(DeclarativeBase):
    pass


# -----------------------------------
# Users & Settings
# -----------------------------------

class User(Base):
    __tablename__ = "users"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    userid       = Column(String(50), unique=True, nullable=False)
    name         = Column(String(30), nullable=False)
    email        = Column(String(50), nullable=False)
    mobile       = Column(String(10), nullable=False, unique=True)
    password     = Column(String(50), nullable=False)

    broker_login = Column(Boolean, nullable=False, server_default="0")
    broker_name  = Column(String(30), nullable=True, server_default="ZERODHA")

    apikey       = Column(String(255), nullable=False, server_default="")
    secretkey    = Column(String(255), nullable=False, server_default="")
    access_token = Column(String(50),  nullable=False, server_default="")

    intraday_only = Column(Boolean, nullable=False, server_default="0")
    stocks       = Column(String(255), nullable=False, server_default="")
    equity       = Column(Integer, nullable=False, server_default="1")
    futures      = Column(Integer, nullable=False, server_default="1")
    options      = Column(Integer, nullable=False, server_default="1")

    execution_mode = Column(String(8), nullable=False, server_default="VIRTUAL")
    autotrade    = Column(Boolean, nullable=False, server_default="0")
    active       = Column(Boolean, nullable=False, server_default="1")

    logged_in    = Column(Boolean, nullable=False, server_default="0")
    logged_time  = Column(DateTime, nullable=True)

    def __repr__(self):
        return f"<User id={self.id} userid={self.userid}>"


# -----------------------------------
# Funds & Margin
# -----------------------------------

class UserFunds(Base):
    __tablename__ = "oms_funds"
    __table_args__ = (
        UniqueConstraint("client_id", "trading_day", name="uk_client_day"),
        Index("idx_trading_day", "trading_day"),
        Index("idx_client_polled_at", "client_id", "polled_at"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    trading_day = Column(Date, nullable=False)
    client_id = Column(String(50), nullable=False)

    net_balance = Column(Numeric(15, 2), nullable=True)
    available_cash = Column(Numeric(15, 2), nullable=True)
    opening_balance = Column(Numeric(15, 2), nullable=True)
    live_balance = Column(Numeric(15, 2), nullable=True)
    collateral = Column(Numeric(15, 2), nullable=True)
    utilised_margin = Column(Numeric(15, 2), nullable=True)

    span_margin = Column(Numeric(15, 2), nullable=True)
    exposure_margin = Column(Numeric(15, 2), nullable=True)
    option_premium = Column(Numeric(15, 2), nullable=True)
    m2m_realised = Column(Numeric(15, 2), nullable=True)
    m2m_unrealised = Column(Numeric(15, 2), nullable=True)

    available_margin = Column(Numeric(15, 2), nullable=True)
    polled_at = Column(DateTime, nullable=False)

    def __repr__(self):
        return (
            f"<UserFunds client_id={self.client_id} "
            f"trading_day={self.trading_day} "
            f"net_balance={self.net_balance}>"
        )


class UserFundsHistory(Base):
    __tablename__ = "oms_funds_history"

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    client_id = Column(String(50), nullable=False)
    trading_day = Column(Date, nullable=False)

    snapshot_json = Column(JSON, nullable=False)
    polled_at = Column(DateTime, nullable=False)

    __table_args__ = (
        Index("idx_client_day_time", "client_id", "trading_day", "polled_at"),
        Index("idx_trading_day_time", "trading_day", "polled_at"),
    )
    def __repr__(self):
        return (
            f"<UserFundsHistory client_id={self.client_id} "
            f"trading_day={self.trading_day} "
            f"polled_at={self.polled_at}>"
        )

# -----------------------------------
# Positions
# -----------------------------------

class UserPositions(Base):
    __tablename__ = "oms_positions"
    __table_args__ = (
        UniqueConstraint("client_id", "trading_day", "tradingsymbol", "product", name="uk_position"),
        Index("idx_position_latest", "client_id", "tradingsymbol", "product", "polled_at"),
        Index("idx_symbol", "tradingsymbol"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    trading_day = Column(Date, nullable=False)
    client_id = Column(String(50), nullable=False)

    tradingsymbol = Column(String(50), nullable=False)
    instrument = Column(String(20), nullable=True)
    instrument_token = Column(BigInteger, nullable=True)

    exchange = Column(String(10), nullable=True)
    segment = Column(String(10), nullable=True)
    product = Column(String(10), nullable=True)

    quantity = Column(Integer, nullable=True)
    overnight_quantity = Column(Integer, nullable=True)

    multiplier = Column(Numeric(10, 4), nullable=True)

    average_price = Column(Numeric(14, 4), nullable=True)
    close_price = Column(Numeric(14, 4), nullable=True)
    last_price = Column(Numeric(14, 4), nullable=True)

    value = Column(Numeric(14, 4), nullable=True)

    pnl = Column(Numeric(14, 4), nullable=True)
    m2m = Column(Numeric(14, 4), nullable=True)
    unrealised = Column(Numeric(14, 4), nullable=True)
    realised = Column(Numeric(14, 4), nullable=True)

    buy_quantity = Column(Integer, nullable=True)
    buy_price = Column(Numeric(14, 4), nullable=True)
    buy_value = Column(Numeric(14, 4), nullable=True)
    buy_m2m = Column(Numeric(14, 4), nullable=True)

    sell_quantity = Column(Integer, nullable=True)
    sell_price = Column(Numeric(14, 4), nullable=True)
    sell_value = Column(Numeric(14, 4), nullable=True)
    sell_m2m = Column(Numeric(14, 4), nullable=True)

    day_buy_quantity = Column(Integer, nullable=True)
    day_buy_price = Column(Numeric(14, 4), nullable=True)
    day_buy_value = Column(Numeric(14, 4), nullable=True)

    day_sell_quantity = Column(Integer, nullable=True)
    day_sell_price = Column(Numeric(14, 4), nullable=True)
    day_sell_value = Column(Numeric(14, 4), nullable=True)

    polled_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, nullable=True, server_default=func.current_timestamp())

    def __repr__(self):
        return (
            f"<UserPositions client_id={self.client_id} "
            f"trading_day={self.trading_day} "
            f"tradingsymbol={self.tradingsymbol} "
            f"product={self.product} "
            f"quantity={self.quantity}>"
        )


class UserPositionsHistory(Base):
    __tablename__ = "oms_positions_history"
    __table_args__ = (
        Index("idx_client_day_time", "client_id", "trading_day", "polled_at"),
        Index("idx_polled_at", "polled_at"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    client_id = Column(String(50), nullable=False)
    trading_day = Column(Date, nullable=False)
    polled_at = Column(DateTime, nullable=False)

    broker_payload = Column(JSON, nullable=False)

    def __repr__(self):
        return (
            f"<UserPositionsHistory client_id={self.client_id} "
            f"trading_day={self.trading_day} "
            f"polled_at={self.polled_at}>"
        )

# -----------------------------------
# Orders
# -----------------------------------

class UserOrders(Base):
    __tablename__ = "oms_orders"
    __table_args__ = (
        UniqueConstraint("client_id", "order_id", name="uk_client_order"),
        Index("idx_day_client_time", "trading_day", "client_id", "order_timestamp"),
        Index("idx_symbol", "tradingsymbol"),
        Index("idx_status", "status"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    trading_day = Column(Date, nullable=False)
    client_id = Column(String(50), nullable=False)

    order_id = Column(String(40), nullable=False)
    exchange_order_id = Column(String(40), nullable=True)

    tradingsymbol = Column(String(50), nullable=True)
    instrument = Column(String(30), nullable=True)
    instrument_token = Column(BigInteger, nullable=True)

    exchange = Column(String(10), nullable=True)
    transaction_type = Column(String(5), nullable=True)
    product = Column(String(10), nullable=True)
    order_type = Column(String(10), nullable=True)
    variety = Column(String(10), nullable=True)

    validity = Column(String(10), nullable=True)
    validity_ttl = Column(Integer, nullable=True)

    quantity = Column(Integer, nullable=True)
    disclosed_quantity = Column(Integer, nullable=True)
    filled_quantity = Column(Integer, nullable=True)
    pending_quantity = Column(Integer, nullable=True)
    cancelled_quantity = Column(Integer, nullable=True)

    price = Column(Numeric(14, 4), nullable=True)
    average_price = Column(Numeric(14, 4), nullable=True)
    trigger_price = Column(Numeric(14, 4), nullable=True)

    status = Column(String(30), nullable=True)

    order_timestamp = Column(DateTime, nullable=True)
    exchange_timestamp = Column(DateTime, nullable=True)

    tag = Column(String(50), nullable=True)
    order_issued_at = Column(String(10), nullable=True)
    order_placed_by = Column(String(50), nullable=True)

    recon_status = Column(String(20), nullable=True)

    created_at = Column(DateTime, nullable=True, server_default=func.current_timestamp())
    first_seen_at = Column(DateTime, nullable=True)
    polled_at = Column(DateTime, nullable=False)

    def __repr__(self):
        return (
            f"<UserOrders client_id={self.client_id} "
            f"order_id={self.order_id} "
            f"status={self.status} "
            f"tradingsymbol={self.tradingsymbol}>"
        )


class UserOrdersHistory(Base):
    __tablename__ = "oms_orders_history"
    __table_args__ = (
        Index("idx_client_day_time", "client_id", "trading_day", "polled_at"),
        Index("idx_polled_at", "polled_at"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    client_id = Column(String(50), nullable=False)
    trading_day = Column(Date, nullable=False)
    polled_at = Column(DateTime, nullable=False)

    broker_payload = Column(JSON, nullable=False)

    def __repr__(self):
        return (
            f"<UserOrdersHistory client_id={self.client_id} "
            f"trading_day={self.trading_day} "
            f"polled_at={self.polled_at}>"
        )

# -----------------------------------
# Universe / Market data
# -----------------------------------

class Symbol(Base):
    __tablename__ = "symbols"

    # DB PK remains 'symbol'; 'id' stays unique/auto-increment
    id                   = Column(Integer, autoincrement=True, unique=True)
    symbol               = Column(String(50), primary_key=True)

    token                = Column(String(50), nullable=True)
    name                 = Column(String(50), nullable=True)
    type                 = Column(String(10), nullable=False)
    price                = Column(Numeric(13, 2), nullable=True)
    exchange             = Column(String(20), nullable=True)
    segment              = Column(String(20), nullable=True)
    signal_profile       = Column(String(1000), nullable=False, default="DEFAULT")
    lotsize              = Column(Integer, nullable=False, default=1)
    expiry               = Column(Date, nullable=True)
    strike_price         = Column(Numeric(13, 2), nullable=True)
    tick_size            = Column(Numeric(13, 2), nullable=True)
    equity_ref           = Column(String(50), nullable=True, index=True)

    last_time            = Column(DateTime, nullable=True)
    last_snapshot        = Column(JSON, nullable=True)

    # Intraday dynamic flags
    generate_candles     = Column(Boolean, nullable=False, default=False)
    merge_candles        = Column(Boolean, nullable=False, default=False)
    update_performance   = Column(Boolean, nullable=False, default=False)
    generate_signals     = Column(Boolean, nullable=False, default=False)
    processed            = Column(Boolean, nullable=False, default=False)

    # Long-lived flags
    active               = Column(Boolean, nullable=False, default=False)   # curated observed universe
    enabled              = Column(Boolean, nullable=False, default=True)    # policy/universe gate

    # Promotion/demotion timestamps
    promoted_when        = Column(DateTime, nullable=True)
    demoted_when         = Column(DateTime, nullable=True)

    def __repr__(self):
        return f"<Symbol {self.symbol}>"


class Instrument(Base):
    __tablename__ = "instruments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    instrument_token = Column(String(50), nullable=False)
    exchange_token = Column(String(50), nullable=False)
    tradingsymbol = Column(String(50), nullable=False)
    name = Column(String(50), nullable=False)
    last_price = Column(Float, nullable=True)
    expiry = Column(Date, nullable=True)
    strike = Column(Float, nullable=True)
    tick_size = Column(Float, nullable=True)
    lot_size = Column(Float, nullable=True)
    instrument_type = Column(String(10), nullable=False)
    segment = Column(String(10), nullable=False)
    exchange = Column(String(10), nullable=False)

    def __repr__(self):
        return f"<Instrument {self.tradingsymbol}>"


class Candle(Base):
    __tablename__ = "candles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(50), nullable=False)
    frequency = Column(Integer, nullable=False)
    candle_time = Column(DateTime, nullable=False)
    open = Column(DECIMAL(13, 2), nullable=False, default=0.00)
    high = Column(DECIMAL(13, 2), nullable=False, default=0.00)
    low = Column(DECIMAL(13, 2), nullable=False, default=0.00)
    close = Column(DECIMAL(13, 2), nullable=False, default=0.00)
    volume = Column(DECIMAL(13, 2), nullable=False, default=0.00)
    oi = Column(DECIMAL(13, 2), nullable=False)
    active = Column(Boolean, nullable=False, default=True)

    def __repr__(self):
        return f"<Candle {self.symbol} - {self.frequency} - {self.candle_time}>"


class Snapshot(Base):
    __tablename__ = "snapshots"

    symbol = Column(String(50), primary_key=True, nullable=False)
    snapshot_time = Column(DateTime, primary_key=True, nullable=False)

    # NEW: live price fields
    ltp = Column(DECIMAL(13, 2), nullable=True)
    ltp_time = Column(DateTime, nullable=True)

    data = Column(JSON, nullable=True)
    processed = Column(Boolean, nullable=False, default=False)

    def __repr__(self):
        return f"<Snapshot {self.symbol} - {self.snapshot_time}>"


class DerivativesChain(Base):
    __tablename__ = "derivativeschain"

    symbol = Column(String(50), primary_key=True, nullable=False)
    snapshot_time = Column(DateTime, primary_key=True, nullable=False)

    # New structure
    raw = Column(JSON, nullable=False)
    derived = Column(JSON, nullable=True)

    def __repr__(self):
        return (
            f"<DerivativesChain"
            f"{self.symbol} @ {self.snapshot_time}>"
        )


# -----------------------------------
# Diagnostic stock movement ranking
# -----------------------------------

class StockRank(Base):
    """One symbol's diagnostic rank at one completed snapshot cadence.

    StockRank is intentionally append-by-cadence and idempotent by
    (symbol, rank_time).  It never controls symbol.active, signal creation or
    trade lifecycle in this first implementation.
    """

    __tablename__ = "stock_rank"

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    run_id = Column(String(64), nullable=False)
    trading_day = Column(Date, nullable=False)
    rank_time = Column(DateTime, nullable=False)
    symbol = Column(String(32), nullable=False)
    rank_position = Column(Integer, nullable=False)
    universe_size = Column(Integer, nullable=False)

    direction = Column(String(8), nullable=False)
    classification = Column(String(32), nullable=False)

    total_score = Column(DECIMAL(10, 4), nullable=False)
    movement_score = Column(DECIMAL(10, 4), nullable=False)
    quality_score = Column(DECIMAL(10, 4), nullable=False)
    range_penalty = Column(DECIMAL(10, 4), nullable=False)
    stall_penalty = Column(DECIMAL(10, 4), nullable=False)

    close_price = Column(DECIMAL(16, 6), nullable=False)
    previous_close = Column(DECIMAL(16, 6), nullable=True)
    today_open = Column(DECIMAL(16, 6), nullable=True)
    gap_pct = Column(DECIMAL(12, 6), nullable=True)
    session_move_pct = Column(DECIMAL(12, 6), nullable=True)
    post_open_move_pct = Column(DECIMAL(12, 6), nullable=True)

    move_15m_pct = Column(DECIMAL(12, 6), nullable=True)
    move_30m_pct = Column(DECIMAL(12, 6), nullable=True)
    move_60m_pct = Column(DECIMAL(12, 6), nullable=True)
    move_15m_atr = Column(DECIMAL(12, 6), nullable=True)
    move_30m_atr = Column(DECIMAL(12, 6), nullable=True)
    move_60m_atr = Column(DECIMAL(12, 6), nullable=True)

    atr_value = Column(DECIMAL(16, 6), nullable=False)
    atr_pct = Column(DECIMAL(12, 6), nullable=True)
    directional_efficiency = Column(DECIMAL(12, 6), nullable=True)
    recent_efficiency = Column(DECIMAL(12, 6), nullable=True)
    direction_consistency = Column(DECIMAL(12, 6), nullable=False)
    acceleration_score = Column(DECIMAL(12, 6), nullable=False)
    volume_ratio = Column(DECIMAL(12, 6), nullable=True)
    freshness_score = Column(DECIMAL(12, 6), nullable=False)
    bars_since_extreme = Column(Integer, nullable=False)

    range_active = Column(Boolean, nullable=False, default=False)
    range_episode_id = Column(String(128), nullable=True)
    range_id = Column(String(128), nullable=True)
    range_age_bars = Column(Integer, nullable=False, default=0)
    range_width_pct = Column(DECIMAL(12, 6), nullable=True)
    containment_ratio = Column(DECIMAL(12, 6), nullable=True)
    midpoint_crossings = Column(Integer, nullable=False, default=0)
    vwap_crossings = Column(Integer, nullable=False, default=0)
    failed_escape_count = Column(Integer, nullable=False, default=0)
    rearm_required = Column(Boolean, nullable=False, default=False)
    attempt_limit_reached = Column(Boolean, nullable=False, default=False)

    metrics_json = Column(JSON, nullable=False)

    created_at = Column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_at = Column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )

    __table_args__ = (
        UniqueConstraint("symbol", "rank_time", name="uq_stock_rank_symbol_time"),
        Index("idx_stock_rank_run", "run_id"),
        Index("idx_stock_rank_time_position", "rank_time", "rank_position"),
        Index("idx_stock_rank_day_symbol", "trading_day", "symbol"),
        Index("idx_stock_rank_day_class", "trading_day", "classification"),
        Index("idx_stock_rank_day_score", "trading_day", "total_score"),
    )

    def __repr__(self):
        return (
            f"<StockRank {self.symbol} rank={self.rank_position} "
            f"score={self.total_score} class={self.classification}>"
        )


# -----------------------------------
# Signals (lifecycle-based)
# -----------------------------------

class Signal(Base):
    __tablename__ = "signals"

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    # Stable UUID for this signal instance
    signal_id = Column(String(36), nullable=False)

    # Identity / grouping
    equity_ref = Column(String(32), nullable=False)
    symbol = Column(String(32), nullable=False)
    lifecycle = Column(String(64), nullable=False)
    # Immutable originating setup for this signal instance. lifecycle remains
    # the backend engine grouping (DEFAULT); mutable same/opposite evidence is
    # stored in meta_json and must never replace this identity.
    setup = Column(String(64), nullable=False)
    side = Column(String(8), nullable=False)  # BUY / SELL

    # Lifecycle
    stage = Column(String(32), nullable=False, default="TRACKING")
    status = Column(String(16), nullable=False, default="OPEN")
    status_reason = Column(String(255), nullable=True)

    # Timing
    first_seen_time = Column(DateTime, nullable=True)

    # Creation tracking
    created_price = Column(DECIMAL(16, 6), nullable=True)

    last_eval_time = Column(DateTime, nullable=False)
    last_snapshot_time = Column(DateTime, nullable=False)

    stage_changed_time = Column(DateTime, nullable=True)
    status_changed_time = Column(DateTime, nullable=True)

    qualified_time = Column(DateTime, nullable=True)
    actionable_time = Column(DateTime, nullable=True)

    closed_time = Column(DateTime, nullable=True)
    closed_price = Column(DECIMAL(16, 6), nullable=True)

    # Latest price state
    last_price = Column(DECIMAL(16, 6), nullable=True)
    ltp = Column(DECIMAL(16, 6), nullable=True)
    ltp_time = Column(DateTime, nullable=True)

    # -----------------------------------
    # Signal Analytics / Excursion
    # -----------------------------------

    last_pnl = Column(DECIMAL(10, 4), nullable=False, default=0.0000)
    last_pnl_value = Column(DECIMAL(13, 2), nullable=False, default=0.00)

    max_price = Column(DECIMAL(13, 2), nullable=False, default=0.00)
    min_price = Column(DECIMAL(13, 2), nullable=False, default=0.00)

    max_time = Column(DateTime, nullable=True)
    min_time = Column(DateTime, nullable=True)

    max_pnl = Column(DECIMAL(10, 4), nullable=False, default=0.0000)
    min_pnl = Column(DECIMAL(10, 4), nullable=False, default=0.0000)

    max_pnl_value = Column(DECIMAL(13, 2), nullable=False, default=0.00)
    min_pnl_value = Column(DECIMAL(13, 2), nullable=False, default=0.00)

    # JSON payloads
    criteria_json = Column(JSON, nullable=False)
    snapshot_json = Column(JSON, nullable=False)
    meta_json = Column(JSON, nullable=True)

    __table_args__ = (
        UniqueConstraint("signal_id", name="uq_signal_id"),

        Index("idx_active_lookup", "equity_ref", "lifecycle", "status", "last_eval_time"),
        Index("idx_lifecycle_status_time", "lifecycle", "status", "last_eval_time"),
        Index("idx_setup_status_time", "setup", "status", "last_eval_time"),
        Index("idx_equity_time", "equity_ref", "last_eval_time"),
        Index("idx_ltp_time", "ltp_time"),
        Index("idx_status_stage_time", "status", "stage", "last_eval_time"),
    )

    def __repr__(self):
        return (
            f"<Signal "
            f"{self.lifecycle} | {self.equity_ref} | {self.side} | "
            f"stage={self.stage} status={self.status} "
            f"pnl={self.last_pnl}>"
        )


# -----------------------------------
# Stock opportunities (authoritative structural lifecycle)
# -----------------------------------

class StockOpportunity(Base):
    __tablename__ = "stock_opportunities"

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    opportunity_key = Column(String(64), nullable=False)
    candidate_id = Column(String(64), nullable=False)
    latest_candidate_id = Column(String(64), nullable=False)

    symbol = Column(String(32), nullable=False)
    equity_ref = Column(String(32), nullable=False)
    trading_day = Column(Date, nullable=False)

    setup_family = Column(String(64), nullable=False)
    current_setup_family = Column(String(64), nullable=False)
    setup_subtype = Column(String(64), nullable=False)
    side = Column(String(8), nullable=False)

    source_event_id = Column(String(128), nullable=False)
    source_event_type = Column(String(64), nullable=False)
    source_episode_id = Column(String(128), nullable=False)
    boundary_event_key = Column(String(128), nullable=False)

    latest_event_id = Column(String(128), nullable=False)
    latest_event_type = Column(String(64), nullable=False)
    latest_episode_id = Column(String(128), nullable=False)

    lifecycle_state = Column(String(32), nullable=False)
    lifecycle_reason = Column(String(255), nullable=False)
    structural_result = Column(String(16), nullable=False)

    first_seen_time = Column(DateTime, nullable=False)
    last_eval_time = Column(DateTime, nullable=False)
    deployed_at = Column(DateTime, nullable=False)
    progressed_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    invalidated_at = Column(DateTime, nullable=True)
    replaced_at = Column(DateTime, nullable=True)

    entry_price = Column(DECIMAL(16, 6), nullable=False)
    reference_price = Column(DECIMAL(16, 6), nullable=False)
    stop_reference_price = Column(DECIMAL(16, 6), nullable=False)
    target_reference_price = Column(DECIMAL(16, 6), nullable=False)

    signal_id = Column(String(36), nullable=False)
    replacement_opportunity_key = Column(String(64), nullable=True)
    replaced_opportunity_key = Column(String(64), nullable=True)

    transition_history = Column(JSON, nullable=False)
    candidate_interpretations = Column(JSON, nullable=False)
    authoritative_event_lineage = Column(JSON, nullable=False)
    latest_setup_evaluation = Column(JSON, nullable=True)
    latest_advisor_evaluation = Column(JSON, nullable=True)
    metadata_json = Column(JSON, nullable=True)

    created_at = Column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_at = Column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )

    __table_args__ = (
        UniqueConstraint("opportunity_key", name="uq_stock_opportunity_key"),
        UniqueConstraint("signal_id", name="uq_stock_opportunity_signal_id"),
        Index("idx_stock_opp_symbol_day", "symbol", "trading_day"),
        Index("idx_stock_opp_equity_day", "equity_ref", "trading_day"),
        Index("idx_stock_opp_day_state", "trading_day", "lifecycle_state"),
        Index(
            "idx_stock_opp_family_side_day",
            "setup_family",
            "side",
            "trading_day",
        ),
        Index("idx_stock_opp_episode", "source_episode_id"),
        Index("idx_stock_opp_latest_episode", "latest_episode_id"),
        Index("idx_stock_opp_last_eval", "last_eval_time"),
    )

    def __repr__(self):
        return (
            f"<StockOpportunity {self.symbol} {self.setup_family} {self.side} "
            f"state={self.lifecycle_state} signal_id={self.signal_id}>"
        )


# -----------------------------------
# Signals History
# -----------------------------------

class SignalHistory(Base):
    __tablename__ = "signals_history"

    __table_args__ = (
        UniqueConstraint("id", "trading_date", name="uq_signal_liveid_by_day"),

        Index("idx_signalhist_signalid_day", "signal_id", "trading_date"),
        Index("idx_signalhist_equity_time", "equity_ref", "last_eval_time"),
        Index("idx_signalhist_lifecycle_status_time", "lifecycle", "status", "last_eval_time"),
        Index("idx_signalhist_setup_status_time", "setup", "status", "last_eval_time"),
        Index("idx_signalhist_symbol_time", "symbol", "last_eval_time"),

        {"info": {"intraday": False}},
    )

    hist_id = Column(BigInteger, primary_key=True, autoincrement=True)

    # original live id
    id = Column(BigInteger, nullable=False)

    signal_id = Column(String(36), nullable=False)

    equity_ref = Column(String(32), nullable=False)
    symbol = Column(String(32), nullable=False)
    lifecycle = Column(String(64), nullable=False)
    setup = Column(String(64), nullable=False)
    side = Column(String(8), nullable=False)

    stage = Column(String(32), nullable=False)
    status = Column(String(16), nullable=False)
    status_reason = Column(String(255), nullable=True)

    first_seen_time = Column(DateTime, nullable=True)
    created_price = Column(DECIMAL(16, 6), nullable=True)

    last_eval_time = Column(DateTime, nullable=False)
    last_snapshot_time = Column(DateTime, nullable=False)

    stage_changed_time = Column(DateTime, nullable=True)
    status_changed_time = Column(DateTime, nullable=True)

    qualified_time = Column(DateTime, nullable=True)
    actionable_time = Column(DateTime, nullable=True)

    closed_time = Column(DateTime, nullable=True)
    closed_price = Column(DECIMAL(16, 6), nullable=True)

    last_price = Column(DECIMAL(16, 6), nullable=True)
    ltp = Column(DECIMAL(16, 6), nullable=True)
    ltp_time = Column(DateTime, nullable=True)

    # -----------------------------------
    # Signal Analytics / Excursion
    # -----------------------------------

    last_pnl = Column(DECIMAL(10, 4), nullable=False, default=0.0000)
    last_pnl_value = Column(DECIMAL(13, 2), nullable=False, default=0.00)

    max_price = Column(DECIMAL(13, 2), nullable=False, default=0.00)
    min_price = Column(DECIMAL(13, 2), nullable=False, default=0.00)

    max_time = Column(DateTime, nullable=True)
    min_time = Column(DateTime, nullable=True)

    max_pnl = Column(DECIMAL(10, 4), nullable=False, default=0.0000)
    min_pnl = Column(DECIMAL(10, 4), nullable=False, default=0.0000)

    max_pnl_value = Column(DECIMAL(13, 2), nullable=False, default=0.00)
    min_pnl_value = Column(DECIMAL(13, 2), nullable=False, default=0.00)

    criteria_json = Column(JSON, nullable=False)
    snapshot_json = Column(JSON, nullable=False)
    meta_json = Column(JSON, nullable=True)

    archived_on = Column(
        DateTime,
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )

    trading_date = Column(
        Date,
        Computed("DATE(`last_eval_time`)", persisted=True),
        nullable=False,
    )

    def __repr__(self):
        return (
            f"<SignalHistory "
            f"{self.lifecycle} | {self.equity_ref} | {self.side} | "
            f"stage={self.stage} status={self.status} "
            f"pnl={self.last_pnl}>"
        )
# -----------------------------------
# Trades (live + history) — vNext ORM
# -----------------------------------
class UserTrade(Base):
    __tablename__ = "user_trades"
    __table_args__ = (
        # A signal may deploy at most one row per user/instrument type.  This is
        # the final race-safe guard behind the application-level deployment
        # check.  Re-entry, if ever introduced, must use a new explicit model.
        UniqueConstraint(
            "userid",
            "signal_id",
            "instrument_type",
            name="uq_user_trade_user_signal_instrument",
        ),
    )

    # =============================
    # 1) Identity & provenance
    # =============================
    id              = Column(Integer, primary_key=True, autoincrement=True)
    userid          = Column(String(50),  nullable=False, index=True)

    # Originating lifecycle signal reference (not a FK)
    signal_id  = Column(String(100), nullable=False, index=True)

    source          = Column(String(50),  nullable=False, default="")
    message         = Column(Text,        nullable=True)

    entry_snapshot  = Column(JSON,        nullable=False)
    last_snapshot   = Column(JSON,        nullable=True)

    # =============================
    # 2) Instrument & trade type
    # =============================
    symbol          = Column(String(255), nullable=False)
    equity_ref      = Column(String(50),  nullable=False, index=True)

    instrument_type = Column(String(20),  nullable=False)   # EQ | FUT | CE | PE
    trade_type      = Column(String(10),  nullable=False)   # BUY | SELL

    # hedge tagging
    position_style  = Column(String(20),  nullable=False, default="NAKED")  # NAKED | HEDGED
    hedged_symbol   = Column(String(255), nullable=True)

    # =============================
    # 3) Lifecycle control (ENTRY + EXIT)
    # =============================
    entry_status    = Column(String(30),  nullable=False, default="CREATED", index=True)
    # CREATED | READY | SUBMITTED | FILLED | EXPIRED | CANCELLED | REJECTED | INVALID

    exit_status     = Column(String(30),  nullable=False, default="NONE", index=True)
    # NONE | READY | SUBMITTED | FILLED | CANCELLED | FAILED

    execution_mode  = Column(String(10),  nullable=False, server_default="VIRTUAL")  # REAL | VIRTUAL
    intraday_only   = Column(Boolean,     nullable=False, default=False)

    # =============================
    # 4) Entry (plan + execution audit)
    # =============================
    entry_time           = Column(DateTime,      nullable=False)
    entry_intent_time    = Column(DateTime,      nullable=True)
    entry_exec_time      = Column(DateTime,      nullable=True)

    entry_reconciled_at  = Column(DateTime,      nullable=True)

    entry_price          = Column(Numeric(13, 2), nullable=False, default=0.00)
    executed_entry_price = Column(Numeric(13, 2), nullable=True)
    executed_entry_qty   = Column(Integer,        nullable=True)   # broker truth qty

    quantity             = Column(Integer,        nullable=False, default=1)  # planned qty

    entry_order_id            = Column(String(255), nullable=True)
    entry_order_response_json = Column(Text,        nullable=True)
    entry_retries             = Column(Integer,     nullable=False, default=3)

    # Adaptive trade management state (JSON schema validated in application layer)
    trade_management           = Column(JSON,        nullable=True)

    # =============================
    # 5) Exit (plan + execution audit)
    # =============================
    exit_reason         = Column(String(50),     nullable=True)
    exit_rule           = Column(String(100),    nullable=True)

    exit_time           = Column(DateTime,       nullable=True)
    exit_intent_time    = Column(DateTime,       nullable=True)
    exit_exec_time      = Column(DateTime,       nullable=True)

    exit_reconciled_at  = Column(DateTime,       nullable=True)

    exit_price          = Column(Numeric(13, 2), nullable=True)
    executed_exit_price = Column(Numeric(13, 2), nullable=True)
    executed_exit_qty   = Column(Integer,        nullable=False, default=0)   # cumulative closed qty

    exit_order_id            = Column(String(255), nullable=True)
    exit_order_response_json = Column(Text,        nullable=True)
    exit_retries             = Column(Integer,     nullable=False, default=3)

    exit_pnl = Column(Numeric(13, 2), nullable=True)

    # =============================
    # 8) Live monitoring / MTM
    # =============================
    last_time      = Column(DateTime,       nullable=False)
    last_price     = Column(Numeric(13, 2), nullable=False, default=0.00)

    last_pnl       = Column(Numeric(10, 4), nullable=False, default=0.0000)
    last_pnl_value = Column(Numeric(13, 2), nullable=False, default=0.00)

    max_price      = Column(Numeric(13, 2), nullable=False, default=0.00)
    min_price      = Column(Numeric(13, 2), nullable=False, default=0.00)
    max_time       = Column(DateTime,       nullable=False)
    min_time       = Column(DateTime,       nullable=False)

    # =============================
    # 9) Execution observability (executor-owned)
    # =============================
    exec_last_checked_at = Column(DateTime,    nullable=True)
    exec_status          = Column(String(50),  nullable=True)
    exec_status_message  = Column(String(255), nullable=True)

    # =============================
    # 10) Reconciliation observability (backfill-owned)
    # =============================
    reconcile_last_checked_at = Column(DateTime,    nullable=True)
    reconcile_status          = Column(String(50),  nullable=True)
    reconcile_status_message  = Column(String(255), nullable=True)

    def __repr__(self):
        return (
            f"<UserTrade id={self.id} userid={self.userid} "
            f"symbol={self.symbol} entry_status={self.entry_status} "
            f"exit_status={self.exit_status} mode={self.execution_mode} "
            f"pos={self.position_style}>"
        )


class UserTradeHistory(Base):
    __tablename__ = "user_trades_history"
    __table_args__ = (
        UniqueConstraint("id", "trading_date", name="uq_usertrade_liveid_by_day"),
        Index("idx_uth_userid_day", "userid", "trading_date"),
        Index("idx_uth_signal_day", "signal_id", "trading_date"),
        Index("idx_uth_symbol_entry", "symbol", "entry_time"),
        Index("idx_uth_exit_time", "exit_time"),
        Index("idx_uth_last_time", "last_time"),
        {"info": {"intraday": False}},
    )

    hist_id = Column(BigInteger, primary_key=True, autoincrement=True)

    # original live id
    id = Column(Integer, nullable=False)

    userid = Column(String(50), nullable=False)
    signal_id = Column(String(100), nullable=False)

    source = Column(String(50), nullable=False, server_default="")
    message = Column(Text, nullable=True)

    entry_snapshot = Column(JSON, nullable=False)
    last_snapshot = Column(JSON, nullable=True)

    symbol = Column(String(255), nullable=False)
    equity_ref = Column(String(50), nullable=False)

    instrument_type = Column(String(20), nullable=False)
    trade_type = Column(String(10), nullable=False)

    position_style = Column(String(20), nullable=False)
    hedged_symbol = Column(String(255), nullable=True)

    entry_status = Column(String(30), nullable=False)
    exit_status = Column(String(30), nullable=False)

    execution_mode = Column(String(10), nullable=False)
    intraday_only = Column(Boolean, nullable=False, default=False)

    entry_time = Column(DateTime, nullable=False)
    entry_intent_time = Column(DateTime, nullable=True)
    entry_exec_time = Column(DateTime, nullable=True)

    entry_reconciled_at = Column(DateTime, nullable=True)

    entry_price = Column(Numeric(13, 2), nullable=False)
    executed_entry_price = Column(Numeric(13, 2), nullable=True)
    executed_entry_qty = Column(Integer, nullable=True)

    quantity = Column(Integer, nullable=False)

    entry_order_id = Column(String(255), nullable=True)
    entry_order_response_json = Column(Text, nullable=True)
    entry_retries = Column(Integer, nullable=False)

    # Adaptive trade management state (JSON schema validated in application layer)
    trade_management = Column(JSON, nullable=True)

    exit_reason = Column(String(50), nullable=True)
    exit_rule = Column(String(100), nullable=True)

    exit_time = Column(DateTime, nullable=True)
    exit_intent_time = Column(DateTime, nullable=True)
    exit_exec_time = Column(DateTime, nullable=True)

    exit_reconciled_at = Column(DateTime, nullable=True)

    exit_price = Column(Numeric(13, 2), nullable=True)
    executed_exit_price = Column(Numeric(13, 2), nullable=True)
    executed_exit_qty = Column(Integer, nullable=False, default=0)

    exit_order_id = Column(String(255), nullable=True)
    exit_order_response_json = Column(Text, nullable=True)
    exit_retries = Column(Integer, nullable=False)

    exit_pnl = Column(Numeric(13, 2), nullable=True)

    last_time = Column(DateTime, nullable=False)
    last_price = Column(Numeric(13, 2), nullable=False)

    last_pnl = Column(Numeric(10, 4), nullable=False)
    last_pnl_value = Column(Numeric(13, 2), nullable=False)

    max_price = Column(Numeric(13, 2), nullable=False)
    min_price = Column(Numeric(13, 2), nullable=False)
    max_time = Column(DateTime, nullable=False)
    min_time = Column(DateTime, nullable=False)

    # executor-owned observability
    exec_last_checked_at = Column(DateTime,    nullable=True)
    exec_status          = Column(String(50),  nullable=True)
    exec_status_message  = Column(String(255), nullable=True)

    # backfill-owned observability
    reconcile_last_checked_at = Column(DateTime,    nullable=True)
    reconcile_status          = Column(String(50),  nullable=True)
    reconcile_status_message  = Column(String(255), nullable=True)

    archived_on = Column(DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    trading_date = Column(Date, Computed("DATE(`entry_time`)", persisted=True), nullable=False)
   
# -----------------------------------
# Audit log
# -----------------------------------

class AuditLog(Base):
    __tablename__ = "auditlog"
    __table_args__ = (
        Index("idx_auditlog_ts", "ts"),
        Index("idx_auditlog_entity", "entity_type", "entity_id"),
        Index("idx_auditlog_symbol_ts", "symbol", "ts"),
        Index("idx_auditlog_userid_ts", "userid", "ts"),
        Index("idx_auditlog_stage_ts", "evaluation_stage", "ts"),
        {"info": {"intraday": True}},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    ts = Column(DateTime, nullable=False)

    entity_type = Column(String(30), nullable=False)      # SIGNAL | TRADE | SYSTEM
    entity_id = Column(String(80), nullable=True)
    symbol = Column(String(50), nullable=True)
    userid = Column(String(50), nullable=True)

    evaluation_stage = Column(String(50), nullable=False) # resolver | trade_generator | trade_monitor | init_reset
    previous_state = Column(String(80), nullable=True)
    new_state = Column(String(80), nullable=True)
    action = Column(String(80), nullable=True)

    reason_code = Column(String(120), nullable=True)
    reason_text = Column(Text, nullable=True)
    confidence = Column(DECIMAL(8, 2), nullable=True)
    payload_json = Column(JSON, nullable=True)

    def __repr__(self):
        return (
            f"<AuditLog id={self.id} entity={self.entity_type}:{self.entity_id} "
            f"stage={self.evaluation_stage} action={self.action}>"
        )


class AuditLogHistory(Base):
    __tablename__ = "auditlog_history"
    __table_args__ = (
        Index("idx_auditloghist_ts", "ts"),
        Index("idx_auditloghist_entity", "entity_type", "entity_id"),
        Index("idx_auditloghist_symbol_ts", "symbol", "ts"),
        Index("idx_auditloghist_userid_ts", "userid", "ts"),
        Index("idx_auditloghist_stage_ts", "evaluation_stage", "ts"),
    )

    history_id = Column(BigInteger, primary_key=True, autoincrement=True)
    auditlog_id = Column(BigInteger, nullable=True)
    ts = Column(DateTime, nullable=False)
    entity_type = Column(String(30), nullable=False)
    entity_id = Column(String(80), nullable=True)
    symbol = Column(String(50), nullable=True)
    userid = Column(String(50), nullable=True)

    evaluation_stage = Column(String(50), nullable=False)
    previous_state = Column(String(80), nullable=True)
    new_state = Column(String(80), nullable=True)
    action = Column(String(80), nullable=True)

    reason_code = Column(String(120), nullable=True)
    reason_text = Column(Text, nullable=True)
    confidence = Column(DECIMAL(8, 2), nullable=True)
    payload_json = Column(JSON, nullable=True)


    def __repr__(self):
        return (
            f"<AuditLogHistory history_id={self.history_id} "
            f"auditlog_id={self.auditlog_id} entity={self.entity_type}:{self.entity_id}>"
        )

# -----------------------------------
# Events / Alerts
# -----------------------------------

class Event(Base):
    __tablename__ = "events"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    event_type = Column(String(100), nullable=False)
    aggregate_key = Column(String(128), nullable=False)
    correlation_id = Column(String(64), nullable=True)
    payload = Column(JSON, nullable=True)
    status = Column(String(32), nullable=False, default="pending")
    attempts = Column(Integer, nullable=False, default=0)
    last_error = Column(Text, nullable=True)
    available_at = Column(DateTime, nullable=False, server_default=func.now())
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(
        DateTime,
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        Index("ix_status_available", "status", "available_at"),
        Index("ix_event_type", "event_type"),
        Index("ix_aggregate_key", "aggregate_key"),
        Index("ix_correlation_id", "correlation_id"),
    )

    def __repr__(self):
        return f"<Event {self.event_type} {self.aggregate_key} status={self.status} attempts={self.attempts}>"


class Alert(Base):
    __tablename__ = "alerts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    etime = Column(DateTime, nullable=True)
    message = Column(String(500), nullable=False)
    processed = Column(Boolean, default=False)

    def __repr__(self):
        return f"<Alert id={self.id} etime={self.etime} message='{self.message}'>"
