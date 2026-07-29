import os, sys
import logging
from datetime import datetime
from kiteconnect.exceptions import TokenException, InputException  # catch both

# allow imports from project root
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
#  Shared logging setup
from logconfig import setup_logging
setup_logging()
logger = logging.getLogger(__name__)

from services.zerodha.kiteconnect_service import KiteConnectService
logging.getLogger("services.zerodha.kiteconnect_service").setLevel(logging.ERROR)

# Edit these for your test:
API_KEY      = "bv185n0541aaoish"
ACCESS_TOKEN = "bCax17Q0hqQj0An36xeGGMDHngsSEqV6"
INSTR_TOKEN  = "779521"  # SBIN Future Token
START_ISO    = "2025-04-07T09:15:00+05:30"
END_ISO      = "2025-04-07T09:30:00+05:30"
#

def main():
    # Parse the ISO timestamps (with IST offset)
    start_dt = datetime.fromisoformat(START_ISO)
    end_dt   = datetime.fromisoformat(END_ISO)

    kite = KiteConnectService(api_key=API_KEY, access_token=ACCESS_TOKEN)

    try:
        bars = kite.fetch_historical_data(
            instrument_token=INSTR_TOKEN,
            from_date=start_dt,
            to_date=end_dt,
            interval="minute",
            oi=False,
        )
    except TokenException as e:
        # Invalid API Key  abort entire script
        logger.warning(f" Invalid API Key or access token: {e}")
        sys.exit(1)
    except InputException:
        # Invalid symbol token  treat as no data
        bars = []

    if not bars:
        logger.info("  No data returned (invalid symbol or no data in that range).")
    else:
        logger.info(f" Retrieved {len(bars)} bars:")
        for bar in bars:
            logger.info(bar)

if __name__ == "__main__":
    main()
