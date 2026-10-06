"""保留区管理。同线路保留区间不允许重叠。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import schemas
from ..database import get_db
from ..models import Line, Reservation
from ..services import bump_config_version

router = APIRouter(prefix="/api/reservations", tags=["reservations"])


@router.get("", response_model=list[schemas.ReservationOut])
def list_reservations(db: Session = Depends(get_db)):
    return db.query(Reservation).order_by(Reservation.line_id, Reservation.start).all()


@router.post("", response_model=schemas.ReservationOut, status_code=201)
def create_reservation(body: schemas.ReservationIn, db: Session = Depends(get_db)):
    if db.get(Line, body.line_id) is None:
        raise HTTPException(status_code=404, detail="线路不存在")
    clash = (
        db.query(Reservation)
        .filter(
            Reservation.line_id == body.line_id,
            Reservation.start <= body.end,
            Reservation.end >= body.start,
        )
        .first()
    )
    if clash:
        raise HTTPException(
            status_code=409,
            detail=(
                f"与线路 {body.line_id} 上已有保留区 #{clash.id}"
                f"（{clash.start}-{clash.end}）重叠"
            ),
        )
    r = Reservation(**body.model_dump())
    db.add(r)
    db.commit()
    db.refresh(r)
    bump_config_version(db)
    db.commit()
    return r


@router.delete("/{reservation_id}", status_code=204)
def delete_reservation(reservation_id: int, db: Session = Depends(get_db)):
    r = db.get(Reservation, reservation_id)
    if r is None:
        raise HTTPException(status_code=404, detail="保留区不存在")
    db.delete(r)
    bump_config_version(db)
    db.commit()
