#!/usr/bin/env python3
import logging
import os
import sys
from datetime import date

# allow imports from project root
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
#  Shared logging setup 
from logconfig import setup_logging
setup_logging()
# get a modulescoped logger
logger = logging.getLogger(__name__)

from config import AppConfig    
from database.database import get_trades_db
from models.trade_models import Instrument
from schemas.user import UserSchema
from services.zerodha.kiteconnect_service import KiteConnectService

def convert(row: dict) -> Instrument:
    return Instrument(
        instrument_token=int(row["instrument_token"]),
        exchange_token=row.get("exchange_token"),
        tradingsymbol=row["tradingsymbol"],
        name=row.get("name"),
        last_price=row.get("last_price"),
        expiry=row.get("expiry") if isinstance(row.get("expiry"), date) else None,
        strike=row.get("strike"),
        tick_size=row.get("tick_size"),
        lot_size=row.get("lot_size"),
        instrument_type=row.get("instrument_type"),
        segment=row.get("segment"),
        exchange=row.get("exchange"),
    )


def main():
    # 1) Load your AppConfig.DATA_USER credentials
    user = UserSchema.fetch_user(AppConfig.DATA_USER)
    if not user:
        logger.error("User '%s' not found. Aborting.", AppConfig.DATA_USER)
        return

    # 2) Initialize KiteConnect
    kite = KiteConnectService(api_key=user.apikey, access_token=user.access_token)

    # 3) Fetch NSE instruments
    try:
        logger.info("Fetching NSE instruments")
        nse_rows = kite.kite.instruments("NSE")
        logger.info("  Retrieved %d NSE rows", len(nse_rows))
    except Exception as e:
        logger.exception("Failed to fetch NSE instruments: %s", e)
        nse_rows = []

    # 4) Fetch NFO instruments
    try:
        logger.info("Fetching NFO instruments")
        nfo_rows = kite.kite.instruments("NFO")
        logger.info("  Retrieved %d NFO rows", len(nfo_rows))
    except Exception as e:
        logger.exception("Failed to fetch NFO instruments: %s", e)
        nfo_rows = []

    all_rows = nse_rows + nfo_rows
    logger.info("Total instruments to insert: %d", len(all_rows))

    # 5) Truncate & bulkinsert
    with get_trades_db() as db:
        logger.info("Clearing existing instruments")
        db.query(Instrument).delete()
        logger.info("Bulkinserting new instruments")
        db.bulk_save_objects([convert(r) for r in all_rows])
        db.commit()
        logger.info(" Instruments table refreshed (%d rows)", len(all_rows))


if __name__ == "__main__":
    main()