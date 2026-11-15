"""生成器与导入链路测试：确定性、覆盖要求、CSV 解析。"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from src.warehouse import synth
from src.warehouse.ingest import import_dataset, parse_scans_csv, parse_tasks_csv

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


class SynthCoverageTest(unittest.TestCase):
    """合成数据必须覆盖题目要求的三类脏数据。"""

    @classmethod
    def setUpClass(cls):
        cls.dataset = json.loads((DATA / "dataset.json").read_text(encoding="utf-8"))
        cls.day1 = cls.dataset["scenarios"]["day1"]
        cls.day2 = cls.dataset["scenarios"]["day2"]
        cls.topo2 = cls.dataset["topologies"]["v2"]

    def all_scans(self):
        return self.day1["scans"] + self.day2["scans"]

    def test_has_duplicate_scans(self):
        repeats = [s for s in self.all_scans() if s["kind"] in ("repeat", "confirm")]
        self.assertGreater(len(repeats), 0)
        repeats_with_parent = [s for s in repeats if s.get("duplicate_of")]
        self.assertGreater(len(repeats_with_parent), 0)

    def test_has_cancelled_tasks_before_and_mid_task(self):
        tasks = self.day1["tasks"] + self.day2["tasks"]
        cancelled = [t for t in tasks if t.get("cancelled")]
        self.assertGreater(len(cancelled), 0)
        pre = [t for t in tasks if t.get("cancel_mode") == "pre"]
        mid = [t for t in tasks if t.get("cancel_mode") == "mid"]
        self.assertGreater(len(pre), 0, "应有发单前取消")
        self.assertGreater(len(mid), 0, "应有取货中途取消")
        for t in pre:
            self.assertLessEqual(t["cancelled"], t["released"])
        for t in mid:
            self.assertGreater(t["cancelled"], t["released"])

    def test_has_topology_revision(self):
        self.assertNotEqual(self.day1["topology"], self.day2["topology"])
        self.assertTrue(self.topo2.get("changes"))
        self.assertTrue(self.topo2.get("aliases"))

    def test_has_device_outage(self):
        # P05 在 day2 有一段无扫描，推断应产生 data_gap 段
        p05 = [s for s in self.day2["scans"] if s["picker"] == "P05"]
        gaps = [b["ts"] - a["ts"] for a, b in zip(sorted(p05, key=lambda s: s["ts"]),
                                                 sorted(p05, key=lambda s: s["ts"])[1:])]
        self.assertTrue(any(g > 600 for g in gaps), "应有一段明显设备离线")

    def test_ground_truth_and_stats_present(self):
        for sc in (self.day1, self.day2):
            self.assertTrue(sc["ground_truth"])
            self.assertIn("walk_m_by_picker", sc["sim_stats"])

    def test_replays_share_tasks(self):
        a = {t["id"] for t in self.dataset["replays"]["a"]["tasks"]}
        b = {t["id"] for t in self.dataset["replays"]["b"]["tasks"]}
        self.assertEqual(a, b, "两套回放必须使用同一批作业单")


class SynthDeterminismTest(unittest.TestCase):
    def test_writer_is_deterministic_for_fixed_seed(self):
        import random

        v1 = synth.topology_v1()
        base = synth.BASE_DAY1
        tasks_a = synth.schedule_tasks(synth.make_tasks(random.Random(7), v1, base, 12, "X"), base, "wave")
        tasks_b = synth.schedule_tasks(synth.make_tasks(random.Random(7), v1, base, 12, "X"), base, "wave")
        self.assertEqual([t.id for t in tasks_a], [t.id for t in tasks_b])
        self.assertEqual([t.released_at for t in tasks_a], [t.released_at for t in tasks_b])

    def test_mixed_plan_interleaves_zones(self):
        import random

        v1 = synth.topology_v1()
        base = synth.BASE_DAY1
        tasks = synth.make_tasks(random.Random(11), v1, base, 20, "M")
        wave = synth.schedule_tasks(tasks, base, "wave")
        mixed = synth.schedule_tasks(tasks, base, "mixed")
        # 同一批任务，两种顺序的发放时刻不同
        self.assertEqual({t.id for t in wave}, {t.id for t in mixed})
        self.assertNotEqual([t.released_at for t in wave], [t.released_at for t in mixed])
        # 混流方案在更短时间内发完全部任务
        self.assertLess(max(t.released_at for t in mixed), max(t.released_at for t in wave))


class IngestTest(unittest.TestCase):
    def test_parse_csv_with_epoch_seconds(self):
        scans = parse_scans_csv(
            "scan_id,ts,picker,location,task,device,source,kind,duplicate_of,note\n"
            "S1,1794384000.0,P01,B3-L03,T1,HH-1,handheld,pick,,\n"
        )
        self.assertEqual(scans[0].ts, 1794384000.0)
        self.assertEqual(scans[0].picker_id, "P01")

    def test_parse_tasks_with_pipe_lists(self):
        tasks = parse_tasks_csv(
            "task_id,zone,released_at,order_ids,locations\n"
            "T1,fast,1794384000,SO-1|SO-2,A1-L01|A2-R02\n"
        )
        self.assertEqual(tasks[0].order_ids, ("SO-1", "SO-2"))
        self.assertEqual(tasks[0].locations, ("A1-L01", "A2-R02"))

    def test_import_roundtrip_on_generated_csv(self):
        topo = (DATA / "csv" / "topology.json").read_text(encoding="utf-8")
        scans = (DATA / "csv" / "scans.csv").read_text(encoding="utf-8")
        tasks = (DATA / "csv" / "tasks.csv").read_text(encoding="utf-8")
        imported = import_dataset(topo, scans, tasks, "样例")
        self.assertEqual(imported["label"], "样例")
        self.assertGreater(len(imported["scans"]), 0)
        self.assertGreater(len(imported["tasks"]), 0)
        self.assertTrue(imported["pickers"])
        self.assertEqual(imported["warnings"], [])

    def test_import_reports_unknown_locations(self):
        topo = (DATA / "csv" / "topology.json").read_text(encoding="utf-8")
        scans = (
            "scan_id,ts,picker,location,task,device,source,kind,duplicate_of,note\n"
            "S1,1794384000,P01,B3-L03,T1,HH-1,handheld,pick,,\n"
            "S2,1794384010,P01,ZZZ-999,T1,HH-1,handheld,pick,,\n"
        )
        tasks = (DATA / "csv" / "tasks.csv").read_text(encoding="utf-8")
        imported = import_dataset(topo, scans, tasks, "含未知货位")
        self.assertTrue(imported["warnings"])
        self.assertIn("ZZZ-999", imported["warnings"][0])


if __name__ == "__main__":
    unittest.main()
