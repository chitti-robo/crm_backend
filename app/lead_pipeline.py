"""Shared lead pipeline: normalise, dedupe, attach source, save.

Every integration calls upsert_lead(). A lead is matched by phone (E.164) first, then by email.
Each call also records a touch (source, campaign, click id), so repeat enquiries are kept
instead of creating duplicate leads.
"""
import logging
import re
from datetime import datetime, timezone

import phonenumbers
from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from .db import Base, SessionLocal

log = logging.getLogger("crm.pipeline")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Lead(Base):
    __tablename__ = "leads"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    phone: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)  # E.164
    phone_raw: Mapped[str | None] = mapped_column(String, nullable=True)
    email: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    city: Mapped[str | None] = mapped_column(String, nullable=True)
    stage: Mapped[str] = mapped_column(String, default="New")
    owner: Mapped[str | None] = mapped_column(String, nullable=True)
    source: Mapped[str] = mapped_column(String)  # where the lead first came from
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    interests: Mapped[list["LeadInterest"]] = relationship(back_populates="lead", cascade="all, delete-orphan")
    touches: Mapped[list["LeadSource"]] = relationship(
        back_populates="lead", cascade="all, delete-orphan", order_by="LeadSource.id")


class LeadInterest(Base):
    __tablename__ = "lead_interests"
    __table_args__ = (UniqueConstraint("lead_id", "course"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"))
    course: Mapped[str] = mapped_column(String)
    lead: Mapped[Lead] = relationship(back_populates="interests")


class LeadSource(Base):
    __tablename__ = "lead_sources"
    # A source that supplies its own id (Meta, LinkedIn) can never be recorded twice.
    __table_args__ = (UniqueConstraint("source", "external_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"))
    source: Mapped[str] = mapped_column(String)
    campaign_id: Mapped[str | None] = mapped_column(String, nullable=True)
    click_id: Mapped[str | None] = mapped_column(String, nullable=True)
    external_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    lead: Mapped[Lead] = relationship(back_populates="touches")


def norm_phone(raw: str | None, region: str = "IN") -> str | None:
    if not raw:
        return None
    try:
        n = phonenumbers.parse(raw, region)
    except Exception:
        return None
    if not phonenumbers.is_valid_number(n):
        return None
    return phonenumbers.format_number(n, phonenumbers.PhoneNumberFormat.E164)


_COURSES = {
    "ds": "DS", "data science": "DS",
    "aiml": "AIML", "ai ml": "AIML", "ai and ml": "AIML", "machine learning": "AIML",
    "genai": "GENAI", "gen ai": "GENAI", "generative ai": "GENAI",
    "fde": "FDE", "forward deployed eng": "FDE", "forward deployed engineer": "FDE",
}


def norm_course(raw: str | None) -> str | None:
    """Maps free text to the course codes the CRM uses (DS, AIML, GENAI, FDE); unknown text is kept as typed."""
    if not raw:
        return None
    key = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", raw.lower())).strip()
    if key in _COURSES:
        return _COURSES[key]
    if "forward deployed" in key:
        return "FDE"
    return raw.strip()[:40] or None


def _save(db: Session, *, name, phone, e164, mail, code, source, campaign_id, click_id, external_id):
    if external_id:
        seen = db.scalar(select(LeadSource).where(LeadSource.source == source,
                                                  LeadSource.external_id == external_id))
        if seen is not None:  # a retry of something already recorded
            return seen.lead_id, False
    lead = None
    if e164:
        lead = db.scalar(select(Lead).where(Lead.phone == e164))
    if lead is None and mail:
        lead = db.scalar(select(Lead).where(Lead.email == mail))
    created = lead is None
    if created:
        lead = Lead(name=(name or "Unknown").strip() or "Unknown", phone=e164, phone_raw=phone,
                    email=mail, source=source)
        db.add(lead)
    else:  # fill gaps, never overwrite what we already know
        if not lead.phone and e164:
            lead.phone = e164
        if not lead.email and mail:
            lead.email = mail
        lead.updated_at = _utcnow()
    if code and code not in {i.course for i in lead.interests}:
        lead.interests.append(LeadInterest(course=code))
    lead.touches.append(LeadSource(source=source, campaign_id=campaign_id, click_id=click_id,
                                   external_id=external_id))
    db.flush()
    return lead.id, created


def upsert_lead(*, name: str, phone: str | None, email: str | None, course: str | None,
                source: str, campaign_id: str | None = None, click_id: str | None = None,
                external_id: str | None = None) -> tuple[int, bool]:
    """Saves or updates a lead. Returns (lead_id, created)."""
    e164 = norm_phone(phone)
    mail = (email or "").strip().lower() or None
    if not e164 and not mail:
        log.warning("lead %r has no valid phone or email, so it cannot be matched to earlier enquiries", name)
    kwargs = dict(name=name, phone=phone, e164=e164, mail=mail, code=norm_course(course), source=source,
                  campaign_id=campaign_id, click_id=click_id, external_id=external_id)
    for attempt in (1, 2):
        try:
            with SessionLocal() as db:
                result = _save(db, **kwargs)
                db.commit()
            log.info("lead id=%s created=%s source=%s course=%s campaign=%s", result[0], result[1],
                     source, kwargs["code"], campaign_id)
            return result
        except IntegrityError:  # two requests for the same person at once: try again, it will now be found
            if attempt == 2:
                raise
    raise RuntimeError("unreachable")