-- AutoTrades Auction Authority Stage 4
-- Single-table persistence for deployed authoritative stock opportunities.
--
-- Run this against the intended database (backtest for replay validation).
-- No DROP/ALTER of signals or user_trades is required.

CREATE TABLE IF NOT EXISTS `stock_opportunities` (
  `id` bigint NOT NULL AUTO_INCREMENT,

  `opportunity_key` varchar(64) NOT NULL,
  `candidate_id` varchar(64) NOT NULL,
  `latest_candidate_id` varchar(64) NOT NULL,

  `symbol` varchar(32) NOT NULL,
  `equity_ref` varchar(32) NOT NULL,
  `trading_day` date NOT NULL,

  `setup_family` varchar(64) NOT NULL,
  `current_setup_family` varchar(64) NOT NULL,
  `setup_subtype` varchar(64) NOT NULL,
  `side` varchar(8) NOT NULL,

  `source_event_id` varchar(128) NOT NULL,
  `source_event_type` varchar(64) NOT NULL,
  `source_episode_id` varchar(128) NOT NULL,
  `boundary_event_key` varchar(128) NOT NULL,

  `latest_event_id` varchar(128) NOT NULL,
  `latest_event_type` varchar(64) NOT NULL,
  `latest_episode_id` varchar(128) NOT NULL,

  `lifecycle_state` varchar(32) NOT NULL,
  `lifecycle_reason` varchar(255) NOT NULL,
  `structural_result` varchar(16) NOT NULL,

  `first_seen_time` datetime NOT NULL,
  `last_eval_time` datetime NOT NULL,
  `deployed_at` datetime NOT NULL,
  `progressed_at` datetime DEFAULT NULL,
  `completed_at` datetime DEFAULT NULL,
  `invalidated_at` datetime DEFAULT NULL,
  `replaced_at` datetime DEFAULT NULL,

  `entry_price` decimal(16,6) NOT NULL,
  `reference_price` decimal(16,6) NOT NULL,
  `stop_reference_price` decimal(16,6) NOT NULL,
  `target_reference_price` decimal(16,6) NOT NULL,

  `signal_id` varchar(36) NOT NULL,
  `replacement_opportunity_key` varchar(64) DEFAULT NULL,
  `replaced_opportunity_key` varchar(64) DEFAULT NULL,

  `transition_history` json NOT NULL,
  `candidate_interpretations` json NOT NULL,
  `authoritative_event_lineage` json NOT NULL,
  `latest_setup_evaluation` json DEFAULT NULL,
  `latest_advisor_evaluation` json DEFAULT NULL,
  `metadata_json` json DEFAULT NULL,

  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_stock_opportunity_key` (`opportunity_key`),
  UNIQUE KEY `uq_stock_opportunity_signal_id` (`signal_id`),
  KEY `idx_stock_opp_symbol_day` (`symbol`,`trading_day`),
  KEY `idx_stock_opp_equity_day` (`equity_ref`,`trading_day`),
  KEY `idx_stock_opp_day_state` (`trading_day`,`lifecycle_state`),
  KEY `idx_stock_opp_family_side_day` (`setup_family`,`side`,`trading_day`),
  KEY `idx_stock_opp_episode` (`source_episode_id`),
  KEY `idx_stock_opp_latest_episode` (`latest_episode_id`),
  KEY `idx_stock_opp_last_eval` (`last_eval_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
