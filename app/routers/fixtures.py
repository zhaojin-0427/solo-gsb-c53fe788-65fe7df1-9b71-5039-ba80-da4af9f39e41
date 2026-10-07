"""灯具实例 CRUD。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import identity, schemas
from ..database import get_db
from ..models import Fixture, FixtureMode, FixtureType
from ..services import bump_config_version

router = APIRouter(prefix="/api/fixtures", tags=["fixtures"])


def _out(db: Session, fx: Fixture) -> dict:
    mode = db.get(FixtureMode, fx.mode_id)
    t = db.get(FixtureType, mode.type_id) if mode else None
    return {
        "id": fx.id,
        "name": fx.name,
        "mode_id": fx.mode_id,
        "previous_line": fx.previous_line,
        "previous_start": fx.previous_start,
        "locked": fx.locked,
        "type_name": f"{t.manufacturer} {t.name}" if t else None,
        "mode_name": mode.name if mode else None,
        "channels": mode.channels if mode else None,
    }


@router.get("", response_model=list[schemas.FixtureOut])
def list_fixtures(db: Session = Depends(get_db)):
    return [_out(db, fx) for fx in db.query(Fixture).order_by(Fixture.id).all()]


@router.post("", response_model=schemas.FixtureOut, status_code=201)
def create_fixture(body: schemas.FixtureIn, db: Session = Depends(get_db)):
    mode = db.get(FixtureMode, body.mode_id)
    if mode is None:
        raise HTTPException(status_code=404, detail="模式不存在，请先导入型录")
    if db.query(Fixture).filter(func.lower(Fixture.name) == body.name.lower()).first():
        raise HTTPException(status_code=409, detail=f"灯具名称“{body.name}”已存在")
    if body.previous_start is not None:
        max_start = 512 - mode.channels + 1
        if body.previous_start > max_start:
            raise HTTPException(
                status_code=422,
                detail=f"该模式占 {mode.channels} 通道，起始地址不能超过 {max_start}",
            )
    # 持久分配数字 ID 与稳定身份：ID 单调不复用（删灯后新建也拿新 ID），
    # 身份写入台账并随灯具/发布快照留存——与配置版本推进在同一事务提交。
    fixture_id, identity_key = identity.allocate_fixture(db)
    fx = Fixture(
        id=fixture_id,
        identity_key=identity_key,
        name=body.name,
        mode_id=body.mode_id,
        previous_line=body.previous_line,
        previous_start=body.previous_start,
        locked=body.locked,
    )
    db.add(fx)
    bump_config_version(db)
    db.commit()
    db.refresh(fx)
    return _out(db, fx)


def _get_fixture(db: Session, fixture_id: int) -> Fixture:
    fx = db.get(Fixture, fixture_id)
    if fx is None:
        raise HTTPException(status_code=404, detail="灯具不存在")
    return fx


@router.put("/{fixture_id}", response_model=schemas.FixtureOut)
def update_fixture(fixture_id: int, body: schemas.FixtureUpdate, db: Session = Depends(get_db)):
    fx = _get_fixture(db, fixture_id)
    data = body.model_dump(exclude_unset=True)
    mode = db.get(FixtureMode, data.get("mode_id", fx.mode_id))
    if "mode_id" in data and mode is None:
        raise HTTPException(status_code=404, detail="模式不存在")
    if "name" in data and data["name"] is not None:
        clash = (
            db.query(Fixture)
            .filter(Fixture.id != fx.id, func.lower(Fixture.name) == data["name"].lower())
            .first()
        )
        if clash:
            raise HTTPException(status_code=409, detail=f"灯具名称“{data['name']}”已存在")
    # 合并后校验：锁必须有既有地址；起始地址不得超出模式通道
    merged_line = data.get("previous_line", fx.previous_line)
    merged_start = data.get("previous_start", fx.previous_start)
    merged_locked = data.get("locked", fx.locked)
    if merged_locked and (merged_line is None or merged_start is None):
        raise HTTPException(status_code=422, detail="锁定灯具必须提供既有线路与起始地址")
    if merged_start is not None and mode is not None:
        max_start = 512 - mode.channels + 1
        if merged_start > max_start:
            raise HTTPException(
                status_code=422,
                detail=f"该模式占 {mode.channels} 通道，起始地址不能超过 {max_start}",
            )
    for key, value in data.items():
        setattr(fx, key, value)
    db.commit()
    db.refresh(fx)
    bump_config_version(db)
    db.commit()
    return _out(db, fx)


@router.delete("/{fixture_id}", status_code=204)
def delete_fixture(fixture_id: int, db: Session = Depends(get_db)):
    fx = _get_fixture(db, fixture_id)
    db.delete(fx)
    # 台账保留该 ID 与身份（retired）：发布快照引用过它，且该 ID 永不再发给新灯
    identity.retire_fixture(db, fixture_id)
    bump_config_version(db)
    db.commit()
