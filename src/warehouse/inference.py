"""仓内作业分析 — 推断引擎（纯标准库）。

推断原则
--------
1. 扫码只给“某时某货位”的观测点。
2. 同货位连续扫码 -> 驻留可确证（observed dwell）。
3. 两次扫码在不同货位之间：
   - 最早到达 B = t_i + 最短步行
   - 最晚离开 A = t_j - 最短步行
   - 驻留只取最短驻留下限，其后只是 **可能仍在本通道**，不硬算
   - 最晚离开 A 与最早到达 B 之间若还有空档，单列为“位置不可确定”，
     绝不归到 A 货位上。
4. 间隔超过 long_gap_s -> 位置未知段，不再按“仍在最后货位”处理。
5. 容量冲突用扫描线求“同时占用 > 容量”，区分
   - confirmed：占用人数（只算观测/确证）超过容量
   - possible：叠加推测区间后才超容量
   成因只给候选解释（任务交叉 / 数据缺失 / 推测重叠），不作因果断言。
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

from .models import ScanEvent, Task, Topology
from .topology import WalkGraph

DEFAULTS = {
    "walk_speed_mps": 1.15,      # 步行速度（米/秒）
    "dwell_min_s": 18.0,         # 每次扫码对应的最短驻留
    "repeat_scan_window_s": 8.0,  # 同货位重复上报合并窗口
    "same_loc_dwell_s": 90.0,    # 同货位连续扫码，可确证驻留的上限
    "long_gap_s": 420.0,         # 超过该间隔视为位置未知
    "walk_slack": 1.30,          # 行走时间估计的放宽系数
    "gap_alert_s": 120.0,        # 超出预计步行多久算数据缺口
}

PARAM_LABELS = {
    "walk_speed_mps": "步行速度（米/秒）",
    "dwell_min_s": "扫码最短驻留（秒）",
    "repeat_scan_window_s": "重复扫码合并窗口（秒）",
    "same_loc_dwell_s": "同货位驻留确证上限（秒）",
    "long_gap_s": "位置未知阈值（秒）",
    "walk_slack": "行走时间放宽系数",
    "gap_alert_s": "扫描缺口告警（秒）",
}

CAUSE_LABELS = {
    "task_cross": "任务分配交叉",
    "data_gap": "扫描数据缺失",
    "possible": "推测区间重叠",
    "topology": "拓扑改版影响",
}

SEGMENT_LABELS = {
    "dwell": "确切驻留",
    "possible": "可能位置",
    "unknown": "位置未知",
    "gap": "扫描缺口",
    "break": "休息/离岗",
}

CONFIDENCE_LABELS = {
    "observed": "观测",
    "inferred": "推断",
    "unknown": "未知",
}

# 合并相邻超容量区间时，同一句说明会按子区间重复出现；这些正则把“N 人只能推测”
# 与“子区间很短”两类说明压成一句，避免明细里堆出十几条近义句子。
_INFERRED_REASON = re.compile(r"^(?:最多 )?(\d+) 人的位置只能推测，叠加后才超过容量")
_SHORT_ALT = re.compile(r"^(?:本区间仅 (\d+) 秒|合并前含若干短子区间（最短 (\d+) 秒）)")
_SAME_WAVE_POSSIBLE = "同波次多个任务在同一通道并行，观测人数未超容量，叠加推测后才超过"
_SAME_WAVE_CONFIRMED = "同波次批量发单的并行作业，观测人数已超过容量"
_GENERIC_CONFIRMED = "观测到的同时在场人数超过通道容量"


def _compact_reasons(reasons: Iterable[str], overlap_kind: str = "possible") -> tuple[str, ...]:
    out: list[str] = []
    inferred: list[int] = []
    for text in reasons:
        match = _INFERRED_REASON.match(text)
        if match:
            inferred.append(int(match.group(1)))
            continue
        out.append(text)
    if inferred:
        peak = max(inferred)
        if len(set(inferred)) > 1:
            out.append(f"最多 {peak} 人的位置只能推测，叠加后才超过容量（随子区间变化）")
        else:
            out.append(f"{peak} 人的位置只能推测，叠加后才超过容量")
    # 合并前的子区间可能一段只靠推测、一段已确认超容量。整段按合并后的性质陈述，
    # 否则同一条明细里会同时出现“观测未超容量”与“观测已超容量”两句互相打架的话。
    if _SAME_WAVE_POSSIBLE in out or _SAME_WAVE_CONFIRMED in out:
        keep = _SAME_WAVE_CONFIRMED if overlap_kind == "confirmed" else _SAME_WAVE_POSSIBLE
        out = [t for t in out if t not in (_SAME_WAVE_POSSIBLE, _SAME_WAVE_CONFIRMED, _GENERIC_CONFIRMED)]
        out.append(keep)
    return tuple(dict.fromkeys(out))


def _compact_alternatives(alts: Iterable[str]) -> tuple[str, ...]:
    out: list[str] = []
    short: list[int] = []
    for text in alts:
        match = _SHORT_ALT.match(text)
        if match:
            short.append(int(match.group(1) or match.group(2)))
            continue
        out.append(text)
    if short:
        out.append(f"合并前含若干短子区间（最短 {min(short)} 秒），可能只是扫码/通行瞬间的短暂交汇，不代表持续拥堵")
    return tuple(dict.fromkeys(out))


@dataclass
class Segment:
    picker: str
    entity: str
    zone: str
    start: float
    end: float
    kind: str          # dwell | possible | unknown | gap
    confidence: str    # observed | inferred | unknown
    location: str = ""
    task_id: str = ""
    note: str = ""
    scan_ids: tuple[str, ...] = ()
    cause: str = ""

    def as_dict(self) -> dict:
        return {
            "picker": self.picker,
            "entity": self.entity,
            "zone": self.zone,
            "start": round(self.start, 1),
            "end": round(self.end, 1),
            "kind": self.kind,
            "kind_label": SEGMENT_LABELS.get(self.kind, self.kind),
            "confidence": self.confidence,
            "confidence_label": CONFIDENCE_LABELS.get(self.confidence, self.confidence),
            "location": self.location,
            "task": self.task_id,
            "note": self.note,
            "scan_ids": list(self.scan_ids),
            "cause": self.cause,
            "duration": round(max(0.0, self.end - self.start), 1),
        }


@dataclass
class Conflict:
    entity: str
    zone: str
    start: float
    end: float
    people: tuple[str, ...]
    tasks: tuple[str, ...]
    capacity: int
    overlap_kind: str  # confirmed | possible
    cause_seconds: dict[str, float]  # 每种成因在本区间内的时长，多标签按占时排序
    reasons: tuple[str, ...]
    alternatives: tuple[str, ...] = ()
    peak: int = 0

    @property
    def causes(self) -> tuple[str, ...]:
        order = {"task_cross": 0, "data_gap": 1, "possible": 2}
        return tuple(sorted(self.cause_seconds, key=lambda k: (-self.cause_seconds[k], order.get(k, 9))))

    @property
    def cause(self) -> str:
        causes = self.causes
        return causes[0] if causes else "possible"

    def as_dict(self) -> dict:
        return {
            "entity": self.entity,
            "zone": self.zone,
            "start": round(self.start, 1),
            "end": round(self.end, 1),
            "people": list(self.people),
            "tasks": list(self.tasks),
            "capacity": self.capacity,
            "overlap_kind": self.overlap_kind,
            "cause": self.cause,
            "cause_label": CAUSE_LABELS.get(self.cause, self.cause),
            "causes": list(self.causes),
            "cause_labels": [CAUSE_LABELS.get(c, c) for c in self.causes],
            "cause_seconds": {c: round(v, 1) for c, v in self.cause_seconds.items()},
            "detail": "；".join(self.reasons),
            "reasons": list(self.reasons),
            "alternatives": list(self.alternatives),
            "peak": self.peak,
            "duration": round(max(0.0, self.end - self.start), 1),
        }


def _is_repeat(a: ScanEvent, b: ScanEvent) -> bool:
    return (
        a.picker_id == b.picker_id
        and a.location_id == b.location_id
        and b.scan_kind in ("repeat", "confirm", "pick")
    )


def dedupe_scans(scans: list[ScanEvent], params: dict) -> tuple[list[ScanEvent], list[dict]]:
    """合并同货位、短间隔内的重复上报；保留 dropped/kept 审计记录。"""
    ordered = sorted(scans, key=lambda s: (s.picker_id, s.ts, s.id))
    kept: list[ScanEvent] = []
    merged: list[dict] = []
    for scan in ordered:
        parent = None
        if scan.scan_kind != "manual":
            for prev in reversed(kept):
                if prev.picker_id != scan.picker_id:
                    break
                if scan.ts - prev.ts > params["repeat_scan_window_s"]:
                    break
                if _is_repeat(prev, scan):
                    parent = prev
                    break
        if parent is None:
            kept.append(scan)
            continue
        merged.append({
            "dropped": scan.id,
            "kept": parent.id,
            "picker": scan.picker_id,
            "location": scan.location_id,
            "ts": round(scan.ts, 1),
            "gap_s": round(scan.ts - parent.ts, 2),
            "reason": "同一货位重复上报" if scan.scan_kind == "repeat" else "同货位确认键",
        })
    return kept, merged


def resolve_location(scan: ScanEvent, topology: Topology) -> tuple[str, str, str]:
    """货位 ID -> (实体, 区域, 规范化货位)；处理改版后的旧条码别名。"""
    loc_id = scan.location_id
    if loc_id in topology.locations:
        loc = topology.locations[loc_id]
        # 事件发生时的区域以拓扑版本为准，避免旧事件被新分区静默改写
        return loc.entity, loc.zone, loc.id
    target = topology.aliases.get(loc_id)
    if target and target in topology.locations:
        loc = topology.locations[target]
        return loc.entity, loc.zone, target
    if target and target in topology.entities:
        return target, topology.entities[target].zone, loc_id
    return "UNKNOWN", "unknown", loc_id


class InferenceEngine:
    def __init__(self, topology: Topology, tasks: Iterable[Task], scans: Iterable[ScanEvent],
                 params: dict | None = None):
        self.params = {**DEFAULTS, **(params or {})}
        self.topology = topology
        self.tasks = {t.id: t for t in tasks}
        self.raw_scans = sorted(scans, key=lambda s: (s.ts, s.picker_id))
        self.scans, self.merged = dedupe_scans(self.raw_scans, self.params)
        self.graph = WalkGraph(topology)
        self._segments: list[Segment] | None = None

    # -- 步行时间 ---------------------------------------------------------
    def walk_seconds(self, a: str, b: str) -> float:
        try:
            meters = self.graph.location_distance(a, b)
        except (KeyError, ValueError):
            return 180.0
        return meters / max(0.2, self.params["walk_speed_mps"])

    def _task_of(self, scan: ScanEvent) -> Task | None:
        return self.tasks.get(scan.task_id) if scan.task_id else None

    # -- 分段 -------------------------------------------------------------
    def segments(self) -> list[Segment]:
        if self._segments is not None:
            return self._segments
        by_picker: dict[str, list[ScanEvent]] = defaultdict(list)
        for scan in self.scans:
            by_picker[scan.picker_id].append(scan)
        segments: list[Segment] = []
        for picker, scans in by_picker.items():
            segments.extend(self._segments_for_picker(picker, scans))
        self._segments = sorted(segments, key=lambda s: (s.start, s.picker))
        return self._segments

    def _segments_for_picker(self, picker: str, scans: list[ScanEvent]) -> list[Segment]:
        out: list[Segment] = []
        if not scans:
            return out
        p = self.params
        first = scans[0]
        entity, zone, _ = resolve_location(first, self.topology)
        out.append(Segment(
            picker=picker, entity="UNKNOWN", zone="unknown",
            start=first.ts - 60, end=first.ts, kind="unknown", confidence="unknown",
            note="首次扫码之前，位置不可知", scan_ids=(first.id,),
        ))
        for i, scan in enumerate(scans):
            entity, zone, loc = resolve_location(scan, self.topology)
            task = self._task_of(scan)
            if i + 1 >= len(scans):
                end = scan.ts + max(p["dwell_min_s"], 20.0)
                out.append(Segment(
                    picker=picker, entity=entity, zone=zone, start=scan.ts, end=end,
                    kind="dwell", confidence="observed", location=loc,
                    task_id=scan.task_id or "", note="末次扫码，之后无记录",
                    scan_ids=(scan.id,),
                ))
                continue

            nxt = scans[i + 1]
            gap = nxt.ts - scan.ts
            next_entity, next_zone, next_loc = resolve_location(nxt, self.topology)
            same_loc = loc == next_loc
            walk_min = self.walk_seconds(scan.location_id, nxt.location_id)
            dwell_max = p["dwell_min_s"]

            if same_loc and gap <= p["same_loc_dwell_s"]:
                out.append(Segment(
                    picker=picker, entity=entity, zone=zone, start=scan.ts, end=nxt.ts,
                    kind="dwell", confidence="observed", location=loc,
                    task_id=scan.task_id or "",
                    note="同货位连续扫码，驻留可确证", scan_ids=(scan.id, nxt.id),
                ))
                continue

            if gap > p["long_gap_s"]:
                # 长间隔：只保留最短驻留，其余为未知，不继续算在最后货位
                dwell_end = scan.ts + min(dwell_max, gap)
                out.append(Segment(
                    picker=picker, entity=entity, zone=zone, start=scan.ts, end=dwell_end,
                    kind="dwell", confidence="observed", location=loc,
                    task_id=scan.task_id or "", note="末次有效驻留", scan_ids=(scan.id,),
                ))
                out.append(Segment(
                    picker=picker, entity="UNKNOWN", zone="unknown", start=dwell_end,
                    end=nxt.ts, kind="unknown", confidence="unknown",
                    note=f"{gap / 60:.1f} 分钟无扫描，位置未知（休息/离线）",
                    scan_ids=(scan.id, nxt.id),
                    cause="data_gap",
                ))
                continue

            if same_loc:
                confirmed_end = min(scan.ts + p["same_loc_dwell_s"], nxt.ts)
                out.append(Segment(
                    picker=picker, entity=entity, zone=zone, start=scan.ts, end=confirmed_end,
                    kind="dwell", confidence="observed", location=loc,
                    task_id=scan.task_id or "", note="同货位连续扫码，前段可确证",
                    scan_ids=(scan.id, nxt.id),
                ))
                if confirmed_end < nxt.ts:
                    out.append(Segment(
                        picker=picker, entity=entity, zone=zone, start=confirmed_end, end=nxt.ts,
                        kind="possible", confidence="inferred", location=loc,
                        task_id=scan.task_id or "",
                        note="同货位但长时间无新扫码，可能已离开", scan_ids=(scan.id, nxt.id),
                    ))
                continue

            # 不同货位
            dwell_end = min(scan.ts + dwell_max, nxt.ts)
            # 最短路按名义速度走需 walk_min；若实际更快（slack 系数），可晚些离开
            fast_walk = walk_min / max(1.0, p["walk_slack"])
            if gap < fast_walk:
                # 间隔短于最短步行：不推断具体货位，只标“可能位置”
                out.append(Segment(
                    picker=picker, entity=entity, zone=zone, start=scan.ts, end=nxt.ts,
                    kind="possible", confidence="inferred", location=loc,
                    task_id=scan.task_id or "",
                    note="扫码间隔短于最短步行，可能是重复/代扫/坐标误差",
                    scan_ids=(scan.id, nxt.id), cause="data_gap",
                ))
                continue
            latest_departure = nxt.ts - fast_walk
            earliest_arrival = dwell_end + fast_walk
            out.append(Segment(
                picker=picker, entity=entity, zone=zone, start=scan.ts, end=dwell_end,
                kind="dwell", confidence="observed", location=loc,
                task_id=scan.task_id or "", note="扫码所在货位", scan_ids=(scan.id,),
            ))
            if latest_departure > dwell_end:
                # 可能仍在本通道（也可能早已离开）
                out.append(Segment(
                    picker=picker, entity=entity, zone=zone, start=dwell_end,
                    end=latest_departure, kind="possible", confidence="inferred", location=loc,
                    task_id=scan.task_id or "",
                    note="最晚离开本通道前的可能驻留", scan_ids=(scan.id, nxt.id),
                ))
            if earliest_arrival < nxt.ts and next_entity != "UNKNOWN":
                out.append(Segment(
                    picker=picker, entity=next_entity, zone=next_zone, start=earliest_arrival,
                    end=nxt.ts, kind="possible", confidence="inferred", location=next_loc,
                    task_id=nxt.task_id or "",
                    note="最早到达下一通道后的可能驻留", scan_ids=(scan.id, nxt.id),
                ))
            mid_start = max(dwell_end, latest_departure)
            mid_end = min(earliest_arrival, nxt.ts)
            if mid_start < mid_end:
                out.append(Segment(
                    picker=picker, entity="UNKNOWN", zone="unknown", start=mid_start,
                    end=mid_end, kind="possible", confidence="inferred",
                    note="两次扫码之间，在途或位置不可确定", scan_ids=(scan.id, nxt.id),
                ))
            # 数据缺口：间隔显著超过预计步行 + 告警阈值
            if gap > walk_min * p["walk_slack"] + p["gap_alert_s"]:
                out.append(Segment(
                    picker=picker, entity=entity, zone=zone,
                    start=scan.ts, end=min(scan.ts + min(dwell_max * 2, gap), nxt.ts),
                    kind="gap", confidence="inferred", location=loc,
                    task_id=scan.task_id or "",
                    note="间隔远大于步行时间：可能有确认扫码缺失或设备离线",
                    scan_ids=(scan.id, nxt.id), cause="data_gap",
                ))
        return out

    # -- 占用汇总 ---------------------------------------------------------
    def occupancy(self, start: float | None = None, end: float | None = None) -> list[dict]:
        segs = self.segments()
        if start is not None:
            segs = [s for s in segs if s.end > start]
        if end is not None:
            segs = [s for s in segs if s.start < end]
        return [s.as_dict() for s in segs]

    # -- 容量冲突 ---------------------------------------------------------
    def conflicts(self, start: float | None = None, end: float | None = None) -> list[dict]:
        segs = [s for s in self.segments() if s.kind in ("dwell", "possible", "gap")]
        if not segs:
            return []
        lo = start if start is not None else min(s.start for s in segs)
        hi = end if end is not None else max(s.end for s in segs)
        segs = [s for s in segs if s.end > lo and s.start < hi]
        by_entity: dict[str, list[Segment]] = defaultdict(list)
        for seg in segs:
            by_entity[seg.entity].append(seg)
        conflicts: list[Conflict] = []
        for entity, items in by_entity.items():
            if entity not in self.topology.entities:
                continue
            capacity = self.topology.entities[entity].capacity
            found = self._sweep_entity(entity, capacity, items, lo, hi)
            conflicts.extend(found)
        return [c.as_dict() for c in self._merge_conflicts(conflicts)]

    def _sweep_entity(self, entity: str, capacity: int, items: list[Segment],
                      lo: float, hi: float) -> list[Conflict]:
        """扫描线：每个基本区间统计观测人数与推测人数。"""
        points = sorted({max(lo, s.start) for s in items} | {min(hi, s.end) for s in items})
        found: list[Conflict] = []
        for a, b in zip(points, points[1:]):
            if b <= a:
                continue
            mid = (a + b) / 2
            covered = [s for s in items if s.start <= mid < s.end]
            observed: dict[str, list[Segment]] = defaultdict(list)
            inferred: dict[str, list[Segment]] = defaultdict(list)
            for seg in covered:
                target = observed if seg.kind in ("dwell",) else inferred
                target[seg.picker].append(seg)
            obs_people = set(observed)
            all_people = obs_people | set(inferred)
            if len(obs_people) > capacity:
                kind = "confirmed"
            elif len(all_people) > capacity:
                kind = "possible"
            else:
                continue
            relevant = {p: observed.get(p, []) + inferred.get(p, []) for p in all_people}
            causes, reasons, alts = self._explain(entity, relevant, kind, observed, inferred, b - a)
            tasks = tuple(sorted({s.task_id for segs in relevant.values() for s in segs if s.task_id}))
            found.append(Conflict(
                entity=entity, zone=self.topology.entities[entity].zone,
                start=a, end=b, people=tuple(sorted(all_people)), tasks=tasks,
                capacity=capacity, overlap_kind=kind,
                cause_seconds={c: b - a for c in causes},
                reasons=tuple(reasons), alternatives=tuple(alts), peak=len(all_people),
            ))
        return found

    def _explain(self, entity: str, relevant: dict[str, list[Segment]],
                 overlap_kind: str, observed: dict[str, list[Segment]],
                 inferred: dict[str, list[Segment]], seconds: float):
        """给出一段超容量区间的候选成因（可多个）与替代解释。

        成因划分只看证据类型，不推断动机：
        - task_cross：观测到的人来自不同区域/不同波次的任务，属于发单造成的交汇
        - data_gap：超容量依赖被扫描缺口拉长的区间，而不是观测到的同时在场
        - possible：超容量仅由推测区间叠加造成
        """
        reasons: list[str] = []
        alts: list[str] = []
        segs = [s for group in relevant.values() for s in group]
        short = seconds < 45
        obs_segs = [s for group in observed.values() for s in group]
        task_ids = {s.task_id for s in segs if s.task_id}
        obs_task_ids = {s.task_id for s in obs_segs if s.task_id}
        zones = {self.tasks[t].zone for t in task_ids if t in self.tasks}
        obs_zones = {self.tasks[t].zone for t in obs_task_ids if t in self.tasks}
        waves = {self.tasks[t].wave_id for t in obs_task_ids if t in self.tasks}
        gap_like = [s for s in segs if s.kind == "gap"]
        only_inferred = [p for p in relevant if p not in observed]

        causes: list[str] = []
        if len(obs_task_ids) > 1:
            if len(obs_zones) > 1:
                causes.append("task_cross")
                reasons.append("观测到快拣与冷链任务在同通道同时作业，来自不同区域的发单批次")
                alts.append("交汇也可能来自路径选择或复核区折返，不必然是发单顺序造成")
            elif len(waves) > 1:
                causes.append("task_cross")
                reasons.append(f"同区 {len(waves)} 个发单波次的任务同时在场，前一波未清空后一波已进场")
                alts.append("波次重叠可能是人员速度差异造成的排队，而非发单节奏问题")
            else:
                if overlap_kind == "confirmed":
                    reasons.append("同波次批量发单的并行作业，观测人数已超过容量")
                else:
                    reasons.append("同波次多个任务在同一通道并行，观测人数未超容量，叠加推测后才超过")
                alts.append("若通道容量配置偏小，这类并行会被误判成冲突")
        if gap_like:
            causes.append("data_gap")
            reasons.append("区间内有扫描缺口，占用时长可能被拉长")
            alts.append("设备离线与确认扫码缺失都会造成同样现象，无法仅凭数据区分")
        if only_inferred:
            causes.append("possible")
            reasons.append(f"{len(only_inferred)} 人的位置只能推测，叠加后才超过容量")
            alts.append("若实际行走更快，这些人可能并未同时在该通道")
        if not causes:
            causes.append("task_cross")
            reasons.append("观测到的同时在场人数超过通道容量")
        cancelled = [tid for tid in task_ids if tid in self.tasks
                     and self.tasks[tid].cancelled_at is not None]
        if cancelled:
            reasons.append(f"涉及已取消任务（{', '.join(sorted(cancelled))}），占用可能来自取消后的折返")
        alts.append("容量是配置值，若实物通道更宽，统计口径会高估冲突")
        if short:
            alts.append(f"本区间仅 {seconds:.0f} 秒，可能是扫码/通行瞬间的短暂交汇，不代表持续拥堵")
        return causes, reasons, alts

    def _merge_conflicts(self, conflicts: list[Conflict]) -> list[Conflict]:
        if not conflicts:
            return []
        conflicts.sort(key=lambda c: (c.entity, c.start))
        merged: list[Conflict] = []
        for c in conflicts:
            if merged:
                prev = merged[-1]
                if prev.entity == c.entity and c.start <= prev.end + 1.5:
                    secs = dict(prev.cause_seconds)
                    for k, v in c.cause_seconds.items():
                        secs[k] = secs.get(k, 0.0) + v
                    kind = "confirmed" if "confirmed" in (prev.overlap_kind, c.overlap_kind) else "possible"
                    merged[-1] = Conflict(
                        entity=c.entity, zone=c.zone, start=prev.start, end=max(prev.end, c.end),
                        people=tuple(sorted(set(prev.people) | set(c.people))),
                        tasks=tuple(sorted(set(prev.tasks) | set(c.tasks))),
                        capacity=c.capacity,
                        overlap_kind=kind,
                        cause_seconds=secs,
                        reasons=_compact_reasons(list(prev.reasons) + list(c.reasons), kind),
                        alternatives=_compact_alternatives(list(prev.alternatives) + list(c.alternatives)),
                        peak=max(prev.peak, c.peak),
                    )
                    continue
            merged.append(c)
        return merged

    # -- 区域视图 ---------------------------------------------------------
    def zone_occupancy(self, start: float, end: float, step: float = 60.0) -> dict:
        segs = [s for s in self.segments() if s.end > start and s.start < end]
        entities = list(self.topology.entities.values())
        buckets = []
        t = start
        while t < end:
            b_end = min(t + step, end)
            row = {"t": t, "end": b_end, "entities": {}, "unknown": 0}
            for ent in entities:
                observed, possible = set(), set()
                for seg in segs:
                    if seg.entity != ent.id or not (seg.start < b_end and seg.end > t):
                        continue
                    if seg.kind == "dwell":
                        observed.add(seg.picker)
                    elif seg.kind in ("possible", "gap"):
                        possible.add(seg.picker)
                row["entities"][ent.id] = {
                    "observed": len(observed),
                    "possible": len(possible - observed),
                }
            row["unknown"] = len({s.picker for s in segs
                                  if s.entity == "UNKNOWN" and s.start < b_end and s.end > t})
            buckets.append(row)
            t = b_end
        return {"buckets": buckets, "step": step}

    def picker_timeline(self, start: float | None = None, end: float | None = None) -> dict:
        by_picker: dict[str, list[dict]] = defaultdict(list)
        for seg in self.segments():
            if start is not None and seg.end <= start:
                continue
            if end is not None and seg.start >= end:
                continue
            by_picker[seg.picker].append(seg.as_dict())
        return dict(sorted(by_picker.items()))

    def entity_occupancy_series(self, start: float, end: float, step: float = 30.0) -> dict:
        segs = [s for s in self.segments() if s.end > start and s.start < end]
        entities = list(self.topology.entities.values())
        series = []
        t = start
        while t < end:
            b_end = min(t + step, end)
            for ent in entities:
                obs, poss = set(), set()
                for seg in segs:
                    if seg.entity != ent.id or not (seg.start < b_end and seg.end > t):
                        continue
                    if seg.kind == "dwell":
                        obs.add(seg.picker)
                    elif seg.kind in ("possible", "gap"):
                        poss.add(seg.picker)
                if obs or poss:
                    series.append({
                        "t": t, "entity": ent.id, "capacity": ent.capacity,
                        "observed": len(obs), "possible": len(poss - obs),
                    })
            t = b_end
        return {"series": series, "step": step}

    # -- 汇总与数据质量 ---------------------------------------------------
    def summary(self, start: float, end: float) -> dict:
        segs = [s for s in self.segments() if s.end > start and s.start < end]
        total_by_kind: dict[str, float] = defaultdict(float)
        for s in segs:
            total_by_kind[s.kind] += max(0.0, min(s.end, end) - max(s.start, start))
        scans_in_window = [s for s in self.scans if start <= s.ts <= end]
        conflicts = self.conflicts(start, end)
        by_cause: dict[str, int] = defaultdict(int)
        for c in conflicts:
            for tag in c["causes"]:
                by_cause[tag] += 1
        pickers_on_floor = sorted({s.picker for s in segs
                                   if s.entity not in ("UNKNOWN",) and s.kind != "unknown"})
        return {
            "scans": len(scans_in_window),
            "merged_duplicates": len([m for m in self.merged if start <= m["ts"] <= end]),
            "pickers": sorted({s.picker_id for s in scans_in_window}) or pickers_on_floor,
            "segments": len(segs),
            "time_by_kind": {k: round(v, 1) for k, v in sorted(total_by_kind.items())},
            "conflict_count": len(conflicts),
            "conflicts_by_cause": dict(by_cause),
            "confirmed_conflicts": len([c for c in conflicts if c["overlap_kind"] == "confirmed"]),
            "possible_conflicts": len([c for c in conflicts if c["overlap_kind"] == "possible"]),
            "conflict_seconds": round(sum(c["duration"] for c in conflicts), 1),
            "data_quality": self.data_quality(start, end),
        }

    def data_quality(self, start: float, end: float) -> dict:
        scans = [s for s in self.scans if start <= s.ts <= end]
        pickers = {s.picker_id for s in scans}
        gaps = []
        for picker in sorted(pickers):
            ps = sorted(s.ts for s in scans if s.picker_id == picker)
            for a, b in zip(ps, ps[1:]):
                if b - a > self.params["long_gap_s"]:
                    gaps.append({"picker": picker, "start": a, "end": b,
                                 "gap": round(b - a, 1)})
        merged = [m for m in self.merged if start <= m["ts"] <= end]
        unknown_s = sum(max(0.0, min(s.end, end) - max(s.start, start))
                        for s in self.segments() if s.entity == "UNKNOWN")
        by_device = defaultdict(int)
        for s in scans:
            by_device[s.device_id] += 1
        return {
            "long_gaps": sorted(gaps, key=lambda g: g["gap"], reverse=True)[:20],
            "long_gap_count": len(gaps),
            "duplicate_count": len(merged),
            "unknown_seconds": round(unknown_s, 1),
            "devices": [{"device": d, "scans": n} for d, n in sorted(by_device.items())],
        }


# ---------------------------------------------------------------------------
# 拓扑版本对照
# ---------------------------------------------------------------------------


def topology_diff(old: Topology, new: Topology) -> list[dict]:
    changes = []
    for ent_id in sorted(set(old.entities) | set(new.entities)):
        a = old.entities.get(ent_id)
        b = new.entities.get(ent_id)
        if a is None or b is None:
            changes.append({"entity": ent_id, "name": (b or a).name, "kind": "added" if a is None else "removed",
                            "summary": "新增实体" if a is None else "实体被移除"})
            continue
        if a.capacity != b.capacity:
            changes.append({"entity": ent_id, "name": b.name, "kind": "capacity",
                            "before": a.capacity, "after": b.capacity,
                            "summary": f"通道容量 {a.capacity} → {b.capacity}"})
        if a.zone != b.zone:
            names = {"fast": "快拣区", "cold": "冷链区"}
            changes.append({"entity": ent_id, "name": b.name, "kind": "zone",
                            "before": names.get(a.zone, a.zone), "after": names.get(b.zone, b.zone),
                            "summary": f"区域归属 {names.get(a.zone, a.zone)} → {names.get(b.zone, b.zone)}"})
    for alias, target in new.aliases.items():
        changes.append({"entity": target, "kind": "alias", "before": alias, "after": target,
                        "summary": f"货位条码 {alias} → {target}（旧条码保留别名）"})
    return changes
