"""ORM 模型：型录、灯具实例、线路、保留区、配置版本、预览与发布快照。"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class FixtureType(Base):
    """灯具型录中的型号（manufacturer + name 唯一）。"""

    __tablename__ = "fixture_types"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    manufacturer: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)

    modes: Mapped[list[FixtureMode]] = relationship(
        back_populates="fixture_type",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (UniqueConstraint("manufacturer", "name", name="uq_type_mfr_name"),)


class FixtureMode(Base):
    """型号下的 DMX 模式，占用 channels 个连续通道。"""

    __tablename__ = "fixture_modes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    type_id: Mapped[int] = mapped_column(
        ForeignKey("fixture_types.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    channels: Mapped[int] = mapped_column(Integer, nullable=False)

    fixture_type: Mapped[FixtureType] = relationship(back_populates="modes")

    __table_args__ = (UniqueConstraint("type_id", "name", name="uq_mode_type_name"),)


class Fixture(Base):
    """灯具实例，引用一个具体模式，可记录上一版地址、可锁定。"""

    __tablename__ = "fixtures"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    mode_id: Mapped[int] = mapped_column(
        ForeignKey("fixture_modes.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    previous_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    previous_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    mode: Mapped[FixtureMode] = relationship()


class Line(Base):
    """DMX 线路，固定四条；可重命名、可停用。"""

    __tablename__ = "lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class Reservation(Base):
    """某条线路上的保留通道区，任何灯具都不得占用。"""

    __tablename__ = "reservations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    line_id: Mapped[int] = mapped_column(
        ForeignKey("lines.id", ondelete="CASCADE"), nullable=False, index=True
    )
    start: Mapped[int] = mapped_column(Integer, nullable=False)
    end: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(256), nullable=False, default="")


class ConfigVersion(Base):
    """单行版本计数器：任何影响编排的配置变化都会令其自增。"""

    __tablename__ = "config_version"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False, default=1)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)


class Plan(Base):
    """一次求解生成的预览（草稿），发布成功后转为 published。"""

    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    config_version: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    # 最优性结论：optimal 表示三级目标均已证明最优，方可发布；
    # NULL 仅出现在旧版本遗留草稿上，发布时一律拒绝。
    optimality_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    migration_count: Mapped[int] = mapped_column(Integer, nullable=False)
    used_lines: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    allocations: Mapped[list[PlanAllocation]] = relationship(
        back_populates="plan",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class PlanAllocation(Base):
    """预览/发布方案中单台灯具的线路与起始地址。"""

    __tablename__ = "plan_allocations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_id: Mapped[int] = mapped_column(
        ForeignKey("plans.id", ondelete="CASCADE"), nullable=False, index=True
    )
    fixture_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    line_id: Mapped[int] = mapped_column(Integer, nullable=False)
    start: Mapped[int] = mapped_column(Integer, nullable=False)
    end: Mapped[int] = mapped_column(Integer, nullable=False)
    migrated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    plan: Mapped[Plan] = relationship(back_populates="allocations")


class PublishedSnapshot(Base):
    """每次发布写入一条快照记录，完整地址表保存在 payload JSON 中。"""

    __tablename__ = "published_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_id: Mapped[int] = mapped_column(
        ForeignKey("plans.id", ondelete="RESTRICT"), nullable=False
    )
    config_version: Mapped[int] = mapped_column(Integer, nullable=False)
    published_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
