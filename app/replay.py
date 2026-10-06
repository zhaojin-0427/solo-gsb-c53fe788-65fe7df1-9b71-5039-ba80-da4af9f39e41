"""DMX 信号回放：解析信号 CSV，按发布快照/留存落点重建逐帧读数。

本模块为纯函数实现，不触碰数据库：方案 payload（发布快照还原或旧记录留存
明细重建）由调用方传入，回放过程不修改任何编排、发布与配置数据。

CSV 语义（每行更新一个槽位）：
- 四列：非负毫秒时间、线路（1-4）、通道（1-512）、值（0-255）；
- 同一毫秒的多行合为一帧；槽位初值为 0，未更新的值延续上一帧；
- 时间、线路、通道、值任一非法的行属于整表拒收（CsvRejectError），
  不做“隔离后继续”，避免坏数据悄悄改变读数；
- 同一毫秒、同一槽位出现不同值时，由调用方选择策略：
  * reject：整表拒收并逐项列出冲突；
  * last：按文件顺序取末值，并在结果中逐项标注（含被采用的行号）。

归属判定（只认所选版本发布时的事实，绝不读取当前落点/当前保留区）：
- 有快照：灯具闭区间与保留区均取自发布快照，槽位归属为
  灯具（相对通道）/ 保留区 / 未分配；
- 无快照的旧发布记录：灯具闭区间取自留存明细（发布时事实），
  灯具外的槽位一律标为“归属未知”——不得用当前保留区把它判成空位异常。
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass

#: 线路与单线通道上限（与编排规则一致），DMX 取值范围 0-255
VALID_LINE_IDS = {1, 2, 3, 4}
LINE_SIZE = 512
VALUE_MIN = 0
VALUE_MAX = 255

#: 上传文件大小上限，防止异常大文件拖垮服务
MAX_FILE_BYTES = 1_000_000

#: 同毫秒同槽冲突策略
POLICY_REJECT = "reject"  # 整表拒收
POLICY_LAST = "last"  # 按文件顺序取末值
VALID_POLICIES = {POLICY_REJECT, POLICY_LAST}

_INT_RE = re.compile(r"[+-]?\d+")

REQUIRED_HEADER_HINT = "时间(毫秒),线路,通道,值"


class CsvStructureError(ValueError):
    """整表结构问题（空文件、非 UTF-8、缺少必需表头、无数据行）——整表拒收。"""


class CsvRejectError(ValueError):
    """行级非法数据或同槽冲突导致的整表拒收，errors 逐项列出原因。"""

    def __init__(self, message: str, errors: list[dict], code: str = "invalid_rows"):
        super().__init__(message)
        self.message = message
        self.errors = errors
        # invalid_rows（非法行）/ slot_conflicts（同毫秒同槽不同值）
        self.code = code


@dataclass(frozen=True)
class SignalRow:
    """一行通过校验的信号记录：在 (line, channel) 槽位于 time_ms 写入 value。"""

    row_no: int  # 文件中的行号（首个非空行为表头，数据行从表头之后计）
    time_ms: int
    line: int
    channel: int
    value: int
    raw: str


#: 表头别名：规范化（去空白/下划线/括号、小写）后映射到四个必需字段。
_HEADER_ALIASES = {
    "time_ms": {
        "时间", "时间毫秒", "毫秒", "时间戳", "ms", "time", "timems",
        "timestamp", "t", "tms",
    },
    "line": {"线路", "line", "lineid", "线路id", "线路编号", "universe"},
    "channel": {
        "通道", "通道号", "channel", "channelid", "address", "addr",
        "ch", "dmx通道", "通道地址",
    },
    "value": {"值", "数值", "信号值", "dmx值", "value", "val", "level", "v"},
}


def _norm_header(cell: str) -> str:
    # 同时去掉半角/全角括号，使“时间(毫秒)/时间（毫秒）”都能命中“时间毫秒”
    stripped = "".join(cell.split()).replace("_", "")
    for ch in "()（）":
        stripped = stripped.replace(ch, "")
    return stripped.lower()


def _header_mapping(header: list[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for idx, cell in enumerate(header):
        norm = _norm_header(cell)
        if not norm:
            continue
        for field_name, aliases in _HEADER_ALIASES.items():
            if norm in aliases and field_name not in mapping:
                mapping[field_name] = idx
    return mapping


def parse_signal_csv(content: bytes) -> list[SignalRow]:
    """解析信号 CSV，返回按文件顺序排列的 SignalRow 列表。

    结构问题（空文件、非 UTF-8、缺表头、无数据行）抛 CsvStructureError；
    行级问题（时间/线路/通道/值非法、列数不足）汇总后抛 CsvRejectError，
    由调用方整表拒收（HTTP 422）并逐项展示。
    """
    if not content:
        raise CsvStructureError("文件为空，未收到任何内容")
    if len(content) > MAX_FILE_BYTES:
        raise CsvStructureError(f"文件过大（超过 {MAX_FILE_BYTES // 1000} KB），请拆分后重试")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CsvStructureError("文件不是有效的 UTF-8 编码 CSV，请另存为 UTF-8 后重试") from exc

    rows = list(csv.reader(io.StringIO(text)))
    header_idx = next((i for i, r in enumerate(rows) if any(c.strip() for c in r)), None)
    if header_idx is None:
        raise CsvStructureError("文件为空，未找到表头行")
    mapping = _header_mapping(rows[header_idx])
    missing_fields = [f for f in ("time_ms", "line", "channel", "value") if f not in mapping]
    if missing_fields:
        raise CsvStructureError(
            "表头缺少必需列，无法识别信号数据：需要包含 " + REQUIRED_HEADER_HINT
        )

    valid: list[SignalRow] = []
    errors: list[dict] = []
    max_idx = max(mapping.values())
    for i in range(header_idx + 1, len(rows)):
        cells = rows[i]
        row_no = i + 1
        if not any(c.strip() for c in cells):
            continue  # 跳过全空行
        raw = ",".join(cells)
        if len(cells) <= max_idx:
            errors.append(
                {"code": "bad_row", "row": row_no, "raw": raw,
                 "reasons": ["坏行：列数不足，无法解析"]}
            )
            continue

        parsed: dict[str, int] = {}
        reasons: list[str] = []
        for field_name, label in (
            ("time_ms", "时间"),
            ("line", "线路"),
            ("channel", "通道"),
            ("value", "值"),
        ):
            cell = cells[mapping[field_name]].strip()
            if not _INT_RE.fullmatch(cell):
                reasons.append(f"坏行：{label}不是整数（{cell!r}）")
            else:
                parsed[field_name] = int(cell)

        time_ms = parsed.get("time_ms")
        if time_ms is not None and time_ms < 0:
            reasons.append(f"非法时间：{time_ms}（时间必须为非负毫秒）")
        line = parsed.get("line")
        if line is not None and line not in VALID_LINE_IDS:
            reasons.append(f"非法线路：{line}（仅允许 1-4）")
        channel = parsed.get("channel")
        if channel is not None and not (1 <= channel <= LINE_SIZE):
            reasons.append(f"非法通道：{channel}（仅允许 1-512）")
        value = parsed.get("value")
        if value is not None and not (VALUE_MIN <= value <= VALUE_MAX):
            reasons.append(f"非法值：{value}（仅允许 0-255）")

        if reasons:
            errors.append({"code": "bad_row", "row": row_no, "raw": raw, "reasons": reasons})
            continue
        valid.append(
            SignalRow(
                row_no=row_no,
                time_ms=time_ms,  # type: ignore[arg-type]
                line=line,  # type: ignore[arg-type]
                channel=channel,  # type: ignore[arg-type]
                value=value,  # type: ignore[arg-type]
                raw=raw,
            )
        )

    if errors:
        raise CsvRejectError(
            f"CSV 存在 {len(errors)} 行非法数据（时间须为非负毫秒、线路 1-4、通道 1-512、值 0-255），"
            "已整表拒收，未执行回放。",
            errors,
            code="invalid_rows",
        )
    if not valid:
        raise CsvStructureError("没有任何数据行，无法回放（仅表头的文件不构成信号）")
    return valid


def _baseline_from_payload(plan_payload: dict) -> dict:
    """从已发布方案 payload 提取回放基准（快照事实或旧记录留存事实）。

    只使用 payload 内的发布时数据：allocations 给出灯具闭区间（快照或留存
    明细，均为发布时事实）；occupancy 内的 reservations 仅在有快照时可用，
    旧记录为 None（未知）。此函数绝不查询当前灯具/保留区。
    """
    from_snapshot = bool(plan_payload.get("from_snapshot", False))
    fixtures = []
    for a in plan_payload.get("allocations", []):
        item = {
            "fixture_id": a["fixture_id"],
            "name": a.get("name", f"#{a['fixture_id']}"),
            "line_id": a["line_id"],
            "start": a["start"],
            "end": a["end"],
            "channels": a["channels"],
        }
        if a.get("name_source"):
            item["name_source"] = a["name_source"]
        fixtures.append(item)

    reservations = []
    reservations_known = from_snapshot
    if reservations_known:
        for line in plan_payload.get("occupancy", []):
            for r in (line.get("reservations") or []):
                reservations.append(
                    {
                        "id": r["id"],
                        "line_id": line["line_id"],
                        "start": r["start"],
                        "end": r["end"],
                        "reason": r.get("reason", ""),
                    }
                )
    return {
        "from_snapshot": from_snapshot,
        "reservations_known": reservations_known,
        "fixtures": fixtures,
        "reservations": reservations,
    }


def _build_owner_maps(baseline: dict) -> tuple[dict, dict]:
    """构建 (线路,通道) -> 归属 的查找表。

    灯具槽位映射到 (fixture 索引, 相对通道)；保留区槽位映射到保留区对象。
    求解与发布保证灯具块互不重叠且不压保留区，二者不会竞争同一槽位。
    旧记录无保留区事实，只构建灯具表，其余槽位在判定时归为 unknown。
    """
    fixture_slots: dict[tuple[int, int], tuple[int, int]] = {}
    for idx, fx in enumerate(baseline["fixtures"]):
        if fx["line_id"] not in VALID_LINE_IDS:
            continue
        for ch in range(fx["start"], fx["end"] + 1):
            fixture_slots[(fx["line_id"], ch)] = (idx, ch - fx["start"] + 1)
    reservation_slots: dict[tuple[int, int], dict] = {}
    if baseline["reservations_known"]:
        for r in baseline["reservations"]:
            for ch in range(r["start"], r["end"] + 1):
                reservation_slots[(r["line_id"], ch)] = r
    return fixture_slots, reservation_slots


def _scan_conflicts(groups: dict[int, list[SignalRow]]) -> list[dict]:
    """找出每个毫秒内同一槽位出现不同值的全部冲突（按时间/线路/通道排序）。"""
    conflicts: list[dict] = []
    for time_ms in sorted(groups):
        per_slot: dict[tuple[int, int], list[SignalRow]] = {}
        for r in groups[time_ms]:
            per_slot.setdefault((r.line, r.channel), []).append(r)
        for (line, channel), slot_rows in sorted(per_slot.items()):
            distinct = {r.value for r in slot_rows}
            if len(slot_rows) > 1 and len(distinct) > 1:
                # 组内保持文件顺序：末值策略即取最后一行
                chosen = slot_rows[-1]
                conflicts.append(
                    {
                        "time_ms": time_ms,
                        "line_id": line,
                        "channel": channel,
                        "rows": [{"row": r.row_no, "value": r.value} for r in slot_rows],
                        "chosen_row": chosen.row_no,
                        "chosen_value": chosen.value,
                    }
                )
    return conflicts


def replay_signal(plan_payload: dict, rows: list[SignalRow], conflict_policy: str) -> dict:
    """按时间重建帧并给出逐槽位归属与逐灯具相对通道读数。

    帧按毫秒时间升序；同一毫秒的多行合为一帧，帧内严格按文件顺序写入
    （last 策略因此天然取末值）。槽位初值为 0，未更新值延续上一帧。
    返回结构包含基准、冲突标注（仅 last 策略下非空）、逐帧更新事件与
    各灯具最终读数；调用方据此渲染，本函数不写数据库。
    """
    if conflict_policy not in VALID_POLICIES:
        raise ValueError(f"未知冲突策略：{conflict_policy}")

    baseline = _baseline_from_payload(plan_payload)
    fixture_slots, reservation_slots = _build_owner_maps(baseline)

    # 按毫秒分组：保持帧内文件顺序（rows 已按文件顺序到达），帧间再按时间排序
    groups: dict[int, list[SignalRow]] = {}
    for r in rows:
        groups.setdefault(r.time_ms, []).append(r)

    conflicts = _scan_conflicts(groups)
    if conflict_policy == POLICY_REJECT and conflicts:
        raise CsvRejectError(
            f"CSV 在同一毫秒、同一槽位出现 {len(conflicts)} 处不同值，"
            "按所选“整表拒收”策略未执行回放；可改用“按文件顺序取末值”策略。",
            conflicts,
            code="slot_conflicts",
        )

    # 4 条线路 × 512 槽位，初值全 0；bytearray 天然限定 0-255
    grid = [bytearray(LINE_SIZE) for _ in range(4)]

    def _owner_for(line: int, channel: int) -> dict:
        hit = fixture_slots.get((line, channel))
        if hit is not None:
            idx, relative = hit
            fx = baseline["fixtures"][idx]
            owner = {
                "owner": "fixture",
                "fixture_id": fx["fixture_id"],
                "fixture_name": fx["name"],
                "relative_channel": relative,
            }
            if fx.get("name_source"):
                owner["fixture_name_source"] = fx["name_source"]
            return owner
        if baseline["reservations_known"]:
            res = reservation_slots.get((line, channel))
            if res is not None:
                return {
                    "owner": "reservation",
                    "reservation_id": res["id"],
                    "reservation_start": res["start"],
                    "reservation_end": res["end"],
                    "reason": res["reason"],
                }
            return {"owner": "unassigned"}
        # 旧记录无快照：灯具外的归属不可知，绝不能用当前保留区反推
        return {"owner": "unknown"}

    frames: list[dict] = []
    counts = {
        "data_rows": len(rows),
        "frames": 0,
        "fixture_updates": 0,
        "fixture_changed_updates": 0,
        "reservation_updates": 0,
        "unassigned_updates": 0,
        "unknown_updates": 0,
        "external_nonzero_writes": 0,
        "slot_conflicts": len(conflicts),
    }
    for time_ms in sorted(groups):
        updates: list[dict] = []
        for r in groups[time_ms]:
            li, ci = r.line - 1, r.channel - 1
            previous = grid[li][ci]
            entry = {
                "row": r.row_no,
                "time_ms": time_ms,
                "line_id": r.line,
                "channel": r.channel,
                "value": r.value,
                "previous_value": previous,
                "changed": r.value != previous,
            }
            entry.update(_owner_for(r.line, r.channel))
            grid[li][ci] = r.value
            updates.append(entry)

            owner = entry["owner"]
            if owner == "fixture":
                counts["fixture_updates"] += 1
                if entry["changed"]:
                    counts["fixture_changed_updates"] += 1
            else:
                if owner == "reservation":
                    counts["reservation_updates"] += 1
                elif owner == "unassigned":
                    counts["unassigned_updates"] += 1
                else:
                    counts["unknown_updates"] += 1
                if r.value != 0:
                    counts["external_nonzero_writes"] += 1
        frames.append({"time_ms": time_ms, "updates": updates})

    counts["frames"] = len(frames)
    times = sorted(groups)
    counts["time_start"] = times[0]
    counts["time_end"] = times[-1]
    counts["duration_ms"] = times[-1] - times[0]

    # 各灯具最终相对通道读数（稀疏：仅列非零通道；全部熄灭时为空列表）
    final_fixtures = []
    for fx in baseline["fixtures"]:
        readings = []
        if fx["line_id"] in VALID_LINE_IDS:
            li = fx["line_id"] - 1
            for ch in range(fx["start"], fx["end"] + 1):
                value = grid[li][ch - 1]
                if value != 0:
                    readings.append({"relative_channel": ch - fx["start"] + 1, "value": value})
        item = {
            "fixture_id": fx["fixture_id"],
            "name": fx["name"],
            "line_id": fx["line_id"],
            "start": fx["start"],
            "end": fx["end"],
            "channels": fx["channels"],
            "readings": readings,
        }
        if fx.get("name_source"):
            item["name_source"] = fx["name_source"]
        final_fixtures.append(item)

    return {
        "baseline": baseline,
        "conflict_policy": conflict_policy,
        # last 策略下逐项标注冲突与被采用的末值；reject 策略到达此处时必为空
        "conflicts": conflicts if conflict_policy == POLICY_LAST else [],
        "counts": counts,
        "frames": frames,
        "final_fixtures": final_fixtures,
    }
