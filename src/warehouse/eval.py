"""推断校准：用合成数据的真值评估推断，并给出行走速度建议。

关键指标
--------
- 确切驻留的归属准确率（dwell 落在真实驻留且同一实体）
- 位置未知召回率（真实休息/离岗被标为 unknown 的比例）
- 陈旧位置错误率（真实已离开，仍被算在最后货位的比例）——应接近 0
- 冲突时长高估/低估（按容量超限的真实时长对比）

这些指标只在合成数据（带 ground_truth）时可用；导入的真实数据不提供。
"""
from __future__ import annotations

from collections import defaultdict
from statistics import median

from .inference import DEFAULTS, InferenceEngine

SAMPLE_STEP = 15.0


def _truth_at(truth: list[dict], picker: str, t: float):
    for seg in truth:
        if seg["picker"] == picker and seg["start"] <= t < seg["end"]:
            return seg
    return None


def _inferred_at(segs_by_picker: dict, picker: str, t: float):
    best = None
    rank = {"dwell": 4, "gap": 3, "possible": 2, "unknown": 1}
    for seg in segs_by_picker.get(picker, ()):
        if seg.start <= t < seg.end:
            if best is None or rank.get(seg.kind, 0) > rank.get(best.kind, 0):
                best = seg
    return best


def calibrate(engine: InferenceEngine, truth: list[dict], start: float, end: float) -> dict:
    segs_by_picker = defaultdict(list)
    for seg in engine.segments():
        segs_by_picker[seg.picker].append(seg)
    pickers = sorted({s["picker"] for s in truth} | set(segs_by_picker))

    dwell_samples = dwell_hit = 0
    dwell_wrong_entity = 0
    unknown_expected = unknown_hit = 0
    stale_samples = 0
    possible_samples = 0
    truth_present = 0
    observed_time = 0.0
    unknown_time = 0.0

    t = start
    while t < end:
        for picker in pickers:
            truth_seg = _truth_at(truth, picker, t)
            inf = _inferred_at(segs_by_picker, picker, t)
            if truth_seg is None:
                t += SAMPLE_STEP
                continue
            truth_present += 1
            truth_entity = truth_seg["entity"]
            truth_is_unknown = truth_seg["kind"] in ("break",) or truth_entity == "UNKNOWN"
            if truth_is_unknown:
                unknown_expected += 1
                if inf is not None and inf.kind in ("unknown", "possible"):
                    unknown_hit += 1
                if inf is not None and inf.kind in ("dwell", "gap"):
                    stale_samples += 1
            if inf is None:
                continue
            if inf.kind == "dwell":
                observed_time += SAMPLE_STEP
                if truth_seg["kind"] == "dwell":
                    dwell_samples += 1
                    if truth_entity == inf.entity:
                        dwell_hit += 1
                    else:
                        dwell_wrong_entity += 1
            elif inf.kind == "unknown":
                unknown_time += SAMPLE_STEP
            elif inf.kind == "possible":
                possible_samples += 1
        t += SAMPLE_STEP * 2  # 每两档取一档，控制计算量

    truth_conflict_dwell, truth_conflict_any = _truth_conflict_seconds(engine, truth, start, end)
    all_conflicts = engine.conflicts(start, end)
    inferred_confirmed = sum(c["duration"] for c in all_conflicts if c["overlap_kind"] == "confirmed")
    inferred_upper = sum(c["duration"] for c in all_conflicts)
    return {
        "samples": truth_present,
        "dwell_attribution_accuracy": round(dwell_hit / dwell_samples, 3) if dwell_samples else None,
        "dwell_wrong_entity": dwell_wrong_entity,
        "unknown_recall": round(unknown_hit / unknown_expected, 3) if unknown_expected else None,
        "stale_location_rate": round(stale_samples / unknown_expected, 3) if unknown_expected else 0.0,
        "unknown_hours": round(unknown_time / 3600, 2),
        "observed_hours": round(observed_time / 3600, 2),
        "possible_sample_share": round(possible_samples / truth_present, 3) if truth_present else None,
        "truth_conflict_seconds": round(truth_conflict_dwell, 1),
        "truth_conflict_seconds_any": round(truth_conflict_any, 1),
        "inferred_conflict_seconds": round(inferred_confirmed, 1),
        "inferred_conflict_seconds_upper": round(inferred_upper, 1),
        "conflict_seconds_ratio": (
            round(inferred_confirmed / truth_conflict_dwell, 2)
            if truth_conflict_dwell else None),
        "conflict_seconds_ratio_upper": (
            round(inferred_upper / truth_conflict_any, 2)
            if truth_conflict_any else None),
        "notes": [
            "真值来自合成仿真，仅用于检验方法，不代表真实仓库精度。",
            "冲突时长下限只算观测已超容量的区间；上限把推测重叠也算入。真实值应落在两者之间。",
        ],
    }


def _truth_conflict_seconds(engine: InferenceEngine, truth: list[dict],
                            start: float, end: float) -> tuple[float, float]:
    """按真值位置统计同时占用超过容量的时长。

    返回 (驻留口径, 全部在场口径)：
    - 驻留口径只算真实停在货位取货的人，对应推断的「确证重叠」下限；
    - 全部在场口径包括穿行通道的人，对应把推测区间也算入的上限。
    """
    topology = engine.topology
    caps = {e.id: e.capacity for e in topology.entities.values()}
    step = 20.0
    dwell_only = 0.0
    any_presence = 0.0
    t = start
    while t < end:
        dwell_people: dict[str, set[str]] = defaultdict(set)
        any_people: dict[str, set[str]] = defaultdict(set)
        for seg in truth:
            if seg["start"] <= t < seg["end"] and seg["entity"] in caps:
                any_people[seg["entity"]].add(seg["picker"])
                if seg["kind"] == "dwell":
                    dwell_people[seg["entity"]].add(seg["picker"])
        for entity, people in any_people.items():
            if len(people) > caps[entity]:
                any_presence += step
        for entity, people in dwell_people.items():
            if len(people) > caps[entity]:
                dwell_only += step
        t += step
    return dwell_only, any_presence


def walking_speed_estimate(engine: InferenceEngine, min_gap_s: float = 20.0) -> dict:
    """从相邻扫码间隔估计行走速度。

    模型: gap = 驻留 + 步行距离 / 速度。用 Theil–Sen（两两斜率中位数）
    稳健回归，避免把取货/等待异常值当成慢速；截距即平均驻留。
    这是从观测间隔反推的参数，只在合成数据上可用真值校验。
    """
    samples = []
    pairs = []
    by_picker = defaultdict(list)
    for scan in engine.scans:
        by_picker[scan.picker_id].append(scan)
    for picker, scans in by_picker.items():
        scans.sort(key=lambda s: s.ts)
        for a, b in zip(scans, scans[1:]):
            if a.location_id == b.location_id:
                continue
            gap = b.ts - a.ts
            if gap < min_gap_s or gap > engine.params["long_gap_s"]:
                continue
            try:
                meters = engine.graph.location_distance(a.location_id, b.location_id)
            except KeyError:
                continue
            if meters < 5:
                continue
            samples.append((meters, gap))
            pairs.append({
                "picker": picker, "from": a.location_id, "to": b.location_id,
                "gap_s": round(gap, 1), "meters": round(meters, 1),
            })
    if len(samples) < 5:
        return {"samples": len(samples), "suggested_speed": DEFAULTS["walk_speed_mps"],
                "current_speed": engine.params["walk_speed_mps"], "rows": [], "dwell_estimate": None}
    slopes = []
    for i in range(len(samples)):
        mi, gi = samples[i]
        for j in range(i + 1, len(samples)):
            mj, gj = samples[j]
            dm = mj - mi
            if abs(dm) > 20:
                slopes.append((gj - gi) / dm)
    slopes.sort()
    slope = slopes[len(slopes) // 2] if slopes else 0.0
    if slope <= 0.15:
        speed = DEFAULTS["walk_speed_mps"]
        dwell = None
    else:
        speed = 1.0 / slope
        residuals = sorted(g - slope * m for m, g in samples)
        dwell = residuals[len(residuals) // 2]
    speed = max(0.6, min(2.0, speed))
    rank = lambda q: pairs[min(len(pairs) - 1, int(q * len(pairs)))]
    return {
        "samples": len(samples),
        "slope_s_per_m": round(slope, 3),
        "median_speed_from_pairs": round(speed, 2),
        "suggested_speed": round(speed, 2),
        "current_speed": engine.params["walk_speed_mps"],
        "dwell_estimate": round(dwell, 1) if dwell is not None else None,
        "rows": pairs[:200],
        "note": "速度由相邻不同货位扫码的间隔回归得出，混合了取货与等待，仅供参考。",
    }
