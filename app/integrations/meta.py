import hashlib
import hmac
import json

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from ..config import settings
from ..db import SessionLocal, IntegrationEvent, get_db, save_event
from ..lead_pipeline import upsert_lead

router = APIRouter()
GRAPH = "https://graph.facebook.com/v21.0"  # confirm the current version in Meta docs


@router.get("/webhooks/meta")
def verify(mode: str = Query(alias="hub.mode"), token: str = Query(alias="hub.verify_token"),
           challenge: str = Query(alias="hub.challenge")):
    if mode == "subscribe" and hmac.compare_digest(token, settings.META_VERIFY_TOKEN):
        return PlainTextResponse(challenge)
    raise HTTPException(403)


@router.post("/webhooks/meta")
async def receive(request: Request, bg: BackgroundTasks, db=Depends(get_db)):
    raw = await request.body()
    sig = request.headers.get("X-Hub-Signature-256", "")
    good = "sha256=" + hmac.new(settings.META_APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, good):
        raise HTTPException(401)
    for entry in json.loads(raw).get("entry", []):
        for ch in entry.get("changes", []):
            if ch.get("field") == "leadgen":
                ev = save_event(db, "meta", ch["value"]["leadgen_id"], ch["value"])
                if ev:
                    bg.add_task(process_meta_lead, ev.id)
    return {"ok": True}


def process_meta_lead(event_id: int) -> None:
    with SessionLocal() as db:
        ev = db.get(IntegrationEvent, event_id)
        ev.attempts += 1
        try:
            r = httpx.get(f"{GRAPH}/{ev.external_id}", params={"access_token": settings.META_PAGE_TOKEN}, timeout=15)
            r.raise_for_status()
            data = r.json()
            f = {x["name"]: (x["values"] or [""])[0] for x in data.get("field_data", [])}
            upsert_lead(
                name=f.get("full_name") or f.get("first_name", "Unknown"),
                phone=f.get("phone_number"), email=f.get("email"), course=None,
                source="Meta Ads", campaign_id=data.get("campaign_id") or ev.payload.get("form_id"),
                external_id=ev.external_id)
            ev.status = "processed"
        except Exception as e:  # retried later by the worker; shown on the Integrations screen
            ev.status, ev.error = "failed", str(e)[:500]
        db.commit()
