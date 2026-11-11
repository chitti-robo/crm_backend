import hmac
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select

from ..config import settings
from ..db import IntegrationEvent, get_db, save_event
from ..lead_pipeline import Lead, upsert_lead
from .sheets import push_lead

router = APIRouter()


def require_key(x_api_key: str = Header("")):
    if not hmac.compare_digest(x_api_key, settings.PUBLIC_API_KEY):
        raise HTTPException(401)


def _iso(d: datetime | None) -> str | None:
    if d is None:
        return None
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).isoformat()


class WebsiteLead(BaseModel):
    name: str
    phone: str
    email: str | None = None
    course: str
    utm_source: str | None = None
    utm_medium: str | None = None
    utm_campaign: str | None = None
    fbclid: str | None = None
    gclid: str | None = None


@router.post("/api/public/leads", dependencies=[Depends(require_key)])
def website_lead(lead: WebsiteLead, bg: BackgroundTasks, db=Depends(get_db)):
    # Keep the raw submission too: it is the audit trail and powers the Integrations card.
    ev = save_event(db, "website", uuid4().hex, lead.model_dump())
    lead_id, created = upsert_lead(name=lead.name, phone=lead.phone, email=lead.email, course=lead.course,
                                   source=lead.utm_source or "Website", campaign_id=lead.utm_campaign,
                                   click_id=lead.gclid or lead.fbclid)
    bg.add_task(push_lead, {"lead_id": lead_id, "name": lead.name, "phone": lead.phone, "email": lead.email,
                            "course": lead.course, "source": lead.utm_source or "Website", "created": created})
    if ev is not None:
        ev.status = "processed"
        db.commit()
    return {"ok": True, "lead_id": lead_id, "new": created}


@router.post("/api/public/google-forms", dependencies=[Depends(require_key)])
def google_form(body: dict, bg: BackgroundTasks, db=Depends(get_db)):
    a = {k: (v[0] if isinstance(v, list) and v else v) for k, v in body.get("answers", {}).items()}
    ts = a.get("Timestamp", "")
    phone = a.get("Phone") or a.get("Mobile") or a.get("Phone number")
    key = f"{body.get('form')}:{phone}:{ts}"
    ev = save_event(db, "google_forms", key, body)
    if ev is None:  # sent before: skip only if it was fully handled, so a failed one can be retried
        ev = db.scalar(select(IntegrationEvent).where(IntegrationEvent.provider == "google_forms",
                                                      IntegrationEvent.external_id == key))
        if ev is not None and ev.status == "processed":
            return {"ok": True, "duplicate": True}
    lead_id, created = upsert_lead(name=a.get("Name") or a.get("Full name") or "Unknown", phone=phone,
                                   email=a.get("Email"), course=body.get("form", "").split("-")[0] or None,
                                   source="Google Forms")
    bg.add_task(push_lead, {"lead_id": lead_id, "name": a.get("Name") or a.get("Full name") or "Unknown",
                            "phone": phone, "email": a.get("Email"),
                            "course": body.get("form", "").split("-")[0].strip() or None,
                            "source": "Google Forms", "created": created})
    if ev is not None:
        ev.status = "processed"
        db.commit()
    return {"ok": True, "lead_id": lead_id, "new": created}


@router.get("/api/integrations/leads-status")
def leads_status(db=Depends(get_db)):
    """Counts only, for the Integrations cards."""
    week = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=7)
    out = {}
    for provider in ("website", "google_forms", "whatsapp"):
        base = select(func.count()).select_from(IntegrationEvent).where(IntegrationEvent.provider == provider)
        out[provider] = {
            "last_at": _iso(db.scalar(select(func.max(IntegrationEvent.received_at))
                                      .where(IntegrationEvent.provider == provider))),
            "last_7d": db.scalar(base.where(IntegrationEvent.received_at >= week)) or 0,
            "total": db.scalar(base) or 0,
        }
    return out


class ManualLead(BaseModel):
    name: str
    phone: str
    email: str | None = None
    course: str
    source: str


@router.post("/api/leads", dependencies=[Depends(require_key)])
def manual_lead(lead: ManualLead, bg: BackgroundTasks):
    lead_id, created = upsert_lead(name=lead.name, phone=lead.phone, email=lead.email,
                                   course=lead.course, source=lead.source)
    bg.add_task(push_lead, {"lead_id": lead_id, "name": lead.name, "phone": lead.phone, "email": lead.email,
                            "course": lead.course, "source": lead.source, "created": created})
    return {"ok": True, "lead_id": lead_id, "new": created}


@router.get("/api/leads", dependencies=[Depends(require_key)])
def list_leads(limit: int = 200, db=Depends(get_db)):
    rows = db.scalars(select(Lead).order_by(Lead.id.desc()).limit(min(limit, 500))).all()
    return [{"id": l.id, "name": l.name, "phone": l.phone or l.phone_raw, "email": l.email, "stage": l.stage,
             "owner": l.owner, "source": l.source, "courses": [i.course for i in l.interests],
             "touches": len(l.touches), "created_at": _iso(l.created_at)} for l in rows]