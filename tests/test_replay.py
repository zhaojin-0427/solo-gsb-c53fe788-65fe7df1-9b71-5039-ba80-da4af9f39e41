"""DMX 信号回放回端测试：帧重建、延续语义、归属判定、冲突策略、快照不变性与只读保证。"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone

import pytest

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DMX_DB_PATH"] = _tmp.name

from fastapi.testclient import TestClient  # noqa: E402

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


CATALOG_CSV = (
    "manufacturer,name,mode,channels\n"
    "ACME,Solar,Basic,10\n"
    "ACME,Solar,Full,20\n"
    "BrightCo,Par,3CH,3\n"
    "LitePro,Beam,Std,5\n"
)


@pytest.fixture(scope="session")
def published(client):
    """灯具落点：Alpha 线1 1-10、Beta 线1 11-30、Gamma 线1 31-33、Delta 线1 34-38；
    另在 线路2 1-10 设保留区。预览发布后返回 (plan_id, payload)。
    """
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
    assert client.post(
        "/api/reservations", json={"line_id": 2, "start": 1, "end": 10, "reason": "desk"}
    ).status_code == 201
    p = client.post("/api/plans/preview").json()
    assert p["optimality_status"] == "optimal", p
    r = client.post(f"/api/plans/{p['plan_id']}/publish")
    assert r.status_code == 200, r.text
    payload = client.get(f"/api/plans/{p['plan_id']}").json()
    assert payload["from_snapshot"] is True
    return p["plan_id"], payload


HEADER = "时间(毫秒),线路,通道,值\n"


def signal_csv(rows) -> str:
    lines = [HEADER.strip()]
    lines += [",".join(str(v) for v in row) for row in rows]
    return "\n".join(lines) + "\n"


def replay(client, plan_id, csv_text, policy="reject"):
    return client.post(
        f"/api/plans/{plan_id}/replay",
        files={"file": ("signal.csv", csv_text, "text/csv")},
        data={"conflict_policy": policy},
    )


def _find_update(rep, line, channel):
    out = []
    for fr in rep["frames"]:
        for u in fr["updates"]:
            if u["line_id"] == line and u["channel"] == channel:
                out.append(u)
    return out


def test_01_basic_frames_merge_and_carryover(client, published):
    """同毫秒合帧、槽位初值 0、未更新延续、相对通道归属。"""
    plan_id, payload = published
    alpha = next(a for a in payload["allocations"] if a["name"] == "Alpha")
    assert (alpha["line_id"], alpha["start"], alpha["channels"]) == (1, 1, 10)

    csv = signal_csv([
        (0, 1, 1, 100),    # Alpha 相对 ch1
        (0, 1, 10, 200),   # Alpha 相对 ch10
        (100, 1, 1, 100),  # 同值重复写：不构成冲突、changed=False
        (250, 1, 1, 0),    # 熄灭
    ])
    r = replay(client, plan_id, csv)
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["from_snapshot"] is True
    c = rep["counts"]
    assert c["frames"] == 3
    assert c["data_rows"] == 4
    assert (c["time_start"], c["time_end"], c["duration_ms"]) == (0, 250, 250)

    frames = rep["frames"]
    assert [f["time_ms"] for f in frames] == [0, 100, 250]
    assert len(frames[0]["updates"]) == 2  # 同毫秒两行合为一帧

    first_ch1 = frames[0]["updates"][0]
    assert first_ch1["previous_value"] == 0 and first_ch1["changed"] is True
    assert first_ch1["owner"] == "fixture"
    assert first_ch1["fixture_id"] == alpha["fixture_id"]
    assert first_ch1["relative_channel"] == 1
    assert frames[0]["updates"][1]["relative_channel"] == 10

    same = frames[1]["updates"][0]
    assert same["previous_value"] == 100 and same["changed"] is False

    # 最终读数：ch1 已熄灭（不出现在稀疏读数中），ch10 仍是 200（跨帧延续）
    final_alpha = next(f for f in rep["final_fixtures"] if f["fixture_id"] == alpha["fixture_id"])
    assert final_alpha["readings"] == [{"relative_channel": 10, "value": 200}]


def test_02_reservation_and_unassigned_owner(client, published):
    """有快照：灯具外写入按快照保留区分为 reservation / unassigned。"""
    plan_id, _ = published
    csv = signal_csv([
        (0, 2, 5, 80),    # 线路2 保留区 1-10 内
        (0, 2, 11, 40),   # 未分配
        (10, 2, 11, 0),   # 未分配通道熄灭
    ])
    rep = replay(client, plan_id, csv).json()
    owners = {(u["line_id"], u["channel"]): u["owner"] for u in rep["frames"][0]["updates"]}
    assert owners[(2, 5)] == "reservation"
    assert owners[(2, 11)] == "unassigned"
    c = rep["counts"]
    assert c["reservation_updates"] == 1
    assert c["unassigned_updates"] == 2
    # 保留区 80 跨帧延续；第 2 帧未分配 11 已清零 -> 灯具外非零写入只统计写非零的 2 行
    assert c["external_nonzero_writes"] == 2


def test_03_conflict_reject_policy(client, published):
    plan_id, _ = published
    csv = signal_csv([
        (50, 1, 2, 10),
        (50, 1, 2, 20),   # 同毫秒同槽不同值
    ])
    r = replay(client, plan_id, csv)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["code"] == "slot_conflicts"
    assert len(detail["errors"]) == 1
    cf = detail["errors"][0]
    assert cf["time_ms"] == 50 and cf["line_id"] == 1 and cf["channel"] == 2
    assert [x["value"] for x in cf["rows"]] == [10, 20]
    assert cf["chosen_row"] == 3 and cf["chosen_value"] == 20  # 按文件顺序末行


def test_04_conflict_last_policy_applied_and_marked(client, published):
    plan_id, _ = published
    csv = signal_csv([
        (50, 1, 2, 10),
        (50, 1, 2, 20),
        (50, 1, 2, 5),
        (60, 1, 3, 7),
        (60, 1, 3, 7),   # 同值不是冲突
    ])
    r = replay(client, plan_id, csv, policy="last")
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["conflict_policy"] == "last"
    assert len(rep["conflicts"]) == 1
    cf = rep["conflicts"][0]
    assert cf["chosen_row"] == 4 and cf["chosen_value"] == 5
    # 帧内按文件顺序写入：该槽最终值为末值 5
    frame50 = next(f for f in rep["frames"] if f["time_ms"] == 50)
    vals = [u for u in frame50["updates"] if u["channel"] == 2]
    assert [u["value"] for u in vals] == [10, 20, 5]
    alpha = next(f for f in rep["final_fixtures"] if f["name"] == "Alpha")
    assert {"relative_channel": 2, "value": 5} in alpha["readings"]
    assert rep["counts"]["slot_conflicts"] == 1


def test_05_invalid_rows_rejected_whole_table(client, published):
    plan_id, _ = published
    # 时间负、线路越界、通道越界、值越界、非整数，全部整表拒收（非隔离）
    bad_cases = [
        "-1,1,1,10\n",
        "1,9,1,10\n",
        "1,1,513,10\n",
        "1,1,1,256\n",
        "x,1,1,10\n",
        "1,1,1\n",  # 列数不足
    ]
    for body in bad_cases:
        r = replay(client, plan_id, HEADER + body)
        assert r.status_code == 422, body
        detail = r.json()["detail"]
        assert isinstance(detail, dict) and detail["errors"], body


def test_06_structure_errors_rejected(client, published):
    plan_id, _ = published
    # 缺表头
    r = replay(client, plan_id, "foo,bar\n1,2\n")
    assert r.status_code == 422 and "表头" in r.json()["detail"]
    # 空文件
    r = replay(client, plan_id, "")
    assert r.status_code == 422
    # 仅表头、无数据行
    r = replay(client, plan_id, HEADER)
    assert r.status_code == 422 and "数据行" in r.json()["detail"]
    # 非 UTF-8
    r = client.post(
        f"/api/plans/{plan_id}/replay",
        files={"file": ("s.csv", "时间(毫秒),线路,通道,值\n1,1,1,1\n".encode("gbk"), "text/csv")},
        data={"conflict_policy": "reject"},
    )
    assert r.status_code == 422 and "UTF-8" in r.json()["detail"]


def test_07_header_aliases_and_bom(client, published):
    """常见英文表头别名与 UTF-8 BOM 可识别。"""
    plan_id, _ = published
    csv = "﻿timestamp,universe,channel,value\n0,1,1,42\n"
    r = replay(client, plan_id, csv)
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["frames"][0]["updates"][0]["value"] == 42


def test_08_replay_uses_snapshot_not_current_positions(client, published):
    """发布后再发新版本/改保留区：回放旧版本仍按旧快照的灯具闭区间与保留区归属。"""
    plan_id, payload = published
    version_before = client.get("/api/plans/version").json()["config_version"]

    # 现状漂移：新增保留区（线路1 100-110）并再次发布（灯具 previous 地址回写后落点不变，
    # 但新快照携带新保留区）
    assert client.post(
        "/api/reservations", json={"line_id": 1, "start": 100, "end": 110, "reason": "now"}
    ).status_code == 201
    p2 = client.post("/api/plans/preview").json()
    assert p2["optimality_status"] == "optimal"
    assert client.post(f"/api/plans/{p2['plan_id']}/publish").status_code == 200

    # 旧版本回放：线路1 通道105 在旧快照里是未分配（新保留区不存在于旧快照）
    rep = replay(client, plan_id, signal_csv([(0, 1, 105, 33)])).json()
    u = rep["frames"][0]["updates"][0]
    assert u["owner"] == "unassigned"
    # 新版本回放：通道105 归属保留区
    rep2 = replay(client, p2["plan_id"], signal_csv([(0, 1, 105, 33)])).json()
    assert rep2["frames"][0]["updates"][0]["owner"] == "reservation"

    # 旧版本回放完全只读：配置版本未再变化
    assert client.get("/api/plans/version").json()["config_version"] > version_before
    got = client.get(f"/api/plans/{plan_id}").json()
    assert got["occupancy"] == payload["occupancy"]


def test_09_replay_is_readonly(client, published):
    plan_id, payload = published
    payload_before = client.get(f"/api/plans/{plan_id}").json()
    version_before = client.get("/api/plans/version").json()["config_version"]
    replay(client, plan_id, signal_csv([(0, 1, 1, 1), (1, 1, 1, 0)]))
    assert client.get("/api/plans/version").json()["config_version"] == version_before
    assert client.get(f"/api/plans/{plan_id}").json() == payload_before


def test_10_unpublished_or_missing_plan_rejected(client, published):
    p = client.post("/api/plans/preview").json()
    assert p["optimality_status"] == "optimal"
    r = replay(client, p["plan_id"], HEADER + "0,1,1,1\n")
    assert r.status_code == 409 and "尚未发布" in r.json()["detail"]
    r = replay(client, 999999, HEADER + "0,1,1,1\n")
    assert r.status_code == 404
    # 非法冲突策略明确拒绝
    r = client.post(
        f"/api/plans/{published[0]}/replay",
        files={"file": ("s.csv", HEADER + "0,1,1,1\n", "text/csv")},
        data={"conflict_policy": "bogus"},
    )
    assert r.status_code == 422


def test_11_legacy_plan_fixtures_known_external_unknown(client, published):
    """无快照旧记录：灯具按留存落点解读；灯具外非零值归属未知，不查当前保留区。"""
    plan_id, payload = published
    alpha = next(a for a in payload["allocations"] if a["name"] == "Alpha")
    with SessionLocal() as db:
        plan = Plan(
            config_version=get_config_version(db),
            status="published",
            optimality_status="optimal",
            migration_count=0,
            used_lines=1,
            published_at=datetime(2020, 6, 6, tzinfo=timezone.utc),
        )
        db.add(plan)
        db.flush()
        # 留存明细：Alpha 在线路 1 的 1-10（与当前保留区无关）
        db.add(PlanAllocation(
            plan_id=plan.id, fixture_id=alpha["fixture_id"],
            line_id=1, start=1, end=10, migrated=False,
        ))
        db.commit()
        legacy_id = plan.id

    csv = signal_csv([
        (0, 1, 1, 99),     # 灯具槽（留存落点）
        (0, 2, 5, 77),     # 灯具外；线路2 1-10 当前有保留区，但不得据此归类
        (5, 2, 5, 77),     # 延续值再写
    ])
    rep = replay(client, legacy_id, csv).json()
    assert rep["from_snapshot"] is False
    assert rep["baseline"]["reservations_known"] is False
    owners = [u["owner"] for u in rep["frames"][0]["updates"]]
    assert owners == ["fixture", "unknown"]
    assert rep["counts"]["unknown_updates"] == 2
    assert rep["counts"]["reservation_updates"] == 0
    assert rep["counts"]["unassigned_updates"] == 0
    final_alpha = next(f for f in rep["final_fixtures"] if f["fixture_id"] == alpha["fixture_id"])
    assert final_alpha["readings"] == [{"relative_channel": 1, "value": 99}]
    assert rep.get("notice")  # 字段来源说明透传


def test_12_all_four_lines_and_frame_ordering(client, published):
    """四条线路均可写入；帧严格按毫秒升序，初始槽位全 0。

    同帧先写 34 再写 0 属于“同毫秒同槽不同值”：默认 reject 会整表拒收，
    这里显式选择 last 策略验证帧内按文件顺序取末值。
    """
    plan_id, _ = published
    csv = signal_csv([
        (300, 4, 512, 255),
        (0, 1, 500, 12),
        (0, 3, 1, 34),
        (0, 3, 1, 0),    # 当帧内熄灭：末值 0
    ])
    rep = replay(client, plan_id, csv, policy="last").json()
    assert rep["conflicts"][0]["chosen_value"] == 0
    assert [f["time_ms"] for f in rep["frames"]] == [0, 300]
    last_frame = rep["frames"][-1]
    u = last_frame["updates"][0]
    assert (u["line_id"], u["channel"], u["value"]) == (4, 512, 255)
    assert u["previous_value"] == 0 and u["owner"] == "unassigned"
    # 同帧写 34 再写 0：结果以最后一次写入为准且两事件都保留
    f0_events = [u for u in rep["frames"][0]["updates"] if (u["line_id"], u["channel"]) == (3, 1)]
    assert [u["value"] for u in f0_events] == [34, 0]


def test_13_frontend_ships_replay_panel(client):
    html = client.get("/").text
    assert 'id="replay-plan-select"' in html
    assert 'id="replay-file"' in html
    assert 'id="replay-policy"' in html
    assert 'id="btn-replay"' in html
    assert 'id="replay-result"' in html
    js = client.get("/static/app.js").text
    assert "/replay" in js
    assert "conflict_policy" in js
    assert "renderReplayReport" in js
    assert "drawReplayFrame" in js
