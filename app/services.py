"""初始化默认数据、灯具 ID 台账/计数器迁移与配置版本管理。"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import inspect, text, update
from sqlalchemy.orm import Session

from . import identity
from .database import Base, engine
from .models import ConfigVersion, Fixture, FixtureIdLedger, FixtureIdSeq, Line, Plan, PlanAllocation


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    _migrate_schema()
    _migrate_fixture_identity()
    with Session(engine) as db:
        if db.get(ConfigVersion, 1) is None:
            db.add(ConfigVersion(id=1, version=1))
        existing = {row.id for row in db.query(Line.id).all()}
        for line_id, default_name in [(1, "线路 1"), (2, "线路 2"), (3, "线路 3"), (4, "线路 4")]:
            if line_id not in existing:
                db.add(Line(id=line_id, name=default_name, is_active=True))
        db.commit()


def _migrate_schema() -> None:
    """轻量迁移：为旧库补齐新增列/表（SQLite 不支持 IF NOT EXISTS ADD COLUMN）。"""
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
        # 灯具稳定身份列（新库建表时已带；仅旧库需要 ALTER）
        if "identity_key" not in fixture_columns:
            conn.execute(text("ALTER TABLE fixtures ADD COLUMN identity_key VARCHAR(32)"))


def _migrate_fixture_identity() -> None:
    """把旧库升级到“ID 永不复用 + 稳定身份”模型（幂等，可重复执行）。

    1. 现存灯具（升级前创建）补齐 identity_key——它们的身份从这一刻起可核实，
       改名/换模式仍按原 ID 比较；
    2. 台账登记所有现存灯具（active），并从全部方案明细（含已发布快照引用、
       草稿）补录已不在灯具表中的 ID（retired）：这些 ID 一律不得再发出；
    3. 已发布方案引用过的台账 ID 标记 ever_published；
    4. 单调计数器推过“现存 ID 与历史明细 ID 的最大值”，使删除最大 ID 后
       SQLite 的行 ID 复用从应用层被彻底消除——重启后依然有效。

    注意：升级前的旧发布快照不携带 identity_key，无法核实“同 ID 即同一台灯”
    （旧库期间该 ID 可能已被复用），涉及共同 ID 的旧快照换场由 changeover
    模块显式拒绝并说明原因，不在此猜测身份（绝不按名称猜）。
    """
    with Session(engine) as db:
        # 1. 现存灯具补齐身份
        keyless = db.query(Fixture).filter(Fixture.identity_key.is_(None)).all()
        for fx in keyless:
            fx.identity_key = identity.new_identity_key()

        # 2. 现存灯具登记台账（已存在则只校正状态，不覆盖身份/发布留痕）
        for fx in db.query(Fixture).all():
            ledger = db.get(FixtureIdLedger, fx.id)
            if ledger is None:
                db.add(
                    FixtureIdLedger(
                        fixture_id=fx.id,
                        identity_key=fx.identity_key,
                        status="active",
                        ever_published=False,
                    )
                )
            elif ledger.status != "active":
                ledger.status = "active"

        # 3. 方案明细中引用过的灯具 ID：已发布的标记留痕；不在灯具表的补录 retired
        published_ids = {
            row.fixture_id
            for row in (
                db.query(PlanAllocation.fixture_id)
                .join(Plan, PlanAllocation.plan_id == Plan.id)
                .filter(Plan.status == "published")
                .all()
            )
        }
        referenced_ids = {row.fixture_id for row in db.query(PlanAllocation.fixture_id).distinct().all()}
        existing_fixture_ids = {row.id for row in db.query(Fixture.id).all()}
        for fid in sorted(referenced_ids | existing_fixture_ids):
            ledger = db.get(FixtureIdLedger, fid)
            if ledger is None:
                db.add(
                    FixtureIdLedger(
                        fixture_id=fid,
                        identity_key=None,
                        status="retired",
                        ever_published=fid in published_ids,
                    )
                )
            elif fid in published_ids:
                ledger.ever_published = True

        # 4. 计数器推过所有已知 ID（台账已覆盖现存 + 历史引用）。
        # 只增不减：幂等重跑时保留已推进的计数器，绝不回退到更小值。
        db.flush()
        max_id = db.query(FixtureIdLedger.fixture_id).order_by(FixtureIdLedger.fixture_id.desc()).first()
        required_next = (max_id[0] if max_id else 0) + 1
        seq = db.get(FixtureIdSeq, 1)
        if seq is None:
            db.add(FixtureIdSeq(id=1, next_id=required_next))
        elif seq.next_id < required_next:
            seq.next_id = required_next

        db.commit()


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
