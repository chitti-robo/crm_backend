import json
import logging
import urllib.request

from ..config import settings

log = logging.getLogger("crm.sheets")


def push_lead(payload: dict) -> None:
    """Sends a lead to the Google Apps Script web app. Never raises, so lead saving is not affected."""
    url = settings.SHEETS_WEBHOOK_URL
    if not url:
        return
    try:
        req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            log.warning("sheets push status=%s", r.status)
    except Exception:
        log.exception("sheets push failed")