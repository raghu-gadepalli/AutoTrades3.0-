# routes/dash.py

import csv
import io
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Blueprint, Response, jsonify, render_template, request
from sqlalchemy import or_

from schemas.user_trade import UserTradeSchema
from utils.access import (
    get_current_userid,
    is_operator,
)
from utils.account_scope import (
    managed_users_for_actor,
    tradeable_users_for_actor,
    userids as scope_userids,
)
from database.database import get_trades_db
from models.trade_models import (
    AuditLog as AuditLogORM,
    Signal as SignalORM,
    UserTrade as UserTradeORM,
)
from routes.routes import login_required
from schemas.symbol import SymbolSchema
from schemas.signal import SignalSchema
from schemas.user import UserSchema
from schemas.derivatives import DerivativesChainSchema
from services.trade.generator.tradegen_helper import TradeGenHelper
from utils.datetime_utils import IST, business_now_naive
from utils.json_utils import sanitize_json
from configs.ui_config import UI_CONFIG

logger = logging.getLogger(__name__)
dash_bp = Blueprint("dash", __name__, url_prefix="/dashboard")



# =============================================================================
# COMMON HELPERS
# =============================================================================

def _fmt_ist(dt):
    if not dt:
        return ""
    try:
        dt_ist = dt.astimezone(IST) if getattr(dt, "tzinfo", None) else dt.replace(tzinfo=IST)
        return dt_ist.strftime("%d-%m-%Y %H:%M")
    except Exception:
        return ""


def _to_ist_naive(dt):
    if not dt:
        return None
    try:
        return dt.astimezone(IST).replace(tzinfo=None) if getattr(dt, "tzinfo", None) else dt
    except Exception:
        return None


def _safe_float(x, default=0.0):
    try:
        return float(x) if x is not None else default
    except Exception:
        return default


def _safe_int(x, default=0):
    try:
        return int(x) if x is not None else default
    except Exception:
        return default


def _safe_str(x, default=""):
    try:
        return str(x) if x is not None else default
    except Exception:
        return default


def _csv_number(value, decimals: int = 2):
    """Return an Excel-friendly plain numeric value for CSV output."""
    if value is None or value == "N/A":
        return ""
    try:
        return f"{float(value):.{decimals}f}"
    except Exception:
        return _safe_str(value, "")


def _csv_download(filename: str, headers: list[str], rows) -> Response:
    """Build a UTF-8 CSV download without temporary files."""
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(headers)
    writer.writerows(rows)

    # UTF-8 BOM keeps Excel from misreading symbols such as ₹.
    response = Response("\ufeff" + output.getvalue(), content_type="text/csv; charset=utf-8")
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


def _download_timestamp() -> str:
    return datetime.now(IST).strftime("%Y%m%d%H%M")


def _order_origin_label(value) -> str:
    origin = _safe_str(value, "UNKNOWN").strip().upper() or "UNKNOWN"
    labels = {
        "SIGNAL_AUTO": "Signal Auto",
        "SIGNAL_MANUAL": "Signal Manual",
        "WATCHLIST": "Watchlist",
        "POSITION_ADD": "Position Add",
        "UNKNOWN": "Unknown",
    }
    return labels.get(origin, origin.replace("_", " ").title())


def _order_status_label(entry_status, exit_status) -> str:
    es = _safe_str(entry_status, "").strip().upper()
    xs = _safe_str(exit_status, "NONE").strip().upper() or "NONE"

    if xs == "READY":
        return "EXIT READY"
    if xs == "SUBMITTED":
        return "EXIT SUBMITTED"
    if xs == "FILLED":
        return "CLOSED"

    labels = {
        "CREATED": "DRAFT",
        "READY": "CONFIRMED",
        "SUBMITTED": "SUBMITTED",
        "FILLED": "FILLED",
        "CANCELLED": "CANCELLED",
        "REJECTED": "REJECTED",
        "INVALID": "INVALID",
    }
    return labels.get(es, es or "—")


def _all_signal_ui_rows() -> list[dict]:
    """Load every signal in UI order without sorting wide JSON rows in MySQL."""
    batch_size = 250
    output: list[dict] = []

    with get_trades_db() as db:
        ordered_ids = [
            int(row_id)
            for (row_id,) in (
                db.query(SignalORM.id)
                .order_by(SignalORM.last_eval_time.desc(), SignalORM.id.desc())
                .all()
            )
        ]

        for start in range(0, len(ordered_ids), batch_size):
            batch_ids = ordered_ids[start:start + batch_size]
            fetched = db.query(SignalORM).filter(SignalORM.id.in_(batch_ids)).all()
            by_id = {int(row.id): row for row in fetched}

            for row_id in batch_ids:
                orm_row = by_id.get(row_id)
                if orm_row is None:
                    logger.error("signals_download missing ORM row | signal_db_id=%s", row_id)
                    continue
                try:
                    output.append(SignalSchema.model_validate(orm_row).to_ui_row())
                except Exception:
                    logger.exception(
                        "signals_download row conversion failed; continuing | signal_db_id=%s signal_id=%s symbol=%s",
                        row_id,
                        getattr(orm_row, "signal_id", None),
                        getattr(orm_row, "symbol", None),
                    )

    return output


def _status(tr) -> tuple[str, str]:
    es = _safe_str(getattr(tr, "entry_status", ""), "").upper()
    xs = _safe_str(getattr(tr, "exit_status", ""), "").upper()
    return es, xs


def _exec_status_compat(tr) -> str:
    entry_status, exit_status = _status(tr)
    if exit_status and exit_status != "NONE":
        return exit_status
    return entry_status


def _is_exited(tr) -> bool:
    _, exit_status = _status(tr)
    if exit_status == "FILLED":
        return True
    return bool(getattr(tr, "exit_time", None))


def _bool_or_none(v):
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    return s in ("1", "true", "yes", "y")


def _editable_draft(tr) -> bool:
    es, xs = _status(tr)
    return es == "CREATED" and xs == "NONE"


def _editable_live_trade(tr) -> bool:
    es, xs = _status(tr)

    qty = _safe_int(getattr(tr, "quantity", None), 0)
    executed_exit_qty = _safe_int(getattr(tr, "executed_exit_qty", None), 0)
    open_qty = max(qty - executed_exit_qty, 0)

    if es != "FILLED":
        return False
    if open_qty <= 0:
        return False
    if xs in ("READY", "SUBMITTED", "FILLED", "CANCELLED", "FAILED"):
        return False

    return True




def _trade_entry_display_price(tr) -> float:
    """Return displayed entry/average price.

    Use the actual executed entry price when available for both REAL and
    VIRTUAL trades. Fall back to planned entry_price for drafts/pending rows
    or older rows where execution price is not populated.
    """
    executed = _safe_float(getattr(tr, "executed_entry_price", None), 0.0)
    if executed > 0:
        return executed
    return _safe_float(getattr(tr, "entry_price", None), 0.0)


def _trade_entry_display_time(tr):
    """Return actual trade entry time for UI display.

    Prefer execution/intent timestamps over signal/actionable entry_time because
    entry_time can represent signal/actionable time in autogenerated flows.
    """
    return (
        getattr(tr, "entry_exec_time", None)
        or getattr(tr, "entry_intent_time", None)
        or getattr(tr, "entry_time", None)
    )

def _refresh_order_mark_fields(tr):
    """
    Refresh draft/queued order mark-to-market fields so UI values remain
    consistent across edit/submit flows.
    """
    try:
        entry_status, _ = _status(tr)
        planned_entry_price = _safe_float(
            getattr(tr, "entry_price", None),
            0.0,
        )
        executed_entry_price = _safe_float(
            getattr(tr, "executed_entry_price", None),
            0.0,
        )
        executed_entry_qty = _safe_int(
            getattr(tr, "executed_entry_qty", None),
            0,
        )
        side = _safe_str(getattr(tr, "trade_type", None), "").upper()

        last_price = _safe_float(
            getattr(tr, "last_price", None),
            planned_entry_price,
        )
        if last_price <= 0:
            last_price = planned_entry_price

        pnl_value = 0.0
        entry_is_filled = (
            entry_status == "FILLED"
            and executed_entry_price > 0
            and executed_entry_qty > 0
        )
        if entry_is_filled:
            if side == "SELL":
                pnl_value = round(
                    (executed_entry_price - last_price)
                    * executed_entry_qty,
                    2,
                )
            else:
                pnl_value = round(
                    (last_price - executed_entry_price)
                    * executed_entry_qty,
                    2,
                )

        if hasattr(tr, "last_price"):
            tr.last_price = last_price
        if hasattr(tr, "last_pnl_value"):
            tr.last_pnl_value = pnl_value
        if hasattr(tr, "last_pnl"):
            tr.last_pnl = pnl_value

    except Exception:
        logger.exception(
            "Failed to refresh order mark fields for trade_id=%s",
            getattr(tr, "id", None)
        )


def _prepare_draft_for_queue(tr, now=None):
    """
    Shared backend path for transitioning a draft row into queue-ready state.
    """
    now = now or business_now_naive()

    tr.entry_status = "READY"
    tr.entry_intent_time = now
    tr.exec_last_checked_at = now

    if hasattr(tr, "exec_status"):
        tr.exec_status = None
    if hasattr(tr, "exec_status_message"):
        tr.exec_status_message = None

    _refresh_order_mark_fields(tr)


def _normalize_userids(raw):
    out = []
    seen = set()

    if raw is None:
        return out

    if isinstance(raw, str):
        raw = [raw]

    if not isinstance(raw, (list, tuple)):
        return out

    for item in raw:
        uid = _safe_str(item, "").strip()
        if not uid:
            continue
        key = uid.upper()
        if key in seen:
            continue
        seen.add(key)
        out.append(uid)

    return out


def _prioritized_scope_userids(users, actor: str) -> list[str]:
    rows = scope_userids(users)
    return _prioritize_userids(sorted(rows, key=str.upper), actor)


def _fetch_managed_userids(actor: str | None = None) -> list[str]:
    """Return accounts visible in managed Orders/Positions/Performance.

    Operators/admins see every active REAL and VIRTUAL user, including users
    who are not currently logged in. OMS remains a separate REAL-only scope.
    """
    actor = _safe_str(actor or get_current_userid(), "").strip()
    if not actor:
        return []
    return _prioritized_scope_userids(managed_users_for_actor(actor), actor)


def _fetch_tradeable_userids(actor: str | None = None) -> list[str]:
    """Return active app-logged-in accounts eligible for trade actions."""
    actor = _safe_str(actor or get_current_userid(), "").strip()
    if not actor:
        return []
    return _prioritized_scope_userids(tradeable_users_for_actor(actor), actor)


def _requested_userids_from_query() -> list[str]:
    """
    Optional narrowing for GET/data routes.
    Accepts:
      ?userids=A&userids=B
      ?userids=A,B
      ?userid=A
    """
    requested = request.args.getlist("userids")
    if not requested:
        csv_userids = _safe_str(request.args.get("userids"), "").strip()
        if csv_userids:
            requested = [u.strip() for u in csv_userids.split(",") if u.strip()]

    userids = _normalize_userids(requested)

    if not userids:
        single = _safe_str(request.args.get("userid"), "").strip()
        if single:
            userids = [single]

    return userids


def _allowed_userids_for_actor(actor: str | None = None) -> list[str]:
    actor = _safe_str(actor or get_current_userid(), "").strip()
    if not actor:
        return []
    return _fetch_tradeable_userids(actor)


# =============================================================================
# USER TRADE BASE LAYER
# =============================================================================

def _filter_orders_draft(q):
    return q.filter(
        UserTradeORM.entry_status.in_(["CREATED", "READY"]),
        UserTradeORM.exit_status == "NONE",
    )


def _filter_orders_executed(q):
    """
    Show every non-draft row in executed/history bucket.
    Draft is strictly:
      entry_status in (CREATED, READY) AND exit_status == NONE
    Everything else should remain visible, including FAILED/CANCELLED/INVALID.
    """
    return q.filter(
        (~UserTradeORM.entry_status.in_(["CREATED", "READY"])) |
        (UserTradeORM.exit_status != "NONE")
    )


def _filter_positions(q):
    """
    Positions must be built only from FILLED entries.
    SUBMITTED / FAILED / CANCELLED / INVALID do not contribute to positions.
    """
    return q.filter(UserTradeORM.entry_status == "FILLED")


def _resolve_position_open_trade(
    db,
    *,
    userid: str,
    symbol: str,
    equity_ref: str | None = None,
    instrument_type: str | None = None,
):
    q = db.query(UserTradeORM).filter(UserTradeORM.userid == userid)

    symbol0 = _safe_str(symbol, "").strip().upper()
    equity_ref0 = _safe_str(equity_ref, "").strip().upper()
    inst0 = _safe_str(instrument_type, "").strip().upper()

    if symbol0:
        q = q.filter(UserTradeORM.symbol == symbol0)
    if equity_ref0:
        q = q.filter(UserTradeORM.equity_ref == equity_ref0)
    if inst0:
        q = q.filter(UserTradeORM.instrument_type == inst0)

    q = q.filter(~UserTradeORM.exit_status.in_(["FILLED", "CANCELLED", "FAILED"]))
    q = q.filter(UserTradeORM.entry_status.in_(["SUBMITTED", "FILLED"]))

    rows = q.order_by(
        UserTradeORM.last_time.desc(),
        UserTradeORM.entry_time.desc(),
        UserTradeORM.id.desc(),
    ).all()

    for tr in rows:
        qty = _safe_int(getattr(tr, "quantity", 0), 0)
        executed_exit_qty = _safe_int(getattr(tr, "executed_exit_qty", 0), 0)
        open_qty = max(qty - executed_exit_qty, 0)

        es, xs = _status(tr)

        if open_qty <= 0:
            continue
        if es not in ("SUBMITTED", "FILLED"):
            continue
        if xs in ("READY", "SUBMITTED", "FILLED", "CANCELLED", "FAILED"):
            continue

        return tr

    return None




def _trade_management_dict(tr):
    try:
        tm = getattr(tr, "trade_management", None)
        return tm if isinstance(tm, dict) else {}
    except Exception:
        return {}


def _trade_origin(tr) -> str:
    source = _safe_str(getattr(tr, "source", ""), "").strip().upper()
    signal_id = _safe_str(getattr(tr, "signal_id", ""), "").strip()
    if source in ("AUTOGEN", "TRADE_GENERATOR"):
        return "SIGNAL_AUTO"
    if source == "MANUAL_SIGNAL":
        return "SIGNAL_MANUAL"
    if source == "POSITION_ADD":
        return "POSITION_ADD"
    if source in ("WATCHLIST", "MANUAL") or signal_id.startswith("MANUAL:"):
        return "WATCHLIST"
    return source or "UNKNOWN"


def _trade_management_mode(tr) -> str:
    return "SIGNAL_LIFECYCLE" if _trade_origin(tr) in ("SIGNAL_AUTO", "SIGNAL_MANUAL") else "MANUAL_PRICE"


def _trade_signal_reference(tr):
    signal_id = _safe_str(getattr(tr, "signal_id", ""), "").strip()
    return None if not signal_id or signal_id.startswith("MANUAL:") else signal_id


def _managed_stop_price(tr):
    tm = _trade_management_dict(tr)
    return _safe_float(tm.get("current_stop_price"), 0.0)

def _managed_target_price(tr):
    tm = _trade_management_dict(tr)
    return _safe_float(tm.get("current_target_price"), 0.0)

# =============================================================================
# SERIALIZERS
# =============================================================================

def _to_orders_row(tr):
    planned_entry_price = _safe_float(getattr(tr, "entry_price", None), 0.0)
    executed_entry_price = _safe_float(getattr(tr, "executed_entry_price", None), 0.0)
    executed_exit_price = _safe_float(getattr(tr, "executed_exit_price", None), 0.0)

    qty = _safe_int(getattr(tr, "quantity", None))
    executed_exit_qty = _safe_int(getattr(tr, "executed_exit_qty", None), 0)
    exited = _is_exited(tr)

    display_entry_price = _trade_entry_display_price(tr)
    live_last_price = _safe_float(getattr(tr, "last_price", None), display_entry_price)
    display_last_price = executed_exit_price if exited and executed_exit_price > 0 else live_last_price

    stop_price = _managed_stop_price(tr)
    target_price = _managed_target_price(tr)

    required_amt = round(display_entry_price * qty, 2) if display_entry_price and qty else 0.0
    stop_amt = round(abs(display_entry_price - stop_price) * qty, 2) if stop_price and qty else 0.0
    target_amt = round(abs(target_price - display_entry_price) * qty, 2) if target_price and qty else 0.0

    exit_dt = getattr(tr, "exit_time", None)
    exit_time_str = _fmt_ist(exit_dt) if exit_dt else "Active"

    entry_status, exit_status = _status(tr)
    exec_status = _exec_status_compat(tr)
    executed_entry_qty = _safe_int(
        getattr(tr, "executed_entry_qty", None),
        0,
    )
    entry_filled = (
        entry_status == "FILLED"
        and executed_entry_price > 0
        and executed_entry_qty > 0
    )

    trade_side = _safe_str(getattr(tr, "trade_type", None), "").upper()

    exit_pnl_stored = _safe_float(getattr(tr, "exit_pnl", None), 0.0)
    last_pnl_value_stored = _safe_float(getattr(tr, "last_pnl_value", None), 0.0)

    exec_status_code = getattr(tr, "exec_status", None)
    exec_status_message = getattr(tr, "exec_status_message", None)
    reconcile_status_code = getattr(tr, "reconcile_status", None)
    reconcile_status_message = getattr(tr, "reconcile_status_message", None)

    def _calc_pnl(entry_px: float, mark_px: float, side: str, calc_qty: int) -> float:
        if not entry_px or not mark_px or not calc_qty:
            return 0.0
        if side == "SELL":
            return round((entry_px - mark_px) * calc_qty, 2)
        return round((mark_px - entry_px) * calc_qty, 2)

    pnl_basis_entry = executed_entry_price if entry_filled else 0.0

    if not entry_filled:
        # An unfilled order is not a position. Never manufacture P&L from the
        # requested price and requested quantity.
        pnl_value = 0.0
    elif exited:
        close_qty = (
            executed_exit_qty
            if executed_exit_qty > 0
            else executed_entry_qty
        )
        close_px = (
            executed_exit_price
            if executed_exit_price > 0
            else display_last_price
        )

        if abs(exit_pnl_stored) > 0:
            pnl_value = exit_pnl_stored
        else:
            pnl_value = _calc_pnl(
                pnl_basis_entry,
                close_px,
                trade_side,
                close_qty,
            )
    else:
        if abs(last_pnl_value_stored) > 0:
            pnl_value = last_pnl_value_stored
        else:
            pnl_value = _calc_pnl(
                pnl_basis_entry,
                live_last_price,
                trade_side,
                executed_entry_qty,
            )

    return {
        "id": tr.id,
        "userid": getattr(tr, "userid", "") or "",
        "signal_id": _trade_signal_reference(tr),
        "signal_reference": _trade_signal_reference(tr),
        "origin": _trade_origin(tr),
        "management_mode": _trade_management_mode(tr),

        "entry_plan_time": _fmt_ist(_trade_entry_display_time(tr)),
        "execution_time": _fmt_ist(getattr(tr, "entry_exec_time", None)),
        "date": _fmt_ist(getattr(tr, "last_time", None)),

        "symbol": getattr(tr, "symbol", "") or "",
        "equity_ref": getattr(tr, "equity_ref", "") or "",
        "instrument_type": getattr(tr, "instrument_type", "") or "",
        "trade_type": getattr(tr, "trade_type", "") or "",
        "product": getattr(tr, "product", "") or "",

        "entry_price": display_entry_price,
        "planned_entry_price": planned_entry_price,
        "executed_entry_price": executed_entry_price,
        "executed_exit_price": executed_exit_price,
        "last_price": display_last_price,

        "quantity": qty,
        "qty": qty,
        "executed_entry_qty": executed_entry_qty,
        "executed_exit_qty": executed_exit_qty,
        "pnl_available": entry_filled,

        "required_amt": required_amt,

        "current_stop_price": stop_price,
        "current_stop_amt": stop_amt,
        "current_target_price": target_price,
        "current_target_amt": target_amt,
        "trade_management": _trade_management_dict(tr),

        "entry_status": entry_status,
        "exit_status": exit_status,
        "exec_status": exec_status,
        "execution_mode": getattr(tr, "execution_mode", "") or "",

        "exited": exited,
        "exit_time": exit_time_str,

        "pnl": pnl_value,
        "pnl_value": pnl_value,
        "exit_pnl": exit_pnl_stored,
        "last_pnl_value": last_pnl_value_stored,

        "entry_order_id": getattr(tr, "entry_order_id", None),
        "exit_order_id": getattr(tr, "exit_order_id", None),

        "exec_status_code": exec_status_code,
        "exec_status_message": exec_status_message,
        "reconcile_status_code": reconcile_status_code,
        "reconcile_status_message": reconcile_status_message,

        "sl_hit": _bool_or_none(getattr(tr, "sl_hit", None)),
        "sl_hit_time": _fmt_ist(getattr(tr, "sl_hit_time", None)),
        "t1_hit": _bool_or_none(getattr(tr, "t1_hit", None)),
        "t1_hit_time": _fmt_ist(getattr(tr, "t1_hit_time", None)),
        "t2_hit": _bool_or_none(getattr(tr, "t2_hit", None)),
        "t2_hit_time": _fmt_ist(getattr(tr, "t2_hit_time", None)),
        "t3_hit": _bool_or_none(getattr(tr, "t3_hit", None)),
        "t3_hit_time": _fmt_ist(getattr(tr, "t3_hit_time", None)),

        "source": getattr(tr, "source", "") or "",
        "details": f"Trade ID: {tr.id}",
    }


def _to_position_trade_row(tr):
    def fnum(x):
        try:
            return float(x) if x is not None else None
        except Exception:
            return None

    qty = _safe_int(getattr(tr, "quantity", 0), 0)
    executed_exit_qty = _safe_int(getattr(tr, "executed_exit_qty", 0), 0)
    open_qty = max(qty - executed_exit_qty, 0)

    last_pnl_value = fnum(getattr(tr, "last_pnl_value", None))
    last_pnl = fnum(getattr(tr, "last_pnl", None))
    exit_pnl = fnum(getattr(tr, "exit_pnl", None))

    exited = _is_exited(tr)
    pnl_value = exit_pnl if exited else (
        last_pnl_value if last_pnl_value is not None else (last_pnl if last_pnl is not None else 0.0)
    )

    entry_status, exit_status = _status(tr)
    exec_status = _exec_status_compat(tr)

    entry_time_dt = _to_ist_naive(getattr(tr, "entry_time", None))
    entry_intent_time_dt = _to_ist_naive(getattr(tr, "entry_intent_time", None))
    entry_exec_time_dt = _to_ist_naive(getattr(tr, "entry_exec_time", None))
    last_time_dt = _to_ist_naive(getattr(tr, "last_time", None))
    exit_time_dt = _to_ist_naive(getattr(tr, "exit_time", None))

    return {
        "id": tr.id,
        "signal_id": _trade_signal_reference(tr),
        "signal_reference": _trade_signal_reference(tr),
        "userid": getattr(tr, "userid", "") or "",
        "origin": _trade_origin(tr),
        "management_mode": _trade_management_mode(tr),

        "symbol": getattr(tr, "symbol", "") or "",
        "equity_ref": getattr(tr, "equity_ref", "") or "",
        "instrument_type": getattr(tr, "instrument_type", "") or "",
        "trade_type": getattr(tr, "trade_type", "") or "",

        "entry_plan_time": _fmt_ist(_trade_entry_display_time(tr)),
        "execution_time": _fmt_ist(getattr(tr, "entry_exec_time", None)),
        "entry_exec_time": _fmt_ist(getattr(tr, "entry_exec_time", None)),
        "entry_price": _trade_entry_display_price(tr),
        "planned_entry_price": fnum(getattr(tr, "entry_price", None)),
        "executed_entry_price": fnum(getattr(tr, "executed_entry_price", None)),

        "qty": qty,
        "quantity": qty,
        "executed_exit_qty": executed_exit_qty,
        "open_qty": open_qty,

        "current_stop_price": fnum(_managed_stop_price(tr)),
        "current_target_price": fnum(_managed_target_price(tr)),
        "trade_management": _trade_management_dict(tr),

        "last_time": _fmt_ist(getattr(tr, "last_time", None)),
        "last_price": fnum(getattr(tr, "last_price", None)),

        "last_pnl": last_pnl,
        "last_pnl_value": last_pnl_value,
        "pnl_value": pnl_value,

        "entry_status": entry_status,
        "exit_status": exit_status,
        "exec_status": exec_status,
        "execution_mode": getattr(tr, "execution_mode", "") or "",

        "exited": exited,
        "exit_time": _fmt_ist(getattr(tr, "exit_time", None)),
        "exit_price": fnum(getattr(tr, "exit_price", None)),
        "executed_exit_price": fnum(getattr(tr, "executed_exit_price", None)),
        "exit_pnl": exit_pnl,
        "exit_reason": getattr(tr, "exit_reason", "") or "",
        "exit_rule": getattr(tr, "exit_rule", "") or "",

        "_entry_time_dt": entry_time_dt,
        "_entry_intent_time_dt": entry_intent_time_dt,
        "_entry_exec_time_dt": entry_exec_time_dt,
        "_last_time_dt": last_time_dt,
        "_exit_time_dt": exit_time_dt,
    }


# =============================================================================
# USERS / PICKERS
# =============================================================================

@dash_bp.route("/users/tradeable")
@login_required
def users_tradeable():
    actor = get_current_userid()
    if not actor:
        return jsonify({"status": "error", "reason": "not_authenticated"}), 401

    userids = _fetch_tradeable_userids(actor)
    operator = is_operator(actor)

    data = []
    for userid in userids:
        data.append({
            "userid": userid,
            "selected": userid == actor,
            "disabled": (not operator and userid == actor),
        })

    return jsonify({"status": "success", "data": data})




@dash_bp.route("/users/managed")
@login_required
def users_managed():
    actor = get_current_userid()
    if not actor:
        return jsonify({"status": "error", "reason": "not_authenticated"}), 401

    operator = is_operator(actor)
    data = [
        {
            "userid": userid,
            "selected": userid == actor,
            "disabled": not operator,
        }
        for userid in _fetch_managed_userids(actor)
    ]
    return jsonify({"status": "success", "data": data})


# =============================================================================
# AUDIT TIMELINE
# =============================================================================

def _parse_audit_dt(value):
    raw = _safe_str(value, "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d-%m-%Y %H:%M", "%d-%m-%Y %H:%M:%S"):
        try:
            return datetime.strptime(raw[:19], fmt).replace(tzinfo=None)
        except Exception:
            pass
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return _to_ist_naive(dt)
    except Exception:
        return None


def _audit_row_to_dict(row):
    return {
        "id": row.id,
        "ts": _fmt_ist(row.ts),
        "entity_type": _safe_str(row.entity_type, ""),
        "entity_id": _safe_str(row.entity_id, ""),
        "symbol": _safe_str(row.symbol, ""),
        "userid": _safe_str(row.userid, ""),
        "evaluation_stage": _safe_str(row.evaluation_stage, ""),
        "previous_state": _safe_str(row.previous_state, ""),
        "new_state": _safe_str(row.new_state, ""),
        "action": _safe_str(row.action, ""),
        "reason_code": _safe_str(row.reason_code, ""),
        "reason_text": _safe_str(row.reason_text, ""),
        "confidence": _safe_float(row.confidence, None),
        "payload_json": sanitize_json(row.payload_json or {}),
    }


@dash_bp.route("/audit/timeline")
@login_required
def audit_timeline():
    actor = get_current_userid()
    if not actor:
        return jsonify({"status": "error", "reason": "not_authenticated"}), 401

    entity_id = _safe_str(request.args.get("entity_id"), "").strip()
    related_id = _safe_str(request.args.get("related_id"), "").strip()
    symbol = _safe_str(request.args.get("symbol"), "").strip().upper()
    userid = _safe_str(request.args.get("userid"), "").strip()
    start_time = _parse_audit_dt(request.args.get("start_time"))
    end_time = _parse_audit_dt(request.args.get("end_time"))
    limit = max(1, min(_safe_int(request.args.get("limit"), 300), 1000))

    if not entity_id and not related_id and not symbol:
        return jsonify({"status": "error", "reason": "missing_entity_or_symbol"}), 400

    entity_ids = [x for x in [entity_id, related_id] if x]

    try:
        with get_trades_db() as db:
            q = db.query(AuditLogORM)

            if entity_ids:
                q = q.filter(AuditLogORM.entity_id.in_(entity_ids))
            elif symbol:
                q = q.filter(AuditLogORM.symbol == symbol)
            else:
                return jsonify({"status": "error", "reason": "missing_entity_or_symbol"}), 400

            if userid:
                q = q.filter(or_(AuditLogORM.userid == userid, AuditLogORM.userid.is_(None)))

            if start_time:
                q = q.filter(AuditLogORM.ts >= start_time)
            if end_time:
                q = q.filter(AuditLogORM.ts <= end_time)

            rows = (
                q.order_by(AuditLogORM.ts.asc(), AuditLogORM.id.asc())
                 .limit(limit)
                 .all()
            )

        return jsonify({
            "status": "success",
            "count": len(rows),
            "data": [_audit_row_to_dict(r) for r in rows],
        })

    except Exception:
        logger.exception(
            "audit_timeline failed entity_id=%s related_id=%s symbol=%s userid=%s",
            entity_id,
            related_id,
            symbol,
            userid,
        )
        return jsonify({"status": "error", "reason": "audit_timeline_failed"}), 500


# =============================================================================
# TRADING (CENTRALIZED ACTION ENDPOINTS)
# =============================================================================

def _normalize_trade_source(source):
    src = _safe_str(source, "signal").strip().lower()
    if src in ("signals", "signal"):
        return "signal"
    return src


def _trade_preview_summary(userid: str, result: dict) -> dict:
    result = result or {}
    form = result.get("trade_form") if isinstance(result.get("trade_form"), dict) else {}
    validation = form.get("entry_eligibility") if isinstance(form.get("entry_eligibility"), dict) else {}
    details = result.get("details") if isinstance(result.get("details"), dict) else {}
    if not validation and details.get("decision"):
        validation = details

    decision = _safe_str(validation.get("decision"), "BLOCK" if not result.get("ok") else "ALLOW").upper()
    reasons = validation.get("reasons") if isinstance(validation.get("reasons"), list) else []
    warnings = validation.get("warnings") if isinstance(validation.get("warnings"), list) else []
    return {
        "userid": userid,
        "ok": bool(result.get("ok")),
        "decision": decision,
        "allowed": bool(validation.get("allowed")) if validation else decision == "ALLOW",
        "entry_decision": _safe_str(validation.get("entry_decision"), ""),
        "requires_confirmation": bool(form.get("requires_confirmation") or decision == "WAIT"),
        "reasons": reasons,
        "warnings": warnings,
        "error": result.get("error"),
        "details": validation.get("details") if isinstance(validation.get("details"), dict) else details,
    }


def _prioritize_userids(userids: list[str], primary_userid: str | None) -> list[str]:
    primary = _safe_str(primary_userid, "").strip().upper()
    if not primary:
        return list(userids)
    return sorted(
        list(userids),
        key=lambda uid: 0 if _safe_str(uid, "").strip().upper() == primary else 1,
    )


@dash_bp.post("/trading/create_plan")
@login_required
def trading_create_plan():
    payload = request.get_json(silent=True) or {}
    source = _normalize_trade_source(payload.get("source"))

    actor = get_current_userid()
    if not actor:
        return jsonify({"ok": False, "error": "MISSING_USERID_IN_SESSION"}), 401

    requested_userids = _normalize_userids(payload.get("userids"))
    if not requested_userids:
        single = _safe_str(payload.get("userid"), "").strip()
        if single:
            requested_userids = [single]

    visible_userids = _fetch_tradeable_userids(actor)
    if is_operator(actor):
        if requested_userids:
            visible_set = {u.upper() for u in visible_userids}
            userids = [u for u in requested_userids if u.upper() in visible_set]
        else:
            userids = visible_userids
    else:
        userids = [actor]

    if not userids:
        return jsonify({"ok": False, "error": "MISSING_USERID_IN_SESSION"}), 401

    userids = _prioritize_userids(userids, payload.get("primary_userid"))
    userid = userids[0]

    try:
        if source == "signal":
            signal_id = str(payload.get("signal_id") or "").strip()
            if not signal_id:
                return jsonify({"ok": False, "error": "MISSING_SIGNAL_ID"}), 400

            previews = []
            forms = []
            for preview_userid in userids:
                preview = TradeGenHelper.build_signal_trade_form(
                    userid=preview_userid,
                    signal_id=signal_id,
                    source="MANUAL_SIGNAL",
                ) or {}
                summary = _trade_preview_summary(preview_userid, preview)
                previews.append(summary)
                if preview.get("ok") and isinstance(preview.get("trade_form"), dict):
                    rank = 0 if summary.get("decision") == "ALLOW" else 1
                    forms.append((rank, preview_userid, preview.get("trade_form")))

            if not forms:
                return jsonify({
                    "ok": False,
                    "error": "NO_USERS_ELIGIBLE_FOR_SIGNAL_TRADE",
                    "user_validation": previews,
                }), 409

            forms.sort(key=lambda item: (item[0], userids.index(item[1])))
            form_userid = forms[0][1]
            trade_form = dict(forms[0][2])
            trade_form["userid"] = form_userid
            trade_form["user_validation"] = previews
            res = {"ok": True, "trade_form": trade_form}

        elif source == "watchlist":
            symbol = str(payload.get("symbol") or "").strip().upper()
            side = str(payload.get("side") or "BUY").strip().upper()

            if not symbol:
                return jsonify({"ok": False, "error": "MISSING_SYMBOL"}), 400

            res = TradeGenHelper.build_watchlist_trade_form(
                userid=userid,
                symbol=symbol,
                side=side,
                source="WATCHLIST",
            ) or {}

        elif source == "position":
            symbol = str(payload.get("symbol") or "").strip().upper()
            equity_ref = str(payload.get("equity_ref") or symbol).strip().upper()
            instrument_type = str(payload.get("instrument_type") or "EQ").strip().upper()
            side = str(payload.get("side") or "BUY").strip().upper()

            if not symbol:
                return jsonify({"ok": False, "error": "MISSING_SYMBOL"}), 400

            res = TradeGenHelper.build_position_trade_form(
                userid=userid,
                symbol=symbol,
                equity_ref=equity_ref,
                instrument_type=instrument_type,
                side=side,
                source="POSITION_ADD",
            ) or {}

        else:
            return jsonify({
                "ok": False,
                "error": "UNSUPPORTED_SOURCE",
                "details": {"source": source}
            }), 400

    except Exception:
        logger.exception("trading_create_plan failed userid=%s source=%s payload=%s", userid, source, payload)
        return jsonify({"ok": False, "error": "INTERNAL_ERROR"}), 500

    if isinstance(res, dict) and "_plan_obj" in res:
        res = {k: v for k, v in res.items() if k != "_plan_obj"}

    if not res.get("ok"):
        return jsonify(res), 409

    res["userids"] = userids
    return jsonify(res), 200


@dash_bp.post("/trading/create_trade")
@login_required
def trading_create_trade():
    payload = request.get_json(silent=True) or {}
    source = _normalize_trade_source(payload.get("source"))

    actor = get_current_userid()
    if not actor:
        return jsonify({"ok": False, "error": "MISSING_USERID_IN_SESSION"}), 401

    requested_userids = _normalize_userids(payload.get("userids"))
    if not requested_userids:
        single = _safe_str(payload.get("userid"), "").strip()
        if single:
            requested_userids = [single]

    visible_userids = _fetch_tradeable_userids(actor)
    if is_operator(actor):
        if requested_userids:
            visible_set = {u.upper() for u in visible_userids}
            userids = [u for u in requested_userids if u.upper() in visible_set]
        else:
            userids = visible_userids
    else:
        userids = [actor]

    if not userids:
        return jsonify({"ok": False, "error": "MISSING_USERID_IN_SESSION"}), 401

    results = []
    created_count = 0
    trade_ids = []

    try:
        if source == "signal":
            signal_id = str(payload.get("signal_id") or "").strip()
            instrument_choice = str(payload.get("instrument_choice") or "MULTI").strip().upper()
            requested_product = str(payload.get("product") or "MIS").strip().upper()
            confirm_entry_warning = bool(payload.get("confirm_entry_warning") or False)
            selected_trade_symbol = str(payload.get("trade_symbol") or "").strip().upper()
            selected_quantity = _safe_int(payload.get("quantity") or payload.get("qty") or 0, 0)
            selected_lots = _safe_int(payload.get("lots") or 0, 0)

            if not signal_id:
                return jsonify({"ok": False, "error": "MISSING_SIGNAL_ID"}), 400

            for userid in userids:
                res = TradeGenHelper.create_trades_from_signal(
                    userid=userid,
                    signal_id=signal_id,
                    instrument_choice=instrument_choice,
                    source="MANUAL_SIGNAL",
                    confirm_entry_warning=confirm_entry_warning,
                    requested_product=requested_product,
                    selected_trade_symbol=selected_trade_symbol or None,
                    selected_quantity=selected_quantity or None,
                    selected_lots=selected_lots or None,
                ) or {}

                ok = bool(res.get("ok"))
                results.append({
                    "userid": userid,
                    "ok": ok,
                    "result": res,
                })

                if ok:
                    created_count += int(res.get("created_count") or len(res.get("trade_ids") or []) or 0)
                    trade_ids.extend(res.get("trade_ids") or [])

            all_ok = any(r["ok"] for r in results)
            return jsonify({
                "ok": all_ok,
                "results": results,
                "created_count": created_count,
                "trade_ids": trade_ids,
            }), (200 if all_ok else 409)

        elif source in ("watchlist", "position"):
            symbol = str(payload.get("symbol") or "").strip().upper()
            equity_ref = str(payload.get("equity_ref") or symbol).strip().upper()
            instrument_type = str(payload.get("instrument_type") or "EQ").strip().upper()
            trade_type = str(payload.get("trade_type") or payload.get("side") or "BUY").strip().upper()

            if not symbol:
                return jsonify({"ok": False, "error": "MISSING_SYMBOL"}), 400

            if instrument_type not in ("EQ", "FUT", "CE", "PE"):
                return jsonify({
                    "ok": False,
                    "error": "INVALID_INSTRUMENT_TYPE",
                    "details": {"instrument_type": instrument_type}
                }), 400

            if trade_type not in ("BUY", "SELL"):
                return jsonify({
                    "ok": False,
                    "error": "INVALID_TRADE_TYPE",
                    "details": {"trade_type": trade_type}
                }), 400

            base_payload = {
                "symbol": symbol,
                "equity_ref": equity_ref,
                "instrument_type": instrument_type,
                "trade_type": trade_type,
                "product": str(payload.get("product") or "MIS").strip().upper(),
                "execution_mode": str(payload.get("execution_mode") or "").strip().upper(),
                "trade_symbol": str(payload.get("trade_symbol") or symbol).strip().upper(),
                "lots": _safe_int(payload.get("lots") or 1, 1),
                "quantity": _safe_int(payload.get("quantity") or payload.get("qty") or 0, 0),
                "entry_price": payload.get("entry_price"),
                "risk_ref_price": payload.get("risk_ref_price"),
                "message": str(payload.get("message") or source.upper()).strip(),
            }

            for userid in userids:
                res = TradeGenHelper.create_manual_trade(
                    userid=userid,
                    payload=base_payload,
                    source="POSITION_ADD" if source == "position" else "WATCHLIST",
                ) or {}

                ok = bool(res.get("ok"))
                results.append({
                    "userid": userid,
                    "ok": ok,
                    "result": res,
                })

                if ok:
                    created_count += int(res.get("created_count") or len(res.get("trade_ids") or []) or 0)
                    trade_ids.extend(res.get("trade_ids") or [])
                    if res.get("trade_id"):
                        trade_ids.append(res.get("trade_id"))

            all_ok = any(r["ok"] for r in results)
            return jsonify({
                "ok": all_ok,
                "results": results,
                "created_count": created_count,
                "trade_ids": trade_ids,
            }), (200 if all_ok else 409)

        else:
            return jsonify({
                "ok": False,
                "error": "UNSUPPORTED_SOURCE",
                "details": {"source": source}
            }), 400

    except Exception:
        logger.exception("trading_create_trade failed source=%s payload=%s", source, payload)
        return jsonify({"ok": False, "error": "INTERNAL_ERROR"}), 500


@dash_bp.post("/trading/edit_trade")
@login_required
def trading_edit_trade():
    actor = get_current_userid()
    if not actor:
        return jsonify({"ok": False, "error": "not_authenticated"}), 401

    allowed_userids = set(_allowed_userids_for_actor(actor))

    payload = request.get_json(silent=True) or {}
    trade_id = _safe_int(payload.get("id") or payload.get("trade_id") or 0, 0)
    mode = _safe_str(payload.get("mode"), "draft").strip().lower()

    if trade_id <= 0:
        return jsonify({"ok": False, "error": "missing_id"}), 400

    if mode not in ("draft", "live"):
        return jsonify({
            "ok": False,
            "error": "invalid_mode",
            "details": {"mode": mode}
        }), 400

    try:
        with get_trades_db() as db:
            tr = db.get(UserTradeORM, int(trade_id))
            if not tr or _safe_str(getattr(tr, "userid", ""), "") not in allowed_userids:
                return jsonify({"ok": False, "error": "not_found"}), 404

            if mode == "draft":
                if not _editable_draft(tr):
                    es, xs = _status(tr)
                    return jsonify({
                        "ok": False,
                        "error": "draft_not_editable",
                        "details": {
                            "entry_status": es,
                            "exit_status": xs,
                        }
                    }), 409

                entry_price = _safe_float(
                    payload.get("entry_price"),
                    _safe_float(getattr(tr, "entry_price", None), 0.0)
                )
                quantity = _safe_int(
                    payload.get("quantity") if payload.get("quantity") is not None else payload.get("qty"),
                    _safe_int(getattr(tr, "quantity", None), 0)
                )

                if entry_price <= 0:
                    return jsonify({"ok": False, "error": "invalid_entry_price"}), 400
                if quantity <= 0:
                    return jsonify({"ok": False, "error": "invalid_quantity"}), 400

                tr.entry_price = entry_price
                tr.quantity = quantity

            else:
                if not _editable_live_trade(tr):
                    es, xs = _status(tr)
                    qty = _safe_int(getattr(tr, "quantity", None), 0)
                    executed_exit_qty = _safe_int(getattr(tr, "executed_exit_qty", None), 0)
                    open_qty = max(qty - executed_exit_qty, 0)

                    return jsonify({
                        "ok": False,
                        "error": "live_trade_not_editable",
                        "details": {
                            "entry_status": es,
                            "exit_status": xs,
                            "open_qty": open_qty,
                        }
                    }), 409

            if mode == "live":
                return jsonify({
                    "ok": False,
                    "error": "live_trade_management_config_driven",
                    "message": "Live trade stop/target management is controlled by trade_management and monitor config."
                }), 409

            if mode == "draft" and hasattr(tr, "required_amt"):
                tr.required_amt = round(_safe_float(getattr(tr, "entry_price", None), 0.0) * _safe_int(getattr(tr, "quantity", None), 0), 2)

            if hasattr(tr, "last_time"):
                tr.last_time = business_now_naive()

            if hasattr(tr, "exec_status"):
                tr.exec_status = None
            if hasattr(tr, "exec_status_message"):
                tr.exec_status_message = None

            _refresh_order_mark_fields(tr)
            db.commit()
            db.refresh(tr)

            return jsonify({
                "ok": True,
                "trade_id": tr.id,
                "mode": mode,
                "message": "TRADE_UPDATED",
                "row": _to_orders_row(tr),
                "position_row": _to_position_trade_row(tr),
            }), 200

    except Exception as e:
        logger.exception("trading_edit_trade failed: %s", e)
        return jsonify({"ok": False, "error": "trading_edit_trade_failed"}), 500


@dash_bp.post("/trading/submit_trade")
@login_required
def trading_submit_trade():
    actor = get_current_userid()
    if not actor:
        return jsonify({"ok": False, "error": "not_authenticated"}), 401

    allowed_userids = set(_allowed_userids_for_actor(actor))

    payload = request.get_json(silent=True) or {}

    ids = payload.get("ids", None)
    if ids is None:
        ids = [payload.get("id") or payload.get("trade_id")]
    ids = [_safe_int(x, 0) for x in (ids or []) if _safe_int(x, 0) > 0]

    if not ids:
        return jsonify({"ok": False, "error": "missing_ids"}), 400

    now = business_now_naive()
    queued = []
    skipped = []
    rows = []

    try:
        with get_trades_db() as db:
            for tid in ids:
                tr = db.get(UserTradeORM, int(tid))
                if not tr or _safe_str(getattr(tr, "userid", ""), "") not in allowed_userids:
                    skipped.append({"id": tid, "reason": "not_found"})
                    continue

                es, xs = _status(tr)

                if es not in ("CREATED", "READY"):
                    skipped.append({"id": tid, "reason": f"entry_status_{es}"})
                    continue

                if xs and xs != "NONE":
                    skipped.append({"id": tid, "reason": f"exit_status_{xs}"})
                    continue

                try:
                    plan_updates = TradeGenHelper.build_unsubmitted_plan_refresh(
                        tr,
                        asof_time=now,
                        refresh_reason="MANUAL_SUBMIT_PLAN_REFRESH",
                        force=True,
                    )
                    for key, value in plan_updates.items():
                        setattr(tr, key, value)
                except Exception as exc:
                    logger.exception(
                        "Manual submit plan refresh failed trade_id=%s",
                        tid,
                    )
                    skipped.append({
                        "id": tid,
                        "reason": "entry_plan_refresh_failed",
                        "details": str(exc)[:250],
                    })
                    continue

                _prepare_draft_for_queue(tr, now=now)
                queued.append(tid)
                rows.append(tr)

            db.commit()

            for tr in rows:
                db.refresh(tr)

        response_payload = {
            "ok": bool(queued),
            "queued": queued,
            "submitted": queued,
            "skipped": skipped,
            "rows": [_to_orders_row(tr) for tr in rows],
            "note": "QUEUED_FOR_EXECUTOR_NO_BROKER_CALL",
        }
        if not queued:
            response_payload["error"] = "NO_TRADES_QUEUED"
            if skipped:
                response_payload["message"] = (
                    skipped[0].get("details")
                    or skipped[0].get("reason")
                    or "Trade was not queued."
                )
        return jsonify(response_payload), (200 if queued else 409)

    except Exception as e:
        logger.exception("trading_submit_trade failed: %s", e)
        return jsonify({"ok": False, "error": "trading_submit_trade_failed"}), 500


@dash_bp.post("/trading/exit_plan")
@login_required
def trading_exit_plan():
    actor = get_current_userid()
    if not actor:
        return jsonify({"ok": False, "error": "not_authenticated"}), 401

    allowed_userids = set(_allowed_userids_for_actor(actor))

    payload = request.get_json(silent=True) or {}
    trade_id = _safe_int(payload.get("id") or payload.get("trade_id") or 0, 0)

    target_userid = _safe_str(payload.get("userid"), "").strip()
    if not target_userid or target_userid not in allowed_userids:
        target_userid = actor

    symbol = _safe_str(payload.get("symbol"), "").strip().upper()
    equity_ref = _safe_str(payload.get("equity_ref"), "").strip().upper()
    instrument_type = _safe_str(payload.get("instrument_type"), "").strip().upper()

    try:
        with get_trades_db() as db:
            tr = None

            if trade_id > 0:
                tr = db.get(UserTradeORM, int(trade_id))
                if not tr or _safe_str(getattr(tr, "userid", ""), "") not in allowed_userids:
                    tr = None
            else:
                tr = _resolve_position_open_trade(
                    db,
                    userid=target_userid,
                    symbol=symbol,
                    equity_ref=equity_ref,
                    instrument_type=instrument_type,
                )

            if not tr:
                return jsonify({"ok": False, "error": "POSITION_NOT_FOUND"}), 404

            es, xs = _status(tr)
            qty = _safe_int(getattr(tr, "quantity", 0), 0)
            executed_exit_qty = _safe_int(getattr(tr, "executed_exit_qty", 0), 0)
            open_qty = max(qty - executed_exit_qty, 0)

            if es != "FILLED":
                return jsonify({
                    "ok": False,
                    "error": "ENTRY_NOT_FILLED",
                    "details": {"entry_status": es}
                }), 409

            if xs in ("READY", "SUBMITTED"):
                return jsonify({
                    "ok": False,
                    "error": "EXIT_ALREADY_PENDING",
                    "details": {"exit_status": xs}
                }), 409

            if xs in ("FILLED", "CANCELLED", "FAILED"):
                return jsonify({
                    "ok": False,
                    "error": "POSITION_ALREADY_CLOSED",
                    "details": {"exit_status": xs}
                }), 409

            if open_qty <= 0:
                return jsonify({
                    "ok": False,
                    "error": "NO_OPEN_QTY"
                }), 409

            lotsize = 1
            try:
                if str(getattr(tr, "instrument_type", "") or "").upper() != "EQ":
                    lotsize = TradeGenHelper._resolve_lotsize(getattr(tr, "symbol", "") or "")
            except Exception:
                lotsize = 1

            return jsonify({
                "ok": True,
                "exit_form": {
                    "mode": "FULL_ONLY",
                    "trade_id": getattr(tr, "id", None),
                    "symbol": getattr(tr, "symbol", "") or "",
                    "equity_ref": getattr(tr, "equity_ref", "") or "",
                    "instrument_type": getattr(tr, "instrument_type", "") or "",
                    "trade_type": getattr(tr, "trade_type", "") or "",
                    "entry_status": es,
                    "exit_status": xs,
                    "quantity": qty,
                    "executed_exit_qty": executed_exit_qty,
                    "open_qty": open_qty,
                    "lotsize": lotsize,
                    "last_price": _safe_float(getattr(tr, "last_price", None), 0.0),
                }
            }), 200

    except Exception as e:
        logger.exception("trading_exit_plan failed: %s", e)
        return jsonify({"ok": False, "error": "trading_exit_plan_failed"}), 500


@dash_bp.post("/trading/exit_trade")
@login_required
def trading_exit_trade():
    actor = get_current_userid()
    if not actor:
        return jsonify({"status": "error", "reason": "not_authenticated"}), 401

    allowed_userids = set(_allowed_userids_for_actor(actor))

    payload = request.get_json(silent=True) or {}
    trade_id = _safe_int(payload.get("id") or payload.get("trade_id") or 0, 0)

    target_userid = _safe_str(payload.get("userid"), "").strip()
    if not target_userid or target_userid not in allowed_userids:
        target_userid = actor

    symbol = _safe_str(payload.get("symbol"), "").strip().upper()
    equity_ref = _safe_str(payload.get("equity_ref"), "").strip().upper()
    instrument_type = _safe_str(payload.get("instrument_type"), "").strip().upper()
    reason = _safe_str(payload.get("reason") or "MANUAL_EXIT", "MANUAL_EXIT").strip()[:80]

    if trade_id <= 0 and not symbol:
        return jsonify({"status": "error", "reason": "missing_trade_or_symbol"}), 400

    try:
        now = business_now_naive()

        with get_trades_db() as db:
            tr = None

            if trade_id > 0:
                tr = db.get(UserTradeORM, int(trade_id))
                if not tr or _safe_str(getattr(tr, "userid", ""), "") not in allowed_userids:
                    return jsonify({"status": "error", "reason": "not_found"}), 404
            else:
                tr = _resolve_position_open_trade(
                    db,
                    userid=target_userid,
                    symbol=symbol,
                    equity_ref=equity_ref,
                    instrument_type=instrument_type,
                )
                if not tr:
                    return jsonify({"status": "error", "reason": "not_found"}), 404

            es, xs = _status(tr)
            qty = _safe_int(getattr(tr, "quantity", 0), 0)
            executed_exit_qty = _safe_int(getattr(tr, "executed_exit_qty", 0), 0)
            open_qty = max(qty - executed_exit_qty, 0)

            if es != "FILLED":
                return jsonify({
                    "status": "error",
                    "reason": "entry_not_filled",
                    "entry_status": es
                }), 400

            if open_qty <= 0:
                return jsonify({"status": "error", "reason": "no_open_qty"}), 400

            if xs in ("FILLED", "CANCELLED", "FAILED"):
                return jsonify({
                    "status": "error",
                    "reason": "already_closed",
                    "exit_status": xs
                }), 400

            if xs in ("READY", "SUBMITTED"):
                trade_id_out = _safe_int(getattr(tr, "id", 0), 0)
                open_qty_out = _safe_int(open_qty, 0)
                return jsonify({
                    "status": "success",
                    "note": f"exit_already_{xs.lower()}",
                    "trade_id": trade_id_out,
                    "open_qty": open_qty_out,
                }), 200

            tr.exit_status = "READY"
            tr.exit_intent_time = now
            tr.exit_reason = reason
            tr.exit_rule = "UI_TRADING_EXIT_TRADE"
            tr.exec_last_checked_at = now

            if hasattr(tr, "exec_status"):
                tr.exec_status = None
            if hasattr(tr, "exec_status_message"):
                tr.exec_status_message = None

            trade_id_out = _safe_int(getattr(tr, "id", 0), 0)
            open_qty_out = _safe_int(open_qty, 0)

            db.commit()

        return jsonify({
            "status": "success",
            "trade_id": trade_id_out,
            "open_qty": open_qty_out,
        }), 200

    except Exception as e:
        logger.exception("trading_exit_trade failed: %s", e)
        return jsonify({"status": "error", "reason": "trading_exit_trade_failed"}), 500


# =============================================================================
# WATCHLIST
# =============================================================================

@dash_bp.route("/watchlist")
@login_required
def watchlist():
    return render_template(
        "dash_watchlist.html",
        userid=get_current_userid() or "Guest",
        active="watchlist",
        nav_section="dashboard",
    )


@dash_bp.route("/watchlist/data")
@login_required
def watchlist_data():
    actor = get_current_userid()
    if not actor:
        return jsonify({"status": "error", "reason": "not_authenticated"}), 401

    all_syms = SymbolSchema.fetch_symbols(active=1) or []
    whitelist = request.args.getlist("symbols") or []

    if is_operator(actor):
        syms = all_syms
    else:
        session_whitelist = request.cookies.get("noop")
        _ = session_whitelist
        syms = all_syms
        filter_symbols = request.environ.get("werkzeug.request").session.get("filter_symbols", []) if False else None
        _ = filter_symbols

        from flask import session
        user_whitelist = session.get("filter_symbols", []) or []
        syms = [s for s in all_syms if (not user_whitelist or s.symbol in user_whitelist)]

    ist_tz = ZoneInfo("Asia/Kolkata")

    def _as_dict(x):
        if not x:
            return {}
        if isinstance(x, str):
            try:
                return json.loads(x) or {}
            except Exception:
                return {}
        if isinstance(x, dict):
            return x
        return {}

    def _get(dct, path, default=None):
        cur = dct
        for k in path:
            if not isinstance(cur, dict):
                return default
            cur = cur.get(k)
        return default if cur is None else cur

    payload = []

    for s in syms:
        if getattr(s, "type", None) != "EQ":
            continue

        snap_dict = _as_dict(getattr(s, "last_snapshot", None))
        ltp = float(getattr(s, "price", 0.0) or 0.0)
        ltp_time = getattr(s, "last_time", None)

        if ltp_time:
            try:
                ltp_time = ltp_time.astimezone(ist_tz)
                ltp_time_str = ltp_time.strftime("%d-%m-%Y %H:%M")
            except Exception:
                ltp_time_str = ""
        else:
            ltp_time_str = ""

        payload.append({
            "symbol": s.symbol,
            "ltp": ltp,
            "ltp_time": ltp_time_str,
            "state": _get(snap_dict, ["indicators", "hma", "state"], "N/A"),
            "strength": _get(snap_dict, ["indicators", "hma", "strength"], "N/A"),
            "vwap_pct": _get(snap_dict, ["indicators", "vwap", "distance_pct"], 0.0),
            "bb_zone": _get(snap_dict, ["indicators", "bollinger", "zone"], "UNKNOWN"),
            "details": snap_dict,
        })

    return jsonify({"status": "success", "data": payload})


# =============================================================================
# SIGNALS
# =============================================================================

@dash_bp.route("/signals")
@login_required
def signals():
    return render_template(
        "dash_signals.html",
        userid=get_current_userid() or "Guest",
        active="signals",
        nav_section="dashboard",
        signals_config=UI_CONFIG.signals.model_dump(),
    )


@dash_bp.route("/signals/data")
@login_required
def signals_data():
    actor = get_current_userid()
    if not actor:
        return jsonify({"status": "error", "reason": "not_authenticated"}), 401

    limit = _safe_int(request.args.get("limit", UI_CONFIG.signals.default_limit), UI_CONFIG.signals.default_limit)
    status_filter = _safe_str(request.args.get("status"), UI_CONFIG.signals.default_status).strip().upper()
    side_filter = _safe_str(request.args.get("side"), "").strip().upper()
    symbol_filter = _safe_str(request.args.get("symbol"), "").strip().upper()

    rows = SignalSchema.list_for_ui(limit=limit)

    if not is_operator(actor):
        user = UserSchema.fetch_user(actor)
        if not user:
            return jsonify({"status": "error", "reason": "user_not_found"}), 404
        rows = [r for r in rows if user.allowed_symbols(getattr(r, "symbol", ""))]

    data = []
    for row in rows:
        ui_row = row.to_ui_row()
        row_status = _safe_str(ui_row.get("status"), "").strip().upper()
        row_side = _safe_str(ui_row.get("side"), "").strip().upper()
        row_symbol = _safe_str(ui_row.get("symbol"), "").strip().upper()

        if status_filter and status_filter != "ALL" and row_status != status_filter:
            continue
        if side_filter and row_side != side_filter:
            continue
        if symbol_filter and row_symbol != symbol_filter:
            continue

        data.append(ui_row)

    return jsonify({"status": "success", "data": data})


@dash_bp.route("/signals/download")
@login_required
def signals_download():
    """Download every signal visible to the current actor as a compact CSV."""
    actor = get_current_userid()
    if not actor:
        return jsonify({"status": "error", "reason": "not_authenticated"}), 401

    try:
        rows = _all_signal_ui_rows()

        if not is_operator(actor):
            user = UserSchema.fetch_user(actor)
            if not user:
                return jsonify({"status": "error", "reason": "user_not_found"}), 404
            rows = [row for row in rows if user.allowed_symbols(_safe_str(row.get("symbol"), ""))]

        headers = [
            "Side",
            "Symbol",
            "Setup",
            "Stage",
            "Status",
            "Created Price",
            "Created Time",
            "Last Price",
            "Last Time",
            "Closed Price",
            "Closed Time",
            "P&L %",
            "P&L / Share",
            "VWAP",
            "RSI",
            "BB",
        ]

        csv_rows = (
            [
                _safe_str(row.get("side"), ""),
                _safe_str(row.get("symbol"), ""),
                _safe_str(row.get("setup"), ""),
                _safe_str(row.get("stage"), ""),
                _safe_str(row.get("status"), ""),
                _csv_number(row.get("created_price")),
                _safe_str(row.get("first_seen_time"), ""),
                _csv_number(row.get("last_price")),
                _safe_str(row.get("last_eval_time"), ""),
                _csv_number(row.get("closed_price")),
                _safe_str(row.get("closed_time"), ""),
                _csv_number(row.get("last_pnl"), decimals=4),
                _csv_number(row.get("last_pnl_value")),
                _csv_number(row.get("vwap")),
                _csv_number(row.get("rsi")),
                _safe_str(row.get("bb_zone"), ""),
            ]
            for row in rows
        )

        return _csv_download(
            f"signals_{_download_timestamp()}.csv",
            headers,
            csv_rows,
        )
    except Exception as exc:
        logger.exception("signals_download failed: %s", exc)
        return jsonify({"status": "error", "reason": "signals_download_failed"}), 500


# =============================================================================
# ORDERS
# =============================================================================

@dash_bp.route("/orders")
@login_required
def orders():
    return render_template(
        "dash_orders.html",
        userid=get_current_userid() or "Guest",
        active="orders",
        nav_section="dashboard",
    )


@dash_bp.route("/orders/data")
@login_required
def orders_data():
    actor = get_current_userid()
    if not actor:
        return jsonify({"status": "error", "reason": "not_authenticated"}), 401

    requested_userids = _requested_userids_from_query()
    visible_userids = _fetch_managed_userids(actor)

    if is_operator(actor) and requested_userids:
        visible_set = {u.upper() for u in visible_userids}
        userids = [u for u in requested_userids if u.upper() in visible_set]
    else:
        userids = visible_userids

    if not userids:
        return jsonify({"status": "success", "bucket": "draft", "data": []})

    bucket = _safe_str(request.args.get("bucket"), "draft").strip().lower()
    limit = _safe_int(request.args.get("limit", 500), 500)

    if bucket not in ("draft", "executed"):
        return jsonify({
            "status": "error",
            "reason": "invalid_bucket",
            "details": {"bucket": bucket}
        }), 400

    try:
        with get_trades_db() as db:
            q = db.query(UserTradeORM).filter(UserTradeORM.userid.in_(userids))

            if bucket == "draft":
                rows = (
                    _filter_orders_draft(q)
                    .order_by(UserTradeORM.userid.asc(), UserTradeORM.id.desc())
                    .limit(limit)
                    .all()
                )
            else:
                rows = (
                    _filter_orders_executed(q)
                    .order_by(UserTradeORM.userid.asc(), UserTradeORM.id.desc())
                    .limit(limit)
                    .all()
                )

        data = [_to_orders_row(r) for r in rows]
        return jsonify({
            "status": "success",
            "bucket": bucket,
            "data": data
        })

    except Exception as e:
        logger.exception("orders_data failed bucket=%s: %s", bucket, e)
        return jsonify({"status": "error", "reason": "orders_data_failed"}), 500


@dash_bp.route("/orders/download")
@login_required
def orders_download():
    """Download all managed trade rows for the current/selected user scope."""
    actor = get_current_userid()
    if not actor:
        return jsonify({"status": "error", "reason": "not_authenticated"}), 401

    requested_userids = _requested_userids_from_query()
    visible_userids = _fetch_managed_userids(actor)

    if is_operator(actor) and requested_userids:
        visible_by_key = {userid.upper(): userid for userid in visible_userids}
        userids = [
            visible_by_key[userid.upper()]
            for userid in requested_userids
            if userid.upper() in visible_by_key
        ]
    else:
        userids = visible_userids

    headers = [
        "User",
        "Entry Plan",
        "Origin",
        "Symbol",
        "Type",
        "Side",
        "Entry",
        "Qty",
        "Last",
        "P&L",
        "Status",
    ]

    if not userids:
        return _csv_download(
            f"user_trades_{_download_timestamp()}.csv",
            headers,
            [],
        )

    try:
        with get_trades_db() as db:
            rows = (
                db.query(UserTradeORM)
                .filter(UserTradeORM.userid.in_(userids))
                .order_by(UserTradeORM.userid.asc(), UserTradeORM.id.desc())
                .all()
            )

        ui_rows = []
        for trade in rows:
            try:
                ui_rows.append(_to_orders_row(trade))
            except Exception:
                logger.exception(
                    "orders_download row conversion failed; continuing | trade_id=%s userid=%s symbol=%s",
                    getattr(trade, "id", None),
                    getattr(trade, "userid", None),
                    getattr(trade, "symbol", None),
                )

        csv_rows = (
            [
                _safe_str(row.get("userid"), ""),
                _safe_str(row.get("entry_plan_time") or row.get("date"), ""),
                _order_origin_label(row.get("origin")),
                _safe_str(row.get("symbol"), ""),
                " ".join(
                    part
                    for part in (
                        f"{_safe_str(row.get('instrument_type'), '')} / {_safe_str(row.get('product'), 'MIS')}",
                        _safe_str(row.get("execution_mode"), ""),
                    )
                    if part.strip()
                ).strip(),
                _safe_str(row.get("trade_type"), ""),
                _csv_number(row.get("entry_price")),
                _safe_int(row.get("quantity"), 0),
                _csv_number(row.get("last_price")),
                _csv_number(row.get("pnl_value")) if row.get("pnl_available") is True else "",
                _order_status_label(row.get("entry_status"), row.get("exit_status")),
            ]
            for row in ui_rows
        )

        return _csv_download(
            f"user_trades_{_download_timestamp()}.csv",
            headers,
            csv_rows,
        )
    except Exception as exc:
        logger.exception("orders_download failed userids=%s: %s", userids, exc)
        return jsonify({"status": "error", "reason": "orders_download_failed"}), 500


# =============================================================================
# POSITIONS
# =============================================================================

@dash_bp.route("/positions")
@login_required
def positions():
    return render_template(
        "dash_positions.html",
        userid=get_current_userid() or "Guest",
        active="positions",
        nav_section="dashboard",
    )


@dash_bp.route("/positions/data")
@login_required
def positions_data():
    actor = get_current_userid()
    if not actor:
        return jsonify({"status": "error", "reason": "not_authenticated"}), 401

    requested_userids = _requested_userids_from_query()
    visible_userids = _fetch_managed_userids(actor)

    if is_operator(actor) and requested_userids:
        visible_set = {u.upper() for u in visible_userids}
        userids = [u for u in requested_userids if u.upper() in visible_set]
    else:
        userids = visible_userids

    if not userids:
        return jsonify({"status": "success", "data": []})

    limit = _safe_int(request.args.get("limit", 1000), 1000)

    try:
        with get_trades_db() as db:
            q = db.query(UserTradeORM).filter(UserTradeORM.userid.in_(userids))

            rows = (
                _filter_positions(q)
                .order_by(UserTradeORM.userid.asc(), UserTradeORM.entry_time.asc(), UserTradeORM.id.asc())
                .limit(limit)
                .all()
            )

        trade_rows = [_to_position_trade_row(r) for r in rows]

        groups = {}
        for r in trade_rows:
            userid = _safe_str(r.get("userid"), "").strip()
            sym = _safe_str(r.get("symbol"), "").strip()
            equity_ref = _safe_str(r.get("equity_ref"), "").strip()
            instrument_type = _safe_str(r.get("instrument_type"), "").strip()
            product = _safe_str(r.get("product"), "").strip().upper()

            if not userid or not sym:
                continue

            execution_mode = _safe_str(r.get("execution_mode"), "VIRTUAL").strip().upper() or "VIRTUAL"

            # REAL and VIRTUAL trades are separate managed positions even when
            # user, symbol and instrument match. Mixing them would hide the
            # virtual position behind a broker-backed row (or vice versa).
            key = (userid, sym, equity_ref, instrument_type, product, execution_mode)
            groups.setdefault(key, []).append(r)

        summaries = []

        for (userid, sym, equity_ref, instrument_type, product, execution_mode), items in groups.items():

            def _sort_anchor_dt(it):
                return (
                    it.get("_entry_exec_time_dt")
                    or it.get("_entry_intent_time_dt")
                    or it.get("_entry_time_dt")
                    or it.get("_last_time_dt")
                    or datetime.min
                )

            items_sorted = sorted(items, key=_sort_anchor_dt)
            last_item = items_sorted[-1]

            buy_qty = 0
            sell_qty = 0
            total_pnl = 0.0
            weighted_sum = 0.0
            weighted_qty = 0

            latest_open_item = None
            exit_reason = ""

            buy_value = 0.0
            buy_weighted_qty = 0
            sell_value = 0.0
            sell_weighted_qty = 0

            total_open_qty = 0
            latest_close_dt = None

            for it in items_sorted:
                qty = _safe_int(it.get("quantity") or it.get("qty") or 0, 0)
                open_qty = _safe_int(it.get("open_qty") or 0, 0)
                side = _safe_str(it.get("trade_type"), "").upper()

                if side == "SELL":
                    sell_qty += qty
                else:
                    buy_qty += qty

                px = it.get("executed_entry_price")
                if px is None:
                    px = it.get("entry_price")

                px = _safe_float(px, None)

                if px is not None and px > 0 and qty > 0:
                    weighted_sum += px * qty
                    weighted_qty += qty

                    if side == "SELL":
                        sell_value += px * qty
                        sell_weighted_qty += qty
                    else:
                        buy_value += px * qty
                        buy_weighted_qty += qty

                total_pnl += _safe_float(it.get("pnl_value"), 0.0)

                if open_qty > 0:
                    total_open_qty += open_qty
                    latest_open_item = it

                exit_dt = it.get("_exit_time_dt")
                if exit_dt and (latest_close_dt is None or exit_dt > latest_close_dt):
                    latest_close_dt = exit_dt

            for it in reversed(items_sorted):
                er = _safe_str(it.get("exit_reason"), "").strip()
                if er:
                    exit_reason = er
                    break

            net_qty = buy_qty - sell_qty
            avg_price = round(weighted_sum / weighted_qty, 4) if weighted_qty > 0 else 0.0
            buy_avg = round(buy_value / buy_weighted_qty, 4) if buy_weighted_qty > 0 else 0.0
            sell_avg = round(sell_value / sell_weighted_qty, 4) if sell_weighted_qty > 0 else 0.0

            ref_item = latest_open_item or last_item

            status = "OPEN"
            if total_open_qty <= 0:
                status = "CLOSED"
            else:
                xs = _safe_str(ref_item.get("exit_status"), "").upper()
                if xs == "SUBMITTED":
                    status = "EXIT SUBMITTED"
                elif xs == "READY":
                    status = "EXIT READY"

            pos_side = "LONG" if buy_qty > sell_qty else ("SHORT" if sell_qty > buy_qty else "FLAT")
            is_exited = total_open_qty <= 0
            display_open_qty = total_open_qty

            summaries.append({
                "userid": userid,

                "symbol": sym,
                "equity_ref": equity_ref,
                "instrument_type": instrument_type,
                "product": product,

                "side": pos_side,

                "entry_time": ref_item.get("entry_time") or last_item.get("entry_time") or "",
                "last_time": ref_item.get("last_time") or last_item.get("last_time") or "",

                "net_qty": net_qty,
                "open_qty": display_open_qty,
                "avg_price": avg_price,

                "last_price": _safe_float(ref_item.get("last_price"), 0.0),

                "pnl": round(total_pnl, 2),

                "status": status,
                "exit_reason": exit_reason,

                "entry_status": _safe_str(ref_item.get("entry_status"), ""),
                "exit_status": _safe_str(ref_item.get("exit_status"), ""),
                "execution_mode": execution_mode,

                "buy_qty": buy_qty,
                "buy_avg": buy_avg,
                "buy_value": round(buy_value, 2),

                "sell_qty": sell_qty,
                "sell_avg": sell_avg,
                "sell_value": round(sell_value, 2),

                "trade_id": _safe_int(latest_open_item.get("id"), 0) if latest_open_item else 0,
                "id": _safe_int(latest_open_item.get("id"), 0) if latest_open_item else 0,

                "exited": is_exited,

                "_sort_dt": (
                    ref_item.get("_entry_exec_time_dt")
                    or ref_item.get("_entry_intent_time_dt")
                    or ref_item.get("_entry_time_dt")
                    or ref_item.get("_last_time_dt")
                ),
                "_close_dt": latest_close_dt,
            })

        effective_close_date = None
        closed_dates = [
            s["_close_dt"].date()
            for s in summaries
            if s.get("open_qty", 0) <= 0 and s.get("_close_dt") is not None
        ]
        if closed_dates:
            effective_close_date = max(closed_dates)

        out = []
        for s in summaries:
            open_qty = _safe_int(s.get("open_qty"), 0)
            close_dt = s.get("_close_dt")

            include = False
            if open_qty > 0:
                include = True
            elif close_dt is not None and effective_close_date is not None and close_dt.date() == effective_close_date:
                include = True

            if include:
                s.pop("_sort_dt", None)
                s.pop("_close_dt", None)
                out.append(s)

        out.sort(
            key=lambda x: (
                x.get("userid") or "",
                x.get("symbol") or "",
                x.get("instrument_type") or "",
                x.get("last_time") or "",
            ),
            reverse=False
        )

        return jsonify({
            "status": "success",
            "data": out
        })

    except Exception as e:
        logger.exception("positions_data failed: %s", e)
        return jsonify({
            "status": "error",
            "reason": "positions_data_failed"
        }), 500

# =============================================================================
# PERFORMANCE
# =============================================================================

@dash_bp.route("/performance")
@login_required
def performance():
    return render_template(
        "dash_performance.html",
        userid=get_current_userid() or "Guest",
        active="performance",
        nav_section="dashboard",
    )


@dash_bp.route("/performance/data")
@login_required
def performance_data():
    actor = get_current_userid()
    if not actor:
        return jsonify({"status": "error", "reason": "not_authenticated"}), 401

    requested_userids = _requested_userids_from_query()
    visible_userids = _fetch_managed_userids(actor)

    if is_operator(actor) and requested_userids:
        visible_set = {u.upper() for u in visible_userids}
        userids = [u for u in requested_userids if u.upper() in visible_set]
    else:
        userids = visible_userids

    mode = _safe_str(request.args.get("mode"), "all").strip().lower()
    if mode not in ("all", "real", "virtual"):
        mode = "all"

    execution_mode = None
    if mode == "real":
        execution_mode = "REAL"
    elif mode == "virtual":
        execution_mode = "VIRTUAL"

    date_from = _safe_str(request.args.get("date_from"), "").strip() or None
    date_to = _safe_str(request.args.get("date_to"), "").strip() or None
    limit = _safe_int(request.args.get("limit", 5000), 5000)

    try:
        rows = UserTradeSchema.get_perf_summary_for_users(
            userids=userids,
            execution_mode=execution_mode,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
        ) or []

        return jsonify({
            "status": "success",
            "mode": mode,
            "execution_mode": execution_mode or "ALL",
            "userids": userids,
            "all_users": bool(is_operator(actor) and not requested_userids),
            "data": rows,
        }), 200

    except Exception as e:
        logger.exception("performance_data failed: %s", e)
        return jsonify({"status": "error", "reason": "performance_data_failed"}), 500


# =============================================================================
# DERIVATIVES
# =============================================================================

@dash_bp.route("/derivatives")
@login_required
def dash_derivatives():
    return render_template(
        "dash_derivatives.html",
        active="derivatives",
        nav_section="dashboard",
    )


@dash_bp.route("/derivatives/data")
@login_required
def dash_derivatives_data():
    if DerivativesChainSchema is None:
        return jsonify({"status": "error", "reason": "DerivativesChainSchema_not_available"}), 500

    symbol = (request.args.get("symbol") or "").strip().upper()
    asof = (request.args.get("asof") or "").strip()

    if not symbol:
        return jsonify({"status": "error", "reason": "missing_symbol"}), 400

    asof_dt = None
    if asof:
        try:
            asof_dt = datetime.fromisoformat(asof.replace("Z", "+00:00"))
            asof_dt = asof_dt.astimezone(IST) if asof_dt.tzinfo else asof_dt.replace(tzinfo=IST)
        except Exception:
            asof_dt = None

    if asof_dt is None:
        asof_dt = datetime.now(IST)

    def _load_json_field(obj, attr):
        if not obj:
            return {}
        val = getattr(obj, attr, None) or {}
        if isinstance(val, str):
            try:
                val = json.loads(val) or {}
            except Exception:
                val = {}
        return val if isinstance(val, dict) else {}

    try:
        rec = DerivativesChainSchema.fetch_latest_today_for_symbol_before_time(symbol, asof_dt)

        if not rec:
            return jsonify({"status": "success", "data": {}})

        raw = _load_json_field(rec, "raw")
        derived = _load_json_field(rec, "derived")

        snap = getattr(rec, "snapshot_time", None)
        if isinstance(snap, datetime):
            snap = snap.astimezone(IST) if snap.tzinfo else snap.replace(tzinfo=IST)

        chain = {
            "spot_price": raw.get("spot_price"),
            "future": raw.get("future"),
            "options": raw.get("options", {}),
            **(derived or {}),
        }

        return jsonify({
            "status": "success",
            "data": {
                "symbol": symbol,
                "snapshot_time": snap.isoformat() if isinstance(snap, datetime) else None,
                "chain": chain,
            }
        })

    except Exception as e:
        logger.exception("derivatives_data failed: %s", e)
        return jsonify({"status": "error", "reason": "derivatives_fetch_failed"}), 500


# =============================================================================
# NO CACHE
# =============================================================================

@dash_bp.after_app_request
def add_no_cache_headers(resp):
    if request.path.startswith("/static/"):
        return resp
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0, private"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    resp.headers["Vary"] = "Cookie"
    return resp