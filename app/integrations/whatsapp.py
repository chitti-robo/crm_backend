import asyncio
import hashlib
import hmac
import logging
from uuid import uuid4

import requests
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from ..config import settings
from ..db import SessionLocal, get_db, save_event
from ..lead_pipeline import Lead, upsert_lead
from .forms import require_key

log = logging.getLogger("crm.whatsapp")
router = APIRouter()


def _handle(wa_id: str, name: str | None, message: dict) -> None:
    msg_id = message.get("id")
    with SessionLocal() as db:
        ev = save_event(db, "whatsapp", msg_id or uuid4().hex, {"name": name, "message": message})
        upsert_lead(name=name or wa_id, phone="+" + wa_id, email=None, course=None,
                    source="whatsapp", external_id=msg_id)
        if ev is not None:
            ev.status = "processed"
            db.commit()


@router.get("/webhooks/whatsapp")
async def verify(request: Request):
    q = request.query_params
    if q.get("hub.mode") == "subscribe" and q.get("hub.verify_token") == settings.WHATSAPP_VERIFY_TOKEN:
        return Response(q.get("hub.challenge"), media_type="text/plain")
    raise HTTPException(403)


@router.post("/webhooks/whatsapp")
async def receive(request: Request):
    raw = await request.body()
    sig = request.headers.get("X-Hub-Signature-256", "")
    expected = "sha256=" + hmac.new(settings.META_APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        raise HTTPException(401)

    data = await request.json()
    for entry in data.get("entry", []):
        for ch in entry.get("changes", []):
            v = ch.get("value", {})
            for s in v.get("statuses", []):
                err = (s.get("errors") or [{}])[0]
                log.warning("whatsapp status id=%s status=%s code=%s detail=%s", s.get("id"),
                            s.get("status"), err.get("code"), err.get("title") or err.get("message"))
            names = {c["wa_id"]: c.get("profile", {}).get("name") for c in v.get("contacts", [])}
            for m in v.get("messages", []):
                wa_id = m["from"]
                await asyncio.to_thread(_handle, wa_id, names.get(wa_id), m)
    return {"ok": True}


def _first_name(lead) -> str:
    n = (getattr(lead, "name", None) or "").strip()
    return n.split()[0] if n else "there"


def _first_course(lead) -> str:
    c = getattr(lead, "courses", None) or getattr(lead, "course", None)
    if isinstance(c, (list, tuple, set)):
        c = next(iter(c), None)
    c = getattr(c, "name", c)
    return str(c) if c else "AI"


class SendBody(BaseModel):
    lead_id: int
    text: str | None = None


@router.post("/api/whatsapp/send", dependencies=[Depends(require_key)])
def send_whatsapp(body: SendBody, db=Depends(get_db)):
    if not settings.WHATSAPP_TOKEN or not settings.WHATSAPP_PHONE_ID:
        raise HTTPException(503, "WHATSAPP_TOKEN or WHATSAPP_PHONE_ID is not set in .env")
    lead = db.get(Lead, body.lead_id)
    if lead is None or not lead.phone:
        raise HTTPException(404, "Lead not found, or it has no valid phone number")

    payload = {"messaging_product": "whatsapp", "to": lead.phone.lstrip("+")}
    text = (body.text or "").strip()
    if text:
        payload.update(type="text", text={"body": text})
    elif not settings.WHATSAPP_TEMPLATE:
        raise HTTPException(400, "Type a message. Template messages are not set up for this number yet.")
    else:
        payload.update(type="template", template={
            "name": settings.WHATSAPP_TEMPLATE,
            "language": {"code": settings.WHATSAPP_TEMPLATE_LANG},
            "components": [{
                "type": "body",
                "parameters": [
                    {"type": "text", "text": _first_name(lead)},
                    {"type": "text", "text": _first_course(lead)},
                ],
            }],
        })

    r = requests.post(
        f"https://graph.facebook.com/v25.0/{settings.WHATSAPP_PHONE_ID}/messages",
        headers={"Authorization": f"Bearer {settings.WHATSAPP_TOKEN}"},
        json=payload, timeout=15,
    )
    data = r.json() if r.content else {}
    if not r.ok:
        reason = data.get("error", {}).get("message", "WhatsApp rejected the message")
        log.warning("whatsapp send failed for lead %s: %s", body.lead_id, reason)
        raise HTTPException(502, reason)
    msg_id = (data.get("messages") or [{}])[0].get("id")
    log.info("whatsapp sent to lead %s id=%s", body.lead_id, msg_id)
    return {"ok": True, "message_id": msg_id}