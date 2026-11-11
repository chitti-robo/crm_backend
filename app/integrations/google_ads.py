import hmac

from fastapi import APIRouter, Depends, HTTPException, Query

from ..config import settings
from ..db import get_db, save_event
from ..lead_pipeline import upsert_lead

router = APIRouter()


@router.post("/webhooks/google-ads")
async def receive(body: dict, google_key: str | None = Query(None), db=Depends(get_db)):
    # Google puts the key you set on the lead form in the JSON body as "google_key".
    key = body.get("google_key") or google_key or ""
    if not hmac.compare_digest(str(key), settings.GOOGLE_ADS_WEBHOOK_KEY):
        raise HTTPException(401)
    lead_id = body.get("lead_id")
    if not lead_id:
        raise HTTPException(400, "missing lead_id")
    if save_event(db, "google_ads", lead_id, body) is None:
        return {"ok": True, "duplicate": True}
    cols = {c.get("column_id"): c.get("string_value") for c in body.get("user_column_data", [])}
    upsert_lead(name=cols.get("FULL_NAME", "Unknown"), phone=cols.get("PHONE_NUMBER"), email=cols.get("EMAIL"),
                course=None, source="Google Ads", campaign_id=str(body.get("campaign_id") or ""),
                click_id=body.get("gcl_id"), external_id=str(lead_id))
    return {"ok": True}
