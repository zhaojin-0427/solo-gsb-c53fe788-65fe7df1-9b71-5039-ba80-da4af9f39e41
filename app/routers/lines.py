"""四条 DMX 线路：重命名 / 启停。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import schemas
from ..database import get_db
from ..models import Fixture, Line
from ..services import bump_config_version

router = APIRouter(prefix="/api/lines", tags=["lines"])


@router.get("", response_model=list[schemas.LineOut])
def list_lines(db: Session = Depends(get_db)):
    return db.query(Line).order_by(Line.id).all()


@router.patch("/{line_id}", response_model=schemas.LineOut)
def update_line(line_id: int, body: schemas.LineUpdate, db: Session = Depends(get_db)):
    line = db.get(Line, line_id)
    if line is None:
        raise HTTPException(status_code=404, detail="线路不存在")
    if body.name is not None:
        line.name = body.name
    if body.is_active is not None:
        if line.is_active and not body.is_active:
            locked_here = (
                db.query(Fixture)
                .filter(Fixture.locked.is_(True), Fixture.previous_line == line_id)
                .count()
            )
            if locked_here:
                raise HTTPException(
                    status_code=409,
                    detail=f"线路 {line_id} 上仍有 {locked_here} 台锁定灯具，不能停用",
                )
        line.is_active = body.is_active
    bump_config_version(db)
    db.commit()
    db.refresh(line)
    return line
