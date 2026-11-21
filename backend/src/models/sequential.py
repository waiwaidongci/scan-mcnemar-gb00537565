from datetime import datetime
from sqlalchemy import JSON, Float, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base
from src.constants.enums import LookStatus, PlanStatus
from src.utils.time import now


class Plan(Base):
    """A group-sequential acceptance plan, locked before the first look."""

    __tablename__ = 'acceptance_plans'

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey('datasets.id'))
    dataset_checksum: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(120))
    alpha_total: Mapped[float] = mapped_column(Float)
    min_pairs: Mapped[int]
    min_gain_pp: Mapped[float] = mapped_column(Float)
    batches: Mapped[list] = mapped_column(JSON)
    alphas: Mapped[list] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default=PlanStatus.ACTIVE, index=True)
    sealed_look_id: Mapped[int | None]
    sealed_at: Mapped[datetime | None]
    author: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=now)


class Look(Base):
    """One look at the cumulative sample; its alpha and sample are locked."""

    __tablename__ = 'acceptance_looks'
    __table_args__ = (UniqueConstraint('plan_id', 'sequence'),)

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey('acceptance_plans.id'), index=True)
    sequence: Mapped[int]
    batch_id: Mapped[str] = mapped_column(String(120))
    alpha: Mapped[float] = mapped_column(Float)
    min_gain_pp: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(20), default=LookStatus.PENDING, index=True)
    conclusion: Mapped[str | None] = mapped_column(String(20))
    summary: Mapped[dict | None] = mapped_column(JSON)
    result: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(String(500))
    export_file: Mapped[str | None] = mapped_column(String(100))
    attempt: Mapped[int] = mapped_column(default=0)
    lease_until: Mapped[datetime | None]
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    published_at: Mapped[datetime | None]
    published_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=now)
