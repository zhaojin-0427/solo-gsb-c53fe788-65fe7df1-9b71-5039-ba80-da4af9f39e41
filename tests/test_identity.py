"""灯具 ID 持久分配与换场身份对齐测试。

覆盖：
- 删除末位灯具后新建拿到全新 ID（SQLite 行 ID 复用被持久计数器消除）；
- 已发布快照引用过的 ID、升级后删除的 ID 均不再发出，重启后仍有效；
- 同一灯改名/换模式身份不变，换场仍按原 ID 比较（迁址/通道调整）；
- 删灯重建被正确识别为拆除 + 接入，而非迁址；
- 新库与“保留历史数据的旧库”两种升级路径；
- 升级前旧快照缺身份记录：涉及共同 ID 时拒绝自动施工单并说明原因，
  纯拆除/接入（无共同 ID）的历史换场仍可用；
- 既有数字 ID 与快照内容不改写。
"""
from __future__ import annotations

import json
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
from app.models import Fixture, FixtureIdLedger, FixtureIdSeq  # noqa: E402
from app.services import init_db  # noqa: E402


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
        yield c


@pytest.fixture()
def modes(client):
    client.post("/api/catalog/import", files={"file": ("c.csv", CATALOG_CSV, "text/csv")})
    return {(t["name"], m["name"]): m for t in client.get("/api/catalog").json() for m in t["modes"]}


def _publish(client) -> int:
    p = client.post("/api/plans/preview").json()
    assert p["optimality_status"] == "optimal", p
    r = client.post(f"/api/plans/{p['plan_id']}/publish")
    assert r.status_code == 200, r.text
    return p["plan_id"]


# ---------------------------------------------------------------------------
# ID 持久分配：不复用、重启有效、台账留痕
# ---------------------------------------------------------------------------
def test_01_delete_last_fixture_then_create_never_reuses_id(client, modes):
    """删掉当前末位灯具再新建：新灯 ID 必须不同于被删 ID（问题根因）。"""
    r = client.post("/api/fixtures", json={"name": "A1", "mode_id": modes[("Solar", "Basic")]["id"]})
    id_a = r.json()["id"]
    r = client.post("/api/fixtures", json={"name": "A2", "mode_id": modes[("Solar", "Basic")]["id"]})
    id_last = r.json()["id"]
    assert id_last > id_a
    assert client.delete(f"/api/fixtures/{id_last}").status_code == 204

    # 原生 SQLite 行 ID 此时会复用 id_last；应用层计数器必须绕开
    r = client.post("/api/fixtures", json={"name": "A3", "mode_id": modes[("Par", "3CH")]["id"]})
    id_new = r.json()["id"]
    assert id_new != id_last
    assert id_new > id_last

    with SessionLocal() as db:
        # 被删 ID 以 retired 留痕，新 ID 为 active，计数器指向下一个未用 ID
        retired = db.get(FixtureIdLedger, id_last)
        assert retired is not None and retired.status == "retired"
        active = db.get(FixtureIdLedger, id_new)
        assert active is not None and active.status == "active"
        assert active.identity_key is not None
        assert db.get(FixtureIdSeq, 1).next_id == id_new + 1


def test_02_published_fixture_ids_are_never_reissued(client, modes):
    """已发布快照引用过的灯具，删除后其 ID 永不复用，且 ever_published 留痕。"""
    r = client.post("/api/fixtures", json={"name": "B1", "mode_id": modes[("Solar", "Basic")]["id"],
                                           "previous_line": 1, "previous_start": 1})
    id_pub = r.json()["id"]
    _publish(client)
    with SessionLocal() as db:
        assert db.get(FixtureIdLedger, id_pub).ever_published is True

    assert client.delete(f"/api/fixtures/{id_pub}").status_code == 204
    r = client.post("/api/fixtures", json={"name": "B2", "mode_id": modes[("Par", "3CH")]["id"]})
    assert r.json()["id"] != id_pub
    with SessionLocal() as db:
        row = db.get(FixtureIdLedger, id_pub)
        assert row.status == "retired" and row.ever_published is True
        # 身份随台账保留，历史快照中的该灯仍可核对
        assert row.identity_key is not None


def test_03_id_allocation_survives_restart(client):
    """重启（重新初始化）后计数器与台账仍有效，ID 继续单调、不复用。"""
    with SessionLocal() as db:
        before = db.get(FixtureIdSeq, 1).next_id
        ledger_count = db.query(FixtureIdLedger).count()
    init_db()  # 幂等：模拟进程重启时的初始化
    with SessionLocal() as db:
        assert db.get(FixtureIdSeq, 1).next_id == before
        assert db.query(FixtureIdLedger).count() == ledger_count
    r = client.post("/api/fixtures", json={"name": "B3", "mode_id": modes2_id(client)})
    assert r.json()["id"] == before


def modes2_id(client):
    return next(m["id"] for t in client.get("/api/catalog").json() for m in t["modes"]
                if (t["name"], m["name"]) == ("Solar", "Full"))


def test_04_rename_and_mode_change_keep_identity_and_id(client):
    """有可靠身份记录的同一灯：改名、换模式后 ID 与 identity_key 都不变。"""
    fid = next(f["id"] for f in client.get("/api/fixtures").json() if f["name"] == "A1")
    before = client.get("/api/fixtures").json()
    key_before = next(f for f in before if f["id"] == fid)
    with SessionLocal() as db:
        key = db.get(Fixture, fid).identity_key
    plan_before = _publish(client)

    new_mode = next(m["id"] for t in client.get("/api/catalog").json() for m in t["modes"]
                    if (t["name"], m["name"]) == ("Solar", "Full"))
    assert client.put(f"/api/fixtures/{fid}", json={"name": "A1-Renamed"}).status_code == 200
    assert client.put(f"/api/fixtures/{fid}", json={"mode_id": new_mode}).status_code == 200
    with SessionLocal() as db:
        fx = db.get(Fixture, fid)
        assert fx.identity_key == key
    plan_after = _publish(client)

    rep = client.get(f"/api/plans/changeover?from={plan_before}&to={plan_after}").json()
    assert rep.get("feasible") is True, rep
    # 同一灯：只能是迁址或通道数调整，绝不出现拆除/接入
    for s in rep["steps"]:
        assert s["action"] in ("relocate", "channel_adjust", "temp_relocate")
        assert s["fixture_id"] == fid


# ---------------------------------------------------------------------------
# 删灯重建：换场必须输出拆除 + 接入
# ---------------------------------------------------------------------------
def test_05_delete_and_recreate_changeover_is_remove_plus_install(client, modes):
    r = client.post("/api/fixtures", json={"name": "C1", "mode_id": modes[("Solar", "Basic")]["id"],
                                           "previous_line": 2, "previous_start": 1})
    old_id = r.json()["id"]
    # 清理可能影响落点的其它在用灯，仅保留 C1 再发布，便于精确核对区间
    keep = {"C1"}
    for f in client.get("/api/fixtures").json():
        if f["name"] not in keep:
            client.delete(f"/api/fixtures/{f['id']}")
    plan_a = _publish(client)

    assert client.delete(f"/api/fixtures/{old_id}").status_code == 204
    r = client.post("/api/fixtures", json={"name": "C2", "mode_id": modes[("Solar", "Full")]["id"]})
    new_id = r.json()["id"]
    assert new_id != old_id
    plan_b = _publish(client)

    rep = client.get(f"/api/plans/changeover?from={plan_a}&to={plan_b}").json()
    assert rep["feasible"] is True, rep
    by_fid = {s["fixture_id"]: s["action"] for s in rep["steps"]}
    # 根因修复：必须是“拆除旧灯 + 接入新灯”，而不是把新灯误当旧灯迁址
    assert by_fid[old_id] == "remove"
    assert by_fid[new_id] == "install"
    assert not any(s["action"].startswith("relocate") for s in rep["steps"])
    # 报告标注身份依据
    assert rep.get("identity_basis") == "identity_key"


# ---------------------------------------------------------------------------
# 升级路径：旧库（保留历史数据）迁移
# ---------------------------------------------------------------------------
LEGACY_DB = os.path.join(tempfile.gettempdir(), "dmx_legacy_identity_test.db")


def _build_legacy_db():
    """构造升级前形态的旧库：无身份列/台账/计数器，含已发布方案与明细。"""
    for suffix in ("", "-wal", "-shm"):
        p = LEGACY_DB + suffix
        if os.path.exists(p):
            os.remove(p)
    import sqlite3

    conn = sqlite3.connect(LEGACY_DB)
    conn.executescript(
        """
        CREATE TABLE fixture_types (id INTEGER PRIMARY KEY, manufacturer VARCHAR(128), name VARCHAR(128));
        CREATE TABLE fixture_modes (id INTEGER PRIMARY KEY, type_id INTEGER, name VARCHAR(128), channels INTEGER);
        CREATE TABLE fixtures (id INTEGER PRIMARY KEY, name VARCHAR(128) UNIQUE, mode_id INTEGER,
                               previous_line INTEGER, previous_start INTEGER, locked BOOLEAN);
        CREATE TABLE lines (id INTEGER PRIMARY KEY, name VARCHAR(128), is_active BOOLEAN);
        CREATE TABLE reservations (id INTEGER PRIMARY KEY, line_id INTEGER, start INTEGER,
                                   "end" INTEGER, reason VARCHAR(256));
        CREATE TABLE config_version (id INTEGER PRIMARY KEY, version INTEGER, updated_at DATETIME);
        CREATE TABLE plans (id INTEGER PRIMARY KEY, config_version INTEGER, status VARCHAR(16),
                            optimality_status VARCHAR(16), migration_count INTEGER, used_lines INTEGER,
                            created_at DATETIME, published_at DATETIME);
        CREATE TABLE plan_allocations (id INTEGER PRIMARY KEY, plan_id INTEGER, fixture_id INTEGER,
                                       line_id INTEGER, start INTEGER, "end" INTEGER, migrated BOOLEAN);
        CREATE TABLE published_snapshots (id INTEGER PRIMARY KEY, plan_id INTEGER, config_version INTEGER,
                                          published_at DATETIME, payload TEXT);
        """
    )
    conn.execute("INSERT INTO fixture_types VALUES (1, 'ACME', 'Solar')")
    conn.execute("INSERT INTO fixture_modes VALUES (1, 1, 'Basic', 10)")
    # 灯具 1、2（现存）；灯具 3 曾发布但已删除（只留在明细里）
    conn.execute("INSERT INTO fixtures VALUES (1, 'Old1', 1, 1, 1, 0)")
    conn.execute("INSERT INTO fixtures VALUES (2, 'Old2', 1, 1, 11, 0)")
    for i in range(1, 5):
        conn.execute(f"INSERT INTO lines VALUES ({i}, '线路 {i}', 1)")
    conn.execute("INSERT INTO config_version VALUES (1, 5, '2026-01-01 00:00:00')")
    conn.execute(
        "INSERT INTO plans VALUES (10, 4, 'published', 'optimal', 0, 1, "
        "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"
    )
    # 旧明细引用灯具 1、2、3（3 已删除）
    conn.execute("INSERT INTO plan_allocations VALUES (1, 10, 1, 1, 1, 10, 0)")
    conn.execute("INSERT INTO plan_allocations VALUES (2, 10, 2, 1, 11, 20, 0)")
    conn.execute("INSERT INTO plan_allocations VALUES (3, 10, 3, 1, 21, 30, 0)")
    # 旧快照内容（无 identity_key、无 identity_mode）——升级后保持不改写
    old_payload = {
        "feasible": True, "id": 10, "plan_id": 10, "config_version": 4,
        "status": "published", "optimality_status": "optimal",
        "migration_count": 0, "used_lines": 1,
        "from_snapshot": True,
        "allocations": [
            {"fixture_id": 1, "name": "Old1", "line_id": 1, "start": 1, "end": 10,
             "channels": 10, "migrated": False, "locked": False},
            {"fixture_id": 2, "name": "Old2", "line_id": 1, "start": 11, "end": 20,
             "channels": 10, "migrated": False, "locked": False},
            {"fixture_id": 3, "name": "Old3", "line_id": 1, "start": 21, "end": 30,
             "channels": 10, "migrated": False, "locked": False},
        ],
        "occupancy": [], "migrations": [], "conflicts": [],
    }
    conn.execute(
        "INSERT INTO published_snapshots VALUES (1, 10, 4, '2026-01-01 00:00:00', ?)",
        (json.dumps(old_payload, ensure_ascii=False),),
    )
    conn.commit()
    conn.close()


def test_06_legacy_db_upgrade_preserves_ids_and_blocks_reuse():
    """旧库（保留历史数据）升级：现存 ID 不改写、历史引用 ID 补录留痕、
    计数器推过最大历史 ID，升级后新建不复用任何旧 ID，旧快照内容不改写。

    在隔离子进程中完成，避免模块级 engine 与其它用例共享而互相污染。
    """
    _build_legacy_db()
    script = r"""
import json, os, sys
os.environ["DMX_DB_PATH"] = sys.argv[1]
from fastapi.testclient import TestClient
from app.main import app
from app.database import SessionLocal
from app.models import Fixture, FixtureIdLedger, FixtureIdSeq, PublishedSnapshot
from app.services import init_db

init_db()
with SessionLocal() as db:
    f1, f2 = db.get(Fixture, 1), db.get(Fixture, 2)
    assert f1.identity_key and f2.identity_key and f1.identity_key != f2.identity_key
    led3 = db.get(FixtureIdLedger, 3)
    assert led3.status == "retired" and led3.ever_published is True and led3.identity_key is None
    assert db.get(FixtureIdSeq, 1).next_id == 4

with TestClient(app) as c:
    r = c.post("/api/fixtures", json={"name": "NewAfterUpgrade", "mode_id": 1})
    assert r.status_code == 201, r.text
    assert r.json()["id"] == 4, r.text
    assert c.delete("/api/fixtures/4").status_code == 204
    r = c.post("/api/fixtures", json={"name": "NewAfterUpgrade2", "mode_id": 1})
    assert r.json()["id"] == 5

    # 重启后仍然单调不复用
    init_db()
    r = c.post("/api/fixtures", json={"name": "NewAfterRestart", "mode_id": 1})
    assert r.json()["id"] == 6

with SessionLocal() as db:
    stored = json.loads(db.get(PublishedSnapshot, 1).payload)
    assert "identity_key" not in stored["allocations"][0]
print("LEGACY_UPGRADE_OK")
"""
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-c", script, LEGACY_DB],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "LEGACY_UPGRADE_OK" in proc.stdout


def test_07_legacy_snapshot_with_common_id_is_rejected():
    """升级前的旧快照（无身份记录）与新版换场：共同 ID 无法核实身份 -> 拒绝。"""
    from tests.test_changeover import make_payload

    # 旧快照：升级前写入，灯具 1 无 identity_key
    a = make_payload(1, 1, [(1, "OldName", 1, 1, 10)], identities={1: None})
    # 新快照：升级后写入，ID 1 有身份（可能是同一灯，也可能是复用，无法核实）
    b = make_payload(2, 2, [(1, "NewName", 2, 1, 10)])
    with pytest.raises(ChangeoverError) as exc:
        build_changeover(a, b)
    assert exc.value.code == "identity_unverifiable"
    fixtures = exc.value.reasons[0]["fixtures"]
    assert fixtures[0]["fixture_id"] == 1
    assert fixtures[0]["name_a"] == "OldName" and fixtures[0]["name_b"] == "NewName"
    # 原因说明必须点明整数 ID 复用风险且不按名称猜测
    assert "名称" in exc.value.reasons[0]["fixtures"][0]["reason"]


def test_08_legacy_snapshot_pure_remove_install_still_works():
    """无共同 ID 的历史换场（纯拆除/接入）不涉及身份歧义，保持可用。"""
    from tests.test_changeover import make_payload

    # 旧快照全无身份记录，但与新版灯具 ID 集合不相交
    a = make_payload(1, 1, [(1, "Old1", 1, 1, 10)], identities={1: None})
    b = make_payload(2, 2, [(2, "New2", 1, 1, 10)], identities={2: "key-new"})
    rep = build_changeover(a, b)
    assert rep["feasible"] is True
    actions = {s["fixture_id"]: s["action"] for s in rep["steps"]}
    assert actions == {1: "remove", 2: "install"}


def test_09_identity_conflict_same_id_different_lamps_rejected():
    """两版同 ID 但身份不同（ID 已复用给两台灯）：拒绝，不输出误判步骤。"""
    from tests.test_changeover import make_payload

    a = make_payload(1, 1, [(1, "LampA", 1, 1, 10)])
    b = make_payload(2, 2, [(1, "LampB", 2, 1, 10)], identities={1: "totally-different-key"})
    with pytest.raises(ChangeoverError) as exc:
        build_changeover(a, b)
    assert exc.value.code == "identity_conflict"
    assert exc.value.reasons[0]["fixtures"][0]["fixture_id"] == 1


def test_10_snapshot_identity_remains_stable_after_rename(client):
    """发布快照中的 identity_key 与当前灯具身份一致（改名后仍可按身份对齐）。"""
    fid = next(f["id"] for f in client.get("/api/fixtures").json() if f["name"] == "C2")
    plan = next(
        p["plan_id"] for p in reversed(client.get("/api/plans/published").json())
    )
    # 找包含 C2 的最近发布
    target_plan = None
    for p in client.get("/api/plans/published").json():
        payload = client.get(f"/api/plans/{p['plan_id']}").json()
        if any(a["fixture_id"] == fid for a in payload["allocations"]):
            target_plan = payload
            break
    assert target_plan is not None
    alloc = next(a for a in target_plan["allocations"] if a["fixture_id"] == fid)
    with SessionLocal() as db:
        assert alloc["identity_key"] == db.get(Fixture, fid).identity_key


# ---------------------------------------------------------------------------
# 现有拒绝行为不受影响（回归抽查）
# ---------------------------------------------------------------------------
def test_11_existing_rejections_unchanged(client):
    plans = [p["plan_id"] for p in client.get("/api/plans/published").json()]
    # 同一份方案
    r = client.get(f"/api/plans/changeover?from={plans[0]}&to={plans[0]}")
    assert r.status_code == 422
    # 未发布草稿
    p = client.post("/api/plans/preview").json()
    if p["optimality_status"] == "optimal":
        r = client.get(f"/api/plans/changeover?from={p['plan_id']}&to={plans[0]}")
        assert r.status_code == 409
    # 已发布查询仍可用
    assert client.get("/api/plans/published/latest").status_code == 200
