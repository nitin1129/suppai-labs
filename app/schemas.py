import re
from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel, EmailStr, Field, field_validator

PINCODE = r"^[1-9][0-9]{5}$"
PAN = r"^[A-Z]{5}[0-9]{4}[A-Z]$"
GSTIN = r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$"
IFSC = r"^[A-Z]{4}0[A-Z0-9]{6}$"
MOBILE = r"^[6-9][0-9]{9}$"
HHMM = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


# ---------- Application statuses ----------

class ApplicationStatus(str, Enum):
    draft = "draft"
    submitted = "submitted"
    under_review = "under_review"
    on_hold = "on_hold"
    rejected = "rejected"
    active = "active"


EDITABLE_STATUSES = {ApplicationStatus.draft, ApplicationStatus.on_hold}
REVIEWABLE_STATUSES = {ApplicationStatus.submitted, ApplicationStatus.under_review, ApplicationStatus.on_hold}


# ---------- Application sections (six steps of /join/lab) ----------

class Address(BaseModel):
    line1: str
    line2: str | None = None
    city: str
    state: str
    pincode: str = Field(pattern=PINCODE)


class CentreDetails(BaseModel):
    legal_name: str = Field(min_length=2, max_length=150)
    entity_type: str
    contact_phone: str = Field(pattern=MOBILE)
    official_email: EmailStr
    registered_address: Address
    centre_address: Address


class Signatory(BaseModel):
    name: str
    designation: str
    mobile: str = Field(pattern=MOBILE)
    email: EmailStr
    pan: str = Field(pattern=PAN)
    # Only the last four digits are accepted and stored. Full Aadhaar numbers are not kept.
    aadhaar_last4: str = Field(pattern=r"^[0-9]{4}$")

    @field_validator("pan", mode="before")
    @classmethod
    def upper_pan(cls, v):
        return v.upper() if isinstance(v, str) else v


class Regulatory(BaseModel):
    gstin: str | None = Field(default=None, pattern=GSTIN)
    clinical_registration_no: str
    nabl_accredited: bool = False
    nabl_certificate_no: str | None = None
    director_name: str
    pathologist_name: str
    pathologist_registration_no: str


class OpeningHours(BaseModel):
    day: int = Field(ge=0, le=6, description="0 = Monday")
    opens: str = Field(pattern=HHMM)
    closes: str = Field(pattern=HHMM)


class BankDetails(BaseModel):
    account_holder: str
    account_number: str = Field(pattern=r"^[0-9]{9,18}$")
    ifsc: str = Field(pattern=IFSC)
    bank_name: str


class ServiceSettlement(BaseModel):
    home_collection: bool
    pincodes_covered: list[str] = Field(min_length=1)
    opening_hours: list[OpeningHours] = Field(min_length=1)
    bank: BankDetails

    @field_validator("pincodes_covered")
    @classmethod
    def check_pincodes(cls, v):
        bad = [p for p in v if not re.match(PINCODE, p)]
        if bad:
            raise ValueError(f"invalid pincodes: {', '.join(bad)}")
        return sorted(set(v))


class Declarations(BaseModel):
    accepted_terms: bool
    information_true: bool
    signed_name: str


class ApplicationDraft(BaseModel):
    """Any subset of steps can be saved while the application is a draft."""
    centre: CentreDetails | None = None
    signatory: Signatory | None = None
    regulatory: Regulatory | None = None
    service: ServiceSettlement | None = None
    declarations: Declarations | None = None


REQUIRED_SECTIONS = ["centre", "signatory", "regulatory", "service", "declarations"]
REQUIRED_DOCUMENTS = {"clinical_registration", "pan_card", "cancelled_cheque"}
DOCUMENT_KINDS = REQUIRED_DOCUMENTS | {"gst_certificate", "nabl_certificate", "pathologist_registration", "other"}


class ReviewAction(str, Enum):
    hold = "hold"
    reject = "reject"


class HoldRequest(BaseModel):
    action: ReviewAction
    note: str = Field(min_length=5, max_length=2000)


# ---------- Auth ----------

class LoginRequest(BaseModel):
    username: str
    password: str


class SetPasswordRequest(BaseModel):
    token: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


# ---------- Orders ----------

class OrderStatus(str, Enum):
    received = "received"
    confirmed = "confirmed"
    sample_collected = "sample_collected"
    in_lab = "in_lab"
    result_published = "result_published"
    cancelled = "cancelled"


FORWARD_FLOW = [
    OrderStatus.received,
    OrderStatus.confirmed,
    OrderStatus.sample_collected,
    OrderStatus.in_lab,
    OrderStatus.result_published,
]

MEMBER_LABEL = {
    OrderStatus.received: "Booking placed",
    OrderStatus.confirmed: "Booking confirmed",
    OrderStatus.sample_collected: "Sample collected",
    OrderStatus.in_lab: "Processing in lab",
    OrderStatus.result_published: "Report published",
    OrderStatus.cancelled: "Cancelled",
}


class PaymentMethod(str, Enum):
    online = "online"
    cash = "cash"


class Patient(BaseModel):
    name: str
    age: int = Field(ge=0, le=120)
    gender: str
    mobile: str = Field(pattern=MOBILE)


class OrderCreate(BaseModel):
    lab_id: str
    test_code: str
    pincode: str = Field(pattern=PINCODE)
    address: Address | None = None
    home_collection: bool
    patient: Patient
    slot_date: date
    slot_time: str = Field(pattern=HHMM)
    payment_method: PaymentMethod


class StatusChange(BaseModel):
    to: OrderStatus
    collector_name: str | None = None
    reason: str | None = None


class LeaveRequest(BaseModel):
    date: date
    reason: str | None = None


class OrderEventOut(BaseModel):
    status: OrderStatus
    label: str
    at: datetime
    by: str
