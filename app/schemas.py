"""API 请求/响应模型。"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator


# ---- 型录 ----
class ModeIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    channels: int = Field(ge=1, le=512)


class ModeOut(ModeIn):
    id: int
    type_id: int


class FixtureTypeIn(BaseModel):
    manufacturer: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=128)
    modes: list[ModeIn] = Field(min_length=1)


class FixtureTypeOut(BaseModel):
    id: int
    manufacturer: str
    name: str
    modes: list[ModeOut]

    model_config = {"from_attributes": True}


class FixtureTypeRef(BaseModel):
    id: int
    manufacturer: str
    name: str
    mode: ModeOut
    channels: int


# ---- 灯具实例 ----
class FixtureIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    mode_id: int
    previous_line: int | None = Field(default=None, ge=1, le=4)
    previous_start: int | None = Field(default=None, ge=1, le=512)
    locked: bool = False

    @field_validator("previous_start")
    @classmethod
    def _start_needs_line(cls, v, info):
        line = info.data.get("previous_line")
        if v is not None and line is None:
            raise ValueError("指定起始地址时必须同时指定既有线路")
        return v

    @field_validator("locked")
    @classmethod
    def _lock_needs_address(cls, v, info):
        if v and (info.data.get("previous_line") is None or info.data.get("previous_start") is None):
            raise ValueError("锁定灯具必须提供既有线路与起始地址")
        return v


class FixtureUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    mode_id: int | None = None
    previous_line: int | None = Field(default=None, ge=1, le=4)
    previous_start: int | None = Field(default=None, ge=1, le=512)
    locked: bool | None = None


class FixtureOut(BaseModel):
    id: int
    name: str
    mode_id: int
    previous_line: int | None
    previous_start: int | None
    locked: bool
    type_name: str | None = None
    mode_name: str | None = None
    channels: int | None = None

    model_config = {"from_attributes": True}


# ---- 线路 ----
class LineUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    is_active: bool | None = None


class LineOut(BaseModel):
    id: int
    name: str
    is_active: bool

    model_config = {"from_attributes": True}


# ---- 保留区 ----
class ReservationIn(BaseModel):
    line_id: int = Field(ge=1, le=4)
    start: int = Field(ge=1, le=512)
    end: int = Field(ge=1, le=512)
    reason: str = Field(default="", max_length=256)

    @field_validator("end")
    @classmethod
    def _order(cls, v, info):
        s = info.data.get("start")
        if s is not None and v < s:
            raise ValueError("结束地址不能小于起始地址")
        return v


class ReservationOut(ReservationIn):
    id: int

    model_config = {"from_attributes": True}


# ---- 编排/发布 ----
class AllocationOut(BaseModel):
    fixture_id: int
    name: str
    line_id: int
    start: int
    end: int
    channels: int
    migrated: bool
    locked: bool


class MigrationItem(BaseModel):
    fixture_id: int
    name: str
    from_line: int | None
    from_start: int | None
    to_line: int
    to_start: int
    locked: bool


class PlanOut(BaseModel):
    id: int
    config_version: int
    status: str
    # optimal / unproven / infeasible；旧库遗留草稿可能为 null
    optimality_status: str | None = None
    migration_count: int
    used_lines: int
    created_at: datetime
    allocations: list[AllocationOut]
    migrations: list[MigrationItem]
    occupancy: list[dict]


class PublishResult(BaseModel):
    ok: bool
    plan_id: int
    config_version: int
    optimality_status: str
    published_at: datetime


class ConflictOut(BaseModel):
    type: str
    message: str


class VersionOut(BaseModel):
    config_version: int
    latest_published_plan_id: int | None
    latest_published_at: datetime | None
