"""LinkedIn Lead Sync poller. Needs Marketing Developer Platform approval.

Run poll_leads() from the worker every few minutes. The exact endpoint, version header
and field names must be confirmed against LinkedIn's current Lead Sync API docs
(learn.microsoft.com > LinkedIn > Marketing > Lead Sync API) once access is granted.
"""
import httpx

from ..config import settings
from ..db import SessionLocal, save_event
from ..lead_pipeline import upsert_lead

BASE = "https://api.linkedin.com/rest"


def poll_leads(owner_urn: str, since_ms: int) -> int:
    headers = {"Authorization": f"Bearer {settings.LINKEDIN_ACCESS_TOKEN}", "LinkedIn-Version": "202410",
               "X-Restli-Protocol-Version": "2.0.0"}
    params = {"q": "owner", "owner": f"(sponsoredAccount:{owner_urn})", "submittedAfterTimestamp": since_ms}
    r = httpx.get(f"{BASE}/leadFormResponses", headers=headers, params=params, timeout=20)
    r.raise_for_status()
    new = 0
    with SessionLocal() as db:
        for el in r.json().get("elements", []):
            if save_event(db, "linkedin", el["id"], el) is None:
                continue
            new += 1
            answers = {a.get("questionId"): a for a in el.get("formResponse", {}).get("answers", [])}
            # TODO: map question ids to name/phone/email for your form
            course = settings.LINKEDIN_FORM_COURSE_MAP.get(str(el.get("leadGenFormUrn", "")).split(":")[-1])
            upsert_lead(name="LinkedIn lead", phone=None, email=None, course=course, source="LinkedIn",
                        campaign_id=str(el.get("associatedEntity", "")), external_id=el["id"])
    return new
