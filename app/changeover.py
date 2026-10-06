"""双版本换场施工单：从两份发布快照还原现场，模拟出从旧版（A）到新版（B）的有序施工步骤。

本模块为纯函数实现，不触碰数据库：两份方案的 payload（必须都由发布快照还原）
由调用方传入，规划过程不读取当前灯具、当前保留区或任一方案自身的迁移清单——
发布会把落点回写为灯具“既有地址”，迁移清单是相对该既有地址的单版本结论，
二者都不能冒充双版本换场的真实起点。

按灯具 ID 对齐 A、B 两版后，灯具分四类：
- 拆除（remove）：仅 A 中有；
- 接入（install）：仅 B 中有；
- 通道数调整（resize）：两版线路与起始地址相同、通道数不同（同址增减通道）；
- 迁址（relocate）：线路或起始地址不同（通道数可同时变化）。

规划分阶段进行，每一步都在模拟现场上真实执行并校验：
1. 拆除；2. 缩短式通道调整（先释放尾部空间）；3. 迁址（含周转）；
4. 加长式通道调整（待新尾部空间清空后再扩）；5. 接入。
任何中间态：灯具闭区间两两不重叠、不压任一版保留区、不落到当时停用的线路；
末态严格等于 B。

迁址阶段出现互相占位（环）且没有可直接移动的灯具时，把环中一台灯具临时
搬到“空闲且与 B 版所有灯具落点不相交”的连续区间周转，避免它再次挡住后续
落点；找不到这样的周转位即判定为阻塞，列明各启用线路上最大的空闲连续区间
与占用目标位的灯具，绝不输出貌似安全的步骤。

线路启停或保留区在两版之间发生变化时，中间态无法同时尊重两版约束，
直接拒绝生成步骤并逐项说明差异。
"""
from __future__ import annotations

from dataclasses import dataclass

#: 线路与单线通道上限（与编排规则一致）
VALID_LINE_IDS = (1, 2, 3, 4)
LINE_SIZE = 512


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Ival:
    """某条线路上的闭区间 [start, end]。"""

    line: int
    start: int
    end: int

    @property
    def channels(self) -> int:
        return self.end - self.start + 1


@dataclass
class Fx:
    """一台灯具在某一版快照中的落点与名称（名称仅用于展示）。"""

    fid: int
    name: str
    line: int
    start: int
    end: int

    @property
    def channels(self) -> int:
        return self.end - self.start + 1

    @property
    def ival(self) -> Ival:
        return Ival(self.line, self.start, self.end)


class ChangeoverError(ValueError):
    """两版前提不成立（缺快照、保留区/线路启停不一致等），不能生成施工单。"""

    def __init__(self, code: str, message: str, reasons: list[dict] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.reasons = reasons or []


class _Blocked(Exception):
    """规划内部信号：互相占位且找不到安全周转位（属于可行性结论，而非前提错误）。"""

    def __init__(self, message: str, blocking: list[dict]):
        super().__init__(message)
        self.message = message
        self.blocking = blocking


# ---------------------------------------------------------------------------
# 快照提取与一致性校验
# ---------------------------------------------------------------------------
def _extract_snapshot(payload: dict, label: str) -> tuple[dict[int, Fx], dict[int, list[Ival]], dict[int, bool]]:
    """从一份已发布方案 payload 提取（灯具落点、各线保留区、各线启停）。

    只接受发布快照还原的 payload（from_snapshot=true）：旧库无快照记录的
    保留区与线路启停本就未知，无法保证中间态安全，必须显式拒绝。
    """
    plan_id = payload.get("plan_id")
    if not payload.get("from_snapshot"):
        raise ChangeoverError(
            "missing_snapshot",
            f"方案 #{plan_id}（{label}）缺少发布快照：保留区与发布时线路启停无法还原，"
            "不能据此生成换场施工单。",
            [{"plan_id": plan_id, "label": label, "reason": "missing_snapshot"}],
        )

    fixtures: dict[int, Fx] = {}
    for a in payload.get("allocations", []):
        fx = Fx(
            fid=a["fixture_id"],
            name=a.get("name", f"#{a['fixture_id']}"),
            line=a["line_id"],
            start=a["start"],
            end=a["end"],
        )
        if fx.fid in fixtures:
            raise ChangeoverError(
                "snapshot_inconsistent",
                f"方案 #{plan_id}（{label}）快照中灯具 ID {fx.fid} 重复出现，快照数据不一致。",
            )
        if fx.line not in VALID_LINE_IDS or not (1 <= fx.start <= fx.end <= LINE_SIZE):
            raise ChangeoverError(
                "snapshot_inconsistent",
                f"方案 #{plan_id}（{label}）快照中灯具 #{fx.fid} 的落点 "
                f"{fx.line} 线 {fx.start}-{fx.end} 越界，快照数据不一致。",
            )
        fixtures[fx.fid] = fx

    reservations: dict[int, list[Ival]] = {line: [] for line in VALID_LINE_IDS}
    active: dict[int, bool] = {}
    for line in payload.get("occupancy", []):
        line_id = line["line_id"]
        if line_id not in VALID_LINE_IDS:
            raise ChangeoverError(
                "snapshot_inconsistent",
                f"方案 #{plan_id}（{label}）快照含非法线路 {line_id}，快照数据不一致。",
            )
        active[line_id] = bool(line.get("is_active"))
        for r in (line.get("reservations") or []):
            iv = Ival(line_id, r["start"], r["end"])
            if not (1 <= iv.start <= iv.end <= LINE_SIZE):
                raise ChangeoverError(
                    "snapshot_inconsistent",
                    f"方案 #{plan_id}（{label}）快照中线路 {line_id} 的保留区 "
                    f"{iv.start}-{iv.end} 越界，快照数据不一致。",
                )
            reservations[line_id].append(iv)
    if set(active) != set(VALID_LINE_IDS):
        raise ChangeoverError(
            "snapshot_inconsistent",
            f"方案 #{plan_id}（{label}）快照缺少完整的四条线路占用信息，快照数据不一致。",
        )

    # 快照内部一致性：灯具不压线、不落在保留区、不落在停用线路、彼此不重叠
    _check_internal(fixtures, reservations, active, plan_id, label)
    return fixtures, reservations, active


def _check_inner(a: Ival, b: Ival) -> bool:
    return a.line == b.line and a.start <= b.end and b.start <= a.end


def _check_internal(
    fixtures: dict[int, Fx],
    reservations: dict[int, list[Ival]],
    active: dict[int, bool],
    plan_id,
    label: str,
) -> None:
    all_fx = list(fixtures.values())
    for fx in all_fx:
        if not active[fx.line]:
            raise ChangeoverError(
                "snapshot_inconsistent",
                f"方案 #{plan_id}（{label}）快照中灯具 #{fx.fid} 位于停用线路 {fx.line}，"
                "与发布结果矛盾，快照数据不一致。",
            )
        for r in reservations[fx.line]:
            if fx.start <= r.end and r.start <= fx.end:
                raise ChangeoverError(
                    "snapshot_inconsistent",
                    f"方案 #{plan_id}（{label}）快照中灯具 #{fx.fid} 与保留区 "
                    f"{r.start}-{r.end} 重叠，快照数据不一致。",
                )
    for i, left in enumerate(all_fx):
        for right in all_fx[i + 1:]:
            if _check_inner(left.ival, right.ival):
                raise ChangeoverError(
                    "snapshot_inconsistent",
                    f"方案 #{plan_id}（{label}）快照中灯具 #{left.fid} 与 #{right.fid} "
                    "的区间重叠，快照数据不一致。",
                )


def _reservation_set(reservations: dict[int, list[Ival]]) -> set[tuple[int, int, int]]:
    return {(line, r.start, r.end) for line in reservations for r in reservations[line]}


def _diff_reservations(a: dict[int, list[Ival]], b: dict[int, list[Ival]]) -> list[dict]:
    """逐项列出两版保留区差异（同一线路上区间集合不同即列出该线路）。"""
    diffs: list[dict] = []
    sa, sb = _reservation_set(a), _reservation_set(b)
    for line in VALID_LINE_IDS:
        ra = sorted((r.start, r.end) for r in a[line])
        rb = sorted((r.start, r.end) for r in b[line])
        if ra != rb:
            diffs.append(
                {
                    "line_id": line,
                    "from_a": [{"start": s, "end": e} for s, e in ra],
                    "to_b": [{"start": s, "end": e} for s, e in rb],
                }
            )
    return diffs


# ---------------------------------------------------------------------------
# 施工模拟
# ---------------------------------------------------------------------------
def _free_runs(line: int, length_needed: int, blocked: list[Ival], reservations: list[Ival]) -> list[Ival]:
    """返回某条线路上长度足够、且不与任何阻塞区间/保留区相交的全部空闲连续区间。"""
    forbidden = sorted(
        [b for b in blocked if b.line == line] + [r for r in reservations if r.line == line],
        key=lambda iv: iv.start,
    )
    runs: list[Ival] = []
    cursor = 1
    for iv in forbidden:
        if iv.start > cursor:
            runs.append(Ival(line, cursor, iv.start - 1))
        cursor = max(cursor, iv.end + 1)
    if cursor <= LINE_SIZE:
        runs.append(Ival(line, cursor, LINE_SIZE))
    return [r for r in runs if r.channels >= length_needed]


class _Planner:
    """逐步模拟的施工规划器。"""

    def __init__(self, a_fx, b_fx, reservations, active, plan_a_id, plan_b_id):
        self.a_fx = a_fx
        self.b_fx = b_fx
        self.reservations = reservations  # 两版已验证相同
        self.active = active  # 两版已验证相同
        self.plan_a_id = plan_a_id
        self.plan_b_id = plan_b_id
        # 当前现场：fid -> Fx（位置随步骤更新）
        self.cur: dict[int, Fx] = {fid: fx for fid, fx in a_fx.items()}
        self.parked: set[int] = set()  # 当前处于临时周转位的灯具
        self.steps: list[dict] = []
        self.seq = 0

        ids_a, ids_b = set(a_fx), set(b_fx)
        self.removed_ids = sorted(ids_a - ids_b)
        self.installed_ids = sorted(ids_b - ids_a)
        common = sorted(ids_a & ids_b)

        self.shrink_ids: list[int] = []  # 同址缩短
        self.grow_ids: list[int] = []  # 同址加长
        self.relocate_ids: list[int] = []  # 迁址（通道数可同时变化）
        for fid in common:
            x, y = a_fx[fid], b_fx[fid]
            if (x.line, x.start) == (y.line, y.start):
                if x.channels < y.channels:
                    self.grow_ids.append(fid)
                elif x.channels > y.channels:
                    self.shrink_ids.append(fid)
            else:
                self.relocate_ids.append(fid)
        self.remaining_relocators = set(self.relocate_ids)
        self.remaining_grows = set(self.grow_ids)
        self.remaining_installs = set(self.installed_ids)

    # --- 基础工具 ---
    def _emit(
        self,
        action: str,
        fid: int,
        *,
        release: list[Ival] | None,
        occupy: Ival | None,
        temporary: bool = False,
        note: str = "",
        channels_before: int | None = None,
        channels_after: int | None = None,
    ) -> dict:
        self.seq += 1
        step = {
            "seq": self.seq,
            "action": action,
            "temporary": temporary,
            "fixture_id": fid,
            "fixture_name": self.b_fx.get(fid, self.a_fx.get(fid)).name,
            "release": [
                {"line_id": iv.line, "start": iv.start, "end": iv.end, "channels": iv.channels}
                for iv in (release or [])
            ],
            "occupy": (
                {"line_id": occupy.line, "start": occupy.start, "end": occupy.end, "channels": occupy.channels}
                if occupy is not None
                else None
            ),
            "note": note,
        }
        if channels_before is not None:
            step["channels_before"] = channels_before
            step["channels_after"] = channels_after
        self.steps.append(step)
        return step

    def _others(self, fid: int) -> list[Fx]:
        return [fx for other, fx in self.cur.items() if other != fid]

    def _assert_safe(self, fid: int, occupy: Ival | None) -> None:
        """执行前校验：占用区间不压线、不压保留区、不落停用线路、不与他灯重叠。"""
        if occupy is None:
            return
        if occupy.line not in VALID_LINE_IDS or not (1 <= occupy.start <= occupy.end <= LINE_SIZE):
            raise RuntimeError(f"内部错误：灯具 #{fid} 的目标区间越界")
        if not self.active[occupy.line]:
            raise RuntimeError(f"内部错误：灯具 #{fid} 落到停用线路 {occupy.line}")
        for r in self.reservations[occupy.line]:
            if occupy.start <= r.end and r.start <= occupy.end:
                raise RuntimeError(f"内部错误：灯具 #{fid} 压到保留区 {r.start}-{r.end}")
        for other in self._others(fid):
            if _check_inner(occupy, other.ival):
                raise RuntimeError(
                    f"内部错误：灯具 #{fid} 的区间与在场灯具 #{other.fid} 重叠"
                )

    def _do_remove(self, fid: int) -> None:
        fx = self.cur.pop(fid)
        self._emit("remove", fid, release=[fx.ival], occupy=None)

    def _do_move(self, fid: int, dest: Ival, *, temporary: bool, note: str = "") -> None:
        fx = self.cur[fid]
        self._assert_safe(fid, dest)
        self._emit(
            "relocate" if not temporary else "temp_relocate",
            fid,
            release=[fx.ival],
            occupy=dest,
            temporary=temporary,
            note=note,
        )
        self.cur[fid] = Fx(fid, fx.name, dest.line, dest.start, dest.end)
        if temporary:
            self.parked.add(fid)

    def _do_resize(self, fid: int, new_end: int) -> None:
        fx = self.cur[fid]
        target = self.b_fx[fid]
        if new_end < fx.end:  # 缩短：只释放尾部
            release = Ival(fx.line, new_end + 1, fx.end)
            occupy = None
        else:  # 加长：占用新尾部
            release = None
            occupy = Ival(fx.line, fx.end + 1, new_end)
        self._assert_safe(fid, occupy)
        self._emit(
            "channel_adjust",
            fid,
            release=[release] if release else [],
            occupy=occupy,
            channels_before=fx.channels,
            channels_after=new_end - fx.start + 1,
        )
        self.cur[fid] = Fx(fid, fx.name, fx.line, fx.start, new_end)

    # --- 周转位 ---
    def _neutral_runs(self, length_needed: int) -> list[Ival]:
        """所有启用线路上：当前空闲、且与 B 版全部灯具落点不相交的连续区间。

        “与 B 落点不相交”保证临时灯具不会再次挡住任何后续落点——选临时位时
        即兼顾后续落点；B 落点包含加长段与接入位，因此增长与接入同样不受阻。
        """
        blocked = list(self.cur.values())
        b_intervals = [fx.ival for fx in self.b_fx.values()]
        runs: list[Ival] = []
        for line in VALID_LINE_IDS:
            if not self.active[line]:
                continue
            for run in _free_runs(line, length_needed, blocked, self.reservations[line]):
                if all(not _check_inner(run, biv) for biv in b_intervals):
                    runs.append(run)
        return runs

    def _choose_park(self, fid: int) -> tuple[Ival | None, list[dict]]:
        """挑选周转区间；找不到“当前空闲且不挡新版落点”的连续区间时返回 None
        与阻塞说明（各启用线路最大可用连续区间、占用目标位的灯具）。
        """
        need = self.b_fx[fid].channels
        runs = self._neutral_runs(need)
        if not runs:
            return None, self._block_reasons(fid, need)
        target = self.b_fx[fid].ival
        # 优先最贴合的空闲区间（减少碎片化），再优先与最终落点同线、靠前
        runs.sort(key=lambda r: (r.channels, 0 if r.line == target.line else 1, r.line, r.start))
        return Ival(runs[0].line, runs[0].start, runs[0].start + need - 1), []

    def _block_reasons(self, fid: int, need: int) -> list[dict]:
        """列明阻塞：各启用线路最大空闲连续区间、占用目标位的灯具。"""
        target = self.b_fx[fid].ival
        blockers = [
            other.fid for other in self.cur.values() if other.fid != fid and _check_inner(other.ival, target)
        ]
        lines_info = []
        for line in VALID_LINE_IDS:
            if not self.active[line]:
                continue
            runs = _free_runs(line, 1, list(self.cur.values()), self.reservations[line])
            # 仅统计不与 B 落点相交的中性区间（可作周转的候选）
            neutral = [r for r in runs if all(
                not _check_inner(r, biv) for biv in (x.ival for x in self.b_fx.values())
            )]
            biggest = max((r.channels for r in neutral), default=0)
            lines_info.append(
                {
                    "line_id": line,
                    "largest_neutral_free_channels": biggest,
                    "needed_channels": need,
                }
            )
        return [
            {
                "fixture_id": fid,
                "target": {"line_id": target.line, "start": target.start, "end": target.end},
                "blocking_fixture_ids": sorted(blockers),
                "free_runs": lines_info,
            }
        ]

    # --- 主规划流程 ---
    def plan(self) -> list[dict]:
        # 阶段 1：拆除（最先释放空间）
        for fid in self.removed_ids:
            self._do_remove(fid)

        # 阶段 2：同址缩短
        for fid in self.shrink_ids:
            self._do_resize(fid, self.b_fx[fid].end)

        # 阶段 3：迁址（含周转）
        while self.remaining_relocators:
            moved = self._relocate_one_pass()
            if not moved:
                # 环：所有待迁灯具都被彼此挡住，挑一台当前挡住最多其他落点的
                # 灯具临时搬到中性空闲区间，打破环且不再挡后续落点
                fid = self._pick_park_fixture()
                park, blocking = self._choose_park(fid)
                if park is None:
                    raise _Blocked(
                        f"迁址出现互相占位且找不到安全周转位：灯具 #{fid} 需要 "
                        f"{self.b_fx[fid].channels} 个连续空闲通道，但启用线路上不存在"
                        "“当前空闲且不挡住新版落点”的连续区间。",
                        blocking,
                    )
                self._do_move(
                    fid,
                    park,
                    temporary=True,
                    note=(
                        f"临时周转：目标位被其他待迁灯具占用，先迁至 {park.line} 线 "
                        f"{park.start}-{park.end}（该区间与新版所有落点不相交），后续再归位"
                    ),
                )

        # 阶段 4：同址加长（新尾部此刻必须已清空）
        for fid in self.grow_ids:
            self._do_resize(fid, self.b_fx[fid].end)
            self.remaining_grows.discard(fid)

        # 阶段 5：接入
        for fid in self.installed_ids:
            target = self.b_fx[fid]
            self._assert_safe(fid, target.ival)
            self._emit(
                "install",
                fid,
                release=[],
                occupy=target.ival,
            )
            self.cur[fid] = Fx(fid, target.name, target.line, target.start, target.end)
            self.remaining_installs.discard(fid)

        self._verify_final()
        return self.steps

    def _relocate_one_pass(self) -> bool:
        """扫描一遍待迁灯具，直接移动所有当前可达目标位的灯具；返回是否有进展。"""
        moved_any = False
        for fid in sorted(self.remaining_relocators):
            target = self.b_fx[fid].ival
            fx = self.cur[fid]
            # 目标区间与当前区间等长，自身占用不算阻挡；只需其他在场灯具都不与目标相交
            blocked_by = [
                other
                for other in self._others(fid)
                if _check_inner(target, other.ival)
            ]
            if not blocked_by:
                note = "临时周转归位" if fid in self.parked else ""
                self._do_move(fid, target, temporary=False, note=note)
                self.remaining_relocators.discard(fid)
                self.parked.discard(fid)
                moved_any = True
        return moved_any

    def _pick_park_fixture(self) -> int:
        """环中挑选周转灯具：当前挡住最多其他待迁灯具落点者优先（搬走它释放的
        约束最多）；并列时需要的临时通道更少（更容易找到周转位）、ID 更小。
        已在临时位的灯具不阻挡任何新版落点，不会被再次选中。
        """
        waiting = self.remaining_relocators - self.parked

        def blocks_others(fid: int) -> int:
            return sum(
                1
                for other in waiting
                if other != fid and _check_inner(self.cur[fid].ival, self.b_fx[other].ival)
            )

        return min(
            waiting,
            key=lambda fid: (-blocks_others(fid), self.b_fx[fid].channels, fid),
        )

    def _verify_final(self) -> None:
        """末态必须严格等于 B：同一组灯具、同样的线路/起始/结束。"""
        if set(self.cur) != set(self.b_fx):
            raise RuntimeError("内部错误：末态灯具集合与新版不一致")
        for fid, target in self.b_fx.items():
            now = self.cur[fid]
            if (now.line, now.start, now.end) != (target.line, target.start, target.end):
                raise RuntimeError(f"内部错误：灯具 #{fid} 末态 {now.line} 线 {now.start}-{now.end} 不等于目标")
        # 再做一次全局无重叠/不压保留区校验（B 快照本身合法，正常不会触发）
        _check_internal(self.cur, self.reservations, self.active, self.plan_b_id, "新版")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def build_changeover(payload_a: dict, payload_b: dict) -> dict:
    """根据两份发布快照 payload 生成从 A 到 B 的双版本换场施工单。

    前提不成立时抛 ChangeoverError（缺快照/未发布由调用方先行拦截，
    保留区或线路启停两版不一致在此拒绝）；找不到安全周转位时返回的报告中
    feasible=false 并附阻塞区间，不输出任何步骤。
    """
    a_fx, a_res, a_active = _extract_snapshot(payload_a, "旧版（起点）")
    b_fx, b_res, b_active = _extract_snapshot(payload_b, "新版（目标）")

    plan_a, plan_b = payload_a["plan_id"], payload_b["plan_id"]

    # 线路启停两版必须一致：中间态只有一套总线可用，无法同时尊重两种启停
    line_diffs = [
        {"line_id": line, "from_a_active": a_active[line], "to_b_active": b_active[line]}
        for line in VALID_LINE_IDS
        if a_active[line] != b_active[line]
    ]
    if line_diffs:
        raise ChangeoverError(
            "lines_mismatch",
            "两版快照的线路启停不一致：换场期间线路可用性必须相同，不能生成安全步骤。",
            [{"type": "line_active_mismatch", "lines": line_diffs}],
        )

    # 保留区两版必须一致：任何中间态都不得压保留区，保留区变化会让“安全”失去共同基准
    res_diffs = _diff_reservations(a_res, b_res)
    if res_diffs:
        raise ChangeoverError(
            "reservations_mismatch",
            "两版快照的保留区不一致：换场期间保留通道必须始终空置，"
            "请先按新版保留区单独安排线路改造后再生成换场施工单。",
            [{"type": "reservation_mismatch", "lines": res_diffs}],
        )

    planner = _Planner(b_fx=b_fx, a_fx=a_fx, reservations=a_res, active=a_active,
                       plan_a_id=plan_a, plan_b_id=plan_b)

    def _base_report() -> dict:
        return {
            "from_plan_id": plan_a,
            "to_plan_id": plan_b,
            "from_config_version": payload_a.get("config_version"),
            "to_config_version": payload_b.get("config_version"),
            "from_snapshot": True,
        }

    try:
        steps = planner.plan()
    except _Blocked as exc:
        # 前提都成立但物理上无法安全周转：只给结论与阻塞区间，绝不输出貌似安全的步骤
        report = _base_report()
        report.update(
            {
                "feasible": False,
                "reason_code": "no_safe_buffer",
                "message": exc.message,
                "blocking": exc.blocking,
                "steps": [],
                "counts": {
                    "fixtures_in_a": len(a_fx),
                    "fixtures_in_b": len(b_fx),
                    "remove": len(planner.removed_ids),
                    "relocate": len(planner.relocate_ids),
                    "channel_adjust": len(planner.shrink_ids) + len(planner.grow_ids),
                    "channel_adjust_shrink": len(planner.shrink_ids),
                    "channel_adjust_grow": len(planner.grow_ids),
                    "install": len(planner.installed_ids),
                    "temporary_steps": 0,
                    "total_steps": 0,
                },
            }
        )
        return report
    except RuntimeError as exc:  # 模拟自检失败：绝不输出貌似安全的步骤
        raise ChangeoverError("simulation_failed", f"施工模拟自检失败：{exc}") from exc

    counts = {
        "fixtures_in_a": len(a_fx),
        "fixtures_in_b": len(b_fx),
        "remove": len(planner.removed_ids),
        "relocate": len(planner.relocate_ids),
        "channel_adjust": len(planner.shrink_ids) + len(planner.grow_ids),
        "channel_adjust_shrink": len(planner.shrink_ids),
        "channel_adjust_grow": len(planner.grow_ids),
        "install": len(planner.installed_ids),
        "temporary_steps": sum(1 for s in steps if s["temporary"]),
        "total_steps": len(steps),
    }
    report = _base_report()
    report.update({"feasible": True, "counts": counts, "steps": steps})
    return report
