"""End-to-end: lab applies → admin accepts → lab sets password → member books → lab moves the order → member gets the report."""
from datetime import timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from mongomock_motor import AsyncMongoMockClient

from app import db as dbmod
from app.main import app
from app.security import create_access_token, hash_password, local_today
from app.services import storage as storagemod

ADDRESS = {"line1": "Main Road", "city": "Ranchi", "state": "Jharkhand", "pincode": "834001"}
APPLICATION = {
    "centre": {
        "legal_name": "Apex Diagnostics Pvt Ltd", "entity_type": "Private Limited", "contact_phone": "9876543210",
        "official_email": "ops@apexdiagnostics.in", "registered_address": ADDRESS, "centre_address": ADDRESS,
    },
    "signatory": {"name": "R Kumar", "designation": "Director", "mobile": "9876543210",
                  "email": "r@apexdiagnostics.in", "pan": "abcde1234f", "aadhaar_last4": "1234"},
    "regulatory": {"clinical_registration_no": "JH/CE/123", "director_name": "R Kumar",
                   "pathologist_name": "Dr S Rao", "pathologist_registration_no": "MCI-998"},
    "service": {
        "home_collection": True, "pincodes_covered": ["834001", "834002"],
        "opening_hours": [{"day": d, "opens": "07:00", "closes": "20:00"} for d in range(7)],
        "bank": {"account_holder": "Apex Diagnostics", "account_number": "123456789012", "ifsc": "SBIN0001234", "bank_name": "SBI"},
    },
    "declarations": {"accepted_terms": True, "information_true": True, "signed_name": "R Kumar"},
}
PDF = b"%PDF-1.4 test report"


class FakeBucket:
    """Stands in for AsyncIOMotorGridFSBucket (the in-memory test database has no GridFS)."""

    def __init__(self):
        self.files = {}

    async def upload_from_stream(self, filename, data, metadata=None):
        from bson import ObjectId

        fid = ObjectId()
        self.files[fid] = (filename, data, metadata)
        return fid

    async def open_download_stream(self, fid):
        from gridfs.errors import NoFile

        if fid not in self.files:
            raise NoFile(fid)
        filename, data, metadata = self.files[fid]

        class Stream:
            async def read(self_):
                return data

        st = Stream()
        st.filename, st.metadata = filename, metadata
        return st

    async def delete(self, fid):
        from gridfs.errors import NoFile

        if self.files.pop(fid, None) is None:
            raise NoFile(fid)


@pytest_asyncio.fixture
async def client(tmp_path):
    mock = AsyncMongoMockClient()["test"]
    dbmod.set_database(None, mock)  # no client → transactions disabled
    storagemod.set_storage(storagemod.GridFSStorage(FakeBucket()))
    await mock.admin_users.insert_one({"_id": "admin1", "email": "admin@suppai.in", "password_hash": hash_password("adminpass123")})
    await mock.catalog_tests.insert_one({"_id": "THY-3", "name": "Thyroid Profile", "parameters": 3, "price": 399, "mrp": 600,
                                         "fasting": False, "sample_type": "blood", "report_hours": 24, "home_collection": True, "active": True})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        c.db = mock
        yield c


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


async def apply(client) -> tuple[str, str]:
    r = await client.post("/labs/applications", json={"centre": APPLICATION["centre"]})
    assert r.status_code == 201
    app_id, token = r.json()["id"], r.json()["draft_token"]
    h = {"X-Draft-Token": token}

    r = await client.post(f"/labs/applications/{app_id}/submit", headers=h)
    assert r.status_code == 422 and "signatory" in r.json()["detail"]["missing"]

    rest = {k: v for k, v in APPLICATION.items() if k != "centre"}
    assert (await client.patch(f"/labs/applications/{app_id}", json=rest, headers=h)).status_code == 200
    for kind in ("clinical_registration", "pan_card", "cancelled_cheque"):
        r = await client.post(f"/labs/applications/{app_id}/documents", data={"kind": kind},
                              files={"file": (f"{kind}.pdf", PDF, "application/pdf")}, headers=h)
        assert r.status_code == 201
    r = await client.post(f"/labs/applications/{app_id}/submit", headers=h)
    assert r.status_code == 200 and r.json()["reference"].startswith("SA-LAB-")
    return app_id, token


async def accept_and_login(client, app_id) -> str:
    admin = (await client.post("/auth/admin/login", json={"username": "admin@suppai.in", "password": "adminpass123"})).json()["access_token"]
    r = await client.get("/admin/labs/applications?status=submitted", headers=bearer(admin))
    assert [a["id"] for a in r.json()] == [app_id]
    r = await client.get(f"/admin/labs/applications/{app_id}", headers=bearer(admin))
    assert all(d["url"].startswith("http") for d in r.json()["documents"])

    r = await client.post(f"/admin/labs/applications/{app_id}/accept", headers=bearer(admin))
    assert r.status_code == 200
    body = r.json()
    assert body["username"] == "apex.diagnostics.ops" and "password" not in body

    # Can't log in before setting a password.
    r = await client.post("/auth/lab/login", json={"username": "apex.diagnostics.ops", "password": "anything"})
    assert r.status_code == 403

    invite = await client.db.notifications.find_one({"template": "lab_invite"})
    token = invite["data"]["link"].split("token=")[1]
    assert (await client.post("/auth/lab/set-password", json={"token": token, "password": "short"})).status_code == 422
    r = await client.post("/auth/lab/set-password", json={"token": token, "password": "ApexLab2026"})
    assert r.status_code == 200
    # Link is single-use.
    assert (await client.post("/auth/lab/set-password", json={"token": token, "password": "ApexLab2026"})).status_code == 400

    r = await client.post("/auth/lab/login", json={"username": "apex.diagnostics.ops", "password": "ApexLab2026"})
    assert r.status_code == 200
    return r.json()["access_token"]


@pytest.mark.asyncio
async def test_full_flow(client):
    app_id, _ = await apply(client)
    lab_token = await accept_and_login(client, app_id)
    lab_id = (await client.get("/lab/me", headers=bearer(lab_token))).json()["lab"]["id"]

    member = create_access_token("member42", "member")
    r = await client.get("/tests?pincode=834001", headers=bearer(member))
    assert r.json()[0]["labs"][0]["lab_id"] == lab_id
    assert (await client.get("/tests?pincode=110001", headers=bearer(member))).json()[0]["labs"] == []

    tomorrow = local_today() + timedelta(days=1)
    order = {
        "lab_id": lab_id, "test_code": "THY-3", "pincode": "834001", "address": ADDRESS, "home_collection": True,
        "patient": {"name": "Sita Devi", "age": 52, "gender": "F", "mobile": "9123456780"},
        "slot_date": tomorrow.isoformat(), "slot_time": "09:00", "payment_method": "cash",
    }
    r = await client.post("/orders", json=order, headers=bearer(member))
    assert r.status_code == 201, r.text
    oid = r.json()["id"]

    # Lab can't pause a day that already has bookings.
    assert (await client.post("/lab/leave", json={"date": tomorrow.isoformat()}, headers=bearer(lab_token))).status_code == 409

    move = lambda to, **kw: client.patch(f"/lab/orders/{oid}/status", json={"to": to, **kw}, headers=bearer(lab_token))
    assert (await move("in_lab")).status_code == 409  # no skipping
    assert (await move("confirmed")).status_code == 200
    assert (await move("sample_collected")).status_code == 422  # collector required
    r = await move("sample_collected", collector_name="Ravi")
    assert r.json()["payment"]["status"] == "paid"  # cash paid on collection
    assert (await move("in_lab")).status_code == 200
    assert (await move("result_published")).status_code == 422  # report needed

    r = await client.post(f"/lab/orders/{oid}/report", files={"file": ("r.pdf", PDF, "application/pdf")}, headers=bearer(lab_token))
    assert r.status_code == 200
    assert (await move("result_published")).status_code == 200
    assert (await move("cancelled")).status_code == 409  # can't cancel after result

    r = await client.get(f"/orders/{oid}", headers=bearer(member))
    assert [e["label"] for e in r.json()["events"]] == [
        "Booking placed", "Booking confirmed", "Sample collected", "Processing in lab", "Report published"]
    url = (await client.get(f"/orders/{oid}/report", headers=bearer(member))).json()["url"]
    r = await client.get(url.replace("http://localhost:8000", ""))
    assert r.status_code == 200 and r.content == PDF

    summary = (await client.get("/lab/reports/summary", headers=bearer(lab_token))).json()
    assert summary["revenue"] == 399 and summary["collected"] == 399 and summary["top_tests"][0]["name"] == "Thyroid Profile"


@pytest.mark.asyncio
async def test_isolation_and_rules(client):
    app_id, _ = await apply(client)
    lab_token = await accept_and_login(client, app_id)
    lab_id = (await client.get("/lab/me", headers=bearer(lab_token))).json()["lab"]["id"]

    # Another lab's token can't see this lab's orders.
    other = create_access_token("u2", "lab", lab_id="some-other-lab")
    member = create_access_token("member42", "member")
    day = local_today() + timedelta(days=2)

    # Paused date can't be booked.
    assert (await client.post("/lab/leave", json={"date": day.isoformat()}, headers=bearer(lab_token))).status_code == 201
    order = {
        "lab_id": lab_id, "test_code": "THY-3", "pincode": "834002", "home_collection": False,
        "patient": {"name": "A", "age": 30, "gender": "M", "mobile": "9123456780"},
        "slot_date": day.isoformat(), "slot_time": "10:00", "payment_method": "online",
    }
    assert (await client.post("/orders", json=order, headers=bearer(member))).status_code == 409
    order["slot_date"] = (day + timedelta(days=1)).isoformat()
    oid = (await client.post("/orders", json=order, headers=bearer(member))).json()["id"]

    assert (await client.patch(f"/lab/orders/{oid}/status", json={"to": "confirmed"}, headers=bearer(other))).status_code == 404
    assert (await client.get(f"/orders/{oid}", headers=bearer(create_access_token("someone", "member")))).status_code == 404
    assert (await client.get("/lab/orders", headers=bearer(member))).status_code == 403

    # Member can cancel before collection.
    r = await client.post(f"/orders/{oid}/cancel", headers=bearer(member))
    assert r.json()["status"] == "cancelled"

    # Lockout after repeated failures.
    for _ in range(5):
        await client.post("/auth/lab/login", json={"username": "apex.diagnostics.ops", "password": "wrong-pass-1"})
    r = await client.post("/auth/lab/login", json={"username": "apex.diagnostics.ops", "password": "ApexLab2026"})
    assert r.status_code == 429


@pytest.mark.asyncio
async def test_hold_reject_and_duplicate_username(client):
    admin = (await client.post("/auth/admin/login", json={"username": "admin@suppai.in", "password": "adminpass123"})).json()["access_token"]
    first, _ = await apply(client)
    second, token2 = await apply(client)

    assert (await client.post(f"/admin/labs/applications/{second}/hold", json={"action": "hold", "note": "no"}, headers=bearer(admin))).status_code == 422
    r = await client.post(f"/admin/labs/applications/{second}/hold", json={"action": "hold", "note": "NABL certificate is unreadable"}, headers=bearer(admin))
    assert r.json()["status"] == "on_hold"
    # A held application can be edited and resubmitted with the same reference.
    h = {"X-Draft-Token": token2}
    ref = (await client.get(f"/labs/applications/{second}", headers=h)).json()["reference"]
    assert (await client.patch(f"/labs/applications/{second}", json={}, headers=h)).status_code == 200
    r = await client.post(f"/labs/applications/{second}/submit", headers=h)
    assert r.json()["reference"] == ref

    assert (await client.post(f"/admin/labs/applications/{first}/accept", headers=bearer(admin))).json()["username"] == "apex.diagnostics.ops"
    assert (await client.post(f"/admin/labs/applications/{second}/accept", headers=bearer(admin))).json()["username"] == "apex.diagnostics2.ops"

    third, _ = await apply(client)
    await client.post(f"/admin/labs/applications/{third}/hold", json={"action": "reject", "note": "Not a registered lab"}, headers=bearer(admin))
    assert (await client.post(f"/admin/labs/applications/{third}/accept", headers=bearer(admin))).status_code == 409
    actions = {a["action"] async for a in client.db.audit_log.find()}
    assert {"application.accepted", "application.hold", "application.reject"} <= actions


@pytest.mark.asyncio
async def test_documents_in_gridfs(client):
    app_id, token = await apply(client)
    bucket = storagemod.get_storage().bucket
    assert len(bucket.files) == 3

    # Re-uploading the same kind replaces the old file; it does not pile up.
    await client.db.lab_applications.update_one({"_id": app_id}, {"$set": {"status": "on_hold"}})
    r = await client.post(f"/labs/applications/{app_id}/documents", data={"kind": "pan_card"},
                          files={"file": ("pan.png", b"\x89PNG new", "image/png")}, headers={"X-Draft-Token": token})
    assert r.status_code == 201 and len(bucket.files) == 3

    admin = (await client.post("/auth/admin/login", json={"username": "admin@suppai.in", "password": "adminpass123"})).json()["access_token"]
    docs = (await client.get(f"/admin/labs/applications/{app_id}", headers=bearer(admin))).json()["documents"]
    url = next(d["url"] for d in docs if d["kind"] == "pan_card").replace("http://localhost:8000", "")
    r = await client.get(url)
    assert r.status_code == 200 and r.content == b"\x89PNG new" and r.headers["content-type"] == "image/png"

    # Tampered or wrong links are refused.
    assert (await client.get(url[:-1] + ("0" if url[-1] != "0" else "1"))).status_code == 403
