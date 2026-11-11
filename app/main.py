import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import payments, reports  # imported before init_db so their tables are created
from .db import init_db
from .integrations import forms, google_ads, meta, whatsapp


async def _import_loop():
    """Checks hourly; a source runs when it has had no successful import in 20 hours."""
    while True:
        try:
            await asyncio.to_thread(reports.run_due)
        except Exception:
            reports.log.exception("import loop error")
        await asyncio.sleep(3600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    task = asyncio.create_task(_import_loop())
    yield
    task.cancel()


app = FastAPI(title="L&L CRM API", lifespan=lifespan)
app.include_router(meta.router)
app.include_router(whatsapp.router)
app.include_router(google_ads.router)
app.include_router(forms.router)
app.include_router(reports.router)
app.include_router(payments.admin)
app.include_router(payments.webhook)


@app.get("/health")
def health():
    return {"ok": True}