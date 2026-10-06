"""基于 OR-Tools CP-SAT 的 DMX 通道编排求解器。

约束：
- 每台灯具在某条启用线路上占据一段连续通道，不跨线路、不越界；
- 同一线路上灯具之间、灯具与保留区之间不重叠；
- 锁定灯具必须使用既有线路与起始地址。

目标（按优先级，前一级未证明最优前不得固定其值继续下一级）：
1. 已配灯具迁移台数最少；
2. 启用线路数最少；
3. 按灯具 ID 排序，逐台取（线路 ID, 起始地址）字典序最小。

每个阶段只接受 CP-SAT 的 OPTIMAL：限时内仅得到 FEASIBLE（可行 incumbent）
时返回“未证最优”（unproven）状态，与真正的 INFEASIBLE 明确区分，且不会
把未证明的取值固定后继续后续阶段。若求解返回 UNKNOWN 或候选读取失败，
unproven 结果不带任何候选排布（allocations 为空），此时 feasible 为 False，
只表示“暂无可展示排布”，不代表已证明无解。

无解时返回可读的冲突约束诊断。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ortools.sat.python import cp_model

LINE_SIZE = 512
SOLVE_SECONDS_PHASE = 5.0
SOLVE_SECONDS_TIE = 1.5
# 字典序破局目标中“线路优先于起始地址”的权重（起始地址最大 512）
LINE_PRIORITY_WEIGHT = 1024

# 求解结论三态：optimal 已逐级证明最优；infeasible 已证明无解；
# unproven 限时内只拿到可行候选，未能证明某一级最优。
OPTIMAL = "optimal"
INFEASIBLE = "infeasible"
UNPROVEN = "unproven"

_UNPROVEN_MESSAGES = {
    "migration": (
        "第一阶段（已配灯具迁移台数最少）在限定时间内仅找到可行解，"
        "未能证明迁移台数最少；未生成可发布草稿，请减少灯具数量或稍后重试。"
    ),
    "lines": (
        "第二阶段（启用线路数最少）在限定时间内仅找到可行解，"
        "未能证明启用线路数最少；未生成可发布草稿，请重试。"
    ),
    "lexicographic": (
        "字典序破局阶段在限定时间内未能逐台证明（线路 ID、起始地址）字典序最小；"
        "未生成可发布草稿，请重试。"
    ),
}

# 求解返回 UNKNOWN 或候选读取失败时，连可行 incumbent 都没有：与“有候选但未证
# 最优”区分开，明确这不是已证无解，只是暂无可展示排布。
_DIAGNOSIS_TIMEOUT_MESSAGE = (
    "冲突诊断阶段超时：无法证明冲突是否必然，也未取得可展示的候选排布；"
    "这不代表已证明无解，请减少灯具数量或放宽部分锁定后重试。"
)

_UNPROVEN_NO_CANDIDATE_MESSAGES = {
    "migration": (
        "第一阶段（已配灯具迁移台数最少）在限定时间内未能证明最优，且未取得任何"
        "可展示的候选排布；这不代表已证明无解，未生成可发布草稿，"
        "请减少灯具数量或稍后重试。"
    ),
    "lines": (
        "第二阶段（启用线路数最少）在限定时间内未能证明最优，且未取得任何"
        "可展示的候选排布；这不代表已证明无解，未生成可发布草稿，请重试。"
    ),
    "lexicographic": (
        "字典序破局阶段在限定时间内未能逐台证明（线路 ID、起始地址）字典序最小，"
        "且未取得任何可展示的候选排布；这不代表已证明无解，未生成可发布草稿，请重试。"
    ),
}


def _params(seconds: float, *, first_solution: bool = False) -> cp_model.sat_parameters_pb2.SatParameters:
    p = cp_model.sat_parameters_pb2.SatParameters()
    p.max_time_in_seconds = seconds
    p.num_search_workers = 8
    p.log_search_progress = False
    if first_solution:
        # 只需要证明可行并拿到一个解时，尽快返回（不追求最优）
        p.stop_after_first_solution = True
    return p


@dataclass
class FixtureSpec:
    id: int
    name: str
    channels: int
    prev_line: int | None
    prev_start: int | None
    locked: bool


@dataclass
class ReservationSpec:
    id: int
    line_id: int
    start: int
    end: int
    reason: str = ""


@dataclass
class LineSpec:
    id: int
    name: str
    active: bool


@dataclass
class SolveSpec:
    fixtures: list[FixtureSpec]
    reservations: list[ReservationSpec]
    lines: list[LineSpec]


@dataclass
class Allocation:
    fixture_id: int
    name: str
    line_id: int
    start: int
    end: int
    channels: int
    migrated: bool
    locked: bool


@dataclass
class SolveResult:
    # optimal（逐级证明最优）/ infeasible（已证无解）/ unproven（存在可行候选但未证最优）
    optimality: str
    allocations: list[Allocation] = field(default_factory=list)
    migration_count: int = 0
    used_lines: int = 0
    conflicts: list[dict] = field(default_factory=list)
    occupancy: list[dict] = field(default_factory=list)
    # 未证最优时停在哪个阶段：migration / lines / lexicographic
    unproven_stage: str | None = None
    unproven_message: str = ""

    @property
    def feasible(self) -> bool:
        """是否存在可用的可行排布（含未证最优的候选）。"""
        return self.optimality != INFEASIBLE and bool(self.allocations)

    @property
    def is_optimal(self) -> bool:
        return self.optimality == OPTIMAL

    @property
    def has_candidate(self) -> bool:
        """未证最优时是否仍带回了可展示（但不可发布）的可行候选。"""
        return bool(self.allocations)


class _ModelArtifacts:
    def __init__(self) -> None:
        self.present: dict[int, dict[int, cp_model.IntVar]] = {}
        self.starts: dict[int, cp_model.IntVar] = {}
        self.mig: dict[int, cp_model.IntVar] = {}
        self.relax: dict[int, cp_model.IntVar] = {}
        self.total_mig: cp_model.IntVar | None = None
        self.used: dict[int, cp_model.IntVar] = {}
        self.total_used: cp_model.IntVar | None = None


def _precheck(spec: SolveSpec) -> list[dict]:
    """在建模前拦截结构性冲突，给出确定、可读的错误。"""
    conflicts: list[dict] = []
    active_lines = [l for l in spec.lines if l.active]
    active_ids = {l.id for l in active_lines}

    if not active_lines:
        conflicts.append({"type": "no_active_line", "message": "没有启用的线路，至少需要启用一条线路。"})
        return conflicts

    # 每条线路保留区占用通道的并集（允许保留区彼此重叠）
    reserved: dict[int, set[int]] = {lid: set() for lid in active_ids}
    for r in spec.reservations:
        if r.line_id in active_ids:
            reserved[r.line_id].update(range(r.start, r.end + 1))

    total_demand = sum(f.channels for f in spec.fixtures)
    total_capacity = sum(LINE_SIZE - len(ch) for ch in reserved.values())
    if total_demand > total_capacity:
        conflicts.append(
            {
                "type": "capacity",
                "message": (
                    f"通道总需求 {total_demand} 超过启用线路的可用容量 {total_capacity}"
                    f"（{len(active_lines)} 条 × {LINE_SIZE}，扣除保留区）。"
                ),
                "total_demand": total_demand,
                "total_capacity": total_capacity,
            }
        )

    res_by_id = {r.id: r for r in spec.reservations}
    locked: list[FixtureSpec] = []
    for f in spec.fixtures:
        if not f.locked:
            continue
        if f.prev_line is None or f.prev_start is None:
            conflicts.append(
                {
                    "type": "invalid_lock",
                    "fixture_id": f.id,
                    "fixture_name": f.name,
                    "message": f"灯具“{f.name}”被锁定，但缺少既有线路或起始地址。",
                }
            )
            continue
        if f.prev_line not in active_ids:
            conflicts.append(
                {
                    "type": "invalid_lock",
                    "fixture_id": f.id,
                    "fixture_name": f.name,
                    "line_id": f.prev_line,
                    "message": f"灯具“{f.name}”锁定在已停用或不存在的线路 {f.prev_line} 上。",
                }
            )
            continue
        if not (1 <= f.prev_start <= LINE_SIZE - f.channels + 1):
            conflicts.append(
                {
                    "type": "invalid_lock",
                    "fixture_id": f.id,
                    "fixture_name": f.name,
                    "line_id": f.prev_line,
                    "start": f.prev_start,
                    "message": (
                        f"灯具“{f.name}”锁定起始地址 {f.prev_start} 越界"
                        f"（占 {f.channels} 通道，线路 {f.prev_line} 允许 1..{LINE_SIZE - f.channels + 1}）。"
                    ),
                }
            )
            continue
        block = set(range(f.prev_start, f.prev_start + f.channels))
        overlap_r = next(
            (
                r
                for r in spec.reservations
                if r.line_id == f.prev_line
                and not block.isdisjoint(range(r.start, r.end + 1))
            ),
            None,
        )
        if overlap_r is not None:
            r = overlap_r
            conflicts.append(
                {
                    "type": "lock_reservation",
                    "fixture_id": f.id,
                    "fixture_name": f.name,
                    "reservation_id": r.id,
                    "line_id": f.prev_line,
                    "message": (
                        f"灯具“{f.name}”锁定块（线路 {f.prev_line}：{f.prev_start}-"
                        f"{f.prev_start + f.channels - 1}）与保留区 #{r.id}"
                        f"（{res_by_id[r.id].start}-{res_by_id[r.id].end}）重叠。"
                    ),
                }
            )
        locked.append(f)

    # 锁定块两两重叠
    by_line: dict[int, list[FixtureSpec]] = {}
    for f in locked:
        by_line.setdefault(f.prev_line, []).append(f)
    for lid, group in by_line.items():
        group.sort(key=lambda x: (x.prev_start, x.id))
        for i, a in enumerate(group):
            for b in group[i + 1 :]:
                if b.prev_start >= a.prev_start + a.channels:
                    break
                conflicts.append(
                    {
                        "type": "lock_overlap",
                        "fixture_ids": [a.id, b.id],
                        "fixture_names": [a.name, b.name],
                        "line_id": lid,
                        "message": (
                            f"锁定灯具“{a.name}”（{a.prev_start}-{a.prev_start + a.channels - 1}）"
                            f"与“{b.name}”（{b.prev_start}-{b.prev_start + b.channels - 1}）"
                            f"在线路 {lid} 上重叠。"
                        ),
                    }
                )
    return conflicts


def _build_model(spec: SolveSpec, relax_locks: bool = False) -> tuple[cp_model.CpModel, _ModelArtifacts]:
    model = cp_model.CpModel()
    art = _ModelArtifacts()
    active_lines = [l for l in spec.lines if l.active]
    line_ids = [l.id for l in active_lines]
    intervals: dict[int, list] = {lid: [] for lid in line_ids}

    # 保留区：固定区间
    for r in spec.reservations:
        if r.line_id in intervals:
            iv = model.NewIntervalVar(r.start, r.end - r.start + 1, r.end + 1, f"res_{r.id}")
            intervals[r.line_id].append(iv)

    for f in spec.fixtures:
        max_start = LINE_SIZE - f.channels + 1
        start = model.NewIntVar(1, max_start, f"start_{f.id}")
        art.starts[f.id] = start
        present: dict[int, cp_model.IntVar] = {}
        for lid in line_ids:
            p = model.NewBoolVar(f"p_{f.id}_{lid}")
            iv = model.NewOptionalIntervalVar(
                start, f.channels, start + f.channels, p, f"iv_{f.id}_{lid}"
            )
            present[lid] = p
            intervals[lid].append(iv)
        model.Add(sum(present.values()) == 1)
        art.present[f.id] = present

        if f.locked and f.prev_line is not None and f.prev_start is not None and f.prev_line in line_ids:
            if relax_locks:
                # 放宽该锁定需付代价 1；诊断模型据此找出无法满足的锁
                relax = model.NewBoolVar(f"relax_{f.id}")
                art.relax[f.id] = relax
                model.Add(present[f.prev_line] == 1).OnlyEnforceIf(relax.Not())
                model.Add(start == f.prev_start).OnlyEnforceIf(relax.Not())
            else:
                for lid, p in present.items():
                    model.Add(p == 1 if lid == f.prev_line else p == 0)
                model.Add(start == f.prev_start)
            continue  # 锁定灯具要么留在原地，要么由诊断阶段处理，不计迁移

        # 迁移变量：仅“已配灯具”（有既有地址）离开原位才计迁移；新灯具计 0
        if f.prev_line is None or f.prev_start is None:
            mig = model.NewConstant(0)
        elif f.prev_line not in line_ids:
            # 既有线路被停用：必然迁移
            mig = model.NewConstant(1)
        else:
            mig = model.NewBoolVar(f"mig_{f.id}")
            same_start = model.NewBoolVar(f"same_start_{f.id}")
            model.Add(start == f.prev_start).OnlyEnforceIf(same_start)
            model.Add(start != f.prev_start).OnlyEnforceIf(same_start.Not())
            stay = model.NewBoolVar(f"stay_{f.id}")
            prev_p = present[f.prev_line]
            model.Add(stay <= prev_p)
            model.Add(stay <= same_start)
            model.Add(stay >= prev_p + same_start - 1)
            model.Add(mig == 1 - stay)
        art.mig[f.id] = mig

    for lid, ivs in intervals.items():
        model.AddNoOverlap(ivs)
        used = model.NewBoolVar(f"used_{lid}")
        art.used[lid] = used
        if ivs:
            model.AddMaxEquality(used, [art.present[f.id][lid] for f in spec.fixtures])
        else:
            model.Add(used == 0)

    art.total_mig = model.NewIntVar(0, max(len(spec.fixtures), 1), "total_mig")
    model.Add(art.total_mig == sum(art.mig.values()) if art.mig else art.total_mig == 0)
    art.total_used = model.NewIntVar(0, len(line_ids), "total_used")
    model.Add(art.total_used == sum(art.used.values()))
    return model, art


def _diagnose(spec: SolveSpec) -> SolveResult:
    """硬约束模型无解：放宽锁定并最小化放宽数量，定位冲突锁。"""
    model, art = _build_model(spec, relax_locks=True)
    if art.relax:
        model.Minimize(sum(art.relax.values()))
    solver = cp_model.CpSolver()
    solver.parameters.CopyFrom(_params(SOLVE_SECONDS_PHASE))
    status = solver.Solve(model)

    if status == cp_model.OPTIMAL:
        bad_locks = [fid for fid, v in art.relax.items() if solver.Value(v) == 1]
        if bad_locks:
            fix_by_id = {f.id: f for f in spec.fixtures}
            conflicts = [
                {
                    "type": "unsatisfiable_lock",
                    "fixture_id": fid,
                    "fixture_name": fix_by_id[fid].name,
                    "line_id": fix_by_id[fid].prev_line,
                    "start": fix_by_id[fid].prev_start,
                    "message": (
                        f"锁定灯具“{fix_by_id[fid].name}”无法保持在"
                        f"线路 {fix_by_id[fid].prev_line} 地址 {fix_by_id[fid].prev_start}"
                        "（容量或保留区排布冲突），需解锁或调整保留区。"
                    ),
                }
                for fid in sorted(bad_locks)
            ]
            return SolveResult(optimality=INFEASIBLE, conflicts=conflicts)
    elif status == cp_model.FEASIBLE:
        # 只拿到一个可行（放宽了锁）的 incumbent，未能证明必须放宽哪些锁：
        # 不能据此宣布硬约束无解。
        return SolveResult(
            optimality=UNPROVEN,
            unproven_stage="diagnosis",
            unproven_message=_DIAGNOSIS_TIMEOUT_MESSAGE,
            conflicts=[{"type": "solver_timeout", "message": _DIAGNOSIS_TIMEOUT_MESSAGE}],
        )
    elif status == cp_model.UNKNOWN:
        return SolveResult(
            optimality=UNPROVEN,
            unproven_stage="diagnosis",
            unproven_message=_DIAGNOSIS_TIMEOUT_MESSAGE,
            conflicts=[{"type": "solver_timeout", "message": _DIAGNOSIS_TIMEOUT_MESSAGE}],
        )

    active_lines = [l for l in spec.lines if l.active]
    occupied: dict[int, set[int]] = {l.id: set() for l in active_lines}
    for r in spec.reservations:
        if r.line_id in occupied:
            occupied[r.line_id].update(range(r.start, r.end + 1))
    free = sum(LINE_SIZE - len(s) for s in occupied.values())
    demand = sum(f.channels for f in spec.fixtures)
    return SolveResult(
        optimality=INFEASIBLE,
        conflicts=[
            {
                "type": "infeasible",
                "message": (
                    "即使放宽全部锁定仍无可行排布：保留区可能把线路切割成无法容纳灯具的碎块。"
                    f"总需求 {demand} 通道，名义空闲 {free} 通道，但最大连续空闲段不足。"
                ),
                "total_demand": demand,
                "total_free": free,
            }
        ],
    )


def solve(spec: SolveSpec) -> SolveResult:
    conflicts = _precheck(spec)
    if conflicts:
        return SolveResult(optimality=INFEASIBLE, conflicts=conflicts)

    sorted_fixtures = sorted(spec.fixtures, key=lambda f: f.id)
    active_lines = [l for l in spec.lines if l.active]
    line_ids = [l.id for l in active_lines]

    # 第一阶段：最小化迁移台数。只有 OPTIMAL 才采信目标值；FEASIBLE/UNKNOWN
    # 一律作为“未证最优”返回，绝不固定 incumbent 后继续后续阶段。
    model1, art1 = _build_model(spec, relax_locks=False)
    model1.Minimize(art1.total_mig)
    solver = cp_model.CpSolver()
    solver.parameters.CopyFrom(_params(SOLVE_SECONDS_PHASE))
    s1 = solver.Solve(model1)
    if s1 == cp_model.INFEASIBLE:
        return _diagnose(spec)
    if s1 != cp_model.OPTIMAL:
        candidate = _candidate_from(solver, s1, art1, spec, sorted_fixtures, line_ids)
        return _unproven_result("migration", spec, candidate)
    min_mig = solver.Value(art1.total_mig)

    # 第二阶段：迁移数固定为已证最优值，最小化启用线路数。
    model2, art2 = _build_model(spec, relax_locks=False)
    model2.Add(art2.total_mig == min_mig)
    model2.Minimize(art2.total_used)
    solver = cp_model.CpSolver()
    solver.parameters.CopyFrom(_params(SOLVE_SECONDS_PHASE))
    s2 = solver.Solve(model2)
    if s2 == cp_model.INFEASIBLE:
        return _diagnose(spec)
    if s2 != cp_model.OPTIMAL:
        candidate = _candidate_from(solver, s2, art2, spec, sorted_fixtures, line_ids)
        return _unproven_result("lines", spec, candidate)
    min_used = solver.Value(art2.total_used)

    # 第三阶段：迁移数、线路数固定为已证最优值，按灯具 ID 顺序逐台做
    # （线路 ID, 起始地址）字典序最小化。每个前缀只在求解器返回 OPTIMAL
    # 时才固定；任何一台未能证明，立即返回未证最优，不固定该台的值继续。
    chosen, candidate = _lexicographic_phase(
        spec, sorted_fixtures, line_ids, min_mig, min_used
    )
    if chosen is None:
        return _unproven_result("lexicographic", spec, candidate)

    allocations = _allocations_from(spec, sorted_fixtures, chosen)
    occupancy = _build_occupancy(spec, allocations)
    used_lines = sum(1 for line in occupancy if line["used_channels"] > 0)
    return SolveResult(
        optimality=OPTIMAL,
        allocations=allocations,
        migration_count=sum(1 for a in allocations if a.migrated),
        used_lines=used_lines,
        occupancy=occupancy,
    )


def _candidate_from(solver, status, art, spec, sorted_fixtures, line_ids) -> list[Allocation] | None:
    """阶段求解仅得 FEASIBLE 时，取出可展示（但不可发布）的 incumbent。"""
    if status != cp_model.FEASIBLE:
        return None
    try:
        chosen = _read_chosen(solver, art, sorted_fixtures, line_ids)
    except Exception:
        return None
    return _allocations_from(spec, sorted_fixtures, chosen)


def _allocations_from(spec: SolveSpec, sorted_fixtures, chosen: dict[int, tuple[int, int]]):
    by_id = {f.id: f for f in spec.fixtures}
    allocations: list[Allocation] = []
    for f in sorted_fixtures:
        lid, st = chosen[f.id]
        fx = by_id[f.id]
        migrated = fx.prev_line is not None and (fx.prev_line, fx.prev_start) != (lid, st)
        allocations.append(
            Allocation(
                fixture_id=f.id,
                name=f.name,
                line_id=lid,
                start=st,
                end=st + f.channels - 1,
                channels=f.channels,
                migrated=migrated,
                locked=f.locked,
            )
        )
    return allocations


def _unproven_result(stage: str, spec: SolveSpec, candidate: list[Allocation] | None) -> SolveResult:
    allocations = candidate or []
    occupancy = _build_occupancy(spec, allocations) if allocations else []
    # 是否取得完整可用排布决定文案与 feasible 裁决：无候选时明确“不代表已证无解”。
    message = (
        _UNPROVEN_MESSAGES[stage] if allocations else _UNPROVEN_NO_CANDIDATE_MESSAGES[stage]
    )
    return SolveResult(
        optimality=UNPROVEN,
        unproven_stage=stage,
        unproven_message=message,
        allocations=allocations,
        migration_count=sum(1 for a in allocations if a.migrated),
        used_lines=sum(1 for line in occupancy if line["used_channels"] > 0),
        occupancy=occupancy,
        conflicts=[{"type": "unproven", "message": message}],
    )


def _read_chosen(solver: cp_model.CpSolver, art: _ModelArtifacts, fixtures, line_ids):
    chosen = {}
    for f in fixtures:
        chosen_line = next(l for l in line_ids if solver.Value(art.present[f.id][l]) == 1)
        chosen[f.id] = (chosen_line, solver.Value(art.starts[f.id]))
    return chosen


def _max_packed_batch(n_lines: int) -> int:
    """B 进制加权目标能在 int63 内打包多少台灯（base 见 LINE_PRIORITY_WEIGHT）。"""
    base = LINE_PRIORITY_WEIGHT
    digit_max = base * n_lines
    cap = 2**62
    k = 0
    max_coeff = base  # 最末台灯的线路指数项系数
    while k < 100 and max_coeff <= cap:
        k += 1
        max_coeff *= digit_max
    return k


def _lexicographic_phase(spec, sorted_fixtures, line_ids, min_mig, min_used):
    """逐级证明的字典序破局。

    为减少求解次数，把 int63 权重能容纳的前若干台灯打成一个 B 进制加权
    目标一次性求解；仅当返回 OPTIMAL 才固定整批，否则把批量减半重解，
    直到单台灯求解仍非 OPTIMAL 时返回 (None, incumbent候选)。
    未证明的任何取值都不会被固定。
    """
    n_lines = len(line_ids)
    max_batch = _max_packed_batch(n_lines)
    fixed: dict[int, tuple[int, int]] = {}
    rest = list(sorted_fixtures)
    candidate: list[Allocation] | None = None

    while rest:
        batch_size = min(max_batch, len(rest))
        batch = rest[:batch_size]
        while True:
            model, art = _build_model(spec)
            model.Add(art.total_mig == min_mig)
            model.Add(art.total_used == min_used)
            for fid, (lid, stv) in fixed.items():
                model.Add(art.present[fid][lid] == 1)
                model.Add(art.starts[fid] == stv)
            terms = _packed_terms(art, batch, line_ids, batch_size)
            model.Minimize(terms)
            solver = cp_model.CpSolver()
            solver.parameters.CopyFrom(
                _params(SOLVE_SECONDS_PHASE if batch_size > 1 else SOLVE_SECONDS_TIE)
            )
            st = solver.Solve(model)
            if st == cp_model.INFEASIBLE:
                # 前两级目标值下不应发生；视为无法证明，交由上层处理
                return None, candidate
            if st == cp_model.OPTIMAL:
                for f in batch:
                    chosen_line = next(
                        l for l in line_ids if solver.Value(art.present[f.id][l]) == 1
                    )
                    fixed[f.id] = (chosen_line, solver.Value(art.starts[f.id]))
                rest = rest[batch_size:]
                break
            # FEASIBLE / UNKNOWN：这批没能证明。记下 incumbent 作为候选，
            # 收缩批量重解；不固定本批任何取值。
            candidate = _candidate_from(solver, st, art, spec, sorted_fixtures, line_ids)
            if batch_size == 1:
                return None, candidate
            batch_size = max(1, batch_size // 2)
            batch = batch[:batch_size]
    return dict(fixed), None


def _packed_terms(art, batch, line_ids, batch_size):
    """batch 内按灯具顺序的 B 进制字典序加权目标（线路优先于起始地址）。"""
    base = LINE_PRIORITY_WEIGHT
    digit_max = base * len(line_ids)
    terms = []
    for pos, f in enumerate(batch):
        w = digit_max ** (batch_size - 1 - pos)
        for idx, lid in enumerate(line_ids):
            terms.append(art.present[f.id][lid] * (idx * base) * w)
        terms.append(art.starts[f.id] * w)
    return sum(terms)


def _build_occupancy(spec: SolveSpec, allocations: list[Allocation]) -> list[dict]:
    all_lines = sorted(spec.lines, key=lambda l: l.id)
    blocks: dict[int, list[dict]] = {l.id: [] for l in all_lines}
    for a in allocations:
        blocks[a.line_id].append(
            {
                "fixture_id": a.fixture_id,
                "name": a.name,
                "start": a.start,
                "end": a.end,
                "channels": a.channels,
                "migrated": a.migrated,
                "locked": a.locked,
            }
        )
    result = []
    for line in all_lines:
        res = [
            {
                "id": r.id,
                "start": r.start,
                "end": r.end,
                "reason": r.reason,
            }
            for r in sorted(
                (r for r in spec.reservations if r.line_id == line.id),
                key=lambda r: r.start,
            )
        ]
        occupied: set[int] = set()
        for b in blocks[line.id]:
            occupied.update(range(b["start"], b["end"] + 1))
        for r in res:
            occupied.update(range(r["start"], r["end"] + 1))
        result.append(
            {
                "line_id": line.id,
                "name": line.name,
                "is_active": line.active,
                "size": LINE_SIZE,
                "used_channels": len(occupied),
                "free_channels": LINE_SIZE - len(occupied),
                "fixtures": sorted(blocks[line.id], key=lambda b: b["start"]),
                "reservations": res,
            }
        )
    return result
