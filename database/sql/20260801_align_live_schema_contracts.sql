-- AutoTrades core schema alignment after the 1 August 2026 structure review.
--
-- This migration aligns live MySQL identity keys and high-value indexes with
-- models/trade_models.py and the DB-backed Pydantic schemas.  It is intended
-- to run once against the current AutoTrades database while services are
-- stopped.
--
-- It deliberately does not drop any table or business column.

-- -------------------------------------------------------------------------
-- Preflight: abort before changing anything if identities are ambiguous.
-- -------------------------------------------------------------------------
DROP PROCEDURE IF EXISTS `_assert_autotrades_schema_alignment_ready`;
DELIMITER //
CREATE PROCEDURE `_assert_autotrades_schema_alignment_ready`()
BEGIN
    DECLARE v_count BIGINT DEFAULT 0;

    SELECT COUNT(*) INTO v_count
    FROM (SELECT id FROM users GROUP BY id HAVING COUNT(*) > 1) AS duplicates;
    IF v_count > 0 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'users contains duplicate id values';
    END IF;

    SELECT COUNT(*) INTO v_count
    FROM users
    WHERE userid IS NULL OR name IS NULL OR email IS NULL
       OR mobile IS NULL OR password IS NULL;
    IF v_count > 0 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'users contains NULL required identity fields';
    END IF;

    SELECT COUNT(*) INTO v_count
    FROM (SELECT symbol FROM symbols GROUP BY symbol HAVING COUNT(*) > 1) AS duplicates;
    IF v_count > 0 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'symbols contains duplicate symbol values';
    END IF;

    SELECT COUNT(*) INTO v_count
    FROM symbols
    WHERE symbol IS NULL OR type IS NULL;
    IF v_count > 0 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'symbols contains NULL symbol/type values';
    END IF;

    SELECT COUNT(*) INTO v_count
    FROM (SELECT id FROM alerts GROUP BY id HAVING COUNT(*) > 1) AS duplicates;
    IF v_count > 0 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'alerts contains duplicate id values';
    END IF;

    SELECT COUNT(*) INTO v_count
    FROM alerts
    WHERE message IS NULL;
    IF v_count > 0 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'alerts contains NULL messages';
    END IF;

    SELECT COUNT(*) INTO v_count
    FROM (SELECT id FROM events GROUP BY id HAVING COUNT(*) > 1) AS duplicates;
    IF v_count > 0 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'events contains duplicate id values';
    END IF;

    SELECT COUNT(*) INTO v_count
    FROM events
    WHERE event_type IS NULL OR aggregate_key IS NULL;
    IF v_count > 0 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'events contains NULL event identity fields';
    END IF;

    SELECT COUNT(*) INTO v_count
    FROM (
        SELECT symbol, snapshot_time
        FROM snapshots
        GROUP BY symbol, snapshot_time
        HAVING COUNT(*) > 1
    ) AS duplicates;
    IF v_count > 0 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'snapshots contains duplicate symbol/time rows';
    END IF;

    SELECT COUNT(*) INTO v_count FROM snapshots WHERE symbol IS NULL;
    IF v_count > 0 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'snapshots contains NULL symbols';
    END IF;

    SELECT COUNT(*) INTO v_count
    FROM (
        SELECT symbol, snapshot_time
        FROM derivativeschain
        GROUP BY symbol, snapshot_time
        HAVING COUNT(*) > 1
    ) AS duplicates;
    IF v_count > 0 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'derivativeschain contains duplicate symbol/time rows';
    END IF;

    SELECT COUNT(*) INTO v_count FROM derivativeschain WHERE symbol IS NULL;
    IF v_count > 0 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'derivativeschain contains NULL symbols';
    END IF;

    SELECT COUNT(*) INTO v_count FROM candles WHERE symbol IS NULL;
    IF v_count > 0 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'candles contains NULL symbols';
    END IF;

    SELECT COUNT(*) INTO v_count
    FROM instruments
    WHERE instrument_token IS NULL
       OR exchange_token IS NULL
       OR tradingsymbol IS NULL
       OR name IS NULL
       OR instrument_type IS NULL
       OR segment IS NULL
       OR exchange IS NULL;
    IF v_count > 0 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'instruments contains NULL broker identity fields';
    END IF;

    SELECT COUNT(*) INTO v_count
    FROM (
        SELECT hist_id
        FROM user_trades_history
        GROUP BY hist_id
        HAVING COUNT(*) > 1
    ) AS duplicates;
    IF v_count > 0 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'user_trades_history contains duplicate hist_id values';
    END IF;
END//
DELIMITER ;

CALL `_assert_autotrades_schema_alignment_ready`();
DROP PROCEDURE `_assert_autotrades_schema_alignment_ready`;

-- -------------------------------------------------------------------------
-- Normalize nullable flag/default columns before enforcing their contracts.
-- -------------------------------------------------------------------------
UPDATE users SET broker_login = 0 WHERE broker_login IS NULL;
UPDATE users SET broker_name = 'ZERODHA' WHERE broker_name IS NULL;
UPDATE users SET apikey = '' WHERE apikey IS NULL;
UPDATE users SET secretkey = '' WHERE secretkey IS NULL;
UPDATE users SET access_token = '' WHERE access_token IS NULL;
UPDATE users SET intraday_only = 0 WHERE intraday_only IS NULL;
UPDATE users SET stocks = '' WHERE stocks IS NULL;
UPDATE users SET equity = 1 WHERE equity IS NULL;
UPDATE users SET futures = 1 WHERE futures IS NULL;
UPDATE users SET options = 1 WHERE options IS NULL;
UPDATE users SET execution_mode = 'VIRTUAL' WHERE execution_mode IS NULL;
UPDATE users SET autotrade = 0 WHERE autotrade IS NULL;
UPDATE users SET active = 1 WHERE active IS NULL;
UPDATE users SET logged_in = 0 WHERE logged_in IS NULL;

UPDATE symbols SET enabled = 0 WHERE enabled IS NULL;
UPDATE alerts SET processed = 0 WHERE processed IS NULL;
UPDATE events SET status = 'pending' WHERE status IS NULL;
UPDATE snapshots SET processed = 0 WHERE processed IS NULL;

UPDATE candles SET open = 0.00 WHERE open IS NULL;
UPDATE candles SET high = 0.00 WHERE high IS NULL;
UPDATE candles SET low = 0.00 WHERE low IS NULL;
UPDATE candles SET close = 0.00 WHERE close IS NULL;
UPDATE candles SET volume = 0.00 WHERE volume IS NULL;
UPDATE candles SET oi = 0.00 WHERE oi IS NULL;
UPDATE candles SET active = 1 WHERE active IS NULL;

-- -------------------------------------------------------------------------
-- Correct primary keys and required identity contracts.
-- -------------------------------------------------------------------------
ALTER TABLE users
    MODIFY COLUMN id INT NOT NULL AUTO_INCREMENT,
    MODIFY COLUMN userid VARCHAR(50) NOT NULL,
    MODIFY COLUMN name VARCHAR(30) NOT NULL,
    MODIFY COLUMN email VARCHAR(50) NOT NULL,
    MODIFY COLUMN mobile VARCHAR(10) NOT NULL,
    MODIFY COLUMN password VARCHAR(50) NOT NULL,
    MODIFY COLUMN broker_login TINYINT(1) NOT NULL DEFAULT 0,
    MODIFY COLUMN broker_name VARCHAR(30) NOT NULL DEFAULT 'ZERODHA',
    MODIFY COLUMN apikey VARCHAR(255) NOT NULL DEFAULT '',
    MODIFY COLUMN secretkey VARCHAR(255) NOT NULL DEFAULT '',
    MODIFY COLUMN access_token VARCHAR(255) NOT NULL DEFAULT '',
    MODIFY COLUMN intraday_only TINYINT(1) NOT NULL DEFAULT 0,
    MODIFY COLUMN stocks VARCHAR(255) NOT NULL DEFAULT '',
    MODIFY COLUMN equity TINYINT(1) NOT NULL DEFAULT 1,
    MODIFY COLUMN futures TINYINT(1) NOT NULL DEFAULT 1,
    MODIFY COLUMN options TINYINT(1) NOT NULL DEFAULT 1,
    MODIFY COLUMN execution_mode VARCHAR(8) NOT NULL DEFAULT 'VIRTUAL',
    MODIFY COLUMN autotrade TINYINT(1) NOT NULL DEFAULT 0,
    MODIFY COLUMN active TINYINT(1) NOT NULL DEFAULT 1,
    MODIFY COLUMN logged_in TINYINT(1) NOT NULL DEFAULT 0,
    ADD PRIMARY KEY (id);

ALTER TABLE symbols
    DROP INDEX id,
    DROP INDEX symbols_equity_ref_IDX,
    MODIFY COLUMN id INT NOT NULL AUTO_INCREMENT,
    MODIFY COLUMN symbol VARCHAR(50) NOT NULL,
    MODIFY COLUMN type VARCHAR(10) NOT NULL,
    MODIFY COLUMN signal_profile VARCHAR(1000) NOT NULL DEFAULT 'DEFAULT',
    MODIFY COLUMN lotsize INT NOT NULL DEFAULT 1,
    MODIFY COLUMN generate_candles TINYINT(1) NOT NULL DEFAULT 0,
    MODIFY COLUMN merge_candles TINYINT(1) NOT NULL DEFAULT 0,
    MODIFY COLUMN update_performance TINYINT(1) NOT NULL DEFAULT 0,
    MODIFY COLUMN generate_signals TINYINT(1) NOT NULL DEFAULT 0,
    MODIFY COLUMN processed TINYINT(1) NOT NULL DEFAULT 0,
    MODIFY COLUMN active TINYINT(1) NOT NULL DEFAULT 0,
    MODIFY COLUMN enabled TINYINT(1) NOT NULL DEFAULT 0,
    ADD PRIMARY KEY (id),
    ADD UNIQUE KEY uq_symbols_symbol (symbol),
    ADD KEY idx_symbols_type_enabled_active (type, enabled, active),
    ADD KEY idx_symbols_derivative_lookup (
        equity_ref, type, expiry, enabled, strike_price
    );

ALTER TABLE alerts
    MODIFY COLUMN id INT NOT NULL AUTO_INCREMENT,
    MODIFY COLUMN message VARCHAR(500) NOT NULL,
    MODIFY COLUMN processed TINYINT(1) NOT NULL DEFAULT 0,
    ADD PRIMARY KEY (id);

ALTER TABLE events
    MODIFY COLUMN id BIGINT NOT NULL AUTO_INCREMENT,
    MODIFY COLUMN event_type VARCHAR(100) NOT NULL,
    MODIFY COLUMN aggregate_key VARCHAR(128) NOT NULL,
    MODIFY COLUMN status VARCHAR(32) NOT NULL DEFAULT 'pending',
    MODIFY COLUMN attempts INT NOT NULL DEFAULT 0,
    MODIFY COLUMN available_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    MODIFY COLUMN created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    MODIFY COLUMN updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,
    ADD PRIMARY KEY (id);

ALTER TABLE snapshots
    MODIFY COLUMN symbol VARCHAR(50) NOT NULL,
    MODIFY COLUMN processed TINYINT(1) NOT NULL DEFAULT 0,
    ADD PRIMARY KEY (symbol, snapshot_time),
    ADD KEY idx_snapshots_time_symbol (snapshot_time, symbol),
    ADD KEY idx_snapshots_unprocessed (processed, snapshot_time, symbol);

ALTER TABLE derivativeschain
    MODIFY COLUMN symbol VARCHAR(50) NOT NULL,
    MODIFY COLUMN derived JSON NULL,
    ADD PRIMARY KEY (symbol, snapshot_time);

ALTER TABLE candles
    MODIFY COLUMN symbol VARCHAR(50) NOT NULL,
    MODIFY COLUMN open DECIMAL(13,2) NOT NULL DEFAULT 0.00,
    MODIFY COLUMN high DECIMAL(13,2) NOT NULL DEFAULT 0.00,
    MODIFY COLUMN low DECIMAL(13,2) NOT NULL DEFAULT 0.00,
    MODIFY COLUMN close DECIMAL(13,2) NOT NULL DEFAULT 0.00,
    MODIFY COLUMN volume DECIMAL(13,2) NOT NULL DEFAULT 0.00,
    MODIFY COLUMN oi DECIMAL(13,2) NOT NULL DEFAULT 0.00,
    MODIFY COLUMN active TINYINT(1) NOT NULL DEFAULT 1;

ALTER TABLE instruments
    MODIFY COLUMN instrument_token VARCHAR(20) NOT NULL,
    MODIFY COLUMN exchange_token VARCHAR(20) NOT NULL,
    MODIFY COLUMN tradingsymbol VARCHAR(100) NOT NULL,
    MODIFY COLUMN name VARCHAR(100) NOT NULL,
    MODIFY COLUMN expiry DATE DEFAULT NULL,
    MODIFY COLUMN instrument_type VARCHAR(10) NOT NULL,
    MODIFY COLUMN segment VARCHAR(10) NOT NULL,
    MODIFY COLUMN exchange VARCHAR(10) NOT NULL,
    ADD KEY idx_instruments_underlying_expiry (name, instrument_type, expiry);

-- user_trades already has the correct primary key and uniqueness guards.
-- Remove redundant single-column indexes covered by left-prefix composite
-- indexes and add the cross-user signal lookup used by review/runtime helpers.
ALTER TABLE user_trades
    DROP INDEX idx_entry_status,
    DROP INDEX idx_exit_status,
    DROP INDEX idx_userid,
    ADD KEY idx_signal_id (signal_id);

-- The previous history table definition could not support INSERT statements
-- that omit hist_id, archived_on and trading_date.  Align it with the ORM and
-- the archive contract before refactoring init_intraday.
ALTER TABLE user_trades_history
    MODIFY COLUMN hist_id BIGINT NOT NULL AUTO_INCREMENT,
    MODIFY COLUMN archived_on DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    MODIFY COLUMN trading_date DATE
        GENERATED ALWAYS AS (CAST(entry_time AS DATE)) STORED NOT NULL,
    ADD PRIMARY KEY (hist_id);

-- -------------------------------------------------------------------------
-- Post-migration contract summary.
-- -------------------------------------------------------------------------
SELECT
    table_name,
    index_name,
    non_unique,
    GROUP_CONCAT(column_name ORDER BY seq_in_index) AS indexed_columns
FROM information_schema.statistics
WHERE table_schema = DATABASE()
  AND table_name IN (
      'users',
      'symbols',
      'alerts',
      'events',
      'candles',
      'instruments',
      'snapshots',
      'derivativeschain',
      'signals',
      'user_trades',
      'user_trades_history'
  )
GROUP BY table_name, index_name, non_unique
ORDER BY table_name, index_name;

SELECT
    table_name,
    column_name,
    column_type,
    is_nullable,
    column_key,
    column_default,
    extra
FROM information_schema.columns
WHERE table_schema = DATABASE()
  AND (
      (table_name IN ('users', 'symbols', 'alerts', 'events') AND column_name = 'id')
      OR (table_name = 'user_trades_history' AND column_name IN ('hist_id', 'archived_on', 'trading_date'))
      OR (table_name IN ('snapshots', 'derivativeschain') AND column_name IN ('symbol', 'snapshot_time'))
  )
ORDER BY table_name, ordinal_position;
