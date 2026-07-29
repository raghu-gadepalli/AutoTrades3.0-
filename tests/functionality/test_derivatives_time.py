import json, sys, os
from datetime import datetime
from zoneinfo import ZoneInfo


PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from schemas.derivatives import DerivativesChainSchema
from services.derivatives.derivatives_helper import build_derived_from_day
from configs.derivatives_config import DERIVATIVES_CONFIG

IST = ZoneInfo("Asia/Kolkata")


def _parse_minute(s: str) -> datetime:
    """
    Accepts: "2026-02-15 10:30" or "2026-02-15 10:30:00"
    Returns naive datetime (as your DB stores snapshot_time).
    """
    s = s.strip()
    if len(s) == 16:
        s = s + ":00"
    dt = datetime.fromisoformat(s)  # naive
    return dt.replace(second=0, microsecond=0)


def main():
    symbol = "SBIN"  # equity_ref
    asof = _parse_minute("2026-02-13 10:30")

    cfg = DERIVATIVES_CONFIG.derived
    os_cfg = cfg.option_sentiment

    windows_cfg = os_cfg.windows
    ladder_window = cfg.option_ladder.window
    top_n = cfg.options_lite.top_n

    opt_sent_atm_window = os_cfg.atm_window
    opt_sent_notional_floor = os_cfg.notional_floor
    opt_sent_min_contracts_floor = os_cfg.min_contracts_floor

    history_rows = os_cfg.history_rows

    # 1) fetch today rows <= asof
    day_rows = DerivativesChainSchema.fetch_recent_today_for_symbol_before_time(
        symbol=symbol,
        t=asof,
        limit=history_rows,
        ascending=True,
    )

    if not day_rows:
        print(f"No rows found for {symbol} <= {asof}")
        return

    # 2) choose the exact row at asof (or closest <= asof)
    now_row = None
    for r in reversed(day_rows):
        if r.snapshot_time <= asof:
            now_row = r
            break
    now_row = now_row or day_rows[-1]

    raw_now = now_row.raw if isinstance(now_row.raw, dict) else {}

    # 3) build samples: all day rows + current raw (same ts)
    samples = [{"snapshot_time": r.snapshot_time, "raw": r.raw} for r in day_rows if isinstance(r.raw, dict)]
    samples.append({"snapshot_time": asof, "raw": raw_now})

    derived = build_derived_from_day(
        samples=samples,
        asof=asof,
        windows=windows_cfg,
        ladder_window=ladder_window,
        opt_sent_atm_window=opt_sent_atm_window,
        opt_sent_notional_floor=opt_sent_notional_floor,
        opt_sent_min_contracts_floor=opt_sent_min_contracts_floor,
        top_n=top_n,
    )

    print("\n=== DERIVED @", symbol, asof, "===")

    # Print concise summary first
    ol = (derived or {}).get("options_lite") or {}
    print("options_lite:", {
        "atm_strike": ol.get("atm_strike"),
        "pcr": ol.get("pcr"),
        "support": ol.get("support"),
        "resistance": ol.get("resistance"),
        "top_calls": len(ol.get("top_calls") or []),
        "top_puts": len(ol.get("top_puts") or []),
    })

    osw = (derived or {}).get("option_sentiment_windows") or {}
    print("option_sentiment_windows keys:", list(osw.keys()))
    for k, v in osw.items():
        print("  ", k, v.get("status"), v.get("indication"), v.get("strength"), v.get("reason"))

    fsw = (derived or {}).get("future_sentiment_windows") or {}
    print("future_sentiment_windows keys:", list(fsw.keys()))
    for k, v in fsw.items():
        print("  ", k, v.get("status"), v.get("label"), v.get("fut_ltp_delta"), v.get("fut_oi_delta"))

    # Dump full JSON (optional)
    print("\n--- FULL DERIVED JSON ---")
    print(json.dumps(derived, default=str, indent=2))


if __name__ == "__main__":
    main()
