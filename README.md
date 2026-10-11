# SuppAI Labs API

Backend for how diagnostic labs join SuppAI, get approved, run their orders, and how members book and track tests.
Built from *SuppAI Labs Flow v1 (29 Sep 2026)*. FastAPI + MongoDB, same stack as `supp-ai-doctor`.

## Run it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env            # set MONGO_URI and JWT_SECRET
python -m scripts.create_admin admin@suppai.in
python -m scripts.seed_catalog
uvicorn app.main:app --reload
```

Interactive docs: http://localhost:8000/docs

Tests (in-memory Mongo, no server needed): `pytest`

## The flow

| Step | Who | Endpoints |
|---|---|---|
| 1. Apply (6-step form, saved as draft) | Lab | `POST /labs/applications` → `PATCH /labs/applications/{id}` → `POST …/documents` → `POST …/submit` |
| 2. Review | Admin | `GET /admin/labs/applications?status=` · `GET …/{id}` · `POST …/{id}/review` · `POST …/{id}/hold` |
| 3. Accept | Admin | `POST /admin/labs/applications/{id}/accept` · `POST …/{id}/resend-invite` |
| 4. Sign in and run orders | Lab | `POST /auth/lab/set-password` · `POST /auth/lab/login` · `/lab/today` · `/lab/orders` · `PATCH /lab/orders/{id}/status` · `POST /lab/orders/{id}/report` · `/lab/leave` · `/lab/reports/summary` |
| 5. Book and track | Member | `GET /tests?pincode=` · `GET /labs/{id}/slots?date=` · `POST /orders` · `GET /orders/{id}` · `GET /orders/{id}/report` · `POST /orders/{id}/cancel` |

### Application statuses

`draft → submitted → under_review → active`, with `on_hold` (lab fixes and resubmits, same reference) and `rejected` (final).

### Order statuses

`received → confirmed → sample_collected → in_lab → result_published`, one step at a time. `cancelled` from any stage before the result.

| Lab moves to | Member sees | Backend also does |
|---|---|---|
| received | Booking placed | Notify the lab |
| confirmed | Booking confirmed | Notify the member |
| sample_collected | Sample collected | Records collector and time; cash marked paid |
| in_lab | Processing in lab | Starts turnaround clock |
| result_published | Report published | Needs report PDF uploaded first; notify member |
| cancelled | Cancelled | Online-paid orders get `refund_status: pending` |

Every move is written to `order_events` (time + who) and queues a member notification.

## Where this differs from the flow doc

- **No temporary passwords.** Accept creates the login and emails a one-time set-password link (24 h). "New password" is `resend-invite`. Passwords are bcrypt-hashed and never shown to the admin.
- **Aadhaar:** only the last 4 digits are accepted and stored.
- **Pausing a date** that already has active bookings is refused (409) with the list of those orders.
- **Members can cancel** online only before the sample is collected (`received` or `confirmed`).
- **Lab login** locks for 15 minutes after 5 wrong passwords.
- **Tests:** until the Tests & prices tab exists, every active lab offers the shared `catalog_tests` list at catalogue prices.

## Security notes

- Lab APIs take `lab_id` from the token only. Members only see their own orders.
- KYC files and reports are stored on the server's disk (`LOCAL_STORAGE_DIR`), never public, and opened through signed links that expire in 5 minutes. Put that folder on a persistent disk and include it in backups.
- Admin actions, document views and report downloads go to `audit_log`.
- Accept runs in one MongoDB transaction (needs a replica set; Atlas has one).

## Not built yet (next phase)

- Razorpay order creation and payment webhook
- Notification worker that sends the queued `notifications` by SMS / WhatsApp / email
- Tests & prices tab (`/lab/tests`) and editing pincodes / opening hours from the dashboard
- Slot capacity limits per lab
- Refund processing
