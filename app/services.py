"""初始化默认数据与配置版本管理。"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import update
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from .database import Base, engine
from .models import ConfigVersion, Line


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    _migrate_schema()
    with Session(engine) as db:
        if db.get(ConfigVersion, 1) is None:
            db.add(ConfigVersion(id=1, version=1))
        existing = {row.id for row in db.query(Line.id).all()}
        for line_id, default_name in [(1, "线路 1"), (2, "线路 2"), (3, "线路 3"), (4, "线路 4")]:
            if line_id not in existing:
                db.add(Line(id=line_id, name=default_name, is_active=True))
        db.commit()


def _migrate_schema() -> None:
    """轻量迁移：为旧库补齐新增列（SQLite 不支持 IF NOT EXISTS ADD COLUMN）。"""
    inspector = inspect(engine)
    plan_columns = {col["name"] for col in inspector.get_columns("plans")}
    with engine.begin() as conn:
        if "optimality_status" not in plan_columns:
            conn.execute(text("ALTER TABLE plans ADD COLUMN optimality_status VARCHAR(16)"))
        # 已发布的历史方案保留其当时状态；遗留草稿保持 NULL，发布时据此拒发。
        conn.execute(
            text("UPDATE plans SET optimality_status = 'optimal' WHERE status = 'published' AND optimality_status IS NULL")
        )


def bump_config_version(db: Session) -> int:
    """任何影响编排结果的配置变化后调用，返回新版本号。"""
    db.execute(
        update(ConfigVersion)
        .where(ConfigVersion.id == 1)
        .values(version=ConfigVersion.version + 1, updated_at=datetime.now(timezone.utc))
    )
    return get_config_version(db)


def get_config_version(db: Session) -> int:
    row = db.get(ConfigVersion, 1)
    return row.version if row else 0
