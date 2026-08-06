CREATE TABLE IF NOT EXISTS stockmaps (
    symbol VARCHAR(50) NOT NULL,
    stockmap_time DATETIME NOT NULL,
    data JSON NOT NULL,
    PRIMARY KEY (symbol, stockmap_time),
    INDEX idx_stockmaps_time_symbol (stockmap_time, symbol)
);
