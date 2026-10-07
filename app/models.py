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
    """灯具实例，引用一个具体模式，可记录上一版地址、可锁定。

    identity_key 是灯具的稳定身份：创建时分配、永不变更，随发布快照持久化。
    改名、换模式、锁定切换都不改变身份；删除后身份进入台账（retired），
    数字 ID 永不再发给新灯——因此跨快照对齐靠身份而不是名称或可复用的整数 ID。
    升级前的旧灯具在迁移时补齐身份；旧快照里没有身份记录。
    """

    __tablename__ = "fixtures"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    identity_key: Mapped[str | None] = mapped_column(String(32), nullable=True, unique=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    mode_id: Mapped[int] = mapped_column(
        ForeignKey("fixture_modes.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    previous_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    previous_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    mode: Mapped[FixtureMode] = relationship()


class FixtureIdLedger(Base):
    """灯具数字 ID 台账：每个分配过或被发布明细引用过的 ID 占一行，永不删除。

    作用：持久保证数字 ID 不复用——
    - 发布快照引用过的 ID 留痕（ever_published=True），即使灯具后来删除；
    - 升级后删除的灯具保留为 retired，ID 不再发给新灯；
    - identity_key 是该 ID 对应灯具的稳定身份（删除后仍随台账保留），
      供发布快照跨版本核对“同一台灯”，改名/换模式不影响。
    升级前仅在方案明细中出现、灯具已不在表中的 ID 也补进台账（retired，
    无 identity_key），仅用于占位防复用，不构成跨快照身份依据。
    """

    __tablename__ = "fixture_id_ledger"

    fixture_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    identity_key: Mapped[str | None] = mapped_column(String(32), nullable=True, unique=True)
    # active：当前存在的灯具；retired：已删除（升级后删除）或升级时仅见于历史明细
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="retired")
    ever_published: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)


class FixtureIdSeq(Base):
    """灯具数字 ID 的持久单调计数器（单行，id=1）。

    重启后仍有效：新灯具 ID 恒为“当前值之后的下一个整数”，只增不减；
    台账中历史引用（含升级时从方案明细补录的最大 ID）会在迁移时把计数器
    推过它们，因此 SQLite 对已删除最大主键的复用被从应用层彻底消除。
    """

    __tablename__ = "fixture_id_seq"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    next_id: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


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
