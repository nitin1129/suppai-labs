"""Member side: Diagnose → Lab tests, booking, and Track → Tests.

Member tokens come from the main SuppAI auth (role = member, sub = member id), signed with the shared JWT secret.

Temporary rule until labs manage their own tests (Tests & prices tab):
every active lab offers the whole shared catalogue (catalog_tests) at catalogue prices.
"""
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.db import get_db
from app.deps import Principal, current_member
from app.schemas import MEMBER_LABEL, OrderCreate, OrderStatus, PaymentMethod
from app.security import local_now, local_today, now
from app.services import orders as flow
from app.services.common import new_id, next_number
from app.services.storage import get_storage

router = APIRouter(tags=["Member: lab tests"])

SLOT_MINUTES = 30
MEMBER_CANCEL_BEFORE = {OrderStatus.received, OrderStatus.confirmed}


async def _labs_for_pincode(db, pincode: str) -> list[dict]:
    lab_ids = [a["lab_id"] async for a in db.lab_service_areas.find({"pincode": pincode})]
    if not lab_ids:
        return []
    return [l async for l in db.labs.find({"_id": {"$in": lab_ids}, "status": "active"})]


def _slots(lab: dict, day: date) -> list[str]:
    hours = [h for h in lab.get("opening_hours", []) if h["day"] == day.weekday()]
    out = []
    for h in hours:
        t = datetime.combine(day, datetime.strptime(h["opens"], "%H:%M").time())
        end = datetime.combine(day, datetime.strptime(h["closes"], "%H:%M").time())
        while t + timedelta(minutes=SLOT_MINUTES) <= end:
            if t > local_now():
                out.append(t.strftime("%H:%M"))
            t += timedelta(minutes=SLOT_MINUTES)
    return out


async def _is_paused(db, lab_id: str, day: date) -> bool:
    return bool(await db.lab_leave.find_one({"lab_id": lab_id, "date": day.isoformat()}))


@router.get("/tests")
async def tests_near_me(pincode: str = Query(..., pattern=r"^[1-9][0-9]{5}$"), _: Principal = Depends(current_member)):
    db = get_db()
    labs = await _labs_for_pincode(db, pincode)
    catalogue = [t async for t in db.catalog_tests.find({"active": True}).sort("name", 1)]
    lab_cards = [
        {"lab_id": l["_id"], "name": l["name"], "nabl_accredited": l.get("nabl_accredited", False), "home_collection": l.get("home_collection", False)}
        for l in labs
    ]
    return [
        {
            "code": t["_id"],
            "name": t["name"],
            "parameters": t.get("parameters"),
            "fasting": t.get("fasting", False),
            "sample_type": t.get("sample_type"),
            "labs": [
                {**card, "price": t["price"], "mrp": t.get("mrp"), "report_hours": t.get("report_hours"),
                 "home_collection": card["home_collection"] and t.get("home_collection", True)}
                for card in lab_cards
            ],
        }
        for t in catalogue
    ]


@router.get("/labs/{lab_id}/slots")
async def slots(lab_id: str, day: date = Query(..., alias="date"), _: Principal = Depends(current_member)):
    db = get_db()
    lab = await db.labs.find_one({"_id": lab_id, "status": "active"})
    if not lab:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lab not found")
    if day < local_today() or await _is_paused(db, lab_id, day):
        return {"date": day.isoformat(), "slots": []}
    return {"date": day.isoformat(), "slots": _slots(lab, day)}


@router.post("/orders", status_code=status.HTTP_201_CREATED)
async def book(body: OrderCreate, member: Principal = Depends(current_member)):
    db = get_db()
    lab = await db.labs.find_one({"_id": body.lab_id, "status": "active"})
    if not lab:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lab not found")
    if not await db.lab_service_areas.find_one({"lab_id": body.lab_id, "pincode": body.pincode}):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "This lab does not cover your pincode")
    test = await db.catalog_tests.find_one({"_id": body.test_code, "active": True})
    if not test:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Test not found")
    if body.home_collection:
        if not (lab.get("home_collection") and test.get("home_collection", True)):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Home collection is not available for this test at this lab")
        if not body.address:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Address is required for home collection")
    if await _is_paused(db, body.lab_id, body.slot_date):
        raise HTTPException(status.HTTP_409_CONFLICT, "The lab is closed on this date")
    if body.slot_time not in _slots(lab, body.slot_date):
        raise HTTPException(status.HTTP_409_CONFLICT, "This slot is not available")

    t = now()
    order = {
        "_id": new_id(),
        "reference": f"SA-TST-{await next_number(db, 'order', start=100000)}",
        "member_id": member.id,
        "lab_id": body.lab_id,
        "lab_name": lab["name"],
        "test": {"code": test["_id"], "name": test["name"], "parameters": test.get("parameters"),
                 "fasting": test.get("fasting", False), "sample_type": test.get("sample_type"),
                 "report_hours": test.get("report_hours")},
        "patient": body.patient.model_dump(),
        "home_collection": body.home_collection,
        "address": body.address.model_dump() if body.address else None,
        "pincode": body.pincode,
        "slot_date": body.slot_date.isoformat(),
        "slot_time": body.slot_time,
        "amount": test["price"],
        "mrp": test.get("mrp"),
        # Online payments are marked paid by the Razorpay webhook (next phase). Cash is marked paid on collection.
        "payment": {"method": body.payment_method.value, "status": "pending"},
        "status": OrderStatus.received.value,
        "timeline": {OrderStatus.received.value: t},
        "created_at": t,
        "updated_at": t,
    }
    await db.orders.insert_one(order)
    await flow.record_event(db, order, OrderStatus.received, by=f"member:{member.id}")
    return flow.order_out(order, for_member=True)


async def _my_order(member: Principal, order_id: str) -> dict:
    order = await get_db().orders.find_one({"_id": order_id, "member_id": member.id})
    if not order:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found")
    return order


@router.get("/orders")
async def my_orders(member: Principal = Depends(current_member)):
    cursor = get_db().orders.find({"member_id": member.id}).sort("created_at", -1)
    return [flow.order_out(o, for_member=True) async for o in cursor]


@router.get("/orders/{order_id}")
async def track(order_id: str, member: Principal = Depends(current_member)):
    order = await _my_order(member, order_id)
    events = [e async for e in get_db().order_events.find({"order_id": order_id}).sort("at", 1)]
    out = flow.order_out(order, for_member=True)
    out["events"] = [{"status": e["status"], "label": MEMBER_LABEL[OrderStatus(e["status"])], "at": e["at"]} for e in events]
    return out


@router.get("/orders/{order_id}/report")
async def report_link(order_id: str, member: Principal = Depends(current_member)):
    order = await _my_order(member, order_id)
    if order["status"] != OrderStatus.result_published.value or not order.get("report_key"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Report is not ready yet")
    await get_db().audit_log.insert_one(
        {"_id": new_id(), "actor": f"member:{member.id}", "action": "report.downloaded", "target": order_id, "at": now()}
    )
    return {"url": get_storage().signed_url(order["report_key"])}


@router.post("/orders/{order_id}/cancel")
async def member_cancel(order_id: str, member: Principal = Depends(current_member)):
    order = await _my_order(member, order_id)
    if OrderStatus(order["status"]) not in MEMBER_CANCEL_BEFORE:
        raise HTTPException(status.HTTP_409_CONFLICT, "This booking can no longer be cancelled online. Contact the lab.")
    updated = await flow.move(get_db(), order, OrderStatus.cancelled, by=f"member:{member.id}", reason="Cancelled by member")
    return flow.order_out(updated, for_member=True)
