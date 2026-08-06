-- AutoTrades 3.0 StockMap V2 rebuild.
--
-- StockMap is still diagnostic research state and will be regenerated after
-- this migration. This script intentionally removes all existing V1 rows.
-- Stop StockMap replay/live processes before running it.

USE autotrades;

DROP TABLE IF EXISTS stockmaps;

CREATE TABLE stockmaps (
    symbol VARCHAR(50) NOT NULL,
    stockmap_time DATETIME NOT NULL,
    data JSON NOT NULL,
    PRIMARY KEY (symbol, stockmap_time),
    INDEX idx_stockmaps_time_symbol (stockmap_time, symbol)
);
