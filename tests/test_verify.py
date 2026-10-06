"""现场地址核验端到端测试：CSV 解析、隔离策略、逐项清单、基准不变性与只读保证。"""
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
    "LitePro,Beam,Std,5\n"
)


@pytest.fixture(scope="session")
def published(client):
    """导入型录、建 4 台灯具、预览并发布，返回 (plan_id, 发布 payload)。"""
    r = client.post("/api/catalog/import", files={"file": ("c.csv", CATALOG_CSV, "text/csv")})
    assert r.status_code == 200, r.text
    modes = {(t["name"], m["name"]): m for t in client.get("/api/catalog").json() for m in t["modes"]}
    for name, mode_name in [
        ("Alpha", ("Solar", "Basic")),   # 10ch
        ("Beta", ("Solar", "Full")),     # 20ch
        ("Gamma", ("Par", "3CH")),       # 3ch
        ("Delta", ("Beam", "Std")),      # 5ch
    ]:
        r = client.post("/api/fixtures", json={"name": name, "mode_id": modes[mode_name]["id"]})
        assert r.status_code == 201, r.text
    p = client.post("/api/plans/preview").json()
    assert p["optimality_status"] == "optimal", p
    r = client.post(f"/api/plans/{p['plan_id']}/publish")
    assert r.status_code == 200, r.text
    payload = client.get(f"/api/plans/{p['plan_id']}").json()
    assert payload["from_snapshot"] is True
    return p["plan_id"], payload


def make_csv(rows) -> str:
    """rows: (fixture_id, line, start, channels) 元组列表。"""
    lines = ["灯具ID,线路,起始地址,通道数"]
    lines += [",".join(str(v) for v in row) for row in rows]
    return "\n".join(lines) + "\n"


def csv_from_payload(payload) -> str:
    return make_csv(
        (a["fixture_id"], a["line_id"], a["start"], a["channels"]) for a in payload["allocations"]
    )


def verify(client, plan_id, csv_text):
    return client.post(f"/api/plans/{plan_id}/verify", files={"file": ("site.csv", csv_text, "text/csv")})


def test_01_list_published(client, published):
    plan_id, _ = published
    r = client.get("/api/plans/published")
    assert r.status_code == 200
    plans = r.json()
    assert len(plans) == 1
    assert plans[0]["plan_id"] == plan_id
    assert plans[0]["published_at"]


def test_02_consistent(client, published):
    plan_id, payload = published
    r = verify(client, plan_id, csv_from_payload(payload))
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["consistent"] is True
    assert rep["verdict"] == "consistent"
    assert rep["from_snapshot"] is True
    assert rep["plan_id"] == plan_id
    c = rep["counts"]
    assert c["checked"] == c["matched"] == len(payload["allocations"])
    assert c["missing"] == c["extra"] == c["mismatched"] == c["overlap_pairs"] == c["quarantined_rows"] == 0


def test_03_exported_csv_roundtrip(client, published):
    """本系统导出的地址表 CSV（带 BOM、含多余列）可直接上传核验。"""
    plan_id, _ = published
    csv_bytes = client.get(f"/api/plans/published/{plan_id}/csv").content
    assert csv_bytes.startswith(b"\xef\xbb\xbf")
    r = client.post(f"/api/plans/{plan_id}/verify", files={"file": ("export.csv", csv_bytes, "text/csv")})
    assert r.status_code == 200, r.text
    assert r.json()["consistent"] is True


def test_04_missing_and_extra(client, published):
    plan_id, payload = published
    allocs = payload["allocations"]
    rows = [(a["fixture_id"], a["line_id"], a["start"], a["channels"]) for a in allocs]
    dropped = rows.pop(0)          # 删掉一台 -> 缺失
    rows.append((999999, 4, 1, 8))  # 加一台未知 ID -> 额外
    rep = verify(client, plan_id, make_csv(rows)).json()
    assert rep["consistent"] is False
    assert rep["verdict"] == "inconsistent"
    assert [m["fixture_id"] for m in rep["missing"]] == [dropped[0]]
    assert rep["missing"][0]["line_id"] == dropped[1]
    assert [x["fixture_id"] for x in rep["extra"]] == [999999]
    assert rep["extra"][0]["end"] == 8  # 闭区间 end = start + channels - 1
    assert rep["counts"]["missing"] == 1 and rep["counts"]["extra"] == 1


def test_05_mismatch_fields(client, published):
    plan_id, payload = published
    allocs = payload["allocations"]
    rows = [[a["fixture_id"], a["line_id"], a["start"], a["channels"]] for a in allocs]
    # 注意避开相互重叠：线路 1 上剩余灯具区间为 Beta 11-30、Gamma 31-32、Delta 40-44
    rows[0][1] = 2          # Alpha：线路不符（独占线路 2，不撞车）
    rows[3][2] += 6         # Delta：起始地址不符（移到 40-44，与 Gamma 留有空隙）
    rows[2][3] -= 1         # Gamma：通道数不符（缩为 2ch，31-32 不撞 Delta）
    rep = verify(client, plan_id, make_csv(rows)).json()
    assert rep["consistent"] is False
    by_id = {m["fixture_id"]: m for m in rep["mismatches"]}
    assert set(by_id) == {rows[0][0], rows[3][0], rows[2][0]}
    d0 = by_id[rows[0][0]]["diffs"][0]
    assert d0["field"] == "line" and d0["expected"] == allocs[0]["line_id"] and d0["actual"] == 2
    d1 = by_id[rows[3][0]]["diffs"][0]
    assert d1["field"] == "start" and d1["expected"] == allocs[3]["start"] and d1["actual"] == allocs[3]["start"] + 6
    d2 = by_id[rows[2][0]]["diffs"][0]
    assert d2["field"] == "channels" and d2["expected"] == allocs[2]["channels"]
    # 纯地址差异不产生重叠清单——两类问题分别呈现
    assert rep["overlaps"] == []


def test_06_overlap_presented_separately(client, published):
    """现场两台灯具区间相交：重叠与地址不符各自独立成清单。"""
    plan_id, payload = published
    allocs = payload["allocations"]
    rows = [[a["fixture_id"], a["line_id"], a["start"], a["channels"]] for a in allocs]
    # 让第二台后移到与第一台相交：第一台 1-10，第二台改为 5-24
    rows[1][2] = rows[0][2] + 4
    rep = verify(client, plan_id, make_csv(rows)).json()
    assert rep["consistent"] is False
    assert len(rep["overlaps"]) == 1
    ov = rep["overlaps"][0]
    assert ov["line"] == rows[0][1]
    assert {ov["a"]["fixture_id"], ov["b"]["fixture_id"]} == {rows[0][0], rows[1][0]}
    assert (ov["overlap_start"], ov["overlap_end"]) == (rows[1][2], rows[0][2] + rows[0][3] - 1)
    # 第二台同时存在起始地址不符，两类清单分别呈现
    assert [m["fixture_id"] for m in rep["mismatches"]] == [rows[1][0]]


def test_07_duplicate_id_quarantined_never_consistent(client, published):
    """重复 ID：即便重复行内容与基准一致，也全部隔离且整表绝不报一致。"""
    plan_id, payload = published
    allocs = payload["allocations"]
    rows = [(a["fixture_id"], a["line_id"], a["start"], a["channels"]) for a in allocs]
    dup = rows[0]
    rows.append(dup)  # 完全相同的重复行
    rep = verify(client, plan_id, make_csv(rows)).json()
    assert rep["consistent"] is False  # 绝不能报告一致
    dup_rows = [q for q in rep["quarantined"] if q["fixture_id"] == dup[0]]
    assert len(dup_rows) == 2
    assert all("重复 ID" in r for q in dup_rows for r in q["reasons"])
    # 被隔离的 ID 不再计入缺失，其余灯具仍正常核对
    assert rep["missing"] == []
    assert rep["counts"]["matched"] == len(allocs) - 1


def test_08_bad_row_quarantined(client, published):
    plan_id, payload = published
    allocs = payload["allocations"]
    rows = [(a["fixture_id"], a["line_id"], a["start"], a["channels"]) for a in allocs]
    bad_id = rows[1][0]
    rows[1] = (bad_id, 1, "abc", 10)  # 起始地址非整数 -> 坏行
    rep = verify(client, plan_id, make_csv(rows)).json()
    assert rep["consistent"] is False  # 有隔离行绝不能报一致
    q = [q for q in rep["quarantined"] if q["fixture_id"] == bad_id]
    assert len(q) == 1 and any("坏行" in r for r in q[0]["reasons"])
    # 该灯具已有隔离记录，不重复计入缺失；其余灯具全部一致
    assert rep["missing"] == []
    assert rep["counts"]["matched"] == len(allocs) - 1


def test_09_illegal_line_quarantined(client, published):
    plan_id, payload = published
    allocs = payload["allocations"]
    rows = [(a["fixture_id"], a["line_id"], a["start"], a["channels"]) for a in allocs]
    rows[0] = (rows[0][0], 9, rows[0][2], rows[0][3])  # 线路 9 非法
    rep = verify(client, plan_id, make_csv(rows)).json()
    assert rep["consistent"] is False
    q = rep["quarantined"]
    assert len(q) == 1 and q[0]["fixture_id"] == rows[0][0]
    assert any("非法线路" in r for r in q[0]["reasons"])


def test_10_beyond_512_quarantined(client, published):
    plan_id, payload = published
    allocs = payload["allocations"]
    rows = [(a["fixture_id"], a["line_id"], a["start"], a["channels"]) for a in allocs]
    rows[0] = (rows[0][0], rows[0][1], 510, 10)  # 区间 510-519 越过 512
    rep = verify(client, plan_id, make_csv(rows)).json()
    assert rep["consistent"] is False
    q = rep["quarantined"]
    assert len(q) == 1 and any("越过 512" in r for r in q[0]["reasons"])


def test_11_header_only_csv_reports_all_missing(client, published):
    plan_id, payload = published
    rep = verify(client, plan_id, "灯具ID,线路,起始地址,通道数\n").json()
    assert rep["consistent"] is False
    assert rep["counts"]["missing"] == len(payload["allocations"])
    assert rep["quarantined"] == []


def test_12_structure_errors_rejected(client, published):
    plan_id, _ = published
    # 缺少必需表头 -> 整表拒收
    r = verify(client, plan_id, "foo,bar\n1,2\n")
    assert r.status_code == 422
    assert "表头" in r.json()["detail"]
    # 空文件 -> 整表拒收
    r = verify(client, plan_id, "")
    assert r.status_code == 422
    # 非 UTF-8 编码 -> 整表拒收
    r = client.post(
        f"/api/plans/{plan_id}/verify",
        files={"file": ("s.csv", "灯具ID,线路\n1,2\n".encode("gbk"), "text/csv")},
    )
    assert r.status_code == 422


def test_13_unpublished_or_missing_plan_rejected(client, published):
    # 草稿（未发布）明确拒绝
    p = client.post("/api/plans/preview").json()
    assert p["optimality_status"] == "optimal"
    r = verify(client, p["plan_id"], "灯具ID,线路,起始地址,通道数\n")
    assert r.status_code == 409
    assert "尚未发布" in r.json()["detail"]
    # 方案不存在明确拒绝
    r = verify(client, 999999, "灯具ID,线路,起始地址,通道数\n")
    assert r.status_code == 404


def test_14_verify_is_readonly(client, published):
    """核验不修改编排与发布数据：配置版本与方案内容前后一致。"""
    plan_id, payload = published
    version_before = client.get("/api/plans/version").json()["config_version"]
    payload_before = client.get(f"/api/plans/{plan_id}").json()
    rep = verify(client, plan_id, csv_from_payload(payload)).json()
    assert rep["consistent"] is True
    assert client.get("/api/plans/version").json()["config_version"] == version_before
    assert client.get(f"/api/plans/{plan_id}").json() == payload_before
    latest = client.get("/api/plans/published/latest").json()
    assert latest["plan_id"] == plan_id


def test_15_legacy_plan_baseline_without_snapshot(client, published):
    """旧记录（无快照）：以留存明细落点为基准；名称来源透传但不参与裁决。"""
    from datetime import datetime, timezone

    from app.database import SessionLocal
    from app.models import Plan, PlanAllocation
    from app.services import get_config_version

    _, payload = published
    alpha = next(a for a in payload["allocations"] if a["name"] == "Alpha")
    with SessionLocal() as db:
        plan = Plan(
            config_version=get_config_version(db),
            status="published",
            optimality_status="optimal",
            migration_count=0,
            used_lines=2,
            published_at=datetime(2020, 5, 5, tzinfo=timezone.utc),
        )
        db.add(plan)
        db.flush()
        db.add(PlanAllocation(plan_id=plan.id, fixture_id=alpha["fixture_id"],
                              line_id=2, start=100, end=109, migrated=False))
        db.add(PlanAllocation(plan_id=plan.id, fixture_id=999999,
                              line_id=3, start=1, end=5, migrated=True))
        db.commit()
        legacy_id = plan.id

    # 与留存落点完全一致 -> 一致（基准来自留存明细，名称/保留区不参与裁决）
    rep = verify(client, legacy_id, make_csv([
        (alpha["fixture_id"], 2, 100, 10),
        (999999, 3, 1, 5),
    ])).json()
    assert rep["consistent"] is True
    assert rep["from_snapshot"] is False
    assert rep["notice"]  # 旧记录字段来源说明透传

    # 地址不符 + 缺失：名称带来源标注（当前名称/名称不可用）
    rep = verify(client, legacy_id, make_csv([(alpha["fixture_id"], 2, 101, 10)])).json()
    assert rep["consistent"] is False
    m = rep["mismatches"][0]
    assert m["fixture_id"] == alpha["fixture_id"]
    assert m["name"] == "Alpha" and m["name_source"] == "current"
    assert m["diffs"] == [{"field": "start", "expected": 100, "actual": 101}]
    miss = rep["missing"][0]
    assert miss["fixture_id"] == 999999
    assert miss["name"] == "#999999" and miss["name_source"] == "unavailable"
    assert (miss["line_id"], miss["start"], miss["end"], miss["channels"]) == (3, 1, 5, 5)


def test_16_baseline_immutable_after_drift(client, published):
    """灯具改名、删除、再次发布后，核验仍以所选版本的落点为准。"""
    plan_id, payload = published
    original_csv = csv_from_payload(payload)
    alpha = next(a for a in payload["allocations"] if a["name"] == "Alpha")
    beta = next(a for a in payload["allocations"] if a["name"] == "Beta")

    # 漂移源：改名、删除、再次发布新版本
    assert client.put(f"/api/fixtures/{alpha['fixture_id']}", json={"name": "AlphaX"}).status_code == 200
    assert client.delete(f"/api/fixtures/{beta['fixture_id']}").status_code == 204
    p2 = client.post("/api/plans/preview").json()
    assert p2["optimality_status"] == "optimal"
    assert client.post(f"/api/plans/{p2['plan_id']}/publish").status_code == 200

    # 旧版本核验仍以发布快照为准：原始 CSV 依旧整表一致
    rep = verify(client, plan_id, original_csv).json()
    assert rep["consistent"] is True
    assert rep["plan_id"] == plan_id

    # 新版本不含已删除的 Beta：原始 CSV 中 Beta 行成为“额外”
    rep2 = verify(client, p2["plan_id"], original_csv).json()
    assert rep2["consistent"] is False
    assert [x["fixture_id"] for x in rep2["extra"]] == [beta["fixture_id"]]

    # 方案列表包含两个已发布版本，新的在前
    plans = client.get("/api/plans/published").json()
    ids = [p["plan_id"] for p in plans]
    assert ids[0] == p2["plan_id"] and plan_id in ids


def test_17_frontend_ships_verify_panel(client):
    html = client.get("/").text
    assert 'id="verify-plan-select"' in html
    assert 'id="verify-file"' in html
    assert 'id="btn-verify"' in html
    assert 'id="verify-result"' in html
    js = client.get("/static/app.js").text
    assert "renderVerifyReport" in js
    assert "loadVerifyPlans" in js
    assert "/api/plans/published" in js
