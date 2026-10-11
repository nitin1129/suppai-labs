"""One orders collection feeds both the lab dashboard and the member tracker.

Rules:
- Only forward moves, one stage at a time: received → confirmed → sample_collected → in_lab → result_published.
- Cancel is allowed from any stage before result_published.
- result_published needs a report PDF already uploaded.
- Every move is written to order_events (time + who); tracker dates come from there.
- Every move queues a member notification.
"""
from fastapi import HTTPException, status

from app.schemas import FORWARD_FLOW, MEMBER_LABEL, OrderStatus, PaymentMethod
from app.security import now
from app.services.common import new_id, notify

NOTIFY_LAB_ON = {OrderStatus.received}


def next_status(current: OrderStatus) -> OrderStatus | None:
    if current not in FORWARD_FLOW:
        return None
    i = FORWARD_FLOW.index(current)
    return FORWARD_FLOW[i + 1] if i + 1 < len(FORWARD_FLOW) else None


def allowed_moves(current: OrderStatus) -> list[OrderStatus]:
    moves = []
    if nxt := next_status(current):
        moves.append(nxt)
    if current in FORWARD_FLOW and current != OrderStatus.result_published:
        moves.append(OrderStatus.cancelled)
    return moves


async def record_event(db, order: dict, to: OrderStatus, by: str, extra: dict | None = None) -> None:
    await db.order_events.insert_one(
        {"_id": new_id(), "order_id": order["_id"], "status": to.value, "at": now(), "by": by, **(extra or {})}
    )
    data = {"reference": order["reference"], "status": MEMBER_LABEL[to], "test": order["test"]["name"]}
    await notify(db, {"member_id": order["member_id"]}, f"order_{to.value}", data)
    if to in NOTIFY_LAB_ON:
        await notify(db, {"lab_id": order["lab_id"]}, "lab_new_order", data)


async def move(db, order: dict, to: OrderStatus, by: str, collector_name: str | None = None, reason: str | None = None) -> dict:
    current = OrderStatus(order["status"])
    if to not in allowed_moves(current):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Cannot move from {current.value} to {to.value}. Allowed: {[m.value for m in allowed_moves(current)] or 'none'}",
        )

    t = now()
    update: dict = {"status": to.value, "updated_at": t, f"timeline.{to.value}": t}
    extra: dict = {}

    if to == OrderStatus.sample_collected:
        if not collector_name:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "collector_name is required")
        update["collected_by"] = collector_name
        extra["collector_name"] = collector_name
        if order["payment"]["method"] == PaymentMethod.cash.value:
            update["payment.status"] = "paid"
            update["payment.paid_at"] = t
    elif to == OrderStatus.in_lab:
        update["tat_started_at"] = t
    elif to == OrderStatus.result_published:
        if not order.get("report_key"):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Upload the report PDF before publishing")
        update["tat_hours"] = round((t - _aware(order["tat_started_at"])).total_seconds() / 3600, 1) if order.get("tat_started_at") else None
    elif to == OrderStatus.cancelled:
        update["cancel_reason"] = reason
        update["cancelled_by"] = by
        extra["reason"] = reason
        if order["payment"]["status"] == "paid" and order["payment"]["method"] == PaymentMethod.online.value:
            update["payment.refund_status"] = "pending"

    # Guard on the current status so two clicks can't both move the order.
    result = await db.orders.update_one({"_id": order["_id"], "status": current.value}, {"$set": update})
    if result.modified_count == 0:
        raise HTTPException(status.HTTP_409_CONFLICT, "Order was changed by someone else. Refresh and try again.")
    await record_event(db, order, to, by, extra)
    return await db.orders.find_one({"_id": order["_id"]})


def _aware(dt):
    from datetime import timezone

    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def order_out(order: dict, for_member: bool = False) -> dict:
    current = OrderStatus(order["status"])
    out = {
        "id": order["_id"],
        "reference": order["reference"],
        "status": current.value,
        "status_label": MEMBER_LABEL[current],
        "lab_id": order["lab_id"],
        "lab_name": order.get("lab_name"),
        "test": order["test"],
        "patient": order["patient"],
        "home_collection": order["home_collection"],
        "address": order.get("address"),
        "pincode": order["pincode"],
        "slot_date": order["slot_date"],
        "slot_time": order["slot_time"],
        "amount": order["amount"],
        "payment": order["payment"],
        "timeline": order.get("timeline", {}),
        "has_report": bool(order.get("report_key")),
        "created_at": order["created_at"],
    }
    if not for_member:
        out["next_actions"] = [m.value for m in allowed_moves(current)]
        out["collected_by"] = order.get("collected_by")
    return out
