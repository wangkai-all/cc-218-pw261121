"""数据导入：CSV / JSON -> Dataset 对象。

支持从页面导入自己的数据：
- topology.json（结构同 data/topology_v1.json）
- scans.csv：scan_id, ts, picker, location, task, device, source, kind, duplicate_of, note
- tasks.csv：task_id, wave, zone, created_at, released_at, due_at, cancelled_at,
             cancel_reason, order_ids, locations, priority
另外提供 adapter 把内置的 dataset.json 转成同样的模型。
"""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from .models import ScanEvent, Task, parse_ts
from .topology import load_topology


class ImportError_(ValueError):
    pass


def _rows(text: str) -> list[dict]:
    text = text.lstrip("﻿")
    dialect = csv.Sniffer().sniff(text[:4096]) if text.strip() else csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames:
        raise ImportError_("CSV 没有表头")
    return [{k.strip(): (v or "").strip() for k, v in row.items() if k}
            for row in reader]


def _num(value, default=None):
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_tasks_csv(text: str) -> list[Task]:
    tasks = []
    for i, row in enumerate(_rows(text)):
        tid = row.get("task_id") or row.get("id")
        if not tid:
            raise ImportError_(f"第 {i + 2} 行缺少 task_id")
        released = parse_ts(row.get("released_at") or row.get("released"))
        if released is None:
            raise ImportError_(f"任务 {tid} 缺少 released_at")
        cancelled = parse_ts(row.get("cancelled_at") or row.get("cancelled"))
        orders = tuple(x for x in (row.get("order_ids") or "").replace(",", "|").split("|") if x)
        locations = tuple(x for x in (row.get("locations") or "").replace(",", "|").split("|") if x)
        tasks.append(Task(
            id=tid,
            wave_id=row.get("wave") or "",
            zone=row.get("zone") or "unknown",
            created_at=parse_ts(row.get("created_at")) or released,
            released_at=released,
            due_at=parse_ts(row.get("due_at")),
            cancelled_at=cancelled,
            cancel_reason=row.get("cancel_reason") or "",
            order_ids=orders,
            locations=locations,
            release_rank=int(_num(row.get("release_rank"), i) or i),
            priority=row.get("priority") or "normal",
            cancel_mode=row.get("cancel_mode") or ("none" if cancelled is None else "pre"),
        ))
    return tasks


def parse_scans_csv(text: str) -> list[ScanEvent]:
    scans = []
    for i, row in enumerate(_rows(text)):
        sid = row.get("scan_id") or row.get("id")
        ts_raw = row.get("ts") or row.get("timestamp")
        picker = row.get("picker") or row.get("picker_id")
        location = row.get("location") or row.get("location_id")
        if not sid or ts_raw is None or not picker or not location:
            raise ImportError_(f"第 {i + 2} 行缺少 scan_id/ts/picker/location")
        ts = parse_ts(ts_raw)
        if ts is None:
            raise ImportError_(f"扫描 {sid} 的时间无法解析：{ts_raw!r}")
        scans.append(ScanEvent(
            id=sid,
            ts=ts,
            picker_id=picker,
            location_id=location,
            task_id=row.get("task") or row.get("task_id") or None,
            device_id=row.get("device") or row.get("device_id") or "",
            source=row.get("source") or "handheld",
            scan_kind=row.get("kind") or row.get("scan_kind") or "pick",
            duplicate_of=row.get("duplicate_of") or None,
            note=row.get("note") or "",
        ))
    return scans


def import_dataset(topology_text: str, scans_text: str, tasks_text: str,
                   label: str = "导入数据") -> dict:
    """解析并返回可直接交给分析层的字典。"""
    try:
        raw_topo = json.loads(topology_text)
    except json.JSONDecodeError as exc:
        raise ImportError_(f"topology JSON 无法解析：{exc}") from exc
    tmp = Path("/tmp/_warehouse_import_topology.json")
    tmp.write_text(json.dumps(raw_topo, ensure_ascii=False), encoding="utf-8")
    topology = load_topology(tmp)
    tasks = parse_tasks_csv(tasks_text)
    scans = parse_scans_csv(scans_text)
    pickers = sorted({s.picker_id for s in scans})
    if not pickers:
        raise ImportError_("扫描数据中没有任何人员")
    unknown_locations = sorted({s.location_id for s in scans
                                if s.location_id not in topology.locations
                                and s.location_id not in topology.aliases})
    start = min([s.ts for s in scans] + [t.released_at for t in tasks])
    end = max([s.ts for s in scans] + [t.released_at for t in tasks]) + 300
    return {
        "id": "imported",
        "label": label,
        "topology": topology,
        "tasks": tasks,
        "scans": scans,
        "start": start,
        "end": end,
        "pickers": pickers,
        "warnings": ([f"{len(unknown_locations)} 个货位不在拓扑中："
                      + "、".join(unknown_locations[:5])] if unknown_locations else []),
    }


def dataset_to_models(dataset: dict, scenario_id: str, topologies: dict):
    """内置 dataset.json -> (Topology, tasks, scans, start, end)。"""
    scenario = dataset["scenarios"][scenario_id]
    topo_version = scenario["topology"]
    topo_raw = dataset["topologies"][topo_version]
    tmp = Path(f"/tmp/_warehouse_{topo_version}.json")
    tmp.write_text(json.dumps(topo_raw, ensure_ascii=False), encoding="utf-8")
    topology = load_topology(tmp)
    tasks = [Task(
        id=t["id"], wave_id=t.get("wave", ""), zone=t.get("zone", ""),
        created_at=t["created"], released_at=t["released"], due_at=t.get("due"),
        cancelled_at=t.get("cancelled"), cancel_reason=t.get("cancel_reason", ""),
        order_ids=tuple(t.get("orders", ())), locations=tuple(t.get("locations", ())),
        release_rank=t.get("release_rank", 0), priority=t.get("priority", "normal"),
        cancel_mode=t.get("cancel_mode", "none"),
    ) for t in scenario["tasks"]]
    scans = [ScanEvent(
        id=s["id"], ts=s["ts"], picker_id=s["picker"], location_id=s["location"],
        task_id=s.get("task"), device_id=s.get("device", ""), source=s.get("source", "handheld"),
        scan_kind=s.get("kind", "pick"), duplicate_of=s.get("duplicate_of"),
        note=s.get("note", ""),
    ) for s in scenario["scans"]]
    return topology, tasks, scans, scenario["start"], scenario["end"]


def replays_to_models(dataset: dict, key: str):
    scenario = dataset["replays"][key]
    topo_raw = dataset["topologies"][scenario["topology"]]
    tmp = Path(f"/tmp/_warehouse_replay_{scenario['topology']}.json")
    tmp.write_text(json.dumps(topo_raw, ensure_ascii=False), encoding="utf-8")
    topology = load_topology(tmp)
    tasks = [Task(
        id=t["id"], wave_id=t.get("wave", ""), zone=t.get("zone", ""),
        created_at=t["created"], released_at=t["released"], due_at=t.get("due"),
        cancelled_at=t.get("cancelled"), cancel_reason=t.get("cancel_reason", ""),
        order_ids=tuple(t.get("orders", ())), locations=tuple(t.get("locations", ())),
        release_rank=t.get("release_rank", 0), priority=t.get("priority", "normal"),
        cancel_mode=t.get("cancel_mode", "none"),
    ) for t in scenario["tasks"]]
    scans = [ScanEvent(
        id=s["id"], ts=s["ts"], picker_id=s["picker"], location_id=s["location"],
        task_id=s.get("task"), device_id=s.get("device", ""), source=s.get("source", "handheld"),
        scan_kind=s.get("kind", "pick"), duplicate_of=s.get("duplicate_of"),
        note=s.get("note", ""),
    ) for s in scenario["scans"]]
    return topology, tasks, scans, scenario["start"], scenario["end"]
