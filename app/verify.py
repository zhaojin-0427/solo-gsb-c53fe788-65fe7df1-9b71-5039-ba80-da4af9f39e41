"""现场地址核验：解析现场 CSV 并与已发布方案的落点基准比对。

本模块为纯函数实现，不触碰数据库：基准（发布快照或旧记录留存明细还原出的
allocations）由调用方传入，核验过程不修改任何编排与发布数据。

裁决规则：
- 按灯具 ID 对齐，逐台计算闭区间 [start, start+channels-1]；
- 坏行、非法线路、越过 512 通道、重复 ID 的行一律隔离（quarantine），
  隔离行不参与对齐与重叠判定，但只要存在隔离行，整表结论绝不能为“一致”；
- 表头缺失/文件不可读等结构问题属于整表拒收，抛出 CsvStructureError；
- 名称与保留区不参与裁决（旧记录的历史名称、保留区本就未知）。
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field

#: 现场 CSV 允许出现的线路与单线通道上限（与编排规则一致）
VALID_LINE_IDS = {1, 2, 3, 4}
LINE_SIZE = 512

#: 上传文件大小上限，防止异常大文件拖垮服务
MAX_FILE_BYTES = 1_000_000

_INT_RE = re.compile(r"[+-]?\d+")


class CsvStructureError(ValueError):
    """整表结构问题（空文件、非 UTF-8、缺少必需表头）——整表拒收。"""


@dataclass
class SiteRow:
    """一行通过行级校验的现场记录。"""

    row_no: int  # 文件中的行号（首个非空行为表头，数据行从表头之后计）
    fixture_id: int
    line: int
    start: int
    channels: int
    raw: str

    @property
    def end(self) -> int:
        return self.start + self.channels - 1


@dataclass
class QuarantinedRow:
    """被隔离的异常行：不参与核对，但必须在报告中逐项列出。"""

    row_no: int
    fixture_id: int | None  # 灯具 ID 可解析且合法时保留，否则为 None
    raw: str
    reasons: list[str] = field(default_factory=list)


#: 表头别名：规范化（去空白与下划线、小写）后映射到四个必需字段。
#: 兼容本系统导出的地址表（灯具ID/线路/起始地址/通道数）——多余列一律忽略。
_HEADER_ALIASES = {
    "fixture_id": {"灯具id", "fixtureid", "id", "灯具编号", "fixture"},
    "line": {"线路", "line", "lineid", "线路id", "universe"},
    "start": {"起始地址", "起始", "start", "startaddress", "地址", "起始通道", "dmx地址"},
    "channels": {"通道数", "通道", "channels", "channelcount", "通道数量", "占用通道", "ch"},
}

REQUIRED_HEADER_HINT = "灯具ID,线路,起始地址,通道数"


def _norm_header(cell: str) -> str:
    return "".join(cell.split()).replace("_", "").lower()


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


def parse_on_site_csv(content: bytes) -> tuple[list[SiteRow], list[QuarantinedRow]]:
    """解析现场 CSV，返回（有效行, 隔离行）。

    结构问题（空文件、非 UTF-8、缺表头）抛 CsvStructureError，由调用方整表拒收；
    行级问题（坏行、非法线路、越过 512、重复 ID）隔离后继续核对。
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
    missing_fields = [f for f in ("fixture_id", "line", "start", "channels") if f not in mapping]
    if missing_fields:
        raise CsvStructureError(
            "表头缺少必需列，无法识别现场数据：需要包含 " + REQUIRED_HEADER_HINT +
            "（兼容本系统导出的地址表 CSV，多余列会被忽略）"
        )

    valid: list[SiteRow] = []
    quarantined: list[QuarantinedRow] = []
    max_idx = max(mapping.values())
    for i in range(header_idx + 1, len(rows)):
        cells = rows[i]
        row_no = i + 1
        if not any(c.strip() for c in cells):
            continue  # 跳过全空行
        raw = ",".join(cells)
        if len(cells) <= max_idx:
            quarantined.append(QuarantinedRow(row_no, None, raw, ["坏行：列数不足，无法解析"]))
            continue

        parsed: dict[str, int] = {}
        reasons: list[str] = []
        for field_name, label in (
            ("fixture_id", "灯具ID"),
            ("line", "线路"),
            ("start", "起始地址"),
            ("channels", "通道数"),
        ):
            cell = cells[mapping[field_name]].strip()
            if not _INT_RE.fullmatch(cell):
                reasons.append(f"坏行：{label}不是整数（{cell!r}）")
            else:
                parsed[field_name] = int(cell)

        fid = parsed.get("fixture_id")
        if fid is not None and fid < 1:
            reasons.append(f"非法灯具ID：{fid}")
        line = parsed.get("line")
        if line is not None and line not in VALID_LINE_IDS:
            reasons.append(f"非法线路：{line}（仅允许 1-4）")
        start = parsed.get("start")
        if start is not None and start < 1:
            reasons.append(f"坏行：起始地址越界（{start}，最小为 1）")
        channels = parsed.get("channels")
        if channels is not None and channels < 1:
            reasons.append(f"坏行：非法通道数（{channels}，最小为 1）")
        if start is not None and start >= 1 and channels is not None and channels >= 1:
            end = start + channels - 1
            if end > LINE_SIZE:
                reasons.append(f"越过 512 通道：区间 {start}-{end} 超出线路容量")

        if reasons:
            quarantined.append(
                QuarantinedRow(row_no, fid if fid is not None and fid >= 1 else None, raw, reasons)
            )
            continue
        valid.append(SiteRow(row_no, fid, line, start, channels, raw))  # type: ignore[arg-type]

    # 重复 ID：同一灯具出现多行有效记录时无法裁决，全部隔离
    by_id: dict[int, list[SiteRow]] = {}
    for r in valid:
        by_id.setdefault(r.fixture_id, []).append(r)
    dup_ids = {fid for fid, rs in by_id.items() if len(rs) > 1}
    if dup_ids:
        kept = []
        for r in valid:
            if r.fixture_id in dup_ids:
                quarantined.append(
                    QuarantinedRow(
                        r.row_no,
                        r.fixture_id,
                        r.raw,
                        [f"重复 ID：灯具 {r.fixture_id} 在表中出现多次，无法裁决"],
                    )
                )
            else:
                kept.append(r)
        valid = kept
    quarantined.sort(key=lambda q: q.row_no)
    return valid, quarantined


def verify_against_baseline(
    baseline: list[dict],
    valid_rows: list[SiteRow],
    quarantined: list[QuarantinedRow],
) -> dict:
    """将有效现场行与基准（发布快照/旧记录留存落点）按 ID 对齐比对。

    baseline 为已发布方案 payload 的 allocations（含 fixture_id、line_id、start、
    end、channels，名称仅用于展示、不参与裁决）。返回逐项清单与整表结论：
    只要存在隔离行、缺失、额外、不符或现场重叠，结论即非“一致”。
    """
    base_by_id = {a["fixture_id"]: a for a in baseline}
    site_by_id = {r.fixture_id: r for r in valid_rows}
    # 被隔离行涉及的灯具 ID 已有明确异常记录，不再重复计入“缺失”
    quarantined_ids = {q.fixture_id for q in quarantined if q.fixture_id is not None}

    def _base_item(a: dict) -> dict:
        item = {
            "fixture_id": a["fixture_id"],
            "name": a.get("name", f"#{a['fixture_id']}"),
            "line_id": a["line_id"],
            "start": a["start"],
            "end": a["end"],
            "channels": a["channels"],
        }
        # 旧记录的名称为当前值/不可用，透传来源标注（不参与裁决，仅供展示）
        if a.get("name_source"):
            item["name_source"] = a["name_source"]
        return item

    missing = [
        _base_item(a)
        for fid, a in sorted(base_by_id.items())
        if fid not in site_by_id and fid not in quarantined_ids
    ]
    extra = [
        {
            "row": r.row_no,
            "fixture_id": r.fixture_id,
            "line": r.line,
            "start": r.start,
            "end": r.end,
            "channels": r.channels,
        }
        for fid, r in sorted(site_by_id.items())
        if fid not in base_by_id
    ]

    mismatches: list[dict] = []
    matched = 0
    for fid in sorted(base_by_id.keys() & site_by_id.keys()):
        a = base_by_id[fid]
        r = site_by_id[fid]
        diffs = []
        if r.line != a["line_id"]:
            diffs.append({"field": "line", "expected": a["line_id"], "actual": r.line})
        if r.start != a["start"]:
            diffs.append({"field": "start", "expected": a["start"], "actual": r.start})
        if r.channels != a["channels"]:
            diffs.append({"field": "channels", "expected": a["channels"], "actual": r.channels})
        if not diffs:
            matched += 1
            continue
        item = {
            "fixture_id": fid,
            "name": a.get("name", f"#{fid}"),
            "row": r.row_no,
            "diffs": diffs,
            "expected": {
                "line": a["line_id"], "start": a["start"], "end": a["end"], "channels": a["channels"],
            },
            "actual": {"line": r.line, "start": r.start, "end": r.end, "channels": r.channels},
        }
        if a.get("name_source"):
            item["name_source"] = a["name_source"]
        mismatches.append(item)

    # 现场区间重叠：仅在同一条线路的有效行之间两两判定（闭区间相交）
    overlaps: list[dict] = []
    by_line: dict[int, list[SiteRow]] = {}
    for r in valid_rows:
        by_line.setdefault(r.line, []).append(r)
    for line, rows_on_line in sorted(by_line.items()):
        ordered = sorted(rows_on_line, key=lambda x: (x.start, x.fixture_id))
        for i, left in enumerate(ordered):
            for right in ordered[i + 1:]:
                if right.start > left.end:
                    break  # 按起始排序，之后的行不可能再与 left 相交
                overlaps.append(
                    {
                        "line": line,
                        "a": {
                            "fixture_id": left.fixture_id,
                            "start": left.start,
                            "end": left.end,
                            "row": left.row_no,
                        },
                        "b": {
                            "fixture_id": right.fixture_id,
                            "start": right.start,
                            "end": right.end,
                            "row": right.row_no,
                        },
                        "overlap_start": max(left.start, right.start),
                        "overlap_end": min(left.end, right.end),
                    }
                )

    consistent = not (missing or extra or mismatches or overlaps or quarantined)
    return {
        "verdict": "consistent" if consistent else "inconsistent",
        "consistent": consistent,
        "counts": {
            "baseline_fixtures": len(base_by_id),
            "valid_rows": len(valid_rows),
            "quarantined_rows": len(quarantined),
            "checked": len(base_by_id.keys() & site_by_id.keys()),
            "matched": matched,
            "missing": len(missing),
            "extra": len(extra),
            "mismatched": len(mismatches),
            "overlap_pairs": len(overlaps),
        },
        "quarantined": [
            {"row": q.row_no, "fixture_id": q.fixture_id, "raw": q.raw, "reasons": q.reasons}
            for q in quarantined
        ],
        "missing": missing,
        "extra": extra,
        "mismatches": mismatches,
        "overlaps": overlaps,
    }
