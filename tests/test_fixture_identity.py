"""灯具 ID 单调分配与换场身份核对测试。

回归目标：删掉已发布方案中的末位灯具再新建时，SQLite 裸整数主键会重发旧 ID，
导致换场施工单把两台灯误当成一台（输出迁址/通道调整，漏掉拆除与接入）。修复后：

- 灯具 ID 由持久计数器单调分配：已发布快照引用过的 ID、删除的灯具 ID 永不复用，
  重启后仍有效（新库与保留历史数据的现有库行为一致）；
- 每台灯具持有可靠身份记录（创建时生成的 UUID，改名/换模式不变），随发布快照
  持久化；换场据此核实同一数字 ID 是否同一台灯；
- 升级前发布的旧快照缺少身份记录：同一 ID 落点有变化时明确拒绝自动施工单并
  说明不确定原因；落点完全一致（无需施工动作）或未涉及该 ID 的历史换场保持可用。
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone

import pytest

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DMX_DB_PATH"] = _tmp.name

from fastapi.testclient import TestClient  # noqa: E402

from app.changeover import ChangeoverError, build_changeover  # noqa: E402
from app.database import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Plan, PlanAllocation, PublishedSnapshot  # noqa: E402
from app.services import get_config_version, init_db  # noqa: E402

CATALOG_CSV = (
    "manufacturer,name,mode,channels\n"
    "ACME,Solar,Basic,10\n"
    "ACME,Solar,Full,20\n"
    "BrightCo,Par,3CH,3\n"
)


@pytest.fixture(scope="session")
def client():
    Base.metadata.drop_all(bind=engine)
    init_db()
    with TestClient(app) as c:
        r = c.post("/api/catalog/import", files={"file": ("c.csv", CATALOG_CSV, "text/csv")})
        assert r.status_code == 200, r.text
        yield c


def _modes(client):
    return {
        (t["name"], m["name"]): m
        for t in client.get("/api/catalog").json()
        for m in t["modes"]
    }


def _add_fixture(client, name, mode_key, **kw) -> dict:
    r = client.post("/api/fixtures", json={"name": name, "mode_id": _modes(client)[mode_key]["id"], **kw})
    assert r.status_code == 201, r.text
    return r.json()


def _publish_current(client) -> int:
    p = client.post("/api/plans/preview").json()
    assert p["optimality_status"] == "optimal", p
    r = client.post(f"/api/plans/{p['plan_id']}/publish")
    assert r.status_code == 200, r.text
    return p["plan_id"]


def make_payload(plan_id, cv, allocs, identities=None):
    """构造快照 payload；identities: {fid: str | None}，默认 "uid-{fid}"，
    显式 None 表示该灯缺少身份记录（模拟升级前发布的旧快照）。"""

    def ident_of(fid):
        if identities is not None and fid in identities:
            return identities[fid]
        return f"uid-{fid}"

    fx_by_line = {l: [] for l in range(1, 5)}
    for fid, name, line, s, e in allocs:
        fx_by_line[line].append(
            {"fixture_id": fid, "name": name, "start": s, "end": e,
             "channels": e - s + 1, "migrated": False, "locked": False}
        )
    occupancy = []
    for line in range(1, 5):
        used = set()
        for b in fx_by_line[line]:
            used.update(range(b["start"], b["end"] + 1))
        occupancy.append(
            {"line_id": line, "name": f"线路 {line}", "is_active": True, "size": 512,
             "used_channels": len(used), "free_channels": 512 - len(used),
             "fixtures": sorted(fx_by_line[line], key=lambda b: b["start"]),
             "reservations": []}
        )
    return {
        "plan_id": plan_id, "config_version": cv, "status": "published",
        "optimality_status": "optimal", "from_snapshot": True,
        "allocations": [
            {"fixture_id": f, "name": n, "identity": ident_of(f), "line_id": l, "start": s, "end": e,
             "channels": e - s + 1, "migrated": False, "locked": False}
            for f, n, l, s, e in allocs
        ],
        "occupancy": occupancy,
    }


# ---------------------------------------------------------------------------
# ID 单调分配：删除不复用、重启仍有效、快照引用过的 ID 不重发
# ---------------------------------------------------------------------------
def test_deleted_last_fixture_id_never_reused(client):
    ids = [
        _add_fixture(client, f"Seq{i}", ("Solar", "Basic"))["id"]
        for i in range(3)
    ]
    last_id = max(ids)
    # 删除末位灯具——SQLite 裸整数主键会重发该 ID 的场景
    assert client.delete(f"/api/fixtures/{last_id}").status_code == 204
    created = _add_fixture(client, "SeqNew", ("Solar", "Basic"))
    assert created["id"] > last_id  # 不复用已删除的末位 ID
    assert created["identity"]  # 新灯自带可靠身份记录


def test_id_allocation_survives_restart_and_delete_all(client):
    max_id = max(f["id"] for f in client.get("/api/fixtures").json())
    # 删除全部灯具后模拟两次重启（init_db 幂等，计数器对账只升不降）
    for f in client.get("/api/fixtures").json():
        assert client.delete(f"/api/fixtures/{f['id']}").status_code == 204
    init_db()
    init_db()
    created = _add_fixture(client, "AfterRestart", ("Solar", "Basic"))
    assert created["id"] > max_id  # 重启后仍不复用任何已删除的 ID


def test_snapshot_referenced_ids_never_reused(client):
    a = _add_fixture(client, "SnapA", ("Solar", "Basic"), previous_line=1, previous_start=1)
    b = _add_fixture(client, "SnapB", ("Solar", "Basic"), previous_line=1, previous_start=20)
    plan_id = _publish_current(client)  # 快照引用 a/b 的 ID
    snap = client.get(f"/api/plans/{plan_id}").json()
    referenced = {al["fixture_id"] for al in snap["allocations"]}
    assert {a["id"], b["id"]} <= referenced
    max_referenced = max(referenced)

    # 删除这些灯具（含末位）并模拟重启对账：快照引用过的 ID 不得重发
    assert client.delete(f"/api/fixtures/{a['id']}").status_code == 204
    assert client.delete(f"/api/fixtures/{b['id']}").status_code == 204
    init_db()
    created = _add_fixture(client, "SnapC", ("Solar", "Basic"))
    assert created["id"] > max_referenced


def test_identity_stable_across_rename_and_mode_change(client):
    fx = _add_fixture(client, "IdStable", ("Solar", "Basic"))
    identity = fx["identity"]
    assert identity
    r = client.put(f"/api/fixtures/{fx['id']}", json={"name": "IdStableRenamed"})
    assert r.status_code == 200, r.text
    assert r.json()["identity"] == identity  # 改名不变
    r = client.put(
        f"/api/fixtures/{fx['id']}",
        json={"mode_id": _modes(client)[("Solar", "Full")]["id"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["identity"] == identity  # 换模式不变


# ---------------------------------------------------------------------------
# 端到端：删灯重建后的换场输出拆除+接入；同一灯改名/换模式仍按同一台比较
# ---------------------------------------------------------------------------
def test_changeover_after_delete_recreate_outputs_remove_and_install(client):
    """核心场景：删掉已发布方案中的末位灯具再新建并发布——新灯必须获得全新 ID，
    换场施工单输出拆除（旧灯）+ 接入（新灯），而不是迁址或通道调整。"""
    alpha = _add_fixture(client, "CoAlpha", ("Solar", "Basic"),
                         previous_line=1, previous_start=1)
    beta = _add_fixture(client, "CoBeta", ("Solar", "Basic"),
                        previous_line=1, previous_start=100)
    plan_a = _publish_current(client)

    # 删除末位灯具 Beta 再新建 Gamma：修复前 SQLite 会把 beta 的 ID 重发给 Gamma
    assert client.delete(f"/api/fixtures/{beta['id']}").status_code == 204
    gamma = _add_fixture(client, "CoGamma", ("Solar", "Full"))
    assert gamma["id"] != beta["id"]
    assert gamma["identity"] != beta["identity"]
    plan_b = _publish_current(client)

    r = client.get(f"/api/plans/changeover?from={plan_a}&to={plan_b}")
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["feasible"] is True
    by_fid = {s["fixture_id"]: s["action"] for s in rep["steps"]}
    # Beta 拆除、Gamma 接入；Alpha 身份不变，绝不被误拆误接
    assert by_fid[beta["id"]] == "remove"
    assert by_fid[gamma["id"]] == "install"
    assert by_fid.get(alpha["id"]) not in ("remove", "install")
    assert rep["counts"]["remove"] == 1
    assert rep["counts"]["install"] == 1
    # 发布快照携带身份记录，供换场核实
    snap_b = client.get(f"/api/plans/{plan_b}").json()
    assert snap_b["from_snapshot"] is True
    assert all(a.get("identity") for a in snap_b["allocations"])


def test_changeover_rename_and_mode_change_stays_same_fixture(client):
    """有可靠身份记录的同一台灯：改名 + 换模式后仍按原 ID 比较，
    输出通道调整/迁址，绝不出现拆除与接入。"""
    fx = _add_fixture(client, "KeepMe", ("Solar", "Basic"),
                      previous_line=1, previous_start=300)
    plan_a = _publish_current(client)

    r = client.put(f"/api/fixtures/{fx['id']}", json={"name": "KeepMeRenamed"})
    assert r.status_code == 200, r.text
    r = client.put(
        f"/api/fixtures/{fx['id']}",
        json={"mode_id": _modes(client)[("Solar", "Full")]["id"]},
    )
    assert r.status_code == 200, r.text
    plan_b = _publish_current(client)

    r = client.get(f"/api/plans/changeover?from={plan_a}&to={plan_b}")
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["feasible"] is True
    assert rep["counts"]["remove"] == 0
    assert rep["counts"]["install"] == 0
    # 通道数 10 → 20：同一台灯体现为通道调整（或迁址），而非拆除+接入
    actions = {s["fixture_id"]: s["action"] for s in rep["steps"]}
    assert actions.get(fx["id"]) in ("channel_adjust", "relocate")
    # 两版快照中该灯身份记录一致
    iden_a = {a["fixture_id"]: a.get("identity")
              for a in client.get(f"/api/plans/{plan_a}").json()["allocations"]}
    iden_b = {a["fixture_id"]: a.get("identity")
              for a in client.get(f"/api/plans/{plan_b}").json()["allocations"]}
    assert iden_a[fx["id"]] == iden_b[fx["id"]] == fx["identity"]


# ---------------------------------------------------------------------------
# 引擎层：身份核对规则
# ---------------------------------------------------------------------------
def test_engine_identity_conflict_classified_as_remove_and_install():
    """同一数字 ID、身份记录不同：拆除旧灯 + 接入新灯，而非迁址/调通道。"""
    a = make_payload(1, 1, [(1, "Old", 1, 1, 10)], identities={1: "uid-old"})
    b = make_payload(2, 2, [(1, "New", 1, 1, 10)], identities={1: "uid-new"})
    rep = build_changeover(a, b)
    assert rep["feasible"] is True
    assert rep["counts"]["remove"] == 1 and rep["counts"]["install"] == 1
    assert rep["counts"]["relocate"] == 0 and rep["counts"]["channel_adjust"] == 0
    actions = [(s["action"], s["fixture_id"]) for s in rep["steps"]]
    assert actions == [("remove", 1), ("install", 1)]
    # 拆除步骤展示旧版灯具名，接入步骤展示新版灯具名（同 ID 两台灯不得张冠李戴）
    assert rep["steps"][0]["fixture_name"] == "Old"
    assert rep["steps"][1]["fixture_name"] == "New"
    # 步骤说明明确提示这是两台不同的灯，勿按迁址处理
    assert "另一台" in rep["steps"][0]["note"]
    assert "另一台" in rep["steps"][1]["note"]


def test_engine_same_identity_despite_rename_and_mode_change():
    """同一台灯改名/换模式（身份记录相同）：仍按同一台比较，输出通道调整。"""
    a = make_payload(1, 1, [(1, "OldName", 1, 1, 10)], identities={1: "uid-1"})
    b = make_payload(2, 2, [(1, "NewName", 1, 1, 20)], identities={1: "uid-1"})
    rep = build_changeover(a, b)
    assert rep["feasible"] is True
    assert rep["counts"]["remove"] == 0 and rep["counts"]["install"] == 0
    assert rep["counts"]["channel_adjust"] == 1
    assert rep["steps"][0]["action"] == "channel_adjust"


def test_engine_missing_identity_with_position_change_rejected():
    """旧快照缺身份记录且同一 ID 落点有变化：拒绝并说明不确定原因（双向）。"""
    a = make_payload(1, 1, [(1, "A", 1, 1, 10)], identities={1: None})
    b = make_payload(2, 2, [(1, "A", 1, 11, 20)])  # 新版有身份记录
    with pytest.raises(ChangeoverError) as exc:
        build_changeover(a, b)
    assert exc.value.code == "identity_uncertain"
    fixtures = exc.value.reasons[0]["fixtures"]
    assert fixtures[0]["fixture_id"] == 1
    assert fixtures[0]["identity_in_a"] is False
    assert fixtures[0]["identity_in_b"] is True
    with pytest.raises(ChangeoverError) as exc2:
        build_changeover(b, a)
    assert exc2.value.code == "identity_uncertain"


def test_engine_missing_identity_same_position_is_safe():
    """缺身份记录但落点完全一致：无需任何施工动作，其余拆除/接入正常输出。"""
    a = make_payload(
        1, 1, [(1, "A", 1, 1, 10), (2, "B", 1, 11, 20)], identities={1: None, 2: None}
    )
    b = make_payload(2, 2, [(1, "A", 1, 1, 10), (3, "C", 1, 21, 30)], identities={1: None})
    rep = build_changeover(a, b)
    assert rep["feasible"] is True
    actions = [(s["action"], s["fixture_id"]) for s in rep["steps"]]
    assert ("remove", 2) in actions
    assert ("install", 3) in actions
    assert all(fid != 1 for _, fid in actions)  # id 1 落点一致，不产生步骤


# ---------------------------------------------------------------------------
# HTTP 层：升级前旧快照（有快照但无身份记录）的拒绝与放行
# ---------------------------------------------------------------------------
def _insert_legacy_snapshot_plan(fixtures, published_at) -> int:
    """直接落库一条“有快照但快照无身份记录”的旧发布记录（模拟升级前发布）。

    fixtures: [(fixture_id, line, start, end, name)]；快照内容自此不再改写。
    """
    fx_by_line = {l: [] for l in range(1, 5)}
    for fid, line, start, end, name in fixtures:
        fx_by_line[line].append(
            {"fixture_id": fid, "name": name, "start": start, "end": end,
             "channels": end - start + 1, "migrated": False, "locked": False}
        )
    occupancy = []
    for line in range(1, 5):
        used = sum(b["channels"] for b in fx_by_line[line])
        occupancy.append(
            {"line_id": line, "name": f"线路 {line}", "is_active": True, "size": 512,
             "used_channels": used, "free_channels": 512 - used,
             "fixtures": sorted(fx_by_line[line], key=lambda b: b["start"]),
             "reservations": []}
        )
    with SessionLocal() as db:
        plan = Plan(
            config_version=get_config_version(db), status="published",
            optimality_status="optimal", migration_count=0, used_lines=1,
            published_at=published_at,
        )
        db.add(plan)
        db.flush()
        payload = {
            "feasible": True, "id": plan.id, "plan_id": plan.id,
            "config_version": plan.config_version, "status": "published",
            "optimality_status": "optimal", "migration_count": 0, "used_lines": 1,
            "from_snapshot": True,
            # 注意：allocations 故意不带 identity——升级前发布的快照即如此
            "allocations": [
                {"fixture_id": fid, "name": name, "line_id": line, "start": start,
                 "end": end, "channels": end - start + 1, "migrated": False, "locked": False}
                for fid, line, start, end, name in fixtures
            ],
            "migrations": [], "occupancy": occupancy, "conflicts": [],
        }
        for fid, line, start, end, _name in fixtures:
            db.add(PlanAllocation(plan_id=plan.id, fixture_id=fid, line_id=line,
                                  start=start, end=end, migrated=False))
        db.add(
            PublishedSnapshot(
                plan_id=plan.id, config_version=plan.config_version,
                published_at=plan.published_at,
                payload=json.dumps(payload, ensure_ascii=False, default=str),
            )
        )
        db.commit()
        return plan.id


def test_http_changeover_rejects_legacy_snapshot_without_identity(client):
    """同一 ID 在旧快照（无身份记录）与新版中落点不同：422 + 不确定原因。"""
    fx = _add_fixture(client, "UncertainFx", ("Solar", "Basic"),
                      previous_line=1, previous_start=100)
    plan_b = _publish_current(client)  # 新版：该灯落点 1 线 100-109，带身份记录
    # 旧版：同一 ID 落点 1 线 1-10，快照无身份记录（模拟升级前发布）
    legacy_id = _insert_legacy_snapshot_plan(
        [(fx["id"], 1, 1, 10, "UncertainFx")],
        datetime(2020, 1, 1, tzinfo=timezone.utc),
    )

    r = client.get(f"/api/plans/changeover?from={legacy_id}&to={plan_b}")
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "identity_uncertain"
    assert "无法排除" in detail["message"]
    fixtures = detail["reasons"][0]["fixtures"]
    assert fixtures[0]["fixture_id"] == fx["id"]
    assert fixtures[0]["identity_in_a"] is False
    assert fixtures[0]["identity_in_b"] is True
    assert fixtures[0]["from_a"] == {"line_id": 1, "start": 1, "end": 10}
    # 反向（旧版作目标）同样拒绝
    r = client.get(f"/api/plans/changeover?from={plan_b}&to={legacy_id}")
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "identity_uncertain"


def test_http_legacy_changeover_without_ambiguity_still_works(client):
    """未涉及身份歧义的历史换场保持可用：两份无身份记录的旧快照之间，
    共有 ID 落点一致（无步骤），仅拆除与接入正常输出。"""
    keep = _add_fixture(client, "LegacyKeep", ("Solar", "Basic"))
    gone = _add_fixture(client, "LegacyGone", ("Solar", "Basic"))
    new = _add_fixture(client, "LegacyNew", ("Solar", "Basic"))
    plan_a = _insert_legacy_snapshot_plan(
        [(keep["id"], 1, 1, 10, "LegacyKeep"), (gone["id"], 1, 11, 20, "LegacyGone")],
        datetime(2020, 2, 2, tzinfo=timezone.utc),
    )
    plan_b = _insert_legacy_snapshot_plan(
        [(keep["id"], 1, 1, 10, "LegacyKeep"), (new["id"], 1, 21, 30, "LegacyNew")],
        datetime(2020, 3, 3, tzinfo=timezone.utc),
    )
    r = client.get(f"/api/plans/changeover?from={plan_a}&to={plan_b}")
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["feasible"] is True
    actions = {(s["action"], s["fixture_id"]) for s in rep["steps"]}
    assert actions == {("remove", gone["id"]), ("install", new["id"])}
    # 共有 ID 落点一致：不产生任何步骤，也无需身份记录
    assert all(s["fixture_id"] != keep["id"] for s in rep["steps"])


def test_changeover_remains_readonly_with_identity_checks(client):
    """身份核对不引入写操作：换场查询后配置版本与灯具列表不变。"""
    before_version = client.get("/api/plans/version").json()["config_version"]
    fixtures_before = client.get("/api/fixtures").json()
    plans = [p["plan_id"] for p in client.get("/api/plans/published").json()]
    assert len(plans) >= 2
    r = client.get(f"/api/plans/changeover?from={plans[-1]}&to={plans[0]}")
    assert r.status_code in (200, 422)  # 可行或明确拒绝，绝不半截输出
    assert client.get("/api/plans/version").json()["config_version"] == before_version
    assert client.get("/api/fixtures").json() == fixtures_before


def test_frontend_ships_identity_features(client):
    """页面包含身份标识列与身份无法核实的渲染分支。"""
    html = client.get("/").text
    assert "身份标识" in html
    js = client.get("/static/app.js").text
    assert "identity_uncertain" in js
