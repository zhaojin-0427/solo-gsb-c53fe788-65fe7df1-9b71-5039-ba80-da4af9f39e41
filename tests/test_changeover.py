"""双版本换场施工单测试：纯引擎逐步模拟安全性 + HTTP 端到端（快照来源、拒绝条件、只读）。"""
from __future__ import annotations

import os
import tempfile

import pytest

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DMX_DB_PATH"] = _tmp.name

from fastapi.testclient import TestClient  # noqa: E402

from app.changeover import ChangeoverError, build_changeover  # noqa: E402
from app.database import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Plan, PlanAllocation  # noqa: E402
from app.services import get_config_version, init_db  # noqa: E402


@pytest.fixture(scope="session")
def client():
    Base.metadata.drop_all(bind=engine)
    init_db()
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# 快照 payload 构造（引擎层只依赖 payload，不碰数据库）
# ---------------------------------------------------------------------------
def make_payload(plan_id, cv, allocs, reservations=None, active_lines=(1, 2, 3, 4)):
    """allocs: [(fid, name, line, start, end)]；reservations: {line: [(start,end), ...]}。"""
    reservations = reservations or {}
    res_by_line = {l: [] for l in range(1, 5)}
    for line, items in reservations.items():
        for k, (s, e) in enumerate(items):
            res_by_line[line].append({"id": k + 1 + line * 100, "start": s, "end": e, "reason": ""})
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
        for r in res_by_line[line]:
            used.update(range(r["start"], r["end"] + 1))
        occupancy.append(
            {
                "line_id": line, "name": f"线路 {line}", "is_active": line in active_lines,
                "size": 512, "used_channels": len(used), "free_channels": 512 - len(used),
                "fixtures": sorted(fx_by_line[line], key=lambda b: b["start"]),
                "reservations": res_by_line[line],
            }
        )
    return {
        "plan_id": plan_id, "config_version": cv, "status": "published",
        "optimality_status": "optimal", "from_snapshot": True,
        "allocations": [
            {"fixture_id": f, "name": n, "line_id": l, "start": s, "end": e,
             "channels": e - s + 1, "migrated": False, "locked": False}
            for f, n, l, s, e in allocs
        ],
        "occupancy": occupancy,
    }


def replay_steps(payload_a, payload_b, report):
    """独立重放报告中的步骤：校验每个中间态无重叠、不压保留区、不落停用线路，
    且每步 release/occupy 与在场灯具实际区间一致；末态严格等于 B。"""
    state = {a["fixture_id"]: (a["line_id"], a["start"], a["end"]) for a in payload_a["allocations"]}
    reservations = {
        (l, r["start"], r["end"])
        for l in range(1, 5)
        for r in payload_a["occupancy"][l - 1]["reservations"]
    }
    active = {l: payload_a["occupancy"][l - 1]["is_active"] for l in range(1, 5)}

    def assert_safe(pos):
        items = sorted(pos.items())
        for fid, (l, s, e) in items:
            assert active[l], f"灯具 #{fid} 位于停用线路 {l}"
            assert 1 <= s <= e <= 512
            for rl, rs, re_ in reservations:
                assert not (l == rl and s <= re_ and rs <= e), f"灯具 #{fid} 压保留区"
        for i, (f, (l, s, e)) in enumerate(items):
            for g, (l2, s2, e2) in items[i + 1:]:
                assert not (l == l2 and s <= e2 and s2 <= e), f"灯具 #{f} 与 #{g} 重叠"

    assert_safe(state)
    for st in report["steps"]:
        fid = st["fixture_id"]
        # 拆除与迁址步骤的 release 必须等于该灯当前实际区间（resize 的缩短只释放尾部）
        if st["action"] in ("remove", "relocate", "temp_relocate"):
            assert len(st["release"]) == 1
            r = st["release"][0]
            assert state[fid] == (r["line_id"], r["start"], r["end"])
        if st["action"] == "channel_adjust":
            l, s, e = state[fid]
            if st["occupy"] is None:
                assert [(r["line_id"], r["start"], r["end"]) for r in st["release"]] == [
                    (l, st["channels_after"] + s, e)
                ]
            else:
                assert st["release"] == []
                assert (st["occupy"]["line_id"], st["occupy"]["start"]) == (l, e + 1)
        if st["action"] == "remove":
            del state[fid]
        elif st["action"] == "channel_adjust":
            l, s, _e = state[fid]
            if st["occupy"] is None:
                state[fid] = (l, s, s + st["channels_after"] - 1)
            else:
                state[fid] = (l, s, st["occupy"]["end"])
        else:
            o = st["occupy"]
            state[fid] = (o["line_id"], o["start"], o["end"])
        assert_safe(state)

    target = {a["fixture_id"]: (a["line_id"], a["start"], a["end"]) for a in payload_b["allocations"]}
    assert state == target


# ---------------------------------------------------------------------------
# 引擎层：分类、顺序与逐步模拟
# ---------------------------------------------------------------------------
def test_engine_classifies_four_kinds_in_order():
    # 1 拆除；2 同址缩短；3 迁址（跨线）；4 同址加长；5 接入
    a = make_payload(
        1, 1,
        [(1, "R", 1, 1, 10), (2, "S", 1, 11, 30), (3, "M", 1, 31, 40), (4, "G", 1, 41, 45)],
    )
    b = make_payload(
        2, 2,
        [(2, "S", 1, 11, 20), (3, "M", 2, 1, 10), (4, "G", 1, 41, 55), (5, "N", 1, 1, 10)],
    )
    rep = build_changeover(a, b)
    assert rep["feasible"] is True
    assert rep["counts"] == {
        "fixtures_in_a": 4, "fixtures_in_b": 4,
        "remove": 1, "relocate": 1,
        "channel_adjust": 2, "channel_adjust_shrink": 1, "channel_adjust_grow": 1,
        "install": 1, "temporary_steps": 0, "total_steps": 5,
    }
    actions = [(s["seq"], s["action"], s["fixture_id"]) for s in rep["steps"]]
    # 顺序：拆除 → 缩短 → 迁址 → 加长 → 接入
    assert actions == [
        (1, "remove", 1),
        (2, "channel_adjust", 2),
        (3, "relocate", 3),
        (4, "channel_adjust", 4),
        (5, "install", 5),
    ]
    # 加长步骤在迁址腾出空间后执行，占用新尾部
    grow = rep["steps"][3]
    assert grow["occupy"] == {"line_id": 1, "start": 46, "end": 55, "channels": 10}
    assert grow["channels_before"] == 5 and grow["channels_after"] == 15
    replay_steps(a, b, rep)


def test_engine_swap_cycle_uses_temporary_buffer():
    a = make_payload(1, 1, [(1, "A", 1, 1, 10), (2, "B", 1, 11, 20)])
    b = make_payload(2, 2, [(1, "A", 1, 11, 20), (2, "B", 1, 1, 10)])
    rep = build_changeover(a, b)
    assert rep["feasible"] is True
    assert rep["counts"]["temporary_steps"] == 1
    kinds = [s["action"] for s in rep["steps"]]
    assert kinds == ["temp_relocate", "relocate", "relocate"]
    # 临时位不得与新版任何落点相交（1-10、11-20）
    park = rep["steps"][0]["occupy"]
    assert not (park["line_id"] == 1 and park["start"] <= 20 and park["end"] >= 1)
    # 末步临时周转归位
    assert rep["steps"][2]["note"] == "临时周转归位"
    replay_steps(a, b, rep)


def test_engine_full_pair_of_lines_swaps_with_shared_buffer():
    # 线路 1、2 各自被两台灯占满，线路 3 整线保留，唯一周转空间是线路 4
    res = {3: [(1, 512)]}
    a = make_payload(
        1, 1,
        [(1, "A", 1, 1, 256), (2, "B", 1, 257, 512),
         (3, "C", 2, 1, 256), (4, "D", 2, 257, 512)],
        reservations=res,
    )
    b = make_payload(
        2, 2,
        [(1, "A", 1, 257, 512), (2, "B", 1, 1, 256),
         (3, "C", 2, 257, 512), (4, "D", 2, 1, 256)],
        reservations=res,
    )
    rep = build_changeover(a, b)
    assert rep["feasible"] is True
    # 每对互换各需要一次周转；周转都落在空闲的线路 4，且不与任何 B 落点相交
    for s in rep["steps"]:
        if s["temporary"]:
            assert s["occupy"]["line_id"] == 4
    replay_steps(a, b, rep)


def test_engine_blocked_when_no_buffer_lists_blocking_runs():
    # 单条启用线路两台 256ch 互换，其余线路整线保留：没有任何周转位
    res = {2: [(1, 512)], 3: [(1, 512)], 4: [(1, 512)]}
    a = make_payload(1, 1, [(1, "A", 1, 1, 256), (2, "B", 1, 257, 512)], reservations=res)
    b = make_payload(2, 2, [(1, "A", 1, 257, 512), (2, "B", 1, 1, 256)], reservations=res)
    rep = build_changeover(a, b)
    assert rep["feasible"] is False
    assert rep["reason_code"] == "no_safe_buffer"
    assert rep["steps"] == []  # 绝不输出貌似安全的步骤
    blocking = rep["blocking"]
    assert blocking and blocking[0]["blocking_fixture_ids"]
    for line_info in blocking[0]["free_runs"]:
        assert line_info["largest_neutral_free_channels"] == 0
        assert line_info["needed_channels"] == 256


def test_engine_rejects_reservation_mismatch():
    a = make_payload(1, 1, [(1, "A", 1, 1, 10)])
    b = make_payload(2, 2, [(1, "A", 1, 1, 10)], reservations={1: [(500, 512)]})
    with pytest.raises(ChangeoverError) as exc:
        build_changeover(a, b)
    assert exc.value.code == "reservations_mismatch"
    assert exc.value.reasons[0]["type"] == "reservation_mismatch"
    assert exc.value.reasons[0]["lines"][0]["line_id"] == 1


def test_engine_rejects_line_activity_mismatch():
    a = make_payload(1, 1, [(1, "A", 1, 1, 10)], active_lines=(1, 2, 3, 4))
    b = make_payload(2, 2, [(1, "A", 1, 1, 10)], active_lines=(1, 2, 3))
    with pytest.raises(ChangeoverError) as exc:
        build_changeover(a, b)
    assert exc.value.code == "lines_mismatch"
    assert exc.value.reasons[0]["lines"] == [
        {"line_id": 4, "from_a_active": True, "to_b_active": False}
    ]


def test_engine_rejects_missing_snapshot():
    a = make_payload(1, 1, [(1, "A", 1, 1, 10)])
    a["from_snapshot"] = False
    b = make_payload(2, 2, [(1, "A", 1, 1, 10)])
    with pytest.raises(ChangeoverError) as exc:
        build_changeover(a, b)
    assert exc.value.code == "missing_snapshot"
    with pytest.raises(ChangeoverError) as exc2:
        build_changeover(b, a)
    assert exc2.value.code == "missing_snapshot"


def test_engine_rejects_inconsistent_snapshot():
    a = make_payload(1, 1, [(1, "A", 1, 1, 10), (2, "B", 1, 5, 15)])  # 快照内灯具重叠
    b = make_payload(2, 2, [(1, "A", 1, 1, 10)])
    with pytest.raises(ChangeoverError) as exc:
        build_changeover(a, b)
    assert exc.value.code == "snapshot_inconsistent"


def test_engine_relocation_with_channel_change():
    # 迁址同时改变通道数（10ch → 20ch），一步到位，占用 20ch
    a = make_payload(1, 1, [(1, "A", 1, 1, 10)])
    b = make_payload(2, 2, [(1, "A", 2, 100, 119)])
    rep = build_changeover(a, b)
    assert rep["feasible"] is True
    assert rep["counts"] == {
        "fixtures_in_a": 1, "fixtures_in_b": 1,
        "remove": 0, "relocate": 1, "channel_adjust": 0,
        "channel_adjust_shrink": 0, "channel_adjust_grow": 0,
        "install": 0, "temporary_steps": 0, "total_steps": 1,
    }
    st = rep["steps"][0]
    assert st["release"] == [{"line_id": 1, "start": 1, "end": 10, "channels": 10}]
    assert st["occupy"] == {"line_id": 2, "start": 100, "end": 119, "channels": 20}
    replay_steps(a, b, rep)


def test_engine_shrink_then_grow_with_mover_in_between():
    # 灯具 1 同址加长到 30，灯具 2 迁址让出其尾部（B: 11-30 在线 2），
    # 灯具 3 原位不动；加长必须排在迁址之后
    a = make_payload(1, 1, [(1, "A", 1, 1, 10), (2, "B", 1, 11, 30), (3, "C", 1, 31, 40)])
    b = make_payload(2, 2, [(1, "A", 1, 1, 30), (2, "B", 2, 11, 30), (3, "C", 1, 31, 40)])
    rep = build_changeover(a, b)
    assert rep["feasible"] is True
    seq = [(s["seq"], s["action"], s["fixture_id"]) for s in rep["steps"]]
    grow_seq = next(s for s, act, f in seq if act == "channel_adjust" and f == 1)
    move_seq = next(s for s, act, f in seq if act == "relocate" and f == 2)
    assert move_seq < grow_seq
    replay_steps(a, b, rep)


def test_engine_two_disjoint_swap_cycles_need_two_temp_moves():
    # 单线上两组互不相交的互换环（1↔2、3↔4），唯一周转空间 41-50：
    # 每解一个环都需要一次临时周转，共两次 temp_relocate（不同灯具）
    a = make_payload(
        1, 1,
        [(1, "A", 1, 1, 10), (2, "B", 1, 11, 20), (3, "C", 1, 21, 30), (4, "D", 1, 31, 40)],
    )
    b = make_payload(
        2, 2,
        [(1, "A", 1, 11, 20), (2, "B", 1, 1, 10), (3, "C", 1, 31, 40), (4, "D", 1, 21, 30)],
    )
    rep = build_changeover(a, b)
    assert rep["feasible"] is True
    temp_steps = [s for s in rep["steps"] if s["temporary"]]
    assert len(temp_steps) == 2
    assert {s["fixture_id"] for s in temp_steps} <= {1, 2, 3, 4}
    # 每次临时占用都与新版全部落点（1-40）不相交
    for st in temp_steps:
        o = st["occupy"]
        assert o["line_id"] == 1 and o["start"] >= 41
    replay_steps(a, b, rep)


def test_engine_temp_park_avoids_grow_tail_and_install_slot():
    # 灯具 1、2 互换位置，灯具 3 同址加长占满 1 线尾部，灯具 4 接入 1 线 31-40；
    # 临时位必须同时避开 1、2 的新版落点、3 的加长段与 4 的接入位
    a = make_payload(1, 1, [(1, "A", 1, 1, 10), (2, "B", 1, 11, 20), (3, "C", 1, 21, 30)])
    b = make_payload(
        2, 2,
        [(1, "A", 1, 11, 20), (2, "B", 1, 1, 10), (3, "C", 1, 21, 60), (4, "N", 1, 61, 70)],
    )
    rep = build_changeover(a, b)
    assert rep["feasible"] is True
    # 加长（40ch）与接入发生在迁址之后，临时位从未占用 21-70
    for st in rep["steps"]:
        if st["temporary"]:
            assert not (st["occupy"]["line_id"] == 1 and st["occupy"]["start"] <= 70
                        and st["occupy"]["end"] >= 21)
    order = {(s["action"], s["fixture_id"]): s["seq"] for s in rep["steps"]}
    assert order[("channel_adjust", 3)] > max(
        s["seq"] for s in rep["steps"] if s["fixture_id"] in (1, 2) and s["action"] != "channel_adjust"
    )
    replay_steps(a, b, rep)


def test_engine_identical_versions_yield_no_steps():
    a = make_payload(1, 1, [(1, "A", 1, 1, 10), (2, "B", 1, 11, 20)])
    b = make_payload(2, 2, [(1, "A", 1, 1, 10), (2, "B", 1, 11, 20)])
    rep = build_changeover(a, b)
    assert rep["feasible"] is True
    assert rep["steps"] == []
    assert rep["counts"]["total_steps"] == 0


# ---------------------------------------------------------------------------
# HTTP 端到端：真实发布快照、状态校验与只读
# ---------------------------------------------------------------------------
CATALOG_CSV = (
    "manufacturer,name,mode,channels\n"
    "ACME,Solar,Basic,10\n"
    "ACME,Solar,Full,20\n"
    "BrightCo,Par,3CH,3\n"
)


def _publish_current(client) -> int:
    p = client.post("/api/plans/preview").json()
    assert p["optimality_status"] == "optimal", p
    r = client.post(f"/api/plans/{p['plan_id']}/publish")
    assert r.status_code == 200, r.text
    return p["plan_id"]


def test_http_changeover_from_real_snapshots(client):
    r = client.post("/api/catalog/import",
                    files={"file": ("c.csv", CATALOG_CSV, "text/csv")})
    assert r.status_code == 200, r.text
    modes = {(t["name"], m["name"]): m for t in client.get("/api/catalog").json() for m in t["modes"]}
    client.post("/api/fixtures", json={"name": "Alpha", "mode_id": modes[("Solar", "Basic")]["id"],
                                       "previous_line": 1, "previous_start": 1})
    client.post("/api/fixtures", json={"name": "Beta", "mode_id": modes[("Solar", "Full")]["id"],
                                       "previous_line": 1, "previous_start": 100})
    plan_a = _publish_current(client)

    # 改配置：先新增 Gamma（取得不与 Beta 复用的新 ID），再删除 Beta，发布新版
    fx = {f["name"]: f for f in client.get("/api/fixtures").json()}
    beta_id = fx["Beta"]["id"]
    r = client.post("/api/fixtures", json={"name": "Gamma", "mode_id": modes[("Par", "3CH")]["id"]})
    gamma_id = r.json()["id"]
    assert gamma_id != beta_id
    assert client.delete(f"/api/fixtures/{beta_id}").status_code == 204
    plan_b = _publish_current(client)

    r = client.get(f"/api/plans/changeover?from={plan_a}&to={plan_b}")
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["feasible"] is True
    assert rep["from_snapshot"] is True
    by_fid = {s["fixture_id"]: s["action"] for s in rep["steps"]}
    # Beta 仅在旧版 -> 拆除；Gamma 仅在新版 -> 接入
    assert by_fid[beta_id] == "remove"
    assert by_fid[gamma_id] == "install"
    snap_b = client.get(f"/api/plans/{plan_b}").json()
    ids_b = {a["fixture_id"] for a in snap_b["allocations"]}
    snap_ids = {a["fixture_id"] for a in client.get(f"/api/plans/{plan_a}").json()["allocations"]} | ids_b
    assert all(s["fixture_id"] in snap_ids for s in rep["steps"])


def test_http_changeover_uses_snapshot_not_current_addresses(client):
    """发布后当前灯具的既有地址已被回写：施工单仍以两份快照为起点/目标。"""
    plans = [p["plan_id"] for p in client.get("/api/plans/published").json()]
    assert len(plans) >= 2
    r = client.get(f"/api/plans/changeover?from={plans[-1]}&to={plans[0]}")
    assert r.status_code == 200, r.text
    rep = r.json()
    # 起点快照中的拆除灯具（Beta，删除后已不在当前灯具表）必须出现在步骤里；
    # 若误用当前既有地址/当前灯具表冒充起点，拆除步骤将消失
    fx_names = {f["name"] for f in client.get("/api/fixtures").json()}
    assert "Beta" not in fx_names
    removed = [s for s in rep["steps"] if s["action"] == "remove"]
    assert removed, rep
    assert removed[0]["fixture_name"] == "Beta"


def test_http_changeover_validations(client):
    plans = [p["plan_id"] for p in client.get("/api/plans/published").json()]
    # 同一份方案
    r = client.get(f"/api/plans/changeover?from={plans[0]}&to={plans[0]}")
    assert r.status_code == 422
    # 不存在
    r = client.get(f"/api/plans/changeover?from=999999&to={plans[0]}")
    assert r.status_code == 404
    # 未发布草稿
    p = client.post("/api/plans/preview").json()
    if p["optimality_status"] == "optimal":
        draft_id = p["plan_id"]
        r = client.get(f"/api/plans/changeover?from={draft_id}&to={plans[0]}")
        assert r.status_code == 409


def test_http_changeover_rejects_legacy_plan_without_snapshot(client):
    from datetime import datetime, timezone

    fx = client.get("/api/fixtures").json()
    fid = fx[0]["id"]
    with SessionLocal() as db:
        plan = Plan(
            config_version=get_config_version(db), status="published",
            optimality_status="optimal", migration_count=0, used_lines=1,
            # 旧时间戳，避免抢占“最近发布”影响其它用例的版本选择
            published_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        )
        db.add(plan)
        db.flush()
        db.add(PlanAllocation(plan_id=plan.id, fixture_id=fid, line_id=1, start=1, end=3,
                              migrated=False))
        db.commit()
        legacy_id = plan.id
    plans = [p["plan_id"] for p in client.get("/api/plans/published").json()]
    other = next(p for p in plans if p != legacy_id)
    r = client.get(f"/api/plans/changeover?from={legacy_id}&to={other}")
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "missing_snapshot"
    r = client.get(f"/api/plans/changeover?from={other}&to={legacy_id}")
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "missing_snapshot"


def test_http_changeover_is_readonly(client):
    before_version = client.get("/api/plans/version").json()["config_version"]
    plans = [p["plan_id"] for p in client.get("/api/plans/published").json()]
    fixtures_before = client.get("/api/fixtures").json()
    # 两版都有快照且保留区/线路一致的组合（无快照的遗留记录不参与）
    pair = None
    for a in plans:
        for b in plans:
            if a == b:
                continue
            r0 = client.get(f"/api/plans/changeover?from={a}&to={b}")
            if r0.status_code == 200:
                pair = (a, b)
                break
        if pair:
            break
    assert pair, plans
    r = client.get(f"/api/plans/changeover?from={pair[0]}&to={pair[1]}")
    assert r.status_code == 200
    assert client.get("/api/plans/version").json()["config_version"] == before_version
    assert client.get("/api/fixtures").json() == fixtures_before
    # 现有发布查询接口行为不变
    latest = client.get("/api/plans/published/latest").json()
    assert latest["has_published"] is True


def test_http_changeover_page_ships_panel(client):
    html = client.get("/").text
    assert 'id="changeover-panel"' in html
    assert 'id="changeover-from-select"' in html
    assert 'id="changeover-to-select"' in html
    assert 'id="btn-changeover"' in html
    js = client.get("/static/app.js").text
    assert "/api/plans/changeover" in js
    assert "renderChangeover" in js
