"""Search Console + GA4 snapshots: tables, import job, and API routes.

Each run stores the trailing-28-day window under today's date (re-running the
same day replaces that day's rows). Every run, good or bad, is logged in
import_runs so the Integrations page can show real status.
"""
import logging
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Header, HTTPException
from sqlalchemy import Boolean, Date, DateTime, Float, Integer, String, UniqueConstraint, delete, select
from sqlalchemy.orm import Mapped, mapped_column

from .config import settings
from .db import Base, SessionLocal
from .integrations import google_reports

log = logging.getLogger("crm.reports")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SeoQuery(Base):
    __tablename__ = "seo_queries"
    __table_args__ = (UniqueConstraint("day", "query"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    day: Mapped[date] = mapped_column(Date)
    query: Mapped[str] = mapped_column(String)
    clicks: Mapped[int] = mapped_column(Integer)
    impressions: Mapped[int] = mapped_column(Integer)
    position: Mapped[float] = mapped_column(Float)


class TrafficSource(Base):
    __tablename__ = "traffic_sources"
    __table_args__ = (UniqueConstraint("day", "source_medium"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    day: Mapped[date] = mapped_column(Date)
    source_medium: Mapped[str] = mapped_column(String)
    sessions: Mapped[int] = mapped_column(Integer)
    users: Mapped[int] = mapped_column(Integer)


class ImportRun(Base):
    __tablename__ = "import_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String, index=True)
    ran_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    ok: Mapped[bool] = mapped_column(Boolean)
    rows: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(String, nullable=True)


SOURCES = {
    "search_console": google_reports.search_console_top_queries,
    "ga4": google_reports.ga4_sessions_by_source,
}


def _record(source: str, ok: bool, rows: int, error: str | None) -> ImportRun:
    with SessionLocal() as db:
        run = ImportRun(source=source, ok=ok, rows=rows, error=error)
        db.add(run)
        db.commit()
        return run


def run_import(source: str) -> ImportRun:
    today = date.today()
    try:
        data = SOURCES[source]()
        with SessionLocal() as db:
            if source == "search_console":
                db.execute(delete(SeoQuery).where(SeoQuery.day == today))
                db.add_all(SeoQuery(day=today, query=r["query"], clicks=r["clicks"],
                                    impressions=r["impressions"], position=r["position"]) for r in data)
            else:
                db.execute(delete(TrafficSource).where(TrafficSource.day == today))
                db.add_all(TrafficSource(day=today, source_medium=r["source_medium"],
                                         sessions=r["sessions"], users=r["users"]) for r in data)
            db.commit()
        return _record(source, True, len(data), None)
    except Exception as e:
        log.exception("import failed: %s", source)
        return _record(source, False, 0, str(e)[:500])


def run_due(max_age_hours: int = 20) -> None:
    """Run any source with no successful import in the last max_age_hours."""
    cutoff = _utcnow() - timedelta(hours=max_age_hours)
    for source in SOURCES:
        with SessionLocal() as db:
            last = db.scalar(select(ImportRun.ran_at)
                             .where(ImportRun.source == source, ImportRun.ok.is_(True))
                             .order_by(ImportRun.ran_at.desc()).limit(1))
        if last is not None and last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if last is None or last < cutoff:
            run_import(source)


def latest_status() -> list[dict]:
    out = []
    for source in SOURCES:
        with SessionLocal() as db:
            run = db.scalar(select(ImportRun).where(ImportRun.source == source)
                            .order_by(ImportRun.ran_at.desc()).limit(1))
        if run is None:
            out.append({"source": source, "ok": None, "ran_at": None, "rows": 0, "error": None})
            continue
        ran = run.ran_at if run.ran_at.tzinfo else run.ran_at.replace(tzinfo=timezone.utc)
        out.append({"source": source, "ok": run.ok, "ran_at": ran.isoformat(), "rows": run.rows,
                    "error": (run.error or "")[:200] or None})
    return out


router = APIRouter(prefix="/api")


@router.get("/integrations/status")
def integration_status():
    return latest_status()


@router.post("/integrations/{source}/run")
def run_now(source: str, x_api_key: str | None = Header(default=None)):
    if x_api_key != settings.PUBLIC_API_KEY:
        raise HTTPException(401, "bad key")
    if source not in SOURCES:
        raise HTTPException(404, "unknown source")
    run = run_import(source)
    return {"ok": run.ok, "rows": run.rows, "error": run.error}


@router.get("/reports/seo")
def seo_report():
    with SessionLocal() as db:
        day = db.scalar(select(SeoQuery.day).order_by(SeoQuery.day.desc()).limit(1))
        rows = db.scalars(select(SeoQuery).where(SeoQuery.day == day)
                          .order_by(SeoQuery.clicks.desc(), SeoQuery.impressions.desc())).all() if day else []
    return {"day": day.isoformat() if day else None,
            "rows": [{"query": r.query, "clicks": r.clicks, "impressions": r.impressions,
                      "position": r.position} for r in rows]}


@router.get("/reports/traffic")
def traffic_report():
    with SessionLocal() as db:
        day = db.scalar(select(TrafficSource.day).order_by(TrafficSource.day.desc()).limit(1))
        rows = db.scalars(select(TrafficSource).where(TrafficSource.day == day)
                          .order_by(TrafficSource.sessions.desc())).all() if day else []
    return {"day": day.isoformat() if day else None,
            "rows": [{"source_medium": r.source_medium, "sessions": r.sessions, "users": r.users} for r in rows]}