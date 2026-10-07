"""灯具数字 ID 的持久分配与稳定身份管理。

背景：SQLite 的整数主键在“删除当前最大 ID 行”后会把该 ID 再次发给新行。
换场施工单按灯具 ID 对齐两份发布快照，ID 复用会让“删灯 + 新建灯”被误判
为同一台灯（输出迁址/通道调整，漏掉拆除与接入）。

本模块用两张持久表消除复用：
- ``fixture_id_seq``：单调计数器，新 ID 恒为“当前值之后的下一个整数”，
  只增不减，重启后仍然有效；
- ``fixture_id_ledger``：每个分配过或被发布明细引用过的 ID 留痕，永不删除，
  并记录该 ID 对应灯具的稳定身份 ``identity_key``（UUID，创建即固定，
  改名/换模式都不变；灯具删除后身份仍在台账中）。

因此：
- 已发布快照引用过的 ID、升级后删除的灯具 ID，都不会再发给新灯；
- 跨快照判断“同一台灯”只认 identity_key，不靠名称；
- 升级前的旧快照没有身份记录，凡涉及共同 ID 的旧快照换场由调用方拒绝。
"""
from __future__ import annotations

import uuid

from sqlalchemy import update
from sqlalchemy.orm import Session

from .models import Fixture, FixtureIdLedger, FixtureIdSeq

#: 计数器单行的固定主键
_SEQ_ROW_ID = 1


def new_identity_key() -> str:
    """生成一盏灯具的稳定身份（32 位十六进制 UUID）。"""
    return uuid.uuid4().hex


def allocate_fixture(db: Session) -> tuple[int, str]:
    """在当前事务中分配一个永不再用的灯具数字 ID 与稳定身份。

    先执行计数器自增（立即取得 SQLite 写锁，把并发创建串行化），并用
    RETURNING 取回自增前的值作为新 ID；与台账插入、调用方的 Fixture 插入
    在同一事务内提交。新 ID 恒大于所有历史 ID（含已删除 ID 与升级时从方案
    明细补录的 ID），因此 SQLite 对“已删除最大主键”的复用对灯具永久失效。
    调用方须先完成名称等校验再调用：事务回滚（如其它约束失败）时计数器一并
    回滚，不产生跳号。
    """
    next_id = db.scalar(
        update(FixtureIdSeq)
        .where(FixtureIdSeq.id == _SEQ_ROW_ID)
        .values(next_id=FixtureIdSeq.next_id + 1)
        .returning(FixtureIdSeq.next_id - 1)
    )
    key = new_identity_key()
    db.add(
        FixtureIdLedger(
            fixture_id=next_id,
            identity_key=key,
            status="active",
            ever_published=False,
        )
    )
    db.flush()
    return next_id, key


def retire_fixture(db: Session, fixture_id: int) -> None:
    """灯具删除：台账行保留为 retired，身份随 ID 留痕，数字 ID 永不复用。

    台账行在迁移时可能尚未存在（理论上不会，所有现存灯具都有台账行），
    缺失时补一条 retired 占位，仍然阻止该 ID 被再次发出。
    """
    ledger = db.get(FixtureIdLedger, fixture_id)
    if ledger is None:
        db.add(
            FixtureIdLedger(
                fixture_id=fixture_id,
                identity_key=None,
                status="retired",
                ever_published=False,
            )
        )
        return
    ledger.status = "retired"


def mark_published(db: Session, fixture_ids) -> None:
    """发布成功后把快照中引用的灯具 ID 标记为 ever_published（永不复用的留痕）。

    与发布在同一事务内执行；只更新尚未标记的行，已删除灯具的台账行同样标记。
    """
    for fixture_id in set(fixture_ids):
        ledger = db.get(FixtureIdLedger, fixture_id)
        if ledger is not None:
            ledger.ever_published = True


def get_identity_map(db: Session) -> dict[int, str | None]:
    """返回当前灯具 数字ID -> identity_key（未补齐身份的旧灯具为 None）。"""
    return {fid: key for fid, key in db.query(Fixture.id, Fixture.identity_key).all()}
