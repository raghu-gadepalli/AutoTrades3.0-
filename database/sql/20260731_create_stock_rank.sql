-- AutoTrades diagnostic rolling stock movement ranking.
--
-- One row per symbol per completed ranking cadence.  This table is diagnostic
-- only: it does not control symbols.active, signal creation or trade lifecycle.

CREATE TABLE IF NOT EXISTS `stock_rank` (
  `id` bigint NOT NULL AUTO_INCREMENT,

  `run_id` varchar(64) NOT NULL,
  `trading_day` date NOT NULL,
  `rank_time` datetime NOT NULL,
  `symbol` varchar(32) NOT NULL,
  `rank_position` int NOT NULL,
  `universe_size` int NOT NULL,

  `direction` varchar(8) NOT NULL,
  `classification` varchar(32) NOT NULL,

  `total_score` decimal(10,4) NOT NULL,
  `movement_score` decimal(10,4) NOT NULL,
  `quality_score` decimal(10,4) NOT NULL,
  `range_penalty` decimal(10,4) NOT NULL,
  `stall_penalty` decimal(10,4) NOT NULL,

  `close_price` decimal(16,6) NOT NULL,
  `previous_close` decimal(16,6) DEFAULT NULL,
  `today_open` decimal(16,6) DEFAULT NULL,
  `gap_pct` decimal(12,6) DEFAULT NULL,
  `session_move_pct` decimal(12,6) DEFAULT NULL,
  `post_open_move_pct` decimal(12,6) DEFAULT NULL,

  `move_15m_pct` decimal(12,6) DEFAULT NULL,
  `move_30m_pct` decimal(12,6) DEFAULT NULL,
  `move_60m_pct` decimal(12,6) DEFAULT NULL,
  `move_15m_atr` decimal(12,6) DEFAULT NULL,
  `move_30m_atr` decimal(12,6) DEFAULT NULL,
  `move_60m_atr` decimal(12,6) DEFAULT NULL,

  `atr_value` decimal(16,6) NOT NULL,
  `atr_pct` decimal(12,6) DEFAULT NULL,
  `directional_efficiency` decimal(12,6) DEFAULT NULL,
  `recent_efficiency` decimal(12,6) DEFAULT NULL,
  `direction_consistency` decimal(12,6) NOT NULL,
  `acceleration_score` decimal(12,6) NOT NULL,
  `volume_ratio` decimal(12,6) DEFAULT NULL,
  `freshness_score` decimal(12,6) NOT NULL,
  `bars_since_extreme` int NOT NULL,

  `range_active` tinyint(1) NOT NULL DEFAULT 0,
  `range_episode_id` varchar(128) DEFAULT NULL,
  `range_id` varchar(128) DEFAULT NULL,
  `range_age_bars` int NOT NULL DEFAULT 0,
  `range_width_pct` decimal(12,6) DEFAULT NULL,
  `containment_ratio` decimal(12,6) DEFAULT NULL,
  `midpoint_crossings` int NOT NULL DEFAULT 0,
  `vwap_crossings` int NOT NULL DEFAULT 0,
  `failed_escape_count` int NOT NULL DEFAULT 0,
  `rearm_required` tinyint(1) NOT NULL DEFAULT 0,
  `attempt_limit_reached` tinyint(1) NOT NULL DEFAULT 0,

  `metrics_json` json NOT NULL,

  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_stock_rank_symbol_time` (`symbol`,`rank_time`),
  KEY `idx_stock_rank_run` (`run_id`),
  KEY `idx_stock_rank_time_position` (`rank_time`,`rank_position`),
  KEY `idx_stock_rank_day_symbol` (`trading_day`,`symbol`),
  KEY `idx_stock_rank_day_class` (`trading_day`,`classification`),
  KEY `idx_stock_rank_day_score` (`trading_day`,`total_score`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
