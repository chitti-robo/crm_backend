from datetime import datetime
from sqlalchemy import JSON, DateTime, Integer, String, UniqueConstraint, create_engine, func
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from .config import settings

engine = create_engine(settings.DATABASE_URL)
SessionLocal = sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class IntegrationEvent(Base):
    __tablename__ = "integration_events"
    __table_args__ = (UniqueConstraint("provider", "external_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String)
    external_id: Mapped[str] = mapped_column(String)
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String, default="received")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


def init_db() -> None:
    Base.metadata.create_all(engine)


def get_db():
    with SessionLocal() as db:
        yield db


def save_event(db: Session, provider: str, external_id: str, payload: dict) -> IntegrationEvent | None:
    """Store the raw payload first. Returns None if this event was already received (idempotency)."""
    ev = IntegrationEvent(provider=provider, external_id=str(external_id), payload=payload)
    db.add(ev)
    try:
        db.commit()
    except Exception:
        db.rollback()
        return None
    return ev
