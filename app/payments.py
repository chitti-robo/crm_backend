"""Admissions, installments, Razorpay payment links and the payment webhook.

Amounts are whole rupees in the database and converted to paise only when talking to Razorpay.
"""
import hashlib
import hmac
import json
import logging
import time
from datetime import date, datetime, timezone

import requests
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, func, select
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship
from starlette.concurrency import run_in_threadpool

from .config import settings
from .db import Base, IntegrationEvent, SessionLocal, get_db, save_event
from .lead_pipeline import norm_phone

log = logging.getLogger("crm.payments")
RZP = "https://api.razorpay.com/v1"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Admission(Base):
    __tablename__ = "admissions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    student_name: Mapped[str] = mapped_column(String)
    phone: Mapped[str | None] = mapped_column(String, nullable=True)
    email: Mapped[str | None] = mapped_column(String, nullable=True)
    course: Mapped[str] = mapped_column(String)
    batch: Mapped[str] = mapped_column(String)
    fee: Mapped[int] = mapped_column(Integer)
    discount: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    installments: Mapped[list["Installment"]] = relationship(
        back_populates="admission", order_by="Installment.seq", cascade="all, delete-orphan")


class Installment(Base):
    __tablename__ = "installments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    admission_id: Mapped[int] = mapped_column(ForeignKey("admissions.id"))
    seq: Mapped[int] = mapped_column(Integer)
    amount: Mapped[int] = mapped_column(Integer)
    due: Mapped[date] = mapped_column(Date)
    paid: Mapped[bool] = mapped_column(Boolean, default=False)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    payment_link_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    payment_link_url: Mapped[str | None] = mapped_column(String, nullable=True)
    payment_id: Mapped[str | None] = mapped_column(String, nullable=True)
    admission: Mapped[Admission] = relationship(back_populates="installments")


def require_key(x_api_key: str | None = Header(default=None)):
    if not x_api_key or not hmac.compare_digest(x_api_key, settings.PUBLIC_API_KEY):
        raise HTTPException(401, "bad key")


class InstIn(BaseModel):
    amount: int = Field(gt=0)
    due: date


class AdmissionIn(BaseModel):
    student_name: str
    phone: str | None = None
    email: str | None = None
    course: str
    batch: str
    fee: int = Field(gt=0)
    discount: int = Field(default=0, ge=0)
    installments: list[InstIn] = Field(min_length=1)


def _out(a: Admission) -> dict:
    return {"id": a.id, "student_name": a.student_name, "phone": a.phone, "email": a.email,
            "course": a.course, "batch": a.batch, "fee": a.fee, "discount": a.discount,
            "installments": [{"id": i.id, "seq": i.seq, "amount": i.amount, "due": i.due.isoformat(),
                              "paid": i.paid, "paid_at": i.paid_at.isoformat() if i.paid_at else None,
                              "payment_link_url": i.payment_link_url} for i in a.installments]}


admin = APIRouter(prefix="/api", dependencies=[Depends(require_key)])


@admin.post("/admissions")
def create_admission(body: AdmissionIn, db: Session = Depends(get_db)):
    net = body.fee - body.discount
    if sum(i.amount for i in body.installments) != net:
        raise HTTPException(422, f"installments must add up to fee minus discount ({net})")
    a = Admission(student_name=body.student_name, phone=body.phone, email=body.email, course=body.course,
                  batch=body.batch, fee=body.fee, discount=body.discount,
                  installments=[Installment(seq=n, amount=i.amount, due=i.due)
                                for n, i in enumerate(body.installments, 1)])
    db.add(a)
    db.commit()
    return _out(a)


@admin.get("/admissions")
def list_admissions(db: Session = Depends(get_db)):
    return [_out(a) for a in db.scalars(select(Admission).order_by(Admission.id.desc())).all()]


@admin.post("/installments/{inst_id}/payment-link")
def create_payment_link(inst_id: int, force: bool = False, db: Session = Depends(get_db)):
    """Returns the existing link if there is one. force=true makes a new link (the old one stays payable)."""
    inst = db.get(Installment, inst_id)
    if inst is None:
        raise HTTPException(404, "installment not found")
    if inst.paid:
        raise HTTPException(409, "already paid")
    if inst.payment_link_url and not force:
        return {"url": inst.payment_link_url, "existing": True}
    if not (settings.RAZORPAY_KEY_ID and settings.RAZORPAY_KEY_SECRET):
        raise HTTPException(503, "Razorpay keys are not set")
    a = inst.admission
    customer = {"name": a.student_name}
    contact = norm_phone(a.phone)
    if contact:
        customer["contact"] = contact
    if a.email:
        customer["email"] = a.email
    body = {
        "amount": inst.amount * 100,
        "currency": "INR",
        "accept_partial": False,
        "reference_id": f"inst-{inst.id}-{int(time.time())}"[:40],
        "description": f"{a.course} {a.batch}: installment {inst.seq}",
        "customer": customer,
        "notify": {"sms": False, "email": False},  # we send the link ourselves, e.g. on WhatsApp
        "reminder_enable": False,
        "notes": {"installment_id": str(inst.id), "admission_id": str(a.id)},
    }
    r = requests.post(f"{RZP}/payment_links", json=body, timeout=20,
                      auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))
    if r.status_code >= 400:
        raise HTTPException(502, f"Razorpay said: {r.text[:300]}")
    link = r.json()
    inst.payment_link_id = link["id"]
    inst.payment_link_url = link["short_url"]
    db.commit()
    return {"url": inst.payment_link_url, "existing": False}


webhook = APIRouter()


def _apply(db: Session, payload: dict) -> tuple[str, str | None]:
    if payload.get("event") != "payment_link.paid":
        return "ignored", None
    ents = payload["payload"]
    link = ents["payment_link"]["entity"]
    pay = (ents.get("payment") or {}).get("entity") or {}
    inst = db.scalar(select(Installment).where(Installment.payment_link_id == link["id"]))
    if inst is None:
        iid = (link.get("notes") or {}).get("installment_id")
        inst = db.get(Installment, int(iid)) if iid else None
    if inst is None:
        return "needs_review", "no installment matches this payment link"
    if inst.paid:
        return "processed", "already paid"
    if int(link.get("amount_paid", 0)) != inst.amount * 100:
        return "needs_review", f"paid {link.get('amount_paid')} paise, expected {inst.amount * 100}"
    inst.paid = True
    inst.paid_at = _utcnow()
    inst.payment_id = pay.get("id")
    return "processed", None


def _handle(event_id: str, payload: dict) -> dict:
    with SessionLocal() as db:
        ev = save_event(db, "razorpay", event_id, payload)
        if ev is None:  # seen before: skip only if it was fully handled
            ev = db.scalar(select(IntegrationEvent).where(
                IntegrationEvent.provider == "razorpay", IntegrationEvent.external_id == event_id))
            if ev is not None and ev.status not in ("received", "failed"):
                return {"ok": True, "duplicate": True}
        ev.attempts = (ev.attempts or 0) + 1
        try:
            ev.status, ev.error = _apply(db, payload)
            db.commit()
        except Exception as e:
            log.exception("razorpay webhook failed")
            db.rollback()
            ev.status, ev.error = "failed", str(e)[:500]
            db.commit()
            raise HTTPException(500, "processing failed")  # Razorpay retries
    return {"ok": True}


@webhook.post("/webhooks/razorpay")
async def razorpay_webhook(request: Request):
    secret = settings.RAZORPAY_WEBHOOK_SECRET
    if not secret:
        raise HTTPException(503, "webhook secret not set")
    raw = await request.body()
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(request.headers.get("X-Razorpay-Signature", ""), expected):
        raise HTTPException(400, "bad signature")
    event_id = request.headers.get("x-razorpay-event-id") or hashlib.sha256(raw).hexdigest()
    return await run_in_threadpool(_handle, event_id, json.loads(raw))


@webhook.get("/api/integrations/razorpay/status")
def razorpay_status(db: Session = Depends(get_db)):
    """Status for the Integrations card. Never returns the keys themselves."""
    kid = settings.RAZORPAY_KEY_ID

    def iso(d):
        if d is None:
            return None
        return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).isoformat()

    last = db.scalar(select(IntegrationEvent).where(IntegrationEvent.provider == "razorpay")
                     .order_by(IntegrationEvent.received_at.desc()).limit(1))
    paid_at = db.scalar(select(func.max(Installment.paid_at)))
    return {
        "configured": bool(kid and settings.RAZORPAY_KEY_SECRET),
        "mode": "test" if kid.startswith("rzp_test_") else "live" if kid.startswith("rzp_live_") else None,
        "webhook_secret_set": bool(settings.RAZORPAY_WEBHOOK_SECRET),
        "last_webhook_at": iso(last.received_at) if last else None,
        "last_webhook_status": last.status if last else None,
        "last_paid_at": iso(paid_at),
    }