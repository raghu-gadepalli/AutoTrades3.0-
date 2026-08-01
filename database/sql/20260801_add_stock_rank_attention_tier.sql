-- Run once only when stock_rank already exists from the 31 July schema.
ALTER TABLE `stock_rank`
  ADD COLUMN `attention_tier` varchar(16) NOT NULL DEFAULT 'SUPPRESSED' AFTER `classification`,
  ADD KEY `idx_stock_rank_day_tier` (`trading_day`,`attention_tier`);
