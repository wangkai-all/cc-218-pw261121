"""仓内作业分析 — HTTP 服务与 JSON API（仅标准库）。

启动：python3 -m src.warehouse.server [端口]
静态页面在 web/ 下。
"""
from __future__ import annotations

import json
import mimetypes
import os
import sys
import threading
from collections import defaultdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import synth
from .eval import calibrate, walking_speed_estimate
from .inference import (
    CAUSE_LABELS,
    DEFAULTS,
    PARAM_LABELS,
    SEGMENT_LABELS,
    InferenceEngine,
    topology_diff,
)
from .ingest import import_dataset, dataset_to_models, replays_to_models
from .models import fmt_clock, fmt_duration, fmt_ts

ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = ROOT / "web"
DATA_DIR = ROOT / "data"
DATASET_PATH = DATA_DIR / "dataset.json"
CACHE_LOCK = threading.Lock()
_CACHE: dict = {}


def load_dataset() -> dict:
    with CACHE_LOCK:
        if "dataset" in _CACHE:
            return _CACHE["dataset"]
    if not DATASET_PATH.exists():
        synth.build_all()
    dataset = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    with CACHE_LOCK:
        _CACHE["dataset"] = dataset
    return dataset


def _params(raw: dict | None) -> dict:
    params = dict(DEFAULTS)
    for key, value in (raw or {}).items():
        if key in DEFAULTS:
            try:
                params[key] = float(value)
            except (TypeError, ValueError):
                continue
    params["walk_speed_mps"] = max(0.4, min(3.0, params["walk_speed_mps"]))
    params["long_gap_s"] = max(120.0, min(3600.0, params["long_gap_s"]))
    params["walk_slack"] = max(0.8, min(3.0, params["walk_slack"]))
    return params


def _engine_for(scenario_id: str, params: dict):
    dataset = load_dataset()
    topology, tasks, scans, start, end = dataset_to_models(dataset, scenario_id, dataset["topologies"])
    return InferenceEngine(topology, tasks, scans, params), start, end


def _engine_for_replay(key: str, params: dict):
    dataset = load_dataset()
    topology, tasks, scans, start, end = replays_to_models(dataset, key)
    return InferenceEngine(topology, tasks, scans, params), start, end


def json_payload(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def topology_view(dataset: dict, version: str) -> dict:
    raw = dataset["topologies"][version]
    return {
        "version": version,
        "label": raw["label"],
        "effective_from": raw["effective_from"],
        "effective_to": raw["effective_to"],
        "changes": raw.get("changes", []),
        "zones": raw.get("zones", []),
        "entities": [
            {"id": e["id"], "name": e["name"], "kind": e["kind"], "zone": e["zone"],
             "capacity": e["capacity"], "rect": e["rect"]}
            for e in raw["entities"]
        ],
        "nodes": raw["nodes"],
        "edges": raw["edges"],
        "locations": [
            {"id": l["id"], "entity": l["entity"], "zone": l["zone"], "x": l["x"],
             "y": l["y"], "kind": l.get("kind", "bin")}
            for l in raw["locations"]
        ],
        "aliases": raw.get("aliases", {}),
    }


def scenario_payload(scenario_id: str, params: dict) -> dict:
    dataset = load_dataset()
    engine, start, end = _engine_for(scenario_id, params)
    scenario = dataset["scenarios"][scenario_id]
    summary = engine.summary(start, end)
    conflicts = engine.conflicts(start, end)
    zoo = engine.zone_occupancy(start, end, step=60.0)
    occ = engine.entity_occupancy_series(start, end, step=30.0)
    truth = scenario.get("ground_truth")
    calibration = calibrate(engine, truth, start, end) if truth else None
    tasks = [
        {"id": t.id, "wave": t.wave_id, "zone": t.zone, "created": t.created_at,
         "released": t.released_at, "due": t.due_at, "cancelled": t.cancelled_at,
         "cancel_reason": t.cancel_reason, "orders": list(t.order_ids),
         "locations": list(t.locations), "release_rank": t.release_rank,
         "priority": t.priority, "cancel_mode": t.cancel_mode}
        for t in engine.tasks.values()
    ]
    topology = engine.topology
    diffs = []
    if dataset["topologies"].get("v1") and dataset["topologies"].get("v2"):
        from .ingest import dataset_to_models as _dm
        _, _, _, _, _ = _dm(dataset, scenario_id, dataset["topologies"])
        topo1, *_ = _dm(dataset, "day1", dataset["topologies"])
        topo2, *_ = _dm(dataset, "day2", dataset["topologies"])
        diffs = topology_diff(topo1, topo2)
    return {
        "scenario": {
            "id": scenario_id,
            "label": scenario["label"],
            "start": start,
            "end": end,
            "topology": scenario["topology"],
            "pickers": scenario["pickers"],
        },
        "params": engine.params,
        "param_labels": PARAM_LABELS,
        "labels": {"segments": SEGMENT_LABELS, "causes": CAUSE_LABELS},
        "summary": summary,
        "segments": engine.occupancy(start, end),
        "scans": [
            {"id": s.id, "ts": s.ts, "picker": s.picker_id, "location": s.location_id,
             "task": s.task_id, "device": s.device_id, "kind": s.scan_kind,
             "duplicate_of": s.duplicate_of, "note": s.note}
            for s in engine.raw_scans if start <= s.ts <= end
        ],
        "timeline": engine.picker_timeline(start, end),
        "conflicts": conflicts,
        "zone_occupancy": zoo,
        "entity_series": occ,
        "merged_duplicates": engine.merged,
        "tasks": tasks,
        "topology": topology_view(dataset, scenario["topology"]),
        "topology_diff": diffs,
        "calibration": calibration,
        "speed_estimate": walking_speed_estimate(engine),
    }


def replay_payload(params: dict) -> dict:
    dataset = load_dataset()
    engines = {}
    results = {}
    for key in ("a", "b"):
        engine, start, end = _engine_for_replay(key, params)
        engines[key] = engine
        summary = engine.summary(start, end)
        conflicts = engine.conflicts(start, end)
        results[key] = {
            "id": key,
            "name": dataset["replays"][key]["name"],
            "plan": dataset["replays"][key]["plan"],
            "start": start,
            "end": end,
            "summary": summary,
            "conflicts": conflicts,
            "segments": engine.occupancy(start, end),
            "scans": [
                {"id": s.id, "ts": s.ts, "picker": s.picker_id, "location": s.location_id,
                 "task": s.task_id, "kind": s.scan_kind}
                for s in engine.raw_scans if start <= s.ts <= end
            ],
            "timeline": engine.picker_timeline(start, end),
            "entity_series": engine.entity_occupancy_series(start, end, 30.0),
            "tasks": [
                {"id": t.id, "wave": t.wave_id, "zone": t.zone, "released": t.released_at,
                 "locations": list(t.locations), "cancel_mode": t.cancel_mode,
                 "orders": list(t.order_ids)}
                for t in engine.tasks.values()
            ],
        }
    a, b = engines["a"], engines["b"]
    sa = results["a"]["summary"]
    sb = results["b"]["summary"]
    task_ids_a = {t.id for t in a.tasks.values()}
    task_ids_b = {t.id for t in b.tasks.values()}
    shared = len(task_ids_a & task_ids_b)
    walk_a = dataset["replays"]["a"]["stats"]["walk_m_by_picker"]
    walk_b = dataset["replays"]["b"]["stats"]["walk_m_by_picker"]
    return {
        "params": a.params,
        "runs": results,
        "shared_tasks": shared,
        "totals": {
            "a": {"walk_m": round(sum(walk_a.values()), 1), "conflicts": sa["conflict_count"],
                  "confirmed": sa["confirmed_conflicts"], "possible": sa["possible_conflicts"],
                  "conflict_seconds": sa["conflict_seconds"],
                  "unknown_seconds": sa["time_by_kind"].get("unknown", 0),
                  "tasks": len(task_ids_a)},
            "b": {"walk_m": round(sum(walk_b.values()), 1), "conflicts": sb["conflict_count"],
                  "confirmed": sb["confirmed_conflicts"], "possible": sb["possible_conflicts"],
                  "conflict_seconds": sb["conflict_seconds"],
                  "unknown_seconds": sb["time_by_kind"].get("unknown", 0),
                  "tasks": len(task_ids_b)},
        },
        "caveats": [
            "两套回放使用同一批作业单与同一批人员，差异只来自发单顺序与节奏。",
            "仿真仍有随机抖动，少量差异不构成显著结论。",
            "冲突少不代表更好：混流可能以更多行走距离换取通道分散。",
            "若两边的扫描缺失程度不同，未知区间比例也不同，冲突总数不可直接横比。",
        ],
    }


def dataset_meta() -> dict:
    dataset = load_dataset()
    return {
        "warehouse": dataset["meta"]["warehouse"],
        "note": dataset["meta"]["note"],
        "window_seconds": dataset["meta"]["window_seconds"],
        "scenarios": [
            {
                "id": key,
                "label": sc["label"],
                "topology": sc["topology"],
                "start": sc["start"],
                "end": sc["end"],
                "pickers": len(sc["pickers"]),
                "scans": len(sc["scans"]),
                "tasks": len(sc["tasks"]),
            }
            for key, sc in dataset["scenarios"].items()
        ],
        "replays": [
            {"id": key, "name": r["name"], "plan": r["plan"],
             "scans": len(r["scans"]), "tasks": len(r["tasks"]),
             "pickers": len(r["pickers"]), "start": r["start"], "end": r["end"]}
            for key, r in dataset["replays"].items()
        ],
        "topologies": [
            {"version": v, "label": t["label"],
             "effective_from": t["effective_from"], "effective_to": t["effective_to"],
             "changes": t.get("changes", [])}
            for v, t in dataset["topologies"].items()
        ],
        "defaults": DEFAULTS,
        "param_labels": PARAM_LABELS,
        "labels": {"segments": SEGMENT_LABELS, "causes": CAUSE_LABELS},
    }


def time_label(start: float, end: float) -> str:
    return f"{fmt_clock(start)}–{fmt_clock(end)}"


class Handler(BaseHTTPRequestHandler):
    server_version = "WarehouseAnalysis/1.0"

    def log_message(self, fmt, *args):
        if os.environ.get("WAREHOUSE_QUIET") != "1":
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, status: int, body: bytes, content_type: str, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, status=HTTPStatus.OK):
        self._send(status, json_payload(obj), "application/json; charset=utf-8")

    def _error(self, message: str, status=HTTPStatus.BAD_REQUEST):
        self._json({"error": message}, status)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            if path == "/api/meta":
                return self._json(dataset_meta())
            if path == "/api/scenario":
                sid = query.get("id", ["day1"])[0]
                if sid not in ("day1", "day2"):
                    return self._error("未知场景")
                params = _params({k: v[0] for k, v in query.items()})
                return self._json(scenario_payload(sid, params))
            if path == "/api/replay":
                params = _params({k: v[0] for k, v in query.items()})
                return self._json(replay_payload(params))
            if path == "/api/topology":
                version = query.get("version", ["v1"])[0]
                dataset = load_dataset()
                if version not in dataset["topologies"]:
                    return self._error("未知拓扑版本")
                return self._json(topology_view(dataset, version))
            if path == "/api/health":
                return self._json({"ok": True})
            if path.startswith("/api/"):
                return self._error("未知接口", HTTPStatus.NOT_FOUND)
            return self._static(path)
        except BrokenPipeError:
            return
        except Exception as exc:  # 保持服务可用，返回结构化错误
            import traceback
            traceback.print_exc()
            return self._error(f"内部错误：{exc}", HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/api/import":
            return self._error("未知接口", HTTPStatus.NOT_FOUND)
        length = int(self.headers.get("Content-Length") or 0)
        if length > 20 * 1024 * 1024:
            return self._error("文件过大（上限 20MB）", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        body = self.rfile.read(length)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self._error("请求体必须是 JSON")
        for field in ("topology", "scans", "tasks"):
            if not isinstance(payload.get(field), str) or not payload[field].strip():
                return self._error(f"缺少 {field} 内容")
        try:
            imported = import_dataset(
                payload["topology"], payload["scans"], payload["tasks"],
                payload.get("label") or "导入数据",
            )
        except Exception as exc:
            return self._error(f"导入失败：{exc}")
        params = _params(payload.get("params") or {})
        engine = InferenceEngine(imported["topology"], imported["tasks"],
                                 imported["scans"], params)
        start, end = imported["start"], imported["end"]
        summary = engine.summary(start, end)
        return self._json({
            "imported": {
                "label": imported["label"],
                "topology": imported["topology"].version_id,
                "tasks": len(imported["tasks"]),
                "scans": len(imported["scans"]),
                "pickers": imported["pickers"],
                "start": start,
                "end": end,
                "warnings": imported["warnings"],
            },
            "summary": summary,
            "conflicts": engine.conflicts(start, end)[:100],
            "segments": engine.occupancy(start, end)[:2000],
            "timeline": engine.picker_timeline(start, end),
            "tasks": [
                {"id": t.id, "zone": t.zone, "released": t.released_at,
                 "cancelled": t.cancelled_at, "locations": list(t.locations)}
                for t in engine.tasks.values()
            ],
            "merged_duplicates": engine.merged,
            "topology": {
                "version": imported["topology"].version_id,
                "label": imported["topology"].label,
                "entities": [
                    {"id": e.id, "name": e.name, "kind": e.kind, "zone": e.zone,
                     "capacity": e.capacity, "rect": e.rect.as_dict()}
                    for e in imported["topology"].entities.values()
                ],
                "locations": [
                    {"id": l.id, "entity": l.entity, "zone": l.zone, "x": l.x, "y": l.y}
                    for l in imported["topology"].locations.values()
                ],
            },
        })

    def _static(self, path: str):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        base = WEB_DIR
        if rel.startswith("data/"):
            base, rel = DATA_DIR, rel[len("data/"):]
        target = (base / rel).resolve()
        if not str(target).startswith(str(base.resolve())) or not target.is_file():
            return self._error("页面不存在", HTTPStatus.NOT_FOUND)
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self._send(HTTPStatus.OK, target.read_bytes(), ctype)


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    port = int(argv[0]) if argv else 8765
    load_dataset()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"仓内作业分析已启动: http://127.0.0.1:{port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
