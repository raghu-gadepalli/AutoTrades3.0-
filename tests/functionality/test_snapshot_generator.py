#!/usr/bin/env python3
import os
import sys
import logging
import time
from datetime import datetime

# allow imports from project root
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

# Shared logging setup
from logconfig import setup_logging
setup_logging(log_file="snapshot.log")
logger = logging.getLogger(__name__)

from kiteconnect.exceptions import TokenException

from schemas.snapshot import SnapshotSchema
from services.snapshot.snapshot_generator import SnapshotGenerator

# Configuration
TOKEN        = 2955009
SYMBOL       = "COFORGE"
API_KEY      = "d17pao9dsc9jsp84"
ACCESS_TOKEN = "r3pcOIBTz6KveSwDdso8jLjo3M3pAiWF"

# TOKEN        = 408065
# SYMBOL       = "INFY"
# API_KEY      = "klknr4znst3h5ohp"
# ACCESS_TOKEN = "PrXwWYQTq4xaMEu19jENUTm0aZ7E2GsA"

def main():
    # parse a timestamp when the market was open; must include offset
    try:
        end_date = datetime.fromisoformat("2026-07-20T10:30:00+05:30")
    except ValueError as e:
        logger.error("Invalid date format: %s", e)
        sys.exit(1)

    gen = SnapshotGenerator(TOKEN, SYMBOL, API_KEY, ACCESS_TOKEN)

    try:
        t0 = time.perf_counter()
        snap2 = gen.generate_snapshot(
            end_date         = end_date,
            # persist_candle   = True,
            persist_snapshot = False,
        )
        elapsed = time.perf_counter() - t0
        logger.info("generate_snapshot total time: %.3f seconds", elapsed)
    except TokenException:
        logger.critical("Invalid API credentials during facade call")
        sys.exit(1)

    if not snap2:
        logger.warning("No snapshot returned. Check token/symbol or data range.")
        sys.exit(0)

    # Verify & log the derivatives payload
    if snap2.derivatives is None:
        logger.error("Snapshot.derivatives is None; no derivatives data was injected.")
    else:
        logger.info(
            "Derivatives block in snapshot:\n%s",
            snap2.derivatives
        )

    logger.info(
        "Snapshot created via facade:\n%s",
        snap2.model_dump_json(indent=2)
    )

if __name__ == "__main__":
    main()
