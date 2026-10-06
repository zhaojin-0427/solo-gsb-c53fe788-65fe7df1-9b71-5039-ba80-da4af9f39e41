"""端到端测试：型录导入、编排、版本失效发布、CSV 导出与冲突诊断。"""
from __future__ import annotations

import os
import tempfile

import pytest

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DMX_DB_PATH"] = _tmp.name

from fastapi.testclient import TestClient  # noqa: E402

from app.database import Base, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.services import init_db  # noqa: E402


@pytest.fixture(scope="session")
def client():
    Base.metadata.drop_all(bind=engine)
    init_db()
    with TestClient(app) as c:
        yield c


CATALOG_CSV = (
    "manufacturer,name,mode,channels\n"
    "ACME,Solar,Basic,10\n"
    "ACME,Solar,Full,20\n"
    "BrightCo,Par,3CH,3\n"
)


def test_01_catalog_import_csv(client):
    r = client.post(
        "/api/catalog/import",
        files={"file": ("catalog.csv", CATALOG_CSV, "text/csv")},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["created_types"] == 2
    assert data["created_modes"] == 3

    listing = client.get("/api/catalog").json()
    assert len(listing) == 2
    return listing


def test_02_catalog_import_duplicate_updates(client):
    csv = "manufacturer,name,mode,channels\nACME,Solar,Basic,11\n"
    r = client.post("/api/catalog/import", files={"file": ("c.csv", csv, "text/csv")})
    assert r.status_code == 200
    assert r.json()["updated_modes"] == 1


def test_03_bad_csv_rejected(client):
    r = client.post(
        "/api/catalog/import",
        files={"file": ("c.csv", "foo,bar\n1,2", "text/csv")},
    )
    assert r.status_code == 422


def test_04_create_fixtures(client):
    types = client.get("/api/catalog").json()
    mode_basic = next(m for t in types if t["name"] == "Solar" for m in t["modes"] if m["name"] == "Basic")
    mode_full = next(m for t in types if t["name"] == "Solar" for m in t["modes"] if m["name"] == "Full")
    mode_par = next(m for t in types if t["name"] == "Par" for m in t["modes"])
    # 两台已配灯具，一台新灯
    client.post("/api/fixtures", json={"name": "Spot1", "mode_id": mode_basic["id"],
                                       "previous_line": 1, "previous_start": 10, "locked": False})
    client.post("/api/fixtures", json={"name": "Spot2", "mode_id": mode_full["id"],
                                       "previous_line": 1, "previous_start": 100, "locked": True})
    client.post("/api/fixtures", json={"name": "Par1", "mode_id": mode_par["id"]})
    fx = client.get("/api/fixtures").json()
    assert len(fx) == 3
    return {f["name"]: f for f in fx}


def test_05_lock_requires_address(client):
    modes = client.get("/api/catalog").json()
    mode_id = modes[0]["modes"][0]["id"]
    r = client.post("/api/fixtures", json={"name": "Bad", "mode_id": mode_id, "locked": True})
    assert r.status_code == 422


def test_06_preview_feasible(client):
    r = client.post("/api/plans/preview")
    assert r.status_code == 200
    p = r.json()
    assert p["feasible"] is True, p
    # 两台已配灯具应留在原位 -> 迁移 0；新灯不计迁移
    assert p["migration_count"] == 0
    assert p["used_lines"] == 1
    by_name = {a["name"]: a for a in p["allocations"]}
    assert (by_name["Spot1"]["line_id"], by_name["Spot1"]["start"]) == (1, 10)
    assert (by_name["Spot2"]["line_id"], by_name["Spot2"]["start"]) == (1, 100)
    assert by_name["Spot2"]["locked"] is True
    # 无重叠且不越界
    for line in p["occupancy"]:
        assert line["used_channels"] + line["free_channels"] == 512
    return p


def test_07_publish_then_stale_preview_rejected(client):
    p = client.post("/api/plans/preview").json()
    plan_id = p["plan_id"]
    pv = p["config_version"]

    # 配置变化：增加一个保留区
    r = client.post("/api/reservations", json={"line_id": 2, "start": 1, "end": 10, "reason": "desk"})
    assert r.status_code == 201
    assert client.get("/api/plans/version").json()["config_version"] > pv

    r = client.post(f"/api/plans/{plan_id}/publish")
    assert r.status_code == 409
    assert "重新生成" in r.json()["detail"]


def test_08_fresh_preview_publish(client):
    p = client.post("/api/plans/preview").json()
    assert p["feasible"] is True
    r = client.post(f"/api/plans/{p['plan_id']}/publish")
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True

    # 重复发布被拒绝
    r2 = client.post(f"/api/plans/{p['plan_id']}/publish")
    assert r2.status_code == 409

    # 发布后既有地址已回写
    fx = {f["name"]: f for f in client.get("/api/fixtures").json()}
    assert (fx["Par1"]["previous_line"], fx["Par1"]["previous_start"]) == (1, 1)

    # 已发布表与 CSV
    latest = client.get("/api/plans/published/latest").json()
    assert latest["has_published"] is True
    csv_resp = client.get(f"/api/plans/published/{p['plan_id']}/csv")
    assert csv_resp.status_code == 200
    assert "Spot1".encode() in csv_resp.content
    assert csv_resp.content.startswith(b"\xef\xbb\xbf")
    return p


def test_09_rerequest_no_migrations_after_publish(client):
    p = client.post("/api/plans/preview").json()
    assert p["feasible"] is True
    assert p["migration_count"] == 0


def test_10_capacity_conflict(client):
    modes = client.get("/api/catalog").json()
    big_mode = next(m for t in modes for m in t["modes"] if m["channels"] == 20)
    # 再加 30 台 20ch 灯，加上保留区/已有灯（约 34ch），将超出一条线路容量
    for i in range(30):
        client.post("/api/fixtures", json={"name": f"Big{i}", "mode_id": big_mode["id"]})
    # 仅保留线路 1 启用
    for lid in (2, 3, 4):
        r = client.patch(f"/api/lines/{lid}", json={"is_active": False})
        assert r.status_code == 200, r.text
    p = client.post("/api/plans/preview").json()
    assert p["feasible"] is False
    assert any(c["type"] == "capacity" for c in p["conflicts"])


def test_11_lock_overlap_conflict(client):
    # 恢复四条线路
    for lid in (2, 3, 4):
        client.patch(f"/api/lines/{lid}", json={"is_active": True})
    modes = client.get("/api/catalog").json()
    mode3 = next(m for t in modes if t["name"] == "Par" for m in t["modes"])
    client.post("/api/fixtures", json={"name": "Lk1", "mode_id": mode3["id"],
                                       "previous_line": 3, "previous_start": 50, "locked": True})
    client.post("/api/fixtures", json={"name": "Lk2", "mode_id": mode3["id"],
                                       "previous_line": 3, "previous_start": 51, "locked": True})
    p = client.post("/api/plans/preview").json()
    assert p["feasible"] is False
    assert any(c["type"] == "lock_overlap" for c in p["conflicts"]), p["conflicts"]


def test_12_lock_reservation_conflict(client):
    client.post("/api/reservations", json={"line_id": 4, "start": 1, "end": 5, "reason": "desk"})
    modes = client.get("/api/catalog").json()
    mode3 = next(m for t in modes if t["name"] == "Par" for m in t["modes"])
    client.post("/api/fixtures", json={"name": "Lk3", "mode_id": mode3["id"],
                                       "previous_line": 4, "previous_start": 3, "locked": True})
    p = client.post("/api/plans/preview").json()
    assert p["feasible"] is False
    assert any(c["type"] == "lock_reservation" for c in p["conflicts"]), p["conflicts"]


def test_13_deterministic_lexicographic_tiebreak():
    """同样迁移数/线路数下，字典序方案确定且最小。"""
    from app.solver import FixtureSpec, LineSpec, SolveSpec, solve

    def run(ids):
        spec = SolveSpec(
            fixtures=[FixtureSpec(i, f"F{i}", 10, None, None, False) for i in ids],
            reservations=[],
            lines=[LineSpec(i, f"L{i}", True) for i in range(1, 5)],
        )
        return solve(spec)

    r1 = run([7, 3, 12])
    r2 = run([7, 3, 12])
    keys1 = [(a.line_id, a.start) for a in sorted(r1.allocations, key=lambda a: a.fixture_id)]
    keys2 = [(a.line_id, a.start) for a in sorted(r2.allocations, key=lambda a: a.fixture_id)]
    assert keys1 == keys2  # 确定性
    # 三台 10ch 都能放线路 1，字典序最小 => 全部 line 1，起始 1/11/21
    assert keys1 == [(1, 1), (1, 11), (1, 21)]
    assert r1.optimality == "optimal"


def test_14_lexicographic_requires_second_line():
    """60 台灯各 10ch：线路 1 放 51 台后，ID 52 起必须进入线路 2 且从地址 1 开始。"""
    from app.solver import FixtureSpec, LineSpec, SolveSpec, solve

    spec = SolveSpec(
        fixtures=[FixtureSpec(i, f"F{i}", 10, None, None, False) for i in range(1, 61)],
        reservations=[],
        lines=[LineSpec(i, f"L{i}", True) for i in range(1, 5)],
    )
    r = solve(spec)
    assert r.feasible
    by_id = {a.fixture_id: a for a in r.allocations}
    # 前 51 台排满线路 1（1..510），第 52 台起线路 2
    assert (by_id[51].line_id, by_id[51].start, by_id[51].end) == (1, 501, 510)
    assert (by_id[52].line_id, by_id[52].start) == (2, 1)
    assert by_id[60].line_id == 2
    assert r.migration_count == 0
    assert r.used_lines == 2


def test_15_unsatisfiable_lock_diagnostic():
    """容量足够但锁定块把线路夹碎时，诊断指出必须放松的锁，而非笼统报容量。"""
    from app.solver import FixtureSpec, LineSpec, ReservationSpec, SolveSpec, solve

    spec = SolveSpec(
        fixtures=[
            FixtureSpec(1, "LockA", 100, 1, 1, True),
            FixtureSpec(2, "LockB", 100, 1, 413, True),
            FixtureSpec(3, "NewBig", 101, None, None, False),
        ],
        reservations=[ReservationSpec(1, 1, 201, 312, "wall")],
        lines=[LineSpec(1, "L1", True)] + [LineSpec(i, f"L{i}", False) for i in range(2, 5)],
    )
    r = solve(spec)
    assert r.feasible is False
    assert any(c["type"] == "unsatisfiable_lock" for c in r.conflicts), r.conflicts


def test_16_feasible_incumbent_at_migration_phase_is_unproven():
    """第一阶段限时只拿到 FEASIBLE 时：返回未证最优，且不得固定值继续第二阶段。"""
    from ortools.sat.python import cp_model

    from app import solver as slv

    spec = slv.SolveSpec(
        fixtures=[slv.FixtureSpec(i, f"F{i}", 10, None, None, False) for i in (1, 2, 3)],
        reservations=[],
        lines=[slv.LineSpec(i, f"L{i}", True) for i in range(1, 5)],
    )

    real_cls = cp_model.CpSolver

    class Scripted(real_cls):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def Solve(self, model):  # noqa: N802
            st = super().Solve(model)
            self.calls += 1
            # 第一次 Solve = 第一阶段；伪造为限时 FEASIBLE（incumbent 仍可读）
            return cp_model.FEASIBLE if self.calls == 1 else st

    slv.cp_model.CpSolver = Scripted
    try:
        r = slv.solve(spec)
    finally:
        slv.cp_model.CpSolver = real_cls

    assert r.optimality == slv.UNPROVEN
    assert r.unproven_stage == "migration"
    assert r.feasible is True  # 带回了候选，但不可发布
    assert r.has_candidate is True


def test_17_phase_gating_no_fix_without_optimal():
    """用计数假求解器验证：第一阶段非 OPTIMAL 时，第二、三阶段根本不会被调用。"""
    from ortools.sat.python import cp_model

    from app import solver as slv

    spec = slv.SolveSpec(
        fixtures=[slv.FixtureSpec(i, f"F{i}", 10, None, None, False) for i in (1, 2)],
        reservations=[],
        lines=[slv.LineSpec(i, f"L{i}", True) for i in range(1, 5)],
    )

    calls = {"n": 0}
    real_cls = cp_model.CpSolver

    class OnlyFirstFeasible(real_cls):
        def Solve(self, model):  # noqa: N802
            super().Solve(model)
            calls["n"] += 1
            return cp_model.FEASIBLE  # 第一阶段即未证最优

    slv.cp_model.CpSolver = OnlyFirstFeasible
    try:
        r = slv.solve(spec)
    finally:
        slv.cp_model.CpSolver = real_cls

    assert calls["n"] == 1  # 只求解了第一阶段，未固定值继续
    assert r.optimality == slv.UNPROVEN
    assert r.unproven_stage == "migration"


def test_18_lexicographic_batch_shrinks_without_fixing():
    """字典序批量未证最优时收缩批量，未证明的一批取值不被固定；最终仍可证明时给出最优。"""
    from ortools.sat.python import cp_model

    from app import solver as slv

    # 4 条线路时一个加权批最多打包 4 台；3 台灯一批完成
    assert slv._max_packed_batch(4) >= 3

    spec = slv.SolveSpec(
        fixtures=[slv.FixtureSpec(i, f"F{i}", 10, None, None, False) for i in (3, 7, 12)],
        reservations=[],
        lines=[slv.LineSpec(i, f"L{i}", True) for i in range(1, 5)],
    )

    real_cls = cp_model.CpSolver
    state = {"n": 0}

    class FirstBatchFeasibleThenOptimal(real_cls):
        """第一次字典序批量（3 台一起）返回 FEASIBLE，之后收缩为逐台求解均 OPTIMAL。

        迁移/线路阶段发生在字典序之前：共 2 次 Solve，需让它们正常返回。
        """

        def Solve(self, model):  # noqa: N802
            st = super().Solve(model)
            state["n"] += 1
            if state["n"] == 3:  # 第一次字典序批量求解
                return cp_model.FEASIBLE
            return st  # 收缩后逐台 OPTIMAL

    slv.cp_model.CpSolver = FirstBatchFeasibleThenOptimal
    try:
        r = slv.solve(spec)
    finally:
        slv.cp_model.CpSolver = real_cls

    # 批量为 3 时 FEASIBLE -> 批量 1（ID 3）OPTIMAL 固定；再 ID 7、ID 12 各一次
    assert r.optimality == slv.OPTIMAL
    keys = [(a.line_id, a.start) for a in sorted(r.allocations, key=lambda a: a.fixture_id)]
    assert keys == [(1, 1), (1, 11), (1, 21)]


def test_19_lexicographic_single_fixture_timeout_is_unproven():
    """收缩到单台灯仍无法证明时：返回未证最优，且该台取值未被固定。"""
    from ortools.sat.python import cp_model

    from app import solver as slv

    spec = slv.SolveSpec(
        fixtures=[slv.FixtureSpec(i, f"F{i}", 10, None, None, False) for i in (3, 7)],
        reservations=[],
        lines=[slv.LineSpec(i, f"L{i}", True) for i in range(1, 5)],
    )

    real_cls = cp_model.CpSolver
    state = {"n": 0}

    class TimeoutLexTail(real_cls):
        def Solve(self, model):  # noqa: N802
            st = super().Solve(model)
            state["n"] += 1
            if state["n"] == 3:  # 字典序唯一一次批量（2 台）未证明
                return cp_model.FEASIBLE
            if state["n"] >= 4:  # 收缩到单台仍未证明
                return cp_model.FEASIBLE
            return st

    slv.cp_model.CpSolver = TimeoutLexTail
    try:
        r = slv.solve(spec)
    finally:
        slv.cp_model.CpSolver = real_cls

    assert r.optimality == slv.UNPROVEN
    assert r.unproven_stage == "lexicographic"
    assert r.feasible is True  # 有候选
    # 不可被当作最优：不生成计划由 API 层测试覆盖


def test_20_preview_unproven_creates_no_draft(client):
    """HTTP：第一阶段 FEASIBLE -> 200 + optimality_status=unproven，且不生成草稿。"""
    from ortools.sat.python import cp_model

    from app import planner
    from app.database import SessionLocal
    from app.models import Plan

    # 清理前面用例遗留的互斥/撞保留区锁定灯具，使配置恢复可解
    fx = {f["name"]: f for f in client.get("/api/fixtures").json()}
    for name in ("Lk1", "Lk2", "Lk3"):
        if name in fx:
            assert client.delete(f"/api/fixtures/{fx[name]['id']}").status_code == 204

    with SessionLocal() as db:
        drafts_before = db.query(Plan).filter(Plan.status == "draft").count()

    real_cls = cp_model.CpSolver

    class FirstFeasible(real_cls):
        def __init__(self):
            super().__init__()
            self._n = 0

        def Solve(self, model):  # noqa: N802
            super().Solve(model)
            self._n += 1
            return cp_model.FEASIBLE if self._n == 1 else cp_model.OPTIMAL

    planner.solver.cp_model.CpSolver = FirstFeasible
    try:
        r = client.post("/api/plans/preview")
    finally:
        planner.solver.cp_model.CpSolver = real_cls

    assert r.status_code == 200, r.text
    p = r.json()
    assert p["feasible"] is True
    assert p["optimality_status"] == "unproven"
    assert p["unproven_stage"] == "migration"
    assert "plan_id" not in p and "id" not in p  # 未生成草稿
    assert p["conflicts"] and p["conflicts"][0]["type"] == "unproven"

    with SessionLocal() as db:
        drafts_after = db.query(Plan).filter(Plan.status == "draft").count()
    assert drafts_after == drafts_before


def test_21_legacy_draft_without_proof_is_rejected(client):
    """无 optimality_status 证明记录的遗留草稿（或未证最优草稿）一律拒发。"""
    from app.database import SessionLocal
    from app.models import Plan, PlanAllocation
    from app.services import get_config_version

    with SessionLocal() as db:
        plan = Plan(
            config_version=get_config_version(db),
            status="draft",
            optimality_status=None,
            migration_count=0,
            used_lines=1,
        )
        db.add(plan)
        db.flush()
        db.add(
            PlanAllocation(
                plan_id=plan.id, fixture_id=1, line_id=1, start=1, end=10, migrated=False
            )
        )
        db.commit()
        legacy_id = plan.id

    r = client.post(f"/api/plans/{legacy_id}/publish")
    assert r.status_code == 409
    assert "最优" in r.json()["detail"]

    # 未证最优状态同样拒发（即使版本匹配）
    with SessionLocal() as db:
        plan = Plan(
            config_version=get_config_version(db),
            status="draft",
            optimality_status="unproven",
            migration_count=0,
            used_lines=1,
        )
        db.add(plan)
        db.commit()
        unproven_id = plan.id
    r = client.post(f"/api/plans/{unproven_id}/publish")
    assert r.status_code == 409
    assert "最优" in r.json()["detail"]


def test_22_optimal_preview_persists_and_publishes(client):
    """成功预览持久化 optimality_status=optimal，查询与发布均可核对。"""
    from app.database import SessionLocal
    from app.models import Plan

    p = client.post("/api/plans/preview").json()
    assert p["optimality_status"] == "optimal"
    plan_id = p["plan_id"]

    got = client.get(f"/api/plans/{plan_id}").json()
    assert got["optimality_status"] == "optimal"

    with SessionLocal() as db:
        assert db.get(Plan, plan_id).optimality_status == "optimal"

    r = client.post(f"/api/plans/{plan_id}/publish")
    assert r.status_code == 200, r.text
    assert r.json()["optimality_status"] == "optimal"


def test_23_solver_unknown_yields_unproven_without_candidate():
    """求解器层：阶段求解返回 UNKNOWN -> unproven 且无候选，feasible=False。"""
    from ortools.sat.python import cp_model

    from app import solver as slv

    spec = slv.SolveSpec(
        fixtures=[slv.FixtureSpec(i, f"F{i}", 10, None, None, False) for i in (1, 2)],
        reservations=[],
        lines=[slv.LineSpec(i, f"L{i}", True) for i in range(1, 5)],
    )

    real_cls = cp_model.CpSolver

    class AlwaysUnknown(real_cls):
        def Solve(self, model):  # noqa: N802
            super().Solve(model)
            return cp_model.UNKNOWN

    slv.cp_model.CpSolver = AlwaysUnknown
    try:
        r = slv.solve(spec)
    finally:
        slv.cp_model.CpSolver = real_cls

    assert r.optimality == slv.UNPROVEN
    assert r.unproven_stage == "migration"
    assert r.allocations == []
    assert r.feasible is False  # 空候选：不是可行，也不是已证无解
    assert r.has_candidate is False
    assert r.conflicts and r.conflicts[0]["type"] == "unproven"


def test_24_preview_unknown_status_empty_candidate(client):
    """HTTP：求解返回 UNKNOWN -> 200 + feasible=false + unproven + 阶段信息，不生成草稿。"""
    from ortools.sat.python import cp_model

    from app import planner
    from app.database import SessionLocal
    from app.models import Plan

    with SessionLocal() as db:
        drafts_before = db.query(Plan).filter(Plan.status == "draft").count()

    real_cls = cp_model.CpSolver

    class AlwaysUnknown(real_cls):
        def Solve(self, model):  # noqa: N802
            super().Solve(model)
            return cp_model.UNKNOWN

    planner.solver.cp_model.CpSolver = AlwaysUnknown
    try:
        r = client.post("/api/plans/preview")
    finally:
        planner.solver.cp_model.CpSolver = real_cls

    assert r.status_code == 200, r.text
    p = r.json()
    assert p["feasible"] is False  # 空候选不得误报为可行
    assert p["optimality_status"] == "unproven"  # 也不是已证无解
    assert p["unproven_stage"] == "migration"
    assert p["message"]
    assert p["allocations"] == [] and p["migrations"] == [] and p["occupancy"] == []
    assert "plan_id" not in p and "id" not in p  # 未生成草稿
    assert p["conflicts"] and p["conflicts"][0]["type"] == "unproven"

    with SessionLocal() as db:
        drafts_after = db.query(Plan).filter(Plan.status == "draft").count()
    assert drafts_after == drafts_before


def test_25_preview_candidate_read_failure_empty_candidate(client):
    """HTTP：FEASIBLE 但候选读取失败 -> 同样按空候选裁决 feasible=false。"""
    from ortools.sat.python import cp_model

    from app import planner
    from app import solver as slv

    real_cls = cp_model.CpSolver
    real_read = slv._read_chosen

    class FirstFeasible(real_cls):
        def __init__(self):
            super().__init__()
            self._n = 0

        def Solve(self, model):  # noqa: N802
            super().Solve(model)
            self._n += 1
            return cp_model.FEASIBLE if self._n == 1 else cp_model.OPTIMAL

    def _boom(*_args, **_kwargs):
        raise RuntimeError("模拟候选读取失败")

    planner.solver.cp_model.CpSolver = FirstFeasible
    slv._read_chosen = _boom
    try:
        r = client.post("/api/plans/preview")
    finally:
        planner.solver.cp_model.CpSolver = real_cls
        slv._read_chosen = real_read

    assert r.status_code == 200, r.text
    p = r.json()
    assert p["feasible"] is False
    assert p["optimality_status"] == "unproven"
    assert p["unproven_stage"] == "migration"
    assert p["allocations"] == []
    assert "plan_id" not in p and "id" not in p


def test_26_preview_diagnosis_timeout_empty_candidate(client):
    """HTTP：主模型 INFEASIBLE 且冲突诊断超时 -> unproven(diagnosis)，feasible=false。"""
    from ortools.sat.python import cp_model

    from app import planner

    real_cls = cp_model.CpSolver
    state = {"n": 0}

    class InfeasibleThenUnknown(real_cls):
        def Solve(self, model):  # noqa: N802
            super().Solve(model)
            state["n"] += 1
            # 第一阶段报无解进入诊断；诊断求解超时（UNKNOWN）
            return cp_model.INFEASIBLE if state["n"] == 1 else cp_model.UNKNOWN

    planner.solver.cp_model.CpSolver = InfeasibleThenUnknown
    try:
        r = client.post("/api/plans/preview")
    finally:
        planner.solver.cp_model.CpSolver = real_cls

    assert r.status_code == 200, r.text
    p = r.json()
    assert p["feasible"] is False
    assert p["optimality_status"] == "unproven"  # 诊断超时不得当作已证无解
    assert p["unproven_stage"] == "diagnosis"
    assert p["allocations"] == []
    assert any(c["type"] == "solver_timeout" for c in p["conflicts"])
    assert "plan_id" not in p and "id" not in p


def test_27_preview_recovers_after_empty_candidate(client):
    """空候选事件后配置未被污染：正常预览仍可得最优并发布。"""
    p = client.post("/api/plans/preview").json()
    assert p["feasible"] is True
    assert p["optimality_status"] == "optimal"
    r = client.post(f"/api/plans/{p['plan_id']}/publish")
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True


def test_28_published_view_restored_from_snapshot(client):
    """发布后改名/切锁定/加保留区/线路重命名，已发布查询与 CSV 仍以快照为准。"""
    modes = client.get("/api/catalog").json()
    mode3 = next(m for t in modes if t["name"] == "Par" for m in t["modes"])
    # 既有地址放在空闲的线路 3，迁移 0 约束下必然留在原位（确定性）
    r = client.post("/api/fixtures", json={"name": "Drift", "mode_id": mode3["id"],
                                           "previous_line": 3, "previous_start": 400})
    assert r.status_code == 201
    drift_id = r.json()["id"]

    p = client.post("/api/plans/preview").json()
    assert p["optimality_status"] == "optimal"
    plan_id, cv = p["plan_id"], p["config_version"]
    drift_alloc = next(a for a in p["allocations"] if a["name"] == "Drift")
    assert (drift_alloc["line_id"], drift_alloc["start"]) == (3, 400)

    r = client.post(f"/api/plans/{plan_id}/publish")
    assert r.status_code == 200, r.text

    # 发布后的漂移源：改名、切换锁定、新增保留区、线路重命名
    assert client.put(f"/api/fixtures/{drift_id}", json={"name": "DriftX"}).status_code == 200
    assert client.put(f"/api/fixtures/{drift_id}", json={"locked": True}).status_code == 200
    assert client.post("/api/reservations",
                       json={"line_id": 1, "start": 450, "end": 460, "reason": "new"}).status_code == 201
    assert client.patch("/api/lines/1", json={"name": "Universe-A"}).status_code == 200

    # 已发布查询：地址/名称/锁定/占用从快照还原，状态与原配置版本以方案记录为准
    got = client.get(f"/api/plans/{plan_id}").json()
    assert got["status"] == "published"  # 不得返回快照里发布前的 draft
    assert got["from_snapshot"] is True
    assert got["config_version"] == cv
    assert got["published_at"]
    by_id = {a["fixture_id"]: a for a in got["allocations"]}
    assert by_id[drift_id]["name"] == "Drift"  # 不随改名漂移
    assert by_id[drift_id]["locked"] is False  # 不随锁定切换漂移
    assert (by_id[drift_id]["line_id"], by_id[drift_id]["start"]) == (3, 400)
    line1 = next(l for l in got["occupancy"] if l["line_id"] == 1)
    assert line1["name"] == "线路 1"  # 不随线路重命名漂移
    assert line1["reservations"] == []  # 不随新增保留区漂移

    # published/latest 同样走快照
    latest = client.get("/api/plans/published/latest").json()
    assert latest["has_published"] is True
    assert latest["plan_id"] == plan_id
    assert latest["status"] == "published"
    latest_names = {a["name"] for a in latest["allocations"]}
    assert "Drift" in latest_names and "DriftX" not in latest_names

    # CSV 与页面同源：旧名在、新名不在
    csv_resp = client.get(f"/api/plans/published/{plan_id}/csv")
    assert csv_resp.status_code == 200
    text = csv_resp.content.decode("utf-8-sig")
    assert "Drift" in text and "DriftX" not in text


def test_29_migrations_survive_publish(client):
    """迁移清单在发布后不从已发布查询/CSV 中消失（回归：发布后立即变空）。"""
    modes = client.get("/api/catalog").json()
    mode20 = next(m for t in modes for m in t["modes"] if m["channels"] == 20)
    r = client.post("/api/fixtures", json={"name": "Mig", "mode_id": mode20["id"],
                                           "previous_line": 3, "previous_start": 100})
    assert r.status_code == 201
    # 保留区覆盖其既有位置，迫使它迁移
    assert client.post("/api/reservations",
                       json={"line_id": 3, "start": 90, "end": 130, "reason": "force-move"}).status_code == 201

    p = client.post("/api/plans/preview").json()
    assert p["optimality_status"] == "optimal"
    plan_id = p["plan_id"]
    mig_moves = [m for m in p["migrations"] if m["name"] == "Mig"]
    assert len(mig_moves) == 1
    assert (mig_moves[0]["from_line"], mig_moves[0]["from_start"]) == (3, 100)
    assert p["migration_count"] >= 1

    assert client.post(f"/api/plans/{plan_id}/publish").status_code == 200

    # 既有地址已回写；若按当前状态重建迁移清单会变空，快照还原则保持发布时内容
    got = client.get(f"/api/plans/{plan_id}").json()
    assert got["from_snapshot"] is True
    assert got["migrations"] == p["migrations"]
    assert got["migration_count"] == p["migration_count"]

    latest = client.get("/api/plans/published/latest").json()
    assert [m for m in latest["migrations"] if m["name"] == "Mig"]

    csv_text = client.get(f"/api/plans/published/{plan_id}/csv").content.decode("utf-8-sig")
    mig_row = next(row for row in csv_text.splitlines() if row.split(",")[1] == "Mig")
    assert mig_row.split(",")[6] == "是"  # 是否迁移列


def test_30_legacy_published_without_snapshot_falls_back(client):
    """旧库缺少快照的发布记录：查询与 CSV 回退重建，并明确标注非快照事实。"""
    from datetime import datetime, timezone

    from app.database import SessionLocal
    from app.models import Plan, PlanAllocation
    from app.services import get_config_version

    with SessionLocal() as db:
        plan = Plan(
            config_version=get_config_version(db),
            status="published",
            optimality_status="optimal",
            migration_count=0,
            used_lines=1,
            # 老记录时间戳，避免抢占 published/latest
            published_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        )
        db.add(plan)
        db.flush()
        db.add(
            PlanAllocation(plan_id=plan.id, fixture_id=1, line_id=1, start=1, end=3, migrated=False)
        )
        db.commit()
        legacy_id = plan.id

    got = client.get(f"/api/plans/{legacy_id}").json()
    assert got["status"] == "published"
    assert got["from_snapshot"] is False
    assert got["notice"]  # 无法还原的发布时字段不得伪装成历史事实
    assert got["allocations"]

    csv_resp = client.get(f"/api/plans/published/{legacy_id}/csv")
    assert csv_resp.status_code == 200
    assert csv_resp.content.startswith(b"\xef\xbb\xbf")

    # latest 仍是真正最近发布的方案
    latest = client.get("/api/plans/published/latest").json()
    assert latest["plan_id"] != legacy_id


def test_31_draft_query_uses_live_state(client):
    """草稿仍按当前预览路径重建：改名即时反映；配置变化后过期草稿拒发。"""
    p = client.post("/api/plans/preview").json()
    assert p["optimality_status"] == "optimal"
    plan_id = p["plan_id"]

    fx = {f["name"]: f for f in client.get("/api/fixtures").json()}
    par1_id = fx["Par1"]["id"]
    assert client.put(f"/api/fixtures/{par1_id}", json={"name": "Par1Renamed"}).status_code == 200
    try:
        got = client.get(f"/api/plans/{plan_id}").json()
        assert got["status"] == "draft"
        assert got["from_snapshot"] is False
        assert "Par1Renamed" in {a["name"] for a in got["allocations"]}

        # 配置已变化，过期草稿拒发
        r = client.post(f"/api/plans/{plan_id}/publish")
        assert r.status_code == 409
    finally:
        client.put(f"/api/fixtures/{par1_id}", json={"name": "Par1"})


def _insert_legacy_plan(allocations, published_at, migration_count=0, used_lines=1) -> int:
    """直接落库一条无快照的旧发布记录（模拟旧库遗留），返回 plan_id。

    allocations: (fixture_id, line_id, start, end, migrated) 元组列表。
    """
    from app.database import SessionLocal
    from app.models import Plan, PlanAllocation
    from app.services import get_config_version

    with SessionLocal() as db:
        plan = Plan(
            config_version=get_config_version(db),
            status="published",
            optimality_status="optimal",
            migration_count=migration_count,
            used_lines=used_lines,
            published_at=published_at,
        )
        db.add(plan)
        db.flush()
        for fx_id, line_id, start, end, migrated in allocations:
            db.add(
                PlanAllocation(
                    plan_id=plan.id, fixture_id=fx_id, line_id=line_id,
                    start=start, end=end, migrated=migrated,
                )
            )
        db.commit()
        return plan.id


def test_32_legacy_payload_field_source_adjudication(client):
    """无快照旧记录逐项裁决：留存明细是事实，名称标来源，锁定/迁移起点/保留区标未知。"""
    from datetime import datetime, timezone

    fx = {f["name"]: f for f in client.get("/api/fixtures").json()}
    spot1_id = fx["Spot1"]["id"]
    mig_id = fx["Mig"]["id"]
    legacy_id = _insert_legacy_plan(
        [(spot1_id, 1, 1, 10, False), (mig_id, 2, 5, 24, True)],
        datetime(2020, 2, 2, tzinfo=timezone.utc),
        migration_count=1,
        used_lines=2,
    )

    got = client.get(f"/api/plans/{legacy_id}").json()
    assert got["status"] == "published"
    assert got["from_snapshot"] is False
    assert got["notice"]
    fs = got["field_sources"]
    assert fs["allocations"] == "publish_record"
    assert fs["names"] == "current"
    assert fs["locked"] == "unknown"
    assert fs["migration_origin"] == "unknown"
    assert fs["reservations"] == "unknown"

    by_id = {a["fixture_id"]: a for a in got["allocations"]}
    a1 = by_id[spot1_id]
    # PlanAllocation 留存的线路/起止地址/迁移标记 = 发布时事实
    assert (a1["line_id"], a1["start"], a1["end"], a1["channels"]) == (1, 1, 10, 10)
    assert a1["migrated"] is False
    # 当前名称标注来源；历史锁定为未知（null），不得写成 false
    assert a1["name"] == "Spot1"
    assert a1["name_source"] == "current"
    assert a1["locked"] is None
    assert a1["locked_source"] == "unknown"
    assert by_id[mig_id]["migrated"] is True

    # 迁移明细来自留存的迁移标记：去向是事实，起点无法还原
    migs = got["migrations"]
    assert len(migs) == 1
    m = migs[0]
    assert m["fixture_id"] == mig_id
    assert m["from_line"] is None and m["from_start"] is None
    assert (m["to_line"], m["to_start"]) == (2, 5)
    assert m["locked"] is None

    # 占用：灯具块是事实；保留区与含保留区的已用/空闲通道数标为未知
    line2 = next(l for l in got["occupancy"] if l["line_id"] == 2)
    assert line2["reservations"] is None
    assert line2["reservations_source"] == "unknown"
    assert line2["used_channels"] is None
    assert line2["free_channels"] is None
    assert line2["fixtures_used_channels"] == 20
    assert [b["fixture_id"] for b in line2["fixtures"]] == [mig_id]
    assert line2["fixtures"][0]["locked"] is None
    assert line2["name_source"] == "current"
    assert line2["is_active_source"] == "current"


def test_33_legacy_payload_immune_to_current_drift(client):
    """现状漂移后：事实字段不变，未知仍是未知（不随当前锁定漂移），名称仍标当前。"""
    from datetime import datetime, timezone

    fx = {f["name"]: f for f in client.get("/api/fixtures").json()}
    spot1_id = fx["Spot1"]["id"]
    legacy_id = _insert_legacy_plan(
        [(spot1_id, 1, 20, 29, False)],
        datetime(2020, 3, 3, tzinfo=timezone.utc),
    )

    # 现状漂移源：改名、切换锁定、新增保留区、线路重命名
    assert client.put(f"/api/fixtures/{spot1_id}", json={"name": "Spot1Now"}).status_code == 200
    assert client.put(f"/api/fixtures/{spot1_id}", json={"locked": True}).status_code == 200
    assert client.post("/api/reservations",
                       json={"line_id": 1, "start": 300, "end": 310, "reason": "drift"}).status_code == 201
    assert client.patch("/api/lines/2", json={"name": "Universe-B"}).status_code == 200
    try:
        got = client.get(f"/api/plans/{legacy_id}").json()
        a = got["allocations"][0]
        # 事实字段不随现状漂移
        assert (a["line_id"], a["start"], a["end"]) == (1, 20, 29)
        assert a["migrated"] is False
        # 历史锁定仍为未知——不得把当前 locked=True 当成发布时事实
        assert a["locked"] is None
        # 名称取当前值但标注来源
        assert a["name"] == "Spot1Now"
        assert a["name_source"] == "current"
        # 保留区占用仍为未知——不随新增保留区漂移
        line1 = next(l for l in got["occupancy"] if l["line_id"] == 1)
        assert line1["reservations"] is None
        assert line1["used_channels"] is None
        assert line1["fixtures_used_channels"] == 10

        # CSV 同步：锁定列写“未知”而非“是/否”，名称标注当前来源
        csv_text = client.get(f"/api/plans/published/{legacy_id}/csv").content.decode("utf-8-sig")
        row = next(r for r in csv_text.splitlines() if r.startswith(f"{spot1_id},"))
        cells = row.split(",")
        assert cells[1] == "Spot1Now（当前名称）"
        assert cells[6] == "否"  # 是否迁移：留存标记是事实
        assert cells[7] == "未知"  # 是否锁定：未知，不得写“是”或“否”
    finally:
        client.put(f"/api/fixtures/{spot1_id}", json={"name": "Spot1", "locked": False})
        client.patch("/api/lines/2", json={"name": "线路 2"})


def test_34_legacy_csv_columns_and_deleted_fixture(client):
    """CSV 列保持不变；已删除灯具的名称标为不可用；迁移列仍取留存标记。"""
    from datetime import datetime, timezone

    fx = {f["name"]: f for f in client.get("/api/fixtures").json()}
    spot1_id = fx["Spot1"]["id"]
    mig_id = fx["Mig"]["id"]
    legacy_id = _insert_legacy_plan(
        [(spot1_id, 1, 1, 10, False), (mig_id, 2, 5, 24, True), (999999, 3, 1, 5, False)],
        datetime(2020, 4, 4, tzinfo=timezone.utc),
        migration_count=1,
        used_lines=3,
    )

    # JSON：已删除灯具的名称标为不可用
    got = client.get(f"/api/plans/{legacy_id}").json()
    by_id = {a["fixture_id"]: a for a in got["allocations"]}
    assert by_id[999999]["name"] == "#999999"
    assert by_id[999999]["name_source"] == "unavailable"
    assert by_id[999999]["locked"] is None

    resp = client.get(f"/api/plans/published/{legacy_id}/csv")
    assert resp.status_code == 200
    assert resp.content.startswith(b"\xef\xbb\xbf")
    lines = resp.content.decode("utf-8-sig").splitlines()
    # 列与表头保持原样
    assert lines[0] == "灯具ID,灯具名称,线路,起始地址,结束地址,通道数,是否迁移,是否锁定"
    rows = {r.split(",")[0]: r.split(",") for r in lines[1:]}
    assert rows[str(spot1_id)][1] == "Spot1（当前名称）"
    assert rows[str(spot1_id)][6] == "否"
    assert rows[str(spot1_id)][7] == "未知"
    assert rows[str(mig_id)][6] == "是"  # 迁移标记是发布时事实
    assert rows[str(mig_id)][7] == "未知"
    assert rows["999999"][1] == "#999999（名称不可用）"
    assert rows["999999"][7] == "未知"


def test_35_latest_falls_back_when_latest_plan_has_no_snapshot(client):
    """最近发布恰好是无快照旧记录时，published/latest 同样按字段来源裁决返回。"""
    from datetime import datetime, timezone

    fx = {f["name"]: f for f in client.get("/api/fixtures").json()}
    spot1_id = fx["Spot1"]["id"]
    legacy_id = _insert_legacy_plan(
        [(spot1_id, 1, 1, 10, False)],
        datetime(2099, 1, 1, tzinfo=timezone.utc),  # 确保成为最近发布
    )

    latest = client.get("/api/plans/published/latest").json()
    assert latest["has_published"] is True
    assert latest["plan_id"] == legacy_id
    assert latest["from_snapshot"] is False
    assert latest["notice"]
    assert latest["field_sources"]["locked"] == "unknown"
    assert latest["allocations"][0]["locked"] is None
    assert latest["occupancy"][0]["reservations"] is None


def test_36_published_page_ships_detail_panels(client):
    """已发布页签附带线路占用与迁移清单容器，前端脚本包含独立渲染器。"""
    html = client.get("/").text
    assert 'id="published-tracks"' in html
    assert 'id="published-migration-panel"' in html
    assert 'id="published-migration-tbody"' in html
    assert 'id="published-migration-count"' in html

    js = client.get("/static/app.js").text
    # 已发布占用/迁移采用独立渲染（不复用预览组件），避免旧记录的 null
    # 与现状启停改写历史占用
    assert "renderPublishedTracks" in js
    assert "renderPublishedMigrations" in js
    assert "publishedNameSuffix" in js


def test_37_legacy_latest_details_survive_line_deactivation(client):
    """最近发布为无快照旧记录：线路后来停用，占用仍返回发布时灯具块与迁移落点。"""
    from datetime import datetime, timezone

    fx = {f["name"]: f for f in client.get("/api/fixtures").json()}
    spot1_id = fx["Spot1"]["id"]
    mig_id = fx["Mig"]["id"]
    legacy_id = _insert_legacy_plan(
        [(spot1_id, 1, 1, 10, False), (mig_id, 2, 5, 24, True)],
        datetime(2099, 2, 2, tzinfo=timezone.utc),  # 晚于 test_35，成为最近发布
        migration_count=1,
        used_lines=2,
    )

    # 线路后来停用：发布时的灯具块不得随之隐藏
    assert client.patch("/api/lines/2", json={"is_active": False}).status_code == 200
    try:
        latest = client.get("/api/plans/published/latest").json()
        assert latest["has_published"] is True
        assert latest["plan_id"] == legacy_id
        assert latest["from_snapshot"] is False

        # 迁移清单：落点（去向）是留存事实，起点无法还原为 null
        migs = latest["migrations"]
        assert len(migs) == 1
        assert migs[0]["fixture_id"] == mig_id
        assert (migs[0]["to_line"], migs[0]["to_start"]) == (2, 5)
        assert migs[0]["from_line"] is None and migs[0]["from_start"] is None
        assert migs[0]["locked"] is None

        # 占用：停用线路仍带发布时灯具块；启停/名称标注为当前现状
        line2 = next(l for l in latest["occupancy"] if l["line_id"] == 2)
        assert line2["is_active"] is False
        assert line2["is_active_source"] == "current"
        assert line2["name_source"] == "current"
        assert [b["fixture_id"] for b in line2["fixtures"]] == [mig_id]
        assert line2["fixtures"][0]["locked"] is None
        # 保留区与含保留区的已用/空闲为未知（null），仅灯具占用给出事实部分
        assert line2["reservations"] is None
        assert line2["used_channels"] is None and line2["free_channels"] is None
        assert line2["fixtures_used_channels"] == 20
    finally:
        client.patch("/api/lines/2", json={"is_active": True})


def test_38_snapshot_latest_keeps_full_values(client):
    """有快照的最近发布：占用/迁移完整还原快照值，不带未知与来源标注。"""
    from app.database import SessionLocal
    from app.models import Plan

    # 前面用例为抢占“最近发布”插入了未来时间（2099）的旧记录；
    # 将其改回过去，使本次新发布成为最近发布
    with SessionLocal() as db:
        for plan in db.query(Plan).filter(Plan.status == "published").all():
            if plan.published_at and plan.published_at.year >= 2030:
                plan.published_at = plan.published_at.replace(year=2020)
        db.commit()

    p = client.post("/api/plans/preview").json()
    assert p["optimality_status"] == "optimal"
    assert client.post(f"/api/plans/{p['plan_id']}/publish").status_code == 200

    latest = client.get("/api/plans/published/latest").json()
    assert latest["has_published"] is True
    assert latest["plan_id"] == p["plan_id"]
    assert latest["from_snapshot"] is True
    assert "notice" not in latest and "field_sources" not in latest
    # 快照值完整：已用/空闲为数值、保留区为数组、锁定为布尔
    for line in latest["occupancy"]:
        assert isinstance(line["used_channels"], int)
        assert isinstance(line["free_channels"], int)
        assert isinstance(line["reservations"], list)
        assert "name_source" not in line and "is_active_source" not in line
    for a in latest["allocations"]:
        assert isinstance(a["locked"], bool)
        assert "name_source" not in a
    # 与发布时的预览内容一致（快照即发布时事实）
    assert latest["occupancy"] == p["occupancy"]
    assert latest["migrations"] == p["migrations"]
