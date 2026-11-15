"""服务端 API 测试：真实起一个 HTTP 服务，验证响应结构与导入接口。"""
from __future__ import annotations

import json
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

from src.warehouse.server import Handler, load_dataset

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_dataset()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def request(self, method, path, body=None, headers=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=30)
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        ctype = resp.getheader("Content-Type", "")
        return resp.status, raw, ctype

    def get_json(self, path):
        status, raw, ctype = self.request("GET", path)
        self.assertEqual(status, 200, raw[:300])
        self.assertIn("application/json", ctype)
        return json.loads(raw)

    def test_meta_lists_scenarios_replays_topologies(self):
        meta = self.get_json("/api/meta")
        ids = {s["id"] for s in meta["scenarios"]}
        self.assertEqual(ids, {"day1", "day2"})
        self.assertEqual({r["id"] for r in meta["replays"]}, {"a", "b"})
        self.assertTrue(meta["topologies"])
        self.assertIn("walk_speed_mps", meta["defaults"])

    def test_scenario_payload_structure(self):
        data = self.get_json("/api/scenario?id=day1")
        for key in ("summary", "segments", "conflicts", "topology", "calibration", "speed_estimate"):
            self.assertIn(key, data)
        seg = data["segments"][0]
        for key in ("kind", "confidence", "start", "end", "picker"):
            self.assertIn(key, seg)
        kinds = {s["kind"] for s in data["segments"]}
        self.assertTrue({"dwell", "possible", "unknown"} <= kinds)

    def test_conflicts_have_multilabel_causes(self):
        data = self.get_json("/api/scenario?id=day2")
        self.assertTrue(data["conflicts"])
        for c in data["conflicts"]:
            self.assertTrue(c["causes"])
            self.assertEqual(len(c["causes"]), len(c["cause_labels"]))
            self.assertTrue(c["reasons"])
            self.assertTrue(c["alternatives"])
            # 多种成因可覆盖同一时段，故各项之和 ≥ 区间时长；单个成因不超过时长
            self.assertGreaterEqual(sum(c["cause_seconds"].values()) + 0.51, c["duration"])
            for secs in c["cause_seconds"].values():
                self.assertLessEqual(secs, c["duration"] + 0.51)

    def test_params_change_inference(self):
        base = self.get_json("/api/scenario?id=day1")
        strict = self.get_json("/api/scenario?id=day1&long_gap_s=180&walk_speed_mps=1.6")
        self.assertNotEqual(base["summary"]["time_by_kind"], strict["summary"]["time_by_kind"])

    def test_replay_payload_has_two_runs_and_caveats(self):
        data = self.get_json("/api/replay")
        self.assertEqual(set(data["runs"]), {"a", "b"})
        self.assertTrue(data["caveats"])
        self.assertEqual(set(data["totals"]), {"a", "b"})
        self.assertIn("walk_m", data["totals"]["a"])
        a_tasks = {t["id"] for t in data["runs"]["a"]["tasks"]}
        b_tasks = {t["id"] for t in data["runs"]["b"]["tasks"]}
        self.assertEqual(a_tasks, b_tasks)

    def test_unknown_scenario_returns_error(self):
        status, raw, _ = self.request("GET", "/api/scenario?id=nope")
        self.assertEqual(status, 400)
        self.assertIn("error", json.loads(raw))

    def test_import_endpoint(self):
        body = json.dumps({
            "topology": (DATA / "csv" / "topology.json").read_text(encoding="utf-8"),
            "scans": (DATA / "csv" / "scans.csv").read_text(encoding="utf-8"),
            "tasks": (DATA / "csv" / "tasks.csv").read_text(encoding="utf-8"),
            "label": "接口测试",
        }).encode("utf-8")
        status, raw, _ = self.request("POST", "/api/import", body,
                                      {"Content-Type": "application/json"})
        self.assertEqual(status, 200, raw[:300])
        data = json.loads(raw)
        self.assertEqual(data["imported"]["label"], "接口测试")
        self.assertGreater(data["summary"]["conflicts_by_cause"].get("possible", 0), 0)
        self.assertEqual(data["imported"]["warnings"], [])

    def test_import_rejects_empty_body(self):
        status, raw, _ = self.request("POST", "/api/import", b"{}",
                                      {"Content-Type": "application/json"})
        self.assertEqual(status, 400)
        self.assertIn("缺少", json.loads(raw)["error"])

    def test_static_index_and_assets(self):
        status, raw, ctype = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        self.assertIn("仓内作业分析", raw.decode("utf-8"))
        status, raw, ctype = self.request("GET", "/app.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", ctype)

    def test_static_traversal_blocked(self):
        status, raw, _ = self.request("GET", "/../src/warehouse/server.py")
        self.assertEqual(status, 404)
        status, raw, _ = self.request("GET", "/data/../src/warehouse/server.py")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
