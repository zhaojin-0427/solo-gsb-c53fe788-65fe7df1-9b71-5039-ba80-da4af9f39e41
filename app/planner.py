"""编排生成、发布与发布快照构建。"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy.orm import Session

from . import solver
from .models import (
    Fixture,
    FixtureMode,
    FixtureType,
    Line,
    Plan,
    PlanAllocation,
    PublishedSnapshot,
    Reservation,
)
from .services import bump_config_version, get_config_version


def build_spec(db: Session) -> solver.SolveSpec:
    lines = [
        solver.LineSpec(id=l.id, name=l.name, active=l.is_active)
        for l in db.query(Line).order_by(Line.id).all()
    ]
    reservations = [
        solver.ReservationSpec(id=r.id, line_id=r.line_id, start=r.start, end=r.end, reason=r.reason)
        for r in db.query(Reservation).order_by(Reservation.id).all()
    ]
    rows = (
        db.query(Fixture, FixtureMode, FixtureType)
        .join(FixtureMode, Fixture.mode_id == FixtureMode.id)
        .join(FixtureType, FixtureMode.type_id == FixtureType.id)
        .order_by(Fixture.id)
        .all()
    )
    fixtures = [
        solver.FixtureSpec(
            id=fx.id,
            name=fx.name,
            channels=mode.channels,
            prev_line=fx.previous_line,
            prev_start=fx.previous_start,
            locked=fx.locked,
        )
        for fx, mode, _t in rows
    ]
    return solver.SolveSpec(fixtures=fixtures, reservations=reservations, lines=lines)


def create_preview(db: Session) -> dict:
    spec = build_spec(db)
    result = solver.solve(spec)
    version = get_config_version(db)
    if result.optimality == solver.INFEASIBLE:
        return {
            "feasible": False,
            "optimality_status": solver.INFEASIBLE,
            "config_version": version,
            "conflicts": result.conflicts,
            "occupancy": [],
        }

    if not result.is_optimal:
        # 未证最优：绝不生成可发布草稿。是否取得完整可用排布决定 feasible——
        # 有候选则 feasible=True 供参考展示；空候选（UNKNOWN/候选读取失败/
        # 诊断超时）则 feasible=False，仅表示暂无可展示排布，不代表已证无解。
        return _unproven_payload(db, result, version)

    plan = Plan(
        config_version=version,
        status="draft",
        optimality_status=solver.OPTIMAL,
        migration_count=result.migration_count,
        used_lines=result.used_lines,
    )
    db.add(plan)
    db.flush()
    for a in result.allocations:
        db.add(
            PlanAllocation(
                plan_id=plan.id,
                fixture_id=a.fixture_id,
                line_id=a.line_id,
                start=a.start,
                end=a.end,
                migrated=a.migrated,
            )
        )
    db.commit()
    db.refresh(plan)
    return _plan_payload(db, plan, feasible=True)


def _unproven_payload(db: Session, result: solver.SolveResult, version: int) -> dict:
    """未证最优时返回的预览（无 plan_id，不可发布）。

    feasible 按是否取得完整可用排布裁决：有候选时为 True（仅供查看）；
    空候选（求解 UNKNOWN、候选读取失败或冲突诊断超时）时为 False，
    仍为 HTTP 200 并携带 unproven 状态与阶段信息，不当作已证无解。
    """
    has_candidate = result.feasible
    names = {f.id: f.name for f in db.query(Fixture).all()}
    locked_ids = {f.id for f in db.query(Fixture).filter(Fixture.locked.is_(True)).all()}
    prev = {f.id: (f.previous_line, f.previous_start, f.locked) for f in db.query(Fixture).all()}
    alloc_out = [
        {
            "fixture_id": a.fixture_id,
            "name": names.get(a.fixture_id, f"#{a.fixture_id}"),
            "line_id": a.line_id,
            "start": a.start,
            "end": a.end,
            "channels": a.channels,
            "migrated": a.migrated,
            "locked": a.fixture_id in locked_ids,
        }
        for a in result.allocations
    ]
    migrations = []
    for a in alloc_out:
        old_line, old_start, _locked = prev.get(a["fixture_id"], (None, None, False))
        if old_line is not None and (old_line, old_start) != (a["line_id"], a["start"]):
            migrations.append(
                {
                    "fixture_id": a["fixture_id"],
                    "name": a["name"],
                    "from_line": old_line,
                    "from_start": old_start,
                    "to_line": a["line_id"],
                    "to_start": a["start"],
                    "locked": a["fixture_id"] in locked_ids,
                }
            )
    return {
        "feasible": has_candidate,
        "optimality_status": solver.UNPROVEN,
        "unproven_stage": result.unproven_stage,
        "message": result.unproven_message,
        "config_version": version,
        "migration_count": result.migration_count,
        "used_lines": result.used_lines,
        "allocations": alloc_out,
        "migrations": migrations,
        "occupancy": result.occupancy,
        "conflicts": result.conflicts,
    }


def _plan_payload(db: Session, plan: Plan, *, feasible: bool = True) -> dict:
    allocs = (
        db.query(PlanAllocation)
        .filter(PlanAllocation.plan_id == plan.id)
        .order_by(PlanAllocation.fixture_id)
        .all()
    )
    names = {f.id: f.name for f in db.query(Fixture).all()}
    locked_ids = {f.id for f in db.query(Fixture).filter(Fixture.locked.is_(True)).all()}
    alloc_out = [
        {
            "fixture_id": a.fixture_id,
            "name": names.get(a.fixture_id, f"#{a.fixture_id}"),
            "line_id": a.line_id,
            "start": a.start,
            "end": a.end,
            "channels": a.end - a.start + 1,
            "migrated": a.migrated,
            "locked": a.fixture_id in locked_ids,
        }
        for a in allocs
    ]
    return {
        "feasible": feasible,
        "id": plan.id,
        "plan_id": plan.id,
        "config_version": plan.config_version,
        "status": plan.status,
        "optimality_status": plan.optimality_status,
        "migration_count": plan.migration_count,
        "used_lines": plan.used_lines,
        "created_at": plan.created_at,
        "published_at": plan.published_at,
        # 本函数始终按当前灯具/线路/保留区实时重建，不是发布快照
        "from_snapshot": False,
        "allocations": alloc_out,
        "migrations": _migrations_from_alloc(db, alloc_out),
        "occupancy": _occupancy_for(db, allocs),
        "conflicts": [],
    }


def _migrations_from_alloc(db: Session, alloc_out: list[dict]) -> list[dict]:
    prev = {f.id: (f.previous_line, f.previous_start, f.locked) for f in db.query(Fixture).all()}
    items = []
    for a in alloc_out:
        old_line, old_start, locked = prev.get(a["fixture_id"], (None, None, False))
        # 从未配置过的新灯具不算迁移
        if old_line is not None and (old_line, old_start) != (a["line_id"], a["start"]):
            items.append(
                {
                    "fixture_id": a["fixture_id"],
                    "name": a["name"],
                    "from_line": old_line,
                    "from_start": old_start,
                    "to_line": a["line_id"],
                    "to_start": a["start"],
                    "locked": locked,
                }
            )
    return items


def _occupancy_for(db: Session, allocs: list[PlanAllocation]) -> list[dict]:
    lines = db.query(Line).order_by(Line.id).all()
    reservations = db.query(Reservation).all()
    names = {f.id: f.name for f in db.query(Fixture).all()}
    locked_ids = {f.id for f in db.query(Fixture).filter(Fixture.locked.is_(True)).all()}
    blocks: dict[int, list[dict]] = {l.id: [] for l in lines}
    for a in allocs:
        blocks[a.line_id].append(
            {
                "fixture_id": a.fixture_id,
                "name": names.get(a.fixture_id, f"#{a.fixture_id}"),
                "start": a.start,
                "end": a.end,
                "channels": a.end - a.start + 1,
                "migrated": a.migrated,
                "locked": a.fixture_id in locked_ids,
            }
        )
    out = []
    for line in lines:
        res = [
            {"id": r.id, "start": r.start, "end": r.end, "reason": r.reason}
            for r in sorted((r for r in reservations if r.line_id == line.id), key=lambda r: r.start)
        ]
        occupied: set[int] = set()
        for b in blocks[line.id]:
            occupied.update(range(b["start"], b["end"] + 1))
        for r in res:
            occupied.update(range(r["start"], r["end"] + 1))
        out.append(
            {
                "line_id": line.id,
                "name": line.name,
                "is_active": line.is_active,
                "size": solver.LINE_SIZE,
                "used_channels": len(occupied),
                "free_channels": solver.LINE_SIZE - len(occupied),
                "fixtures": sorted(blocks[line.id], key=lambda b: b["start"]),
                "reservations": res,
            }
        )
    return out


#: 无快照旧发布记录的字段来源裁决（随响应返回，供页面/调用方核对）
LEGACY_FIELD_SOURCES = {
    # PlanAllocation 留存的线路、起止地址与逐台迁移标记 = 发布时事实
    "allocations": "publish_record",
    # 灯具/线路名称取当前值，随改名漂移，仅标注来源后展示
    "names": "current",
    # 历史锁定状态无法还原 -> 未知（null），不得写成“否”
    "locked": "unknown",
    # 迁移起点（原线路/原地址）无法还原 -> 未知（null）
    "migration_origin": "unknown",
    # 发布时的保留区占用无法还原 -> 未知（null）
    "reservations": "unknown",
}

LEGACY_NOTICE = (
    "该发布记录缺少快照：线路、起止地址与是否迁移取自留存的方案明细，为发布时事实；"
    "灯具/线路名称为当前值（已标注来源）；历史锁定状态、迁移起点与保留区占用无法还原，"
    "已标为未知（null），不代表“否”或“无”。"
)


def _legacy_published_payload(db: Session, plan: Plan) -> dict:
    """旧库缺少快照的发布记录：逐项裁决字段来源后重建。

    - 发布时事实：PlanAllocation 留存的线路、起止地址、逐台迁移标记，
      以及方案记录上的迁移台数、启用线路数、配置版本与发布时间；
    - 当前值（标注 name_source/is_active_source）：灯具名称、线路名称与启停；
    - 未知（None）：历史锁定状态、迁移起点、保留区占用及受其影响的
      已用/空闲通道数——不得以当前值冒充，也不得写成“否”。
    """
    allocs = (
        db.query(PlanAllocation)
        .filter(PlanAllocation.plan_id == plan.id)
        .order_by(PlanAllocation.fixture_id)
        .all()
    )
    names = {f.id: f.name for f in db.query(Fixture).all()}

    def _name_of(fixture_id: int) -> tuple[str, str]:
        # 灯具已被删除时连当前名称也不可得，标为不可用
        if fixture_id in names:
            return names[fixture_id], "current"
        return f"#{fixture_id}", "unavailable"

    alloc_out = []
    for a in allocs:
        name, name_source = _name_of(a.fixture_id)
        alloc_out.append(
            {
                "fixture_id": a.fixture_id,
                "name": name,
                "name_source": name_source,
                "line_id": a.line_id,
                "start": a.start,
                "end": a.end,
                "channels": a.end - a.start + 1,
                "migrated": a.migrated,
                "locked": None,
                "locked_source": "unknown",
            }
        )
    migrations = [
        {
            "fixture_id": a["fixture_id"],
            "name": a["name"],
            "name_source": a["name_source"],
            # 迁移本身（migrated 标记）是事实，但起点已无法还原
            "from_line": None,
            "from_start": None,
            "to_line": a["line_id"],
            "to_start": a["start"],
            "locked": None,
        }
        for a in alloc_out
        if a["migrated"]
    ]
    return {
        "feasible": True,
        "id": plan.id,
        "plan_id": plan.id,
        "config_version": plan.config_version,
        "status": plan.status,
        "optimality_status": plan.optimality_status,
        "migration_count": plan.migration_count,
        "used_lines": plan.used_lines,
        "created_at": plan.created_at,
        "published_at": plan.published_at,
        "from_snapshot": False,
        "field_sources": dict(LEGACY_FIELD_SOURCES),
        "notice": LEGACY_NOTICE,
        "allocations": alloc_out,
        "migrations": migrations,
        "occupancy": _legacy_occupancy(db, alloc_out),
        "conflicts": [],
    }


def _legacy_occupancy(db: Session, alloc_out: list[dict]) -> list[dict]:
    """无快照发布记录的线路占用：灯具块为发布时事实，保留区占用标为未知。

    已用/空闲通道数包含保留区占用，保留区无法还原时二者同样标为未知（None）；
    另提供 fixtures_used_channels 表示仅由灯具块构成的事实部分。
    """
    lines = db.query(Line).order_by(Line.id).all()
    blocks: dict[int, list[dict]] = {l.id: [] for l in lines}
    for a in alloc_out:
        if a["line_id"] not in blocks:
            continue  # 留存明细引用了当前不存在的线路，无法定位展示
        blocks[a["line_id"]].append(
            {
                "fixture_id": a["fixture_id"],
                "name": a["name"],
                "name_source": a["name_source"],
                "start": a["start"],
                "end": a["end"],
                "channels": a["channels"],
                "migrated": a["migrated"],
                "locked": None,
            }
        )
    out = []
    for line in lines:
        fx_blocks = sorted(blocks[line.id], key=lambda b: b["start"])
        # 求解结果保证灯具块互不重叠，直接求和即为灯具占用通道数
        used_by_fixtures = sum(b["channels"] for b in fx_blocks)
        out.append(
            {
                "line_id": line.id,
                "name": line.name,
                "name_source": "current",
                "is_active": line.is_active,
                "is_active_source": "current",
                "size": solver.LINE_SIZE,
                "used_channels": None,
                "free_channels": None,
                "fixtures_used_channels": used_by_fixtures,
                "fixtures": fx_blocks,
                "reservations": None,
                "reservations_source": "unknown",
            }
        )
    return out


def _latest_snapshot(db: Session, plan_id: int) -> PublishedSnapshot | None:
    return (
        db.query(PublishedSnapshot)
        .filter(PublishedSnapshot.plan_id == plan_id)
        .order_by(PublishedSnapshot.id.desc())
        .first()
    )


def _payload_from_snapshot(db: Session, plan: Plan) -> dict | None:
    """已发布方案：从发布时写入的不可变快照还原地址表。

    地址、名称、锁定、迁移与占用信息一律取自快照（发布时刻的事实），
    不随后续改名、锁定切换或保留区调整而漂移；状态、原配置版本等以
    方案记录为准——快照保存于发布事务内，旧快照中的 status 可能仍是
    发布前的 draft，不能直接返回。旧库缺少快照（或快照损坏）时返回
    None，由调用方回退。
    """
    snap = _latest_snapshot(db, plan.id)
    if snap is None:
        return None
    try:
        stored = json.loads(snap.payload)
    except (TypeError, ValueError):
        return None
    return {
        "feasible": bool(stored.get("feasible", True)),
        "id": plan.id,
        "plan_id": plan.id,
        "config_version": plan.config_version,
        "status": plan.status,
        "optimality_status": plan.optimality_status,
        "migration_count": plan.migration_count,
        "used_lines": plan.used_lines,
        "created_at": plan.created_at,
        "published_at": plan.published_at,
        "from_snapshot": True,
        "allocations": stored.get("allocations", []),
        "migrations": stored.get("migrations", []),
        "occupancy": stored.get("occupancy", []),
        "conflicts": stored.get("conflicts", []),
    }


def get_plan_payload(db: Session, plan_id: int) -> dict:
    plan = db.get(Plan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="预览不存在（可能已被新配置覆盖）")
    if plan.status == "published":
        restored = _payload_from_snapshot(db, plan)
        if restored is not None:
            return restored
        # 旧库缺少快照的发布记录：逐项裁决字段来源——留存明细中的线路/
        # 起止地址/迁移标记仍是发布时事实；名称取当前值并标注来源；历史
        # 锁定、迁移起点与保留区占用标为未知，不得伪装成历史事实。
        return _legacy_published_payload(db, plan)
    # 草稿/已作废：仍按当前预览路径实时重建
    return _plan_payload(db, plan, feasible=True)


def latest_published(db: Session) -> Plan | None:
    return (
        db.query(Plan)
        .filter(Plan.status == "published")
        .order_by(Plan.published_at.desc(), Plan.id.desc())
        .first()
    )


def publish_plan(db: Session, plan_id: int) -> dict:
    """原子发布：版本必须与当前配置一致，旧预览一律拒绝。"""
    current_version = get_config_version(db)
    plan = db.get(Plan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="预览不存在")
    if plan.status != "draft":
        raise HTTPException(status_code=409, detail=f"该预览已为 {plan.status} 状态，不能重复发布")
    if plan.optimality_status != solver.OPTIMAL:
        if plan.optimality_status is None:
            raise HTTPException(
                status_code=409,
                detail="该预览由旧版本生成、没有最优性证明记录，不能发布，请重新生成预览。",
            )
        raise HTTPException(
            status_code=409,
            detail=f"该预览的最优性状态为 {plan.optimality_status}，未逐级证明最优，不能发布，请重新生成预览。",
        )
    if plan.config_version != current_version:
        raise HTTPException(
            status_code=409,
            detail=(
                f"配置已变化（预览版本 {plan.config_version}，当前版本 {current_version}），"
                "请基于最新配置重新生成预览后再发布。"
            ),
        )

    now = datetime.now(timezone.utc)

    # 同一事务内完成：发布本预览、作废其余草稿、保存快照、回写既有地址
    db.query(Plan).filter(Plan.status == "draft", Plan.id != plan.id).update(
        {Plan.status: "superseded"}, synchronize_session=False
    )
    plan.status = "published"
    plan.published_at = now
    # 状态翻转后再构建快照，使快照自身即携带 published 状态与发布时间；
    # 既有地址回写必须发生在构建之后，否则迁移清单会被回写提前抹平
    payload = _plan_payload(db, plan, feasible=True)
    db.add(
        PublishedSnapshot(
            plan_id=plan.id,
            config_version=current_version,
            published_at=now,
            payload=json.dumps(payload, ensure_ascii=False, default=str),
        )
    )
    for a in payload["allocations"]:
        fx = db.get(Fixture, a["fixture_id"])
        if fx is not None:
            fx.previous_line = a["line_id"]
            fx.previous_start = a["start"]
    # 发布改变了灯具的“既有地址”，配置版本再推进一步，使旧草稿全部失效
    new_version = bump_config_version(db)
    db.commit()
    return {
        "ok": True,
        "plan_id": plan.id,
        "config_version": new_version,
        "optimality_status": solver.OPTIMAL,
        "published_at": now,
    }
