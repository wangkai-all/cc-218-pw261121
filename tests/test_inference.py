"""推断引擎测试：驻留/可能/未知分层、重复扫码、别名与冲突成因。"""
from __future__ import annotations

import unittest
from pathlib import Path

from src.warehouse.inference import (
    DEFAULTS, InferenceEngine, _compact_alternatives, _compact_reasons, dedupe_scans,
)
from src.warehouse.models import ScanEvent, Task
from src.warehouse.topology import load_topology

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


def topology():
    return load_topology(DATA / "topology_v1.json")


def task(tid="T1", zone="fast", released=1000.0, locations=("B3-L03",), cancelled=None):
    return Task(
        id=tid, wave_id="W1", zone=zone, created_at=released - 600,
        released_at=released, cancelled_at=cancelled,
        order_ids=("SO-1",), locations=tuple(locations),
    )


def scan(sid, ts, picker="P01", location="B3-L03", kind="pick",
         task_id=None, duplicate_of=None):
    return ScanEvent(
        id=sid, ts=ts, picker_id=picker, location_id=location,
        task_id=task_id, device_id="HH-101", scan_kind=kind, duplicate_of=duplicate_of,
    )


class SegmentLayeringTest(unittest.TestCase):
    def test_same_location_short_gap_is_observed_dwell(self):
        engine = InferenceEngine(topology(), [task()], [scan("S1", 1000), scan("S2", 1040)])
        dwell = [s for s in engine.segments() if s.kind == "dwell" and s.start >= 1000]
        self.assertTrue(dwell, "同货位连续扫码应产生确切驻留")
        self.assertEqual(dwell[0].confidence, "observed")
        self.assertAlmostEqual(dwell[0].start, 1000)
        self.assertAlmostEqual(dwell[0].end, 1040)

    def test_different_locations_split_dwell_and_possible(self):
        engine = InferenceEngine(
            topology(), [task(locations=("B3-L03", "F2-R02"))],
            [scan("S1", 1000, location="B3-L03"), scan("S2", 1160, location="F2-R02")],
        )
        segs = [s for s in engine.segments() if s.start >= 1000]
        kinds = {s.kind for s in segs}
        self.assertIn("dwell", kinds)
        self.assertIn("possible", kinds)
        # 移动过程不能标成确切驻留
        for s in segs:
            if s.kind == "possible":
                self.assertEqual(s.confidence, "inferred")
        # 驻留只到 dwell_min，不能覆盖整个间隔
        dwell = [s for s in segs if s.kind == "dwell"]
        # 末次扫码会补一个最短驻留（下限 20 秒），其余驻留不超过 dwell_min
        limit = max(DEFAULTS["dwell_min_s"], 20.0) + 1e-6
        self.assertTrue(all(s.end - s.start <= limit for s in dwell))

    def test_long_gap_becomes_unknown_not_last_location(self):
        gap = DEFAULTS["long_gap_s"] + 600
        engine = InferenceEngine(
            topology(), [task()],
            [scan("S1", 1000, location="B3-L03"), scan("S2", 1000 + gap, location="B3-L03")],
        )
        segs = engine.segments()
        unknown = [s for s in segs if s.kind == "unknown"]
        self.assertTrue(unknown, "超过 long_gap_s 的间隔必须有未知段")
        self.assertEqual(unknown[0].entity, "UNKNOWN")
        # 未知段必须覆盖绝大部分间隔，而不是继续算在最后货位
        covered = sum(s.end - s.start for s in unknown)
        self.assertGreater(covered, gap * 0.9)
        # 驻留不应延伸到间隔末尾
        first_dwell = [s for s in segs if s.kind == "dwell" and s.start == 1000]
        self.assertTrue(first_dwell)
        self.assertLessEqual(first_dwell[0].end - 1000, max(DEFAULTS["dwell_min_s"], 20.0) + 1e-6)

    def test_first_scan_has_unknown_lead_in(self):
        engine = InferenceEngine(topology(), [task()], [scan("S1", 1000)])
        first = engine.segments()[0]
        self.assertEqual(first.kind, "unknown")
        self.assertEqual(first.start, 940)


class DuplicateTest(unittest.TestCase):
    def test_repeat_scan_is_merged(self):
        scans = [scan("S1", 1000), scan("S2", 1001.5, kind="repeat", duplicate_of="S1")]
        kept, merged = dedupe_scans(scans, dict(DEFAULTS))
        self.assertEqual([s.id for s in kept], ["S1"])
        self.assertEqual(merged[0]["kept"], "S1")
        self.assertEqual(merged[0]["dropped"], "S2")

    def test_confirm_scan_inside_window_is_merged(self):
        scans = [scan("S1", 1000), scan("S2", 1005, kind="confirm")]
        kept, merged = dedupe_scans(scans, dict(DEFAULTS))
        self.assertEqual(len(kept), 1)
        self.assertEqual(len(merged), 1)

    def test_manual_scan_is_never_merged(self):
        scans = [scan("S1", 1000), scan("S2", 1001, kind="manual")]
        kept, _ = dedupe_scans(scans, dict(DEFAULTS))
        self.assertEqual({s.id for s in kept}, {"S1", "S2"})

    def test_scans_beyond_window_are_kept(self):
        scans = [scan("S1", 1000), scan("S2", 1000 + DEFAULTS["repeat_scan_window_s"] + 1)]
        kept, merged = dedupe_scans(scans, dict(DEFAULTS))
        self.assertEqual(len(kept), 2)
        self.assertEqual(merged, [])


class AliasTest(unittest.TestCase):
    def test_old_barcode_resolves_to_new_location(self):
        topo = load_topology(DATA / "topology_v2.json")
        engine = InferenceEngine(topo, [task()], [scan("S1", 1000, location="STG-02")])
        segs = engine.segments()
        relevant = [s for s in segs if s.start >= 1000]
        self.assertTrue(relevant)
        self.assertEqual(relevant[0].entity, "STAGE")
        self.assertEqual(relevant[0].location, "STG-02N")

    def test_unknown_location_degrades_to_unknown(self):
        engine = InferenceEngine(topology(), [task()], [scan("S1", 1000, location="NOPE-999")])
        segs = [s for s in engine.segments() if s.start >= 1000]
        self.assertTrue(segs)
        self.assertEqual(segs[0].entity, "UNKNOWN")


class ConflictTest(unittest.TestCase):
    def _engine(self):
        scans = [
            scan("S1", 1000, picker="P01", location="B3-L03", task_id="T1"),
            scan("S2", 1020, picker="P01", location="B3-L03", task_id="T1"),
            scan("S3", 1005, picker="P02", location="B3-L03", task_id="T2"),
            scan("S4", 1025, picker="P02", location="B3-L03", task_id="T2"),
            scan("S5", 1010, picker="P03", location="B3-L03", task_id="T3"),
            scan("S6", 1030, picker="P03", location="B3-L03", task_id="T3"),
        ]
        tasks = [
            task("T1", zone="fast", released=900, locations=("B3-L03",)),
            task("T2", zone="cold", released=900, locations=("B3-L03",)),
            task("T3", zone="fast", released=900, locations=("B3-L03",)),
        ]
        return InferenceEngine(topology(), tasks, scans, {**DEFAULTS})

    def test_confirmed_conflict_has_causes(self):
        conflicts = self._engine().conflicts(990, 1050)
        self.assertTrue(conflicts)
        c = conflicts[0]
        self.assertEqual(c["entity"], "AISLE_B")
        self.assertEqual(c["overlap_kind"], "confirmed")
        self.assertGreaterEqual(c["peak"], 3)
        self.assertTrue(c["causes"])
        self.assertIn("task_cross", c["causes"])
        self.assertTrue(c["alternatives"], "每个成因都必须附替代解释")
        self.assertEqual(len(c["cause_labels"]), len(c["causes"]))

    def test_cause_seconds_within_duration(self):
        for c in self._engine().conflicts(990, 1050):
            self.assertGreaterEqual(sum(c["cause_seconds"].values()) + 0.11, c["duration"] - 0.1)
            for secs in c["cause_seconds"].values():
                self.assertLessEqual(secs, c["duration"] + 0.11)

    def test_capacity_respected(self):
        engine = self._engine()
        cap = engine.topology.entities["AISLE_B"].capacity
        for c in engine.conflicts(990, 1050):
            self.assertGreater(c["peak"], cap)

    def test_summary_counts_each_cause_tag(self):
        engine = self._engine()
        summary = engine.summary(990, 1050)
        total = sum(summary["conflicts_by_cause"].values())
        self.assertGreaterEqual(total, summary["conflict_count"])

    def test_merged_conflict_text_is_compact(self):
        """合并相邻子区间后，同类说明不能各出现一次堆成十几条。"""
        scans = []
        # 同一通道连续三段超容量：0-20、22-42、44-64 秒
        for i, start in enumerate((1000, 1022, 1044)):
            for j, picker in enumerate(("P01", "P02", "P03")):
                scans.append(scan(f"S{i}{j}a", start, picker=picker, location="B3-L03", task_id=f"T{j}"))
                scans.append(scan(f"S{i}{j}b", start + 20, picker=picker, location="B3-L03", task_id=f"T{j}"))
        tasks = [task(f"T{j}", zone="fast", released=900, locations=("B3-L03",)) for j in range(3)]
        conflicts = InferenceEngine(topology(), tasks, scans).conflicts(990, 1100)
        self.assertTrue(conflicts)
        for c in conflicts:
            inferred = [r for r in c["reasons"] if "只能推测" in r]
            self.assertLessEqual(len(inferred), 1, c["reasons"])
            short = [a for a in c["alternatives"] if "短暂交汇" in a]
            self.assertLessEqual(len(short), 1, c["alternatives"])

    def test_compaction_keeps_one_consistent_same_wave_statement(self):
        """合并链里既有“观测未超”又有“观测已超”时，只保留与合并后性质一致的一句。"""
        possible = "同波次多个任务在同一通道并行，观测人数未超容量，叠加推测后才超过"
        confirmed = "同波次批量发单的并行作业，观测人数已超过容量"
        reasons = [possible, "2 人的位置只能推测，叠加后才超过容量", confirmed,
                   "3 人的位置只能推测，叠加后才超过容量"]
        merged_possible = _compact_reasons(reasons, "possible")
        self.assertIn(possible, merged_possible)
        self.assertNotIn(confirmed, merged_possible)
        merged_confirmed = _compact_reasons(reasons, "confirmed")
        self.assertIn(confirmed, merged_confirmed)
        self.assertNotIn(possible, merged_confirmed)
        for out in (merged_possible, merged_confirmed):
            self.assertEqual(sum("只能推测" in r for r in out), 1, out)

    def test_compaction_collapses_repeated_short_alternatives(self):
        alts = ["本区间仅 10 秒，可能是扫码/通行瞬间的短暂交汇，不代表持续拥堵",
                "本区间仅 4 秒，可能是扫码/通行瞬间的短暂交汇，不代表持续拥堵",
                "容量是配置值，若实物通道更宽，统计口径会高估冲突"]
        out = _compact_alternatives(alts)
        self.assertEqual(sum("短暂交汇" in a for a in out), 1, out)
        self.assertTrue(any("最短 4 秒" in a for a in out), out)


class DeterminismTest(unittest.TestCase):
    def test_same_inputs_same_segments(self):
        scans = [scan("S1", 1000), scan("S2", 2000, location="F2-R02")]
        a = InferenceEngine(topology(), [task()], scans).occupancy()
        b = InferenceEngine(topology(), [task()], scans).occupancy()
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
