"""Lab dashboard (/lab). Every query is scoped by lab_id taken from the token, never from the request."""
from collections import Counter
from datetime import date

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status

from app.config import get_settings
from app.db import get_db
from app.deps import Principal, current_lab
from app.schemas import FORWARD_FLOW, LeaveRequest, OrderStatus, StatusChange
from app.security import local_now, local_today, now
from app.services import orders as flow
from app.services.common import new_id
from app.services.storage import get_storage

router = APIRouter(prefix="/lab", tags=["Lab dashboard"])

ACTIVE = [s.value for s in FORWARD_FLOW if s != OrderStatus.result_published]


async def _order(lab: Principal, order_id: str) -> dict:
    order = await get_db().orders.find_one({"_id": order_id, "lab_id": lab.lab_id})
    if not order:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found")
    return order


@router.get("/me")
async def me(lab: Principal = Depends(current_lab)):
    db = get_db()
    lab_doc = await db.labs.find_one({"_id": lab.lab_id}, {"bank": 0})
    user = await db.lab_users.find_one({"_id": lab.id}, {"username": 1, "email": 1})
    pincodes = [a["pincode"] async for a in db.lab_service_areas.find({"lab_id": lab.lab_id})]
    lab_doc["id"] = lab_doc.pop("_id")
    return {"lab": lab_doc, "user": {"username": user["username"], "email": user["email"]}, "pincodes_covered": sorted(pincodes)}


@router.get("/today")
async def today(lab: Principal = Depends(current_lab)):
    db, d = get_db(), local_today().isoformat()
    todays = [o async for o in db.orders.find({"lab_id": lab.lab_id, "slot_date": d}).sort("slot_time", 1)]
    in_lab = await db.orders.count_documents({"lab_id": lab.lab_id, "status": OrderStatus.in_lab.value})
    awaiting = await db.orders.count_documents({"lab_id": lab.lab_id, "status": OrderStatus.received.value})
    return {
        "date": d,
        "paused": bool(await db.lab_leave.find_one({"lab_id": lab.lab_id, "date": d})),
        "counts": {
            "awaiting_confirm": awaiting,
            "in_lab": in_lab,
            "home_visits": sum(1 for o in todays if o["home_collection"] and o["status"] in ("received", "confirmed")),
            "collected": sum(1 for o in todays if o["status"] == OrderStatus.sample_collected.value),
        },
        "orders": [flow.order_out(o) for o in todays],
    }


@router.get("/orders")
async def list_orders(
    status_: OrderStatus | None = Query(None, alias="status"),
    date_: date | None = Query(None, alias="date"),
    q: str | None = Query(None, description="Search reference, patient name or test"),
    lab: Principal = Depends(current_lab),
):
    query: dict = {"lab_id": lab.lab_id}
    if status_:
        query["status"] = status_.value
    if date_:
        query["slot_date"] = date_.isoformat()
    orders = [o async for o in get_db().orders.find(query).sort([("slot_date", -1), ("slot_time", -1)]).limit(500)]
    if q:
        needle = q.lower()
        orders = [
            o for o in orders
            if needle in o["reference"].lower() or needle in o["patient"]["name"].lower() or needle in o["test"]["name"].lower()
        ]
    return [flow.order_out(o) for o in orders]


@router.patch("/orders/{order_id}/status")
async def change_status(order_id: str, body: StatusChange, lab: Principal = Depends(current_lab)):
    order = await _order(lab, order_id)
    updated = await flow.move(get_db(), order, body.to, by=f"lab_user:{lab.id}", collector_name=body.collector_name, reason=body.reason)
    return flow.order_out(updated)


@router.post("/orders/{order_id}/report")
async def upload_report(order_id: str, file: UploadFile = File(...), lab: Principal = Depends(current_lab)):
    order = await _order(lab, order_id)
    if order["status"] != OrderStatus.in_lab.value:
        raise HTTPException(status.HTTP_409_CONFLICT, "Reports can be uploaded only while the order is in lab")
    if file.content_type != "application/pdf":
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Upload the report as a PDF")
    data = await file.read()
    if not data.startswith(b"%PDF"):
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "File is not a valid PDF")
    if len(data) > get_settings().max_upload_mb * 1024 * 1024:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "File is too large")
    storage = get_storage()
    key = await storage.save(f"reports/{lab.lab_id}/{order_id}", file.filename or "report.pdf", data, "application/pdf")
    if order.get("report_key"):  # re-upload replaces the earlier PDF
        await storage.delete(order["report_key"])
    await get_db().orders.update_one(
        {"_id": order_id}, {"$set": {"report_key": key, "report_uploaded_at": now(), "report_uploaded_by": lab.id}}
    )
    return {"id": order_id, "has_report": True}


@router.get("/leave")
async def list_leave(lab: Principal = Depends(current_lab)):
    cursor = get_db().lab_leave.find({"lab_id": lab.lab_id, "date": {"$gte": local_today().isoformat()}}).sort("date", 1)
    return [{"date": l["date"], "reason": l.get("reason")} async for l in cursor]


@router.post("/leave", status_code=status.HTTP_201_CREATED)
async def pause_date(body: LeaveRequest, lab: Principal = Depends(current_lab)):
    """Pause a date so members cannot book it.

    If orders are already booked that day, the pause is refused and those orders are returned,
    so the lab can call the members and cancel or move them first.
    """
    db, d = get_db(), body.date.isoformat()
    if body.date < local_today():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Date is in the past")
    booked = [o async for o in db.orders.find({"lab_id": lab.lab_id, "slot_date": d, "status": {"$in": ACTIVE}})]
    if booked:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"message": f"{len(booked)} order(s) are booked on {d}. Cancel or reschedule them first.",
             "orders": [o["reference"] for o in booked]},
        )
    await db.lab_leave.update_one(
        {"lab_id": lab.lab_id, "date": d},
        {"$setOnInsert": {"_id": new_id(), "created_at": now(), "created_by": lab.id}, "$set": {"reason": body.reason}},
        upsert=True,
    )
    return {"date": d, "paused": True}


@router.delete("/leave/{day}")
async def unpause_date(day: date, lab: Principal = Depends(current_lab)):
    await get_db().lab_leave.delete_one({"lab_id": lab.lab_id, "date": day.isoformat()})
    return {"date": day.isoformat(), "paused": False}


@router.get("/reports/summary")
async def summary(
    from_: date | None = Query(None, alias="from"),
    to: date | None = Query(None),
    lab: Principal = Depends(current_lab),
):
    query: dict = {"lab_id": lab.lab_id}
    if from_ or to:
        query["slot_date"] = {}
        if from_:
            query["slot_date"]["$gte"] = from_.isoformat()
        if to:
            query["slot_date"]["$lte"] = to.isoformat()
    orders = [o async for o in get_db().orders.find(query)]
    live = [o for o in orders if o["status"] != OrderStatus.cancelled.value]
    paid = [o for o in live if o["payment"]["status"] == "paid"]
    tats = [o["tat_hours"] for o in live if o.get("tat_hours") is not None]
    return {
        "orders": len(orders),
        "cancelled": len(orders) - len(live),
        "revenue": sum(o["amount"] for o in live),
        "collected": sum(o["amount"] for o in paid),
        "pending": sum(o["amount"] for o in live if o["payment"]["status"] != "paid"),
        "avg_turnaround_hours": round(sum(tats) / len(tats), 1) if tats else None,
        "payment_methods": dict(Counter(o["payment"]["method"] for o in live)),
        "top_tests": [{"name": n, "orders": c} for n, c in Counter(o["test"]["name"] for o in live).most_common(5)],
    }
