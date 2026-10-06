"""灯具型录：型号/模式的增删与 CSV/JSON 批量导入。

CSV 格式（首行表头，列名不区分大小写）：
    manufacturer,name,mode,channels
同一 (manufacturer,name) 的多个模式用多行表示；重复模式更新通道数。
"""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import schemas
from ..database import get_db
from ..models import Fixture, FixtureMode, FixtureType
from ..services import bump_config_version

router = APIRouter(prefix="/api/catalog", tags=["catalog"])


def _get_type(db: Session, type_id: int) -> FixtureType:
    t = db.get(FixtureType, type_id)
    if t is None:
        raise HTTPException(status_code=404, detail="型号不存在")
    return t


@router.get("", response_model=list[schemas.FixtureTypeOut])
def list_types(db: Session = Depends(get_db)):
    return db.query(FixtureType).order_by(FixtureType.manufacturer, FixtureType.name).all()


@router.post("", response_model=schemas.FixtureTypeOut, status_code=201)
def create_type(body: schemas.FixtureTypeIn, db: Session = Depends(get_db)):
    if db.query(FixtureType).filter_by(manufacturer=body.manufacturer, name=body.name).first():
        raise HTTPException(status_code=409, detail=f"型号 {body.manufacturer} {body.name} 已存在")
    mode_names = [m.name for m in body.modes]
    if len(set(mode_names)) != len(mode_names):
        raise HTTPException(status_code=422, detail="同一型号下模式名不能重复")
    t = FixtureType(manufacturer=body.manufacturer, name=body.name)
    t.modes = [FixtureMode(name=m.name, channels=m.channels) for m in body.modes]
    db.add(t)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="型号已存在")
    db.refresh(t)
    return t


@router.get("/{type_id}", response_model=schemas.FixtureTypeOut)
def get_type(type_id: int, db: Session = Depends(get_db)):
    return _get_type(db, type_id)


@router.delete("/{type_id}", status_code=204)
def delete_type(type_id: int, db: Session = Depends(get_db)):
    t = _get_type(db, type_id)
    used = (
        db.query(Fixture)
        .join(FixtureMode, Fixture.mode_id == FixtureMode.id)
        .filter(FixtureMode.type_id == t.id)
        .count()
    )
    if used:
        raise HTTPException(status_code=409, detail="该型号下仍有灯具实例，不能删除")
    db.delete(t)
    bump_config_version(db)
    db.commit()


def _parse_csv(content: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(content))
    if reader.fieldnames is None:
        raise HTTPException(status_code=422, detail="CSV 缺少表头")
    header = {h.strip().lower(): h for h in reader.fieldnames}
    required = ["manufacturer", "name", "mode", "channels"]
    missing = [c for c in required if c not in header]
    if missing:
        raise HTTPException(status_code=422, detail=f"CSV 缺少列：{', '.join(missing)}")
    rows = []
    for i, raw in enumerate(reader, start=2):
        try:
            rows.append(
                {
                    "manufacturer": raw[header["manufacturer"]].strip(),
                    "name": raw[header["name"]].strip(),
                    "mode": raw[header["mode"]].strip(),
                    "channels": int(raw[header["channels"]]),
                }
            )
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail=f"CSV 第 {i} 行 channels 不是整数")
    if not rows:
        raise HTTPException(status_code=422, detail="CSV 没有数据行")
    return rows


def _import_rows(db: Session, rows: list[dict]) -> dict:
    created_types = updated_types = created_modes = updated_modes = 0
    for row in rows:
        if not row["manufacturer"] or not row["name"] or not row["mode"]:
            raise HTTPException(status_code=422, detail="manufacturer/name/mode 不能为空")
        if not 1 <= row["channels"] <= 512:
            raise HTTPException(status_code=422, detail=f"模式 {row['mode']} 通道数须在 1..512")
        t = (
            db.query(FixtureType)
            .filter_by(manufacturer=row["manufacturer"], name=row["name"])
            .first()
        )
        if t is None:
            t = FixtureType(manufacturer=row["manufacturer"], name=row["name"])
            db.add(t)
            db.flush()
            created_types += 1
        else:
            updated_types += 1
        m = db.query(FixtureMode).filter_by(type_id=t.id, name=row["mode"]).first()
        if m is None:
            db.add(FixtureMode(type_id=t.id, name=row["mode"], channels=row["channels"]))
            created_modes += 1
        else:
            if m.channels != row["channels"]:
                m.channels = row["channels"]
                updated_modes += 1
    db.commit()
    return {
        "created_types": created_types,
        "updated_types": updated_types,
        "created_modes": created_modes,
        "updated_modes": updated_modes,
    }


@router.post("/import")
async def import_catalog(file: UploadFile = File(...), db: Session = Depends(get_db)):
    raw = (await file.read()).decode("utf-8-sig")
    filename = (file.filename or "").lower()
    if filename.endswith(".json") or raw.lstrip().startswith(("[", "{")):
        import json

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=422, detail=f"JSON 解析失败：{exc}")
        if isinstance(data, dict):
            data = [data]
        rows = []
        for item in data:
            try:
                rows.append(
                    {
                        "manufacturer": str(item["manufacturer"]).strip(),
                        "name": str(item["name"]).strip(),
                        "mode": str(item["mode"]).strip(),
                        "channels": int(item["channels"]),
                    }
                )
            except (KeyError, TypeError, ValueError):
                raise HTTPException(status_code=422, detail="JSON 每项需含 manufacturer/name/mode/channels")
        result = _import_rows(db, rows)
    else:
        result = _import_rows(db, _parse_csv(raw))
    # 新增模式不会影响既有灯具，但新增/更新型录整体推一版，使界面保持一致语义
    bump_config_version(db)
    db.commit()
    return {"ok": True, **result}
