-- MySQL dump 10.13  Distrib 8.0.46, for Linux (x86_64)
--
-- Host: localhost    Database: autotrades
-- ------------------------------------------------------
-- Server version	8.0.46-0ubuntu0.22.04.3

/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;
/*!40101 SET @OLD_CHARACTER_SET_RESULTS=@@CHARACTER_SET_RESULTS */;
/*!40101 SET @OLD_COLLATION_CONNECTION=@@COLLATION_CONNECTION */;
/*!50503 SET NAMES utf8mb4 */;
/*!40103 SET @OLD_TIME_ZONE=@@TIME_ZONE */;
/*!40103 SET TIME_ZONE='+00:00' */;
/*!40014 SET @OLD_UNIQUE_CHECKS=@@UNIQUE_CHECKS, UNIQUE_CHECKS=0 */;
/*!40014 SET @OLD_FOREIGN_KEY_CHECKS=@@FOREIGN_KEY_CHECKS, FOREIGN_KEY_CHECKS=0 */;
/*!40101 SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO' */;
/*!40111 SET @OLD_SQL_NOTES=@@SQL_NOTES, SQL_NOTES=0 */;

--
-- Table structure for table `alerts`
--

DROP TABLE IF EXISTS `alerts`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `alerts` (
  `id` int NOT NULL,
  `etime` datetime DEFAULT NULL,
  `message` varchar(500) DEFAULT NULL,
  `processed` tinyint(1) DEFAULT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `auditlog`
--

DROP TABLE IF EXISTS `auditlog`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `auditlog` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `ts` datetime NOT NULL,
  `entity_type` varchar(30) NOT NULL,
  `entity_id` varchar(80) DEFAULT NULL,
  `symbol` varchar(50) DEFAULT NULL,
  `userid` varchar(50) DEFAULT NULL,
  `evaluation_stage` varchar(50) NOT NULL,
  `previous_state` varchar(80) DEFAULT NULL,
  `new_state` varchar(80) DEFAULT NULL,
  `action` varchar(80) DEFAULT NULL,
  `reason_code` varchar(120) DEFAULT NULL,
  `reason_text` text,
  `confidence` decimal(8,2) DEFAULT NULL,
  `payload_json` json DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_auditlog_ts` (`ts`),
  KEY `idx_auditlog_entity` (`entity_type`,`entity_id`),
  KEY `idx_auditlog_symbol_ts` (`symbol`,`ts`),
  KEY `idx_auditlog_userid_ts` (`userid`,`ts`),
  KEY `idx_auditlog_stage_ts` (`evaluation_stage`,`ts`)
) ENGINE=InnoDB AUTO_INCREMENT=6874 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `auditlog_history`
--

DROP TABLE IF EXISTS `auditlog_history`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `auditlog_history` (
  `history_id` bigint NOT NULL AUTO_INCREMENT,
  `auditlog_id` bigint DEFAULT NULL,
  `ts` datetime NOT NULL,
  `entity_type` varchar(30) NOT NULL,
  `entity_id` varchar(80) DEFAULT NULL,
  `symbol` varchar(50) DEFAULT NULL,
  `userid` varchar(50) DEFAULT NULL,
  `evaluation_stage` varchar(50) NOT NULL,
  `previous_state` varchar(80) DEFAULT NULL,
  `new_state` varchar(80) DEFAULT NULL,
  `action` varchar(80) DEFAULT NULL,
  `reason_code` varchar(120) DEFAULT NULL,
  `reason_text` text,
  `confidence` decimal(8,2) DEFAULT NULL,
  `payload_json` json DEFAULT NULL,
  PRIMARY KEY (`history_id`),
  KEY `idx_auditloghist_ts` (`ts`),
  KEY `idx_auditloghist_entity` (`entity_type`,`entity_id`),
  KEY `idx_auditloghist_symbol_ts` (`symbol`,`ts`),
  KEY `idx_auditloghist_userid_ts` (`userid`,`ts`),
  KEY `idx_auditloghist_stage_ts` (`evaluation_stage`,`ts`)
) ENGINE=InnoDB AUTO_INCREMENT=1027 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `candles`
--

DROP TABLE IF EXISTS `candles`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `candles` (
  `id` int NOT NULL AUTO_INCREMENT,
  `symbol` varchar(50) DEFAULT NULL,
  `frequency` int NOT NULL,
  `candle_time` datetime NOT NULL,
  `open` decimal(13,2) DEFAULT NULL,
  `high` decimal(13,2) DEFAULT NULL,
  `low` decimal(13,2) DEFAULT NULL,
  `close` decimal(13,2) DEFAULT NULL,
  `volume` decimal(13,2) DEFAULT NULL,
  `oi` decimal(13,2) DEFAULT NULL,
  `active` tinyint(1) DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `symbol_frequency_ctime` (`symbol`,`frequency`,`candle_time`),
  KEY `ctime_idx` (`candle_time`),
  KEY `frequency_idx` (`frequency`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `derivativeschain`
--

DROP TABLE IF EXISTS `derivativeschain`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `derivativeschain` (
  `symbol` varchar(50) DEFAULT NULL,
  `snapshot_time` datetime NOT NULL,
  `raw` json NOT NULL,
  `derived` json NOT NULL,
  KEY `idx_deriv_v2_time` (`snapshot_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `events`
--

DROP TABLE IF EXISTS `events`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `events` (
  `id` bigint NOT NULL,
  `event_type` varchar(100) DEFAULT NULL,
  `aggregate_key` varchar(128) DEFAULT NULL,
  `correlation_id` varchar(64) DEFAULT NULL,
  `payload` json DEFAULT NULL,
  `status` varchar(32) DEFAULT NULL,
  `attempts` int NOT NULL,
  `last_error` text,
  `available_at` datetime NOT NULL,
  `created_at` datetime NOT NULL,
  `updated_at` datetime NOT NULL,
  KEY `ix_aggregate_key` (`aggregate_key`),
  KEY `ix_correlation_id` (`correlation_id`),
  KEY `ix_event_type` (`event_type`),
  KEY `ix_status_available` (`status`,`available_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `instruments`
--

DROP TABLE IF EXISTS `instruments`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `instruments` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `instrument_token` varchar(20) DEFAULT NULL,
  `exchange_token` varchar(20) DEFAULT NULL,
  `tradingsymbol` varchar(100) DEFAULT NULL,
  `name` varchar(100) DEFAULT NULL,
  `last_price` decimal(13,2) DEFAULT NULL,
  `expiry` datetime DEFAULT NULL,
  `strike` decimal(13,2) DEFAULT NULL,
  `tick_size` decimal(13,2) DEFAULT NULL,
  `lot_size` decimal(13,0) DEFAULT NULL,
  `instrument_type` varchar(10) DEFAULT NULL,
  `segment` varchar(10) DEFAULT NULL,
  `exchange` varchar(10) DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `instrument_token` (`instrument_token`),
  KEY `exchange` (`exchange`),
  KEY `expiry` (`expiry`),
  KEY `instrument_type` (`instrument_type`),
  KEY `name` (`name`),
  KEY `tradingsymbol` (`tradingsymbol`)
) ENGINE=InnoDB AUTO_INCREMENT=97001 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `oms_funds`
--

DROP TABLE IF EXISTS `oms_funds`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `oms_funds` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `trading_day` date NOT NULL,
  `client_id` varchar(50) NOT NULL,
  `net_balance` decimal(15,2) DEFAULT NULL,
  `available_cash` decimal(15,2) DEFAULT NULL,
  `opening_balance` decimal(15,2) DEFAULT NULL,
  `live_balance` decimal(15,2) DEFAULT NULL,
  `collateral` decimal(15,2) DEFAULT NULL,
  `utilised_margin` decimal(15,2) DEFAULT NULL,
  `span_margin` decimal(15,2) DEFAULT NULL,
  `exposure_margin` decimal(15,2) DEFAULT NULL,
  `option_premium` decimal(15,2) DEFAULT NULL,
  `m2m_realised` decimal(15,2) DEFAULT NULL,
  `m2m_unrealised` decimal(15,2) DEFAULT NULL,
  `available_margin` decimal(15,2) DEFAULT NULL,
  `polled_at` datetime NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_client_day` (`client_id`,`trading_day`),
  KEY `idx_trading_day` (`trading_day`),
  KEY `idx_client_polled_at` (`client_id`,`polled_at`)
) ENGINE=InnoDB AUTO_INCREMENT=4 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `oms_funds_history`
--

DROP TABLE IF EXISTS `oms_funds_history`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `oms_funds_history` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `client_id` varchar(50) NOT NULL,
  `trading_day` date NOT NULL,
  `snapshot_json` json NOT NULL,
  `polled_at` datetime NOT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_client_day_time` (`client_id`,`trading_day`,`polled_at`),
  KEY `idx_trading_day_time` (`trading_day`,`polled_at`)
) ENGINE=InnoDB AUTO_INCREMENT=372 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `oms_orders`
--

DROP TABLE IF EXISTS `oms_orders`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `oms_orders` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `trading_day` date NOT NULL,
  `client_id` varchar(50) NOT NULL,
  `order_id` varchar(40) NOT NULL,
  `exchange_order_id` varchar(40) DEFAULT NULL,
  `tradingsymbol` varchar(50) DEFAULT NULL,
  `instrument` varchar(30) DEFAULT NULL,
  `instrument_token` bigint DEFAULT NULL,
  `exchange` varchar(10) DEFAULT NULL,
  `transaction_type` varchar(5) DEFAULT NULL,
  `product` varchar(10) DEFAULT NULL,
  `order_type` varchar(10) DEFAULT NULL,
  `variety` varchar(10) DEFAULT NULL,
  `validity` varchar(10) DEFAULT NULL,
  `validity_ttl` int DEFAULT NULL,
  `quantity` int DEFAULT NULL,
  `disclosed_quantity` int DEFAULT NULL,
  `filled_quantity` int DEFAULT NULL,
  `pending_quantity` int DEFAULT NULL,
  `cancelled_quantity` int DEFAULT NULL,
  `price` decimal(14,4) DEFAULT NULL,
  `average_price` decimal(14,4) DEFAULT NULL,
  `trigger_price` decimal(14,4) DEFAULT NULL,
  `status` varchar(30) DEFAULT NULL,
  `order_timestamp` datetime DEFAULT NULL,
  `exchange_timestamp` datetime DEFAULT NULL,
  `tag` varchar(50) DEFAULT NULL,
  `order_issued_at` varchar(10) DEFAULT NULL,
  `order_placed_by` varchar(50) DEFAULT NULL,
  `recon_status` varchar(20) DEFAULT NULL,
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  `first_seen_at` datetime DEFAULT NULL,
  `polled_at` datetime NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_client_order` (`client_id`,`order_id`),
  KEY `idx_day_client_time` (`trading_day`,`client_id`,`order_timestamp`),
  KEY `idx_symbol` (`tradingsymbol`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB AUTO_INCREMENT=3 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `oms_orders_history`
--

DROP TABLE IF EXISTS `oms_orders_history`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `oms_orders_history` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `client_id` varchar(50) NOT NULL,
  `trading_day` date NOT NULL,
  `polled_at` datetime NOT NULL,
  `broker_payload` json NOT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_client_day_time` (`client_id`,`trading_day`,`polled_at`),
  KEY `idx_polled_at` (`polled_at`)
) ENGINE=InnoDB AUTO_INCREMENT=372 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `oms_positions`
--

DROP TABLE IF EXISTS `oms_positions`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `oms_positions` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `trading_day` date NOT NULL,
  `client_id` varchar(50) NOT NULL,
  `tradingsymbol` varchar(50) NOT NULL,
  `instrument` varchar(20) DEFAULT NULL,
  `instrument_token` bigint DEFAULT NULL,
  `exchange` varchar(10) DEFAULT NULL,
  `segment` varchar(10) DEFAULT NULL,
  `product` varchar(10) DEFAULT NULL,
  `quantity` int DEFAULT NULL,
  `overnight_quantity` int DEFAULT NULL,
  `multiplier` decimal(10,4) DEFAULT NULL,
  `average_price` decimal(14,4) DEFAULT NULL,
  `close_price` decimal(14,4) DEFAULT NULL,
  `last_price` decimal(14,4) DEFAULT NULL,
  `value` decimal(14,4) DEFAULT NULL,
  `pnl` decimal(14,4) DEFAULT NULL,
  `m2m` decimal(14,4) DEFAULT NULL,
  `unrealised` decimal(14,4) DEFAULT NULL,
  `realised` decimal(14,4) DEFAULT NULL,
  `buy_quantity` int DEFAULT NULL,
  `buy_price` decimal(14,4) DEFAULT NULL,
  `buy_value` decimal(14,4) DEFAULT NULL,
  `buy_m2m` decimal(14,4) DEFAULT NULL,
  `sell_quantity` int DEFAULT NULL,
  `sell_price` decimal(14,4) DEFAULT NULL,
  `sell_value` decimal(14,4) DEFAULT NULL,
  `sell_m2m` decimal(14,4) DEFAULT NULL,
  `day_buy_quantity` int DEFAULT NULL,
  `day_buy_price` decimal(14,4) DEFAULT NULL,
  `day_buy_value` decimal(14,4) DEFAULT NULL,
  `day_sell_quantity` int DEFAULT NULL,
  `day_sell_price` decimal(14,4) DEFAULT NULL,
  `day_sell_value` decimal(14,4) DEFAULT NULL,
  `polled_at` datetime NOT NULL,
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_position` (`client_id`,`trading_day`,`tradingsymbol`,`product`),
  KEY `idx_position_latest` (`client_id`,`tradingsymbol`,`product`,`polled_at`),
  KEY `idx_symbol` (`tradingsymbol`)
) ENGINE=InnoDB AUTO_INCREMENT=573 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `oms_positions_history`
--

DROP TABLE IF EXISTS `oms_positions_history`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `oms_positions_history` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `client_id` varchar(50) NOT NULL,
  `trading_day` date NOT NULL,
  `polled_at` datetime NOT NULL,
  `broker_payload` json NOT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_client_day_time` (`client_id`,`trading_day`,`polled_at`),
  KEY `idx_polled_at` (`polled_at`)
) ENGINE=InnoDB AUTO_INCREMENT=372 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `signals`
--

DROP TABLE IF EXISTS `signals`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `signals` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `signal_id` char(36) NOT NULL,
  `equity_ref` varchar(32) NOT NULL,
  `symbol` varchar(32) NOT NULL,
  `lifecycle` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci NOT NULL,
  `setup` varchar(64) NOT NULL,
  `side` varchar(8) NOT NULL,
  `stage` varchar(32) NOT NULL DEFAULT 'TRACKING',
  `status` varchar(16) NOT NULL DEFAULT 'OPEN',
  `status_reason` varchar(255) DEFAULT NULL,
  `first_seen_time` datetime(6) DEFAULT NULL,
  `created_price` decimal(16,6) DEFAULT NULL,
  `last_eval_time` datetime(6) NOT NULL,
  `last_snapshot_time` datetime(6) NOT NULL,
  `stage_changed_time` datetime(6) DEFAULT NULL,
  `status_changed_time` datetime(6) DEFAULT NULL,
  `qualified_time` datetime(6) DEFAULT NULL,
  `actionable_time` datetime(6) DEFAULT NULL,
  `closed_time` datetime(6) DEFAULT NULL,
  `closed_price` decimal(16,6) DEFAULT NULL,
  `last_price` decimal(16,6) DEFAULT NULL,
  `ltp` decimal(16,6) DEFAULT NULL,
  `ltp_time` datetime(6) DEFAULT NULL,
  `last_pnl` decimal(10,4) NOT NULL DEFAULT '0.0000',
  `last_pnl_value` decimal(13,2) NOT NULL DEFAULT '0.00',
  `max_price` decimal(13,2) NOT NULL DEFAULT '0.00',
  `min_price` decimal(13,2) NOT NULL DEFAULT '0.00',
  `max_time` datetime DEFAULT NULL,
  `min_time` datetime DEFAULT NULL,
  `max_pnl` decimal(10,4) NOT NULL DEFAULT '0.0000',
  `min_pnl` decimal(10,4) NOT NULL DEFAULT '0.0000',
  `max_pnl_value` decimal(13,2) NOT NULL DEFAULT '0.00',
  `min_pnl_value` decimal(13,2) NOT NULL DEFAULT '0.00',
  `criteria_json` json NOT NULL,
  `snapshot_json` json NOT NULL,
  `meta_json` json DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_signal_id` (`signal_id`),
  KEY `idx_active_lookup` (`equity_ref`,`lifecycle`,`status`,`last_eval_time`),
  KEY `idx_strategy_status_time` (`lifecycle`,`status`,`last_eval_time`),
  KEY `idx_equity_time` (`equity_ref`,`last_eval_time`),
  KEY `idx_ltp_time` (`ltp_time`),
  KEY `idx_status_stage_time` (`status`,`stage`,`last_eval_time`),
  KEY `idx_signals_status_eval_id` (`status`,`last_eval_time`,`id`),
  KEY `idx_signals_eval_id` (`last_eval_time`,`id`),
  KEY `idx_setup_status_time` (`setup`,`status`,`last_eval_time`)
) ENGINE=InnoDB AUTO_INCREMENT=16 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `signals_history`
--

DROP TABLE IF EXISTS `signals_history`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `signals_history` (
  `hist_id` bigint NOT NULL AUTO_INCREMENT,
  `id` bigint NOT NULL,
  `signal_id` varchar(36) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,
  `equity_ref` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,
  `symbol` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,
  `lifecycle` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,
  `setup` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,
  `side` varchar(8) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,
  `stage` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,
  `status` varchar(16) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,
  `status_reason` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `first_seen_time` datetime DEFAULT NULL,
  `created_price` decimal(16,6) DEFAULT NULL,
  `last_eval_time` datetime NOT NULL,
  `last_snapshot_time` datetime NOT NULL,
  `stage_changed_time` datetime DEFAULT NULL,
  `status_changed_time` datetime DEFAULT NULL,
  `qualified_time` datetime DEFAULT NULL,
  `actionable_time` datetime DEFAULT NULL,
  `closed_time` datetime DEFAULT NULL,
  `closed_price` decimal(16,6) DEFAULT NULL,
  `last_price` decimal(16,6) DEFAULT NULL,
  `ltp` decimal(16,6) DEFAULT NULL,
  `ltp_time` datetime DEFAULT NULL,
  `last_pnl` decimal(10,4) NOT NULL DEFAULT '0.0000',
  `last_pnl_value` decimal(13,2) NOT NULL DEFAULT '0.00',
  `max_price` decimal(13,2) NOT NULL DEFAULT '0.00',
  `min_price` decimal(13,2) NOT NULL DEFAULT '0.00',
  `max_time` datetime DEFAULT NULL,
  `min_time` datetime DEFAULT NULL,
  `max_pnl` decimal(10,4) NOT NULL DEFAULT '0.0000',
  `min_pnl` decimal(10,4) NOT NULL DEFAULT '0.0000',
  `max_pnl_value` decimal(13,2) NOT NULL DEFAULT '0.00',
  `min_pnl_value` decimal(13,2) NOT NULL DEFAULT '0.00',
  `criteria_json` json NOT NULL,
  `snapshot_json` json NOT NULL,
  `meta_json` json DEFAULT NULL,
  `archived_on` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `trading_date` date GENERATED ALWAYS AS (cast(`last_eval_time` as date)) STORED NOT NULL,
  PRIMARY KEY (`hist_id`),
  UNIQUE KEY `uq_signal_liveid_by_day` (`id`,`trading_date`),
  KEY `idx_signalhist_signalid_day` (`signal_id`,`trading_date`),
  KEY `idx_signalhist_equity_time` (`equity_ref`,`last_eval_time`),
  KEY `idx_signalhist_strategy_status_time` (`lifecycle`,`status`,`last_eval_time`),
  KEY `idx_signalhist_symbol_time` (`symbol`,`last_eval_time`),
  KEY `idx_signalhist_setup_status_time` (`setup`,`status`,`last_eval_time`)
) ENGINE=InnoDB AUTO_INCREMENT=17 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `snapshots`
--

DROP TABLE IF EXISTS `snapshots`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `snapshots` (
  `symbol` varchar(50) DEFAULT NULL,
  `snapshot_time` datetime NOT NULL,
  `ltp` decimal(13,2) DEFAULT NULL,
  `ltp_time` datetime DEFAULT NULL,
  `data` json DEFAULT NULL,
  `processed` tinyint(1) DEFAULT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `stock_opportunities`
--

DROP TABLE IF EXISTS `stock_opportunities`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `stock_opportunities` (
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
) ENGINE=InnoDB AUTO_INCREMENT=16 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `symbols`
--

DROP TABLE IF EXISTS `symbols`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `symbols` (
  `id` int NOT NULL AUTO_INCREMENT,
  `symbol` varchar(50) DEFAULT NULL,
  `token` varchar(50) DEFAULT NULL,
  `name` varchar(50) DEFAULT NULL,
  `type` varchar(10) DEFAULT NULL,
  `price` decimal(13,2) DEFAULT NULL,
  `exchange` varchar(20) DEFAULT NULL,
  `segment` varchar(20) DEFAULT NULL,
  `signal_profile` varchar(1000) NOT NULL DEFAULT 'DEFAULT',
  `lotsize` int NOT NULL,
  `expiry` date DEFAULT NULL,
  `strike_price` decimal(13,2) DEFAULT NULL,
  `tick_size` decimal(13,2) DEFAULT NULL,
  `equity_ref` varchar(50) DEFAULT NULL,
  `last_time` datetime DEFAULT NULL,
  `last_snapshot` json DEFAULT NULL,
  `generate_candles` tinyint NOT NULL,
  `merge_candles` tinyint NOT NULL,
  `update_performance` tinyint NOT NULL,
  `generate_signals` tinyint NOT NULL,
  `processed` tinyint NOT NULL,
  `promoted_when` datetime(6) DEFAULT NULL,
  `demoted_when` datetime(6) DEFAULT NULL,
  `active` tinyint NOT NULL,
  `enabled` tinyint(1) DEFAULT NULL,
  UNIQUE KEY `id` (`id`),
  KEY `symbols_equity_ref_IDX` (`equity_ref`)
) ENGINE=InnoDB AUTO_INCREMENT=28825 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `user_trades`
--

DROP TABLE IF EXISTS `user_trades`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `user_trades` (
  `id` int NOT NULL AUTO_INCREMENT,
  `userid` varchar(50) DEFAULT NULL,
  `signal_id` varchar(100) NOT NULL,
  `symbol` varchar(255) DEFAULT NULL,
  `equity_ref` varchar(50) DEFAULT NULL,
  `instrument_type` varchar(20) DEFAULT NULL,
  `trade_type` varchar(10) DEFAULT NULL,
  `position_style` varchar(20) DEFAULT NULL,
  `hedged_symbol` varchar(255) DEFAULT NULL,
  `source` varchar(50) DEFAULT NULL,
  `message` text,
  `entry_snapshot` json NOT NULL,
  `last_snapshot` json DEFAULT NULL,
  `entry_status` varchar(30) DEFAULT NULL,
  `exit_status` varchar(30) DEFAULT NULL,
  `execution_mode` varchar(10) DEFAULT NULL,
  `intraday_only` tinyint(1) DEFAULT NULL,
  `entry_time` datetime NOT NULL,
  `entry_intent_time` datetime DEFAULT NULL,
  `entry_exec_time` datetime DEFAULT NULL,
  `entry_reconciled_at` datetime DEFAULT NULL,
  `exec_last_checked_at` datetime DEFAULT NULL,
  `exec_status` varchar(50) DEFAULT NULL,
  `exec_status_message` varchar(255) DEFAULT NULL,
  `entry_price` decimal(13,2) DEFAULT NULL,
  `executed_entry_price` decimal(13,2) DEFAULT NULL,
  `executed_entry_qty` int DEFAULT NULL,
  `quantity` int NOT NULL,
  `entry_order_id` varchar(255) DEFAULT NULL,
  `entry_order_response_json` text,
  `entry_retries` int NOT NULL,
  `trade_management` json DEFAULT NULL,
  `exit_reason` varchar(50) DEFAULT NULL,
  `exit_rule` varchar(100) DEFAULT NULL,
  `exit_time` datetime DEFAULT NULL,
  `exit_intent_time` datetime DEFAULT NULL,
  `exit_exec_time` datetime DEFAULT NULL,
  `exit_reconciled_at` datetime DEFAULT NULL,
  `reconcile_last_checked_at` datetime DEFAULT NULL,
  `reconcile_status` varchar(50) DEFAULT NULL,
  `reconcile_status_message` varchar(255) DEFAULT NULL,
  `exit_price` decimal(13,2) DEFAULT NULL,
  `executed_exit_price` decimal(13,2) DEFAULT NULL,
  `executed_exit_qty` int NOT NULL,
  `exit_order_id` varchar(255) DEFAULT NULL,
  `exit_order_response_json` text,
  `exit_retries` int NOT NULL,
  `exit_pnl` decimal(13,2) DEFAULT NULL,
  `last_time` datetime NOT NULL,
  `last_price` decimal(13,2) DEFAULT NULL,
  `last_pnl` decimal(10,4) DEFAULT NULL,
  `last_pnl_value` decimal(13,2) DEFAULT NULL,
  `max_price` decimal(13,2) DEFAULT NULL,
  `min_price` decimal(13,2) DEFAULT NULL,
  `max_time` datetime NOT NULL,
  `min_time` datetime NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_user_opp_symbol` (`userid`,`signal_id`,`symbol`),
  UNIQUE KEY `uq_user_trade_user_signal_instrument` (`userid`,`signal_id`,`instrument_type`),
  KEY `idx_entry_pickup` (`entry_status`,`execution_mode`),
  KEY `idx_entry_status` (`entry_status`),
  KEY `idx_equity_ref` (`equity_ref`),
  KEY `idx_exit_pickup` (`exit_status`,`execution_mode`),
  KEY `idx_exit_status` (`exit_status`),
  KEY `idx_userid` (`userid`)
) ENGINE=InnoDB AUTO_INCREMENT=85 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `user_trades_history`
--

DROP TABLE IF EXISTS `user_trades_history`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `user_trades_history` (
  `hist_id` bigint NOT NULL,
  `id` int NOT NULL,
  `userid` varchar(50) DEFAULT NULL,
  `signal_id` varchar(100) NOT NULL,
  `source` varchar(50) DEFAULT NULL,
  `message` text,
  `entry_snapshot` json NOT NULL,
  `last_snapshot` json DEFAULT NULL,
  `symbol` varchar(255) DEFAULT NULL,
  `equity_ref` varchar(50) DEFAULT NULL,
  `instrument_type` varchar(20) DEFAULT NULL,
  `trade_type` varchar(10) DEFAULT NULL,
  `position_style` varchar(20) DEFAULT NULL,
  `hedged_symbol` varchar(255) DEFAULT NULL,
  `entry_status` varchar(30) DEFAULT NULL,
  `exit_status` varchar(30) DEFAULT NULL,
  `execution_mode` varchar(10) DEFAULT NULL,
  `intraday_only` tinyint(1) DEFAULT NULL,
  `entry_time` datetime NOT NULL,
  `entry_intent_time` datetime DEFAULT NULL,
  `entry_exec_time` datetime DEFAULT NULL,
  `entry_reconciled_at` datetime DEFAULT NULL,
  `exec_last_checked_at` datetime DEFAULT NULL,
  `exec_status` varchar(50) DEFAULT NULL,
  `exec_status_message` varchar(255) DEFAULT NULL,
  `entry_price` decimal(13,2) DEFAULT NULL,
  `executed_entry_price` decimal(13,2) DEFAULT NULL,
  `executed_entry_qty` int DEFAULT NULL,
  `quantity` int NOT NULL,
  `entry_order_id` varchar(255) DEFAULT NULL,
  `entry_order_response_json` text,
  `entry_retries` int NOT NULL,
  `trade_management` json DEFAULT NULL,
  `exit_reason` varchar(50) DEFAULT NULL,
  `exit_rule` varchar(100) DEFAULT NULL,
  `exit_time` datetime DEFAULT NULL,
  `exit_intent_time` datetime DEFAULT NULL,
  `exit_exec_time` datetime DEFAULT NULL,
  `exit_reconciled_at` datetime DEFAULT NULL,
  `reconcile_last_checked_at` datetime DEFAULT NULL,
  `reconcile_status` varchar(50) DEFAULT NULL,
  `reconcile_status_message` varchar(255) DEFAULT NULL,
  `exit_price` decimal(13,2) DEFAULT NULL,
  `executed_exit_price` decimal(13,2) DEFAULT NULL,
  `executed_exit_qty` int NOT NULL,
  `exit_order_id` varchar(255) DEFAULT NULL,
  `exit_order_response_json` text,
  `exit_retries` int NOT NULL,
  `exit_pnl` decimal(13,2) DEFAULT NULL,
  `last_time` datetime NOT NULL,
  `last_price` decimal(13,2) DEFAULT NULL,
  `last_pnl` decimal(10,4) DEFAULT NULL,
  `last_pnl_value` decimal(13,2) DEFAULT NULL,
  `max_price` decimal(13,2) DEFAULT NULL,
  `min_price` decimal(13,2) DEFAULT NULL,
  `max_time` datetime NOT NULL,
  `min_time` datetime NOT NULL,
  `archived_on` datetime NOT NULL,
  `trading_date` date NOT NULL,
  UNIQUE KEY `uq_usertrade_liveid_by_day` (`id`,`trading_date`),
  KEY `idx_uth_exit_time` (`exit_time`),
  KEY `idx_uth_last_time` (`last_time`),
  KEY `idx_uth_symbol_entry` (`symbol`,`entry_time`),
  KEY `idx_uth_userid_day` (`userid`,`trading_date`),
  KEY `idx_uth_signal_day` (`signal_id`,`trading_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `users`
--

DROP TABLE IF EXISTS `users`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `users` (
  `id` int NOT NULL,
  `userid` varchar(50) DEFAULT NULL,
  `name` varchar(30) DEFAULT NULL,
  `email` varchar(50) DEFAULT NULL,
  `mobile` varchar(10) DEFAULT NULL,
  `password` varchar(50) DEFAULT NULL,
  `broker_login` tinyint(1) DEFAULT NULL,
  `broker_name` varchar(30) DEFAULT NULL,
  `apikey` varchar(255) DEFAULT NULL,
  `secretkey` varchar(255) DEFAULT NULL,
  `access_token` varchar(50) DEFAULT NULL,
  `intraday_only` tinyint(1) DEFAULT NULL,
  `stocks` varchar(255) DEFAULT NULL,
  `equity` tinyint(1) DEFAULT NULL,
  `futures` tinyint(1) DEFAULT NULL,
  `options` tinyint(1) DEFAULT NULL,
  `execution_mode` varchar(8) DEFAULT NULL,
  `autotrade` tinyint(1) DEFAULT NULL,
  `active` tinyint(1) DEFAULT NULL,
  `logged_in` tinyint(1) DEFAULT NULL,
  `logged_time` datetime DEFAULT NULL,
  UNIQUE KEY `uq_users_mobile` (`mobile`),
  UNIQUE KEY `username` (`userid`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40103 SET TIME_ZONE=@OLD_TIME_ZONE */;

/*!40101 SET SQL_MODE=@OLD_SQL_MODE */;
/*!40014 SET FOREIGN_KEY_CHECKS=@OLD_FOREIGN_KEY_CHECKS */;
/*!40014 SET UNIQUE_CHECKS=@OLD_UNIQUE_CHECKS */;
/*!40101 SET CHARACTER_SET_CLIENT=@OLD_CHARACTER_SET_CLIENT */;
/*!40101 SET CHARACTER_SET_RESULTS=@OLD_CHARACTER_SET_RESULTS */;
/*!40101 SET COLLATION_CONNECTION=@OLD_COLLATION_CONNECTION */;
/*!40111 SET SQL_NOTES=@OLD_SQL_NOTES */;

-- Dump completed on 2026-08-01 11:19:30
