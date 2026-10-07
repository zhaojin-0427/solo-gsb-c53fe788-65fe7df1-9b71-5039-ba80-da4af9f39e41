"""初始化默认数据、配置版本管理与灯具 ID 单调分配。"""
from __future__ import annotations

import json
import uuid
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
    fixture_columns = {col["name"] for col in inspector.get_columns("fixtures")}
    with engine.begin() as conn:
        if "optimality_status" not in plan_columns:
            conn.execute(text("ALTER TABLE plans ADD COLUMN optimality_status VARCHAR(16)"))
        # 已发布的历史方案保留其当时状态；遗留草稿保持 NULL，发布时据此拒发。
        conn.execute(
            text("UPDATE plans SET optimality_status = 'optimal' WHERE status = 'published' AND optimality_status IS NULL")
        )
        if "identity" not in fixture_columns:
            conn.execute(text("ALTER TABLE fixtures ADD COLUMN identity VARCHAR(36)"))
        # 存量灯具补发可靠身份记录：逐台生成 UUID，此后改名/换模式均不变。
        # 既有数字 ID 不改写；升级前发布的旧快照没有该记录，换场时按
        # “缺少可核实身份依据”处理（见 changeover 模块），不在此伪造。
        missing_identity = conn.execute(
            text("SELECT id FROM fixtures WHERE identity IS NULL")
        ).all()
        for (fixture_id,) in missing_identity:
            conn.execute(
                text("UPDATE fixtures SET identity = :uid WHERE id = :fid"),
                {"uid": str(uuid.uuid4()), "fid": fixture_id},
            )
        _reconcile_fixture_id_counter(conn)


def _reconcile_fixture_id_counter(conn) -> None:
    """灯具 ID 计数器与历史数据对账（只升不降）。

    扫描当前灯具、全部方案留存明细与全部已发布快照中出现过的灯具 ID，
    保证计数器永远大于任何曾被使用/引用过的 ID：已发布快照引用过的 ID、
    升级后删除的灯具 ID 都不会再发给新灯，重启后仍然有效。
    既有数字 ID 与快照内容一律不改写，本函数只读取它们。
    """
    max_seen = 0
    for sql in (
        "SELECT COALESCE(MAX(id), 0) FROM fixtures",
        "SELECT COALESCE(MAX(fixture_id), 0) FROM plan_allocations",
    ):
        max_seen = max(max_seen, conn.execute(text(sql)).scalar_one())
    for (payload,) in conn.execute(text("SELECT payload FROM published_snapshots")):
        try:
            stored = json.loads(payload)
        except (TypeError, ValueError):
            continue  # 损坏快照按缺快照处理（查询路径已有回退），此处跳过
        for alloc in stored.get("allocations", []):
            fid = alloc.get("fixture_id")
            if isinstance(fid, int) and not isinstance(fid, bool):
                max_seen = max(max_seen, fid)
    floor = max_seen + 1
    row = conn.execute(text("SELECT next_id FROM fixture_id_counter WHERE id = 1")).first()
    if row is None:
        conn.execute(
            text("INSERT INTO fixture_id_counter (id, next_id) VALUES (1, :n)"), {"n": floor}
        )
    elif row[0] < floor:
        conn.execute(
            text("UPDATE fixture_id_counter SET next_id = :n WHERE id = 1"), {"n": floor}
        )


def allocate_fixture_id(db: Session) -> int:
    """单调分配一个灯具 ID（在当前事务内生效，随事务提交持久化）。

    先 UPDATE 再 SELECT：UPDATE 立即取得 SQLite 写锁，并发创建被串行化，
    两个请求不会分到同一 ID；计数器只升不降，删除灯具不会释放其 ID。
    """
    db.execute(text("UPDATE fixture_id_counter SET next_id = next_id + 1 WHERE id = 1"))
    new_next = db.execute(
        text("SELECT next_id FROM fixture_id_counter WHERE id = 1")
    ).scalar_one()
    return new_next - 1


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
