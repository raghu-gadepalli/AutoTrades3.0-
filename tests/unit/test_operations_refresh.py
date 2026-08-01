from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from operations.refresh_broker_instruments import (
    convert_broker_row,
    validate_broker_download,
)
from operations.refresh_derivative_symbols import (
    _annotate_report_rebuild,
    _build_parser,
    build_refresh_plan,
    select_published_expiries,
)


def _broker_row(**overrides):
    row = {
        "instrument_token": 1,
        "exchange_token": 11,
        "tradingsymbol": "LT",
        "name": "LT",
        "last_price": 0.0,
        "expiry": None,
        "strike": 0.0,
        "tick_size": 0.05,
        "lot_size": 1,
        "instrument_type": "EQ",
        "segment": "NSE",
        "exchange": "NSE",
    }
    row.update(overrides)
    return row


def test_broker_download_requires_nonempty_matching_exchange() -> None:
    assert validate_broker_download("NSE", []).valid is False

    validation = validate_broker_download(
        "NSE",
        [_broker_row(exchange="NFO", segment="NFO-FUT")],
    )
    assert validation.valid is False
    assert any("unexpected_exchange" in issue for issue in validation.issues)


def test_broker_row_conversion_preserves_authoritative_fields() -> None:
    expiry = date(2026, 8, 25)
    converted = convert_broker_row(
        _broker_row(
            instrument_token=123,
            exchange_token=456,
            tradingsymbol="LT26AUGFUT",
            name="LT",
            expiry=expiry,
            instrument_type="FUT",
            segment="NFO-FUT",
            exchange="NFO",
            lot_size=150,
        )
    )

    assert converted.instrument_token == "123"
    assert converted.exchange_token == "456"
    assert converted.tradingsymbol == "LT26AUGFUT"
    assert converted.expiry == expiry
    assert converted.lot_size == 150


def _inst(
    symbol: str,
    *,
    name: str,
    instrument_type: str,
    segment: str,
    exchange: str,
    token: int,
    expiry: date | None = None,
    strike: float = 0.0,
    lot_size: int = 1,
):
    return SimpleNamespace(
        id=token,
        instrument_token=str(token),
        exchange_token=str(token + 1000),
        tradingsymbol=symbol,
        name=name,
        last_price=0.0,
        expiry=expiry,
        strike=strike,
        tick_size=0.05,
        lot_size=lot_size,
        instrument_type=instrument_type,
        segment=segment,
        exchange=exchange,
    )


def _three_month_master():
    expiries = [date(2026, 8, 25), date(2026, 9, 29), date(2026, 10, 27)]
    rows = [
        _inst(
            "LT",
            name="LT",
            instrument_type="EQ",
            segment="NSE",
            exchange="NSE",
            token=1,
        )
    ]
    token = 10
    for expiry in expiries:
        rows.extend(
            [
                _inst(
                    f"LT{expiry:%y%b}FUT".upper(),
                    name="LT",
                    instrument_type="FUT",
                    segment="NFO-FUT",
                    exchange="NFO",
                    token=token,
                    expiry=expiry,
                    lot_size=150,
                ),
                _inst(
                    f"LT{expiry:%y%b}3000CE".upper(),
                    name="LT",
                    instrument_type="CE",
                    segment="NFO-OPT",
                    exchange="NFO",
                    token=token + 1,
                    expiry=expiry,
                    strike=3000.0,
                    lot_size=150,
                ),
                _inst(
                    f"LT{expiry:%y%b}3000PE".upper(),
                    name="LT",
                    instrument_type="PE",
                    segment="NFO-OPT",
                    exchange="NFO",
                    token=token + 2,
                    expiry=expiry,
                    strike=3000.0,
                    lot_size=150,
                ),
            ]
        )
        token += 10
    return rows, expiries


def test_derivative_plan_loads_front_near_far_and_rebuilds_eq_defaults() -> None:
    rows, expiries = _three_month_master()
    assert select_published_expiries(
        rows,
        front_expiry=expiries[0],
        expiry_count=3,
    ) == tuple(expiries)

    plan = build_refresh_plan(
        rows,
        front_expiry=expiries[0],
        expiry_count=3,
    )
    by_symbol = {record.symbol: record for record in plan.records}

    assert len([row for row in plan.records if row.kind == "FUT"]) == 3
    assert len([row for row in plan.records if row.kind == "CE"]) == 3
    assert len([row for row in plan.records if row.kind == "PE"]) == 3
    eq_payload = by_symbol["LT"].payload
    assert eq_payload["enabled"] is False
    assert eq_payload["active"] is False
    assert eq_payload["generate_candles"] is True
    assert eq_payload["merge_candles"] is True
    assert eq_payload["update_performance"] is True
    assert eq_payload["generate_signals"] is True

    future_payload = next(
        record.payload for record in plan.records if record.kind == "FUT"
    )
    assert future_payload["enabled"] is True
    assert future_payload["active"] is True
    assert future_payload["generate_candles"] is False
    assert future_payload["merge_candles"] is False
    assert future_payload["update_performance"] is False
    assert future_payload["generate_signals"] is False
    assert plan.failed_underlyings == ()


def test_derivative_rebuild_report_counts_all_recreated_records() -> None:
    rows, expiries = _three_month_master()
    plan = build_refresh_plan(rows, front_expiry=expiries[0], expiry_count=3)

    report = _annotate_report_rebuild(plan, applied=True)

    assert sum(row.recreated_count for row in report) == len(plan.records)
    assert sum(row.planned_eq for row in report) == 1
    assert sum(row.planned_fut for row in report) == 3
    assert sum(row.planned_ce for row in report) == 3
    assert sum(row.planned_pe for row in report) == 3


def test_derivative_rebuild_cli_does_not_override_expiry_config() -> None:
    args = _build_parser().parse_args([])

    assert args.review_only is False
    assert not hasattr(args, "front_expiry")
    assert not hasattr(args, "expiry_count")
    assert not hasattr(args, "symbols")
