#!/usr/bin/env python3
import os
import sys
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

# allow imports from project root
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from configs.execution_config import EXECUTION_CONFIG

from logconfig import setup_logging
# pick up the execution services log file
conf = getattr(EXECUTION_CONFIG, "service", EXECUTION_CONFIG)
setup_logging(log_file=getattr(conf, "log_file", "trade_executor.log"))
logger = logging.getLogger(__name__)

# quiet down internal libraries to WARNING and above
logging.getLogger("services.zerodha.kiteconnect_service").setLevel(logging.WARNING)
logging.getLogger("services.executor.trade_executor").setLevel(logging.WARNING)

from services.trade.executor.trade_executor import TradeExecutor

def main():
    start_ts = datetime.now(ZoneInfo("Asia/Kolkata"))
    logger.info("Starting TradeExecutor at %s", start_ts.isoformat())

    executor = TradeExecutor()
    executor.execute_all()

    end_ts = datetime.now(ZoneInfo("Asia/Kolkata"))
    duration = end_ts - start_ts
    logger.info(
        "Completed trade execution at %s (duration: %s)",
        end_ts.isoformat(),
        duration,
    )

if __name__ == "__main__":
    main()
